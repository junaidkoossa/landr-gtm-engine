"""Fine-tuning the scoring from outreach results.

- Exploration sample: EXPLORE_SHARE of the leads below the HubSpot cutoff that have an
  email go to HubSpot anyway (with a hook), marked in the `exploration` column, so you
  learn how leads you rated lower actually answer. Leads scored SPOT_CHECK_SCORE are
  reported as their own group ("spot_check_5").
- Feature file: every scoring adds a row to scoring_features.csv next to the output CSV,
  with the points each signal earned per product and the raw figures behind them.
- Outcomes: --sync-outcomes reads the landr_outcome property back from HubSpot into
  data/outcomes.csv (the same file the database and reports read).
- Fit report: --fit-report compares replies with each signal, product, score band and
  group, and once there is enough data suggests which signal weights to raise or lower.
  Nothing changes the rules file by itself: you edit the weights and the scoring_version.
"""
import csv
import hashlib
import logging
import os
from pathlib import Path

from src.db import OUTCOME_COLUMNS, match_outcome, outcomes_path, parse_breakdowns, read_outcomes
from src.scoring import PRODUCT_NAMES, PRODUCTS, default_products
from src.settings import (
    EXPLORE_SHARE,
    FIT_LOWER_LIFT,
    FIT_MIN_CONTACTED,
    FIT_MIN_GROUP,
    FIT_MIN_REPLIES,
    FIT_RAISE_LIFT,
    SPOT_CHECK_SCORE,
)
from src.store import CreatorStore, _write_csv, creator_key, now_iso, related_path

logger = logging.getLogger(__name__)

# exploration column values
BELOW_CUTOFF = "below_cutoff"
SPOT_CHECK = f"spot_check_{SPOT_CHECK_SCORE}"
YES = {"yes", "y", "true", "1", "x"}

# landr_outcome values in HubSpot -> (replied, interested, converted, bounced)
OUTCOMES = {
    "no_reply": ("", "", "", ""),
    "replied": ("yes", "", "", ""),
    "interested": ("yes", "yes", "", ""),
    "converted": ("yes", "yes", "yes", ""),
    "bounced": ("", "", "", "yes"),
}
OUTCOME_FILE_COLUMNS = OUTCOME_COLUMNS + ("bounced",)


def explore_share() -> float:
    value = os.getenv("EXPLORE_SHARE") or EXPLORE_SHARE
    try:
        share = float(value)
    except ValueError:
        raise ValueError(f"EXPLORE_SHARE must be a share from 0 to 1, like 0.05, not {value!r}") from None
    if not 0 <= share <= 1:
        raise ValueError(f"EXPLORE_SHARE must be a share from 0 to 1, like 0.05, not {share}")
    return share


def in_exploration(key: str, share: float) -> bool:
    """Whether this lead is drawn for the exploration sample: stable for the same lead, and
    independent of the big-act test draw."""
    bucket = int(hashlib.sha1(f"explore:{key}".encode("utf-8")).hexdigest()[:8], 16) / 0x1_0000_0000
    return bucket < share


def mark_exploration(rows: list[dict[str, object]], hubspot_cutoff, share: float | None = None) -> int:
    """Mark the drawn leads below their HubSpot cutoff that have an email and a final score.
    hubspot_cutoff(row) gives the cutoff for a row. Returns how many are marked."""
    share = explore_share() if share is None else share
    marked = 0
    for row in rows:
        try:
            score = int(row.get("score"))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        eligible = (
            score < hubspot_cutoff(row)
            and str(row.get("email") or "").strip()
            and row.get("enrichment_status") != "awaiting_clay"
            and in_exploration(creator_key(row), share)
        )
        if eligible:
            row["exploration"] = SPOT_CHECK if score == SPOT_CHECK_SCORE else BELOW_CUTOFF
            marked += 1
        elif row.get("exploration") and score >= hubspot_cutoff(row):
            row["exploration"] = ""  # now qualifies on its own score
    return marked


# --- Feature file ---------------------------------------------------------------------

FEATURE_COLUMNS = (
    "scored_at", "lead_key", "name", "target_type", "lead_source", "scoring_version", "clay_score_version",
    "scored_by", "score", "base_score", "score_adjustment", "best_product",
    *(f"score_{p}" for p in PRODUCTS), *(f"clay_score_{p}" for p in PRODUCTS),
    "limited_data", "big_act_test", "exploration", "enrichment_status", "enrichment_experiment",
    "audience_size", "releases_last_12_months", "latest_release_date", "active_months_last_12",
    "steady_views", "spotify_monthly_listeners", "clay_followers", "roster_size", "self_released",
    "mastering_credit", "pack_count", "latest_pack_date", "producer_or_engineer", "makes_production_content",
)


def features_path(output_path: str | Path) -> Path:
    return related_path(output_path, "scoring_features.csv")


def point_columns(products: dict | None = None) -> tuple[str, ...]:
    products = products or default_products()
    return tuple(f"points_{p}_{signal}" for p in PRODUCTS for signal in products.get(p, {}))


def feature_row(row: dict[str, object], products: dict | None = None) -> dict[str, object]:
    out: dict[str, object] = {column: row.get(column, "") for column in FEATURE_COLUMNS}
    out["scored_at"] = row.get("enriched_at") or now_iso()
    out["lead_key"] = creator_key(row)
    out["limited_data"] = "yes" if row.get("limited_data") else ""
    for product, signal, points, _, _, _ in parse_breakdowns(str(row.get("product_breakdowns") or ""), products):
        out[f"points_{product}_{signal}"] = f"{points:g}"
    return out


class FeatureLog:
    """scoring_features.csv: one row per scoring, never rewritten except to add columns."""

    def __init__(self, path: Path, products: dict | None = None):
        self.path = path
        self.products = products
        self.columns = FEATURE_COLUMNS + point_columns(products)
        self.pending: list[dict[str, object]] = []

    def add(self, row: dict[str, object]) -> None:
        self.pending.append(feature_row(row, self.products))

    def save(self) -> None:
        if not self.pending:
            return
        existing: list[dict[str, str]] = []
        header: tuple[str, ...] = ()
        if self.path.exists() and self.path.stat().st_size:
            with self.path.open(encoding="utf-8", newline="") as source:
                reader = csv.DictReader(source)
                header = tuple(reader.fieldnames or ())
                if set(self.columns) - set(header):
                    existing = list(reader)
        if header and not set(self.columns) - set(header):
            with self.path.open("a", encoding="utf-8", newline="") as destination:
                csv.DictWriter(destination, fieldnames=header, extrasaction="ignore").writerows(self.pending)
        else:
            # New file, or the rules gained signals: rewrite once with every column.
            columns = header + tuple(c for c in self.columns if c not in header)
            _write_csv(self.path, columns, existing + self.pending)
        self.pending = []


# --- Outcomes from HubSpot ------------------------------------------------------------

def merge_outcomes(path: str | Path, contacts: list[dict[str, str]]) -> dict[str, int]:
    """Write HubSpot's landr_outcome for each contact into the outcomes file (email
    channel), updating a row with the same email and adding the rest. Rows for other
    channels, and your notes, are kept. contacts: email, outcome, sent_at."""
    path = Path(path)
    rows: list[dict[str, str]] = read_outcomes(path) if path.exists() else []
    by_email = {
        row.get("email", "").lower(): row for row in rows
        if row.get("email") and (row.get("channel") or "email") == "email"
    }
    counts = {"updated": 0, "added": 0, "skipped": 0}
    for contact in contacts:
        email = contact.get("email", "").strip().lower()
        outcome = contact.get("outcome", "").strip().lower().replace(" ", "_")
        if not email or outcome not in OUTCOMES:
            counts["skipped"] += 1
            if outcome and outcome not in OUTCOMES:
                logger.warning("Unknown landr_outcome %r for %s (use %s)", outcome, email, ", ".join(OUTCOMES))
            continue
        flags = dict(zip(("replied", "interested", "converted", "bounced"), OUTCOMES[outcome]))
        row = by_email.get(email)
        if row is None:
            row = {"email": email, "channel": "email", "sent_at": contact.get("sent_at", "")}
            rows.append(row)
            by_email[email] = row
            counts["added"] += 1
        else:
            counts["updated"] += 1
            row["sent_at"] = row.get("sent_at") or contact.get("sent_at", "")
        row.update(flags)
    columns = OUTCOME_FILE_COLUMNS + tuple(
        sorted({key for row in rows for key in row} - set(OUTCOME_FILE_COLUMNS))
    )
    _write_csv(path, columns, rows)
    return counts


# --- Fit report -----------------------------------------------------------------------

def _yes(value: object) -> bool:
    return str(value or "").strip().lower() in YES


def contacted_leads(store: CreatorStore, outcomes: list[dict[str, str]]) -> dict[str, bool]:
    """lead key -> replied, for leads contacted at least once (bounced emails left out)."""
    by_email = {str(r.get("email") or "").strip().lower(): k for k, r in store.rows.items() if r.get("email")}
    leads: dict[str, bool] = {}
    for outcome in outcomes:
        if not outcome.get("sent_at") or _yes(outcome.get("bounced")):
            continue
        key = match_outcome(store, by_email, outcome)
        if key is None:
            continue
        replied = any(_yes(outcome.get(c)) for c in ("replied", "interested", "converted"))
        leads[key] = leads.get(key, False) or replied
    return leads


def _band(score: object) -> str:
    try:
        value = int(score)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unscored"
    return "1-5" if value <= 5 else "9-10" if value >= 9 else str(value)


def _rate(replied: int, contacted: int) -> str:
    return f"{replied / contacted:.0%}" if contacted else ""


def fit_report(output_path: str | Path, outcomes: str | Path | None = None,
               products: dict | None = None) -> tuple[list[str], list[dict[str, object]]]:
    """(summary lines, table rows) comparing replies with each group and signal."""
    store = CreatorStore(output_path)
    leads = contacted_leads(store, read_outcomes(outcomes or outcomes_path(output_path)))
    contacted, replied = len(leads), sum(leads.values())
    overall = replied / contacted if contacted else 0.0
    ready = contacted >= FIT_MIN_CONTACTED and replied >= FIT_MIN_REPLIES
    table: list[dict[str, object]] = []

    def add(section: str, group: str, keys: list[str], suggestion: str = "") -> None:
        n = len(keys)
        r = sum(leads[k] for k in keys)
        lift = (r / n) / overall if n and overall else None
        table.append({"section": section, "group": group, "contacted": n, "replied": r,
                      "reply_rate": _rate(r, n), "lift": f"{lift:.2f}" if lift is not None else "",
                      "suggestion": suggestion})

    rows = {k: store.rows[k] for k in leads}
    add("overall", "all contacted leads", list(rows))
    groupings = {
        "best_product": lambda r: PRODUCT_NAMES.get(str(r.get("best_product") or ""), "unknown"),
        "score_band": lambda r: _band(r.get("score")),
        "target_type": lambda r: "label" if str(r.get("target_type") or "").lower().startswith("label") else "creator",
        "exploration": lambda r: r.get("exploration") or "regular",
        "big_act_test": lambda r: "test group (full reach points)" if r.get("big_act_test") else "everyone else",
        "scored_by": lambda r: "clay" if r.get("scored_by") == "clay" else "engine",
        "scoring_version": lambda r: r.get("scoring_version") or "unknown",
    }
    for section, group_of in groupings.items():
        groups: dict[str, list[str]] = {}
        for key, row in rows.items():
            groups.setdefault(str(group_of(row)), []).append(key)
        for group in sorted(groups):
            add(section, group, groups[group])

    # Each product signal: leads where it earned points (checked, not half credit) vs not.
    earned: dict[tuple[str, str], set[str]] = {}
    for key, row in rows.items():
        for product, signal, points, _, _, known in parse_breakdowns(str(row.get("product_breakdowns") or ""), products):
            earned.setdefault((product, signal), set())
            if known and points > 0:
                earned[(product, signal)].add(key)
    suggestions = 0
    for (product, signal), with_keys in sorted(earned.items(), key=lambda item: (PRODUCTS.index(item[0][0]), item[0][1])):
        without = [k for k in rows if k not in with_keys]
        n, r = len(with_keys), sum(leads[k] for k in with_keys)
        lift = (r / n) / overall if n and overall else None
        if not ready:
            suggestion = "need more data"
        elif n < FIT_MIN_GROUP or len(without) < FIT_MIN_GROUP or lift is None:
            suggestion = f"too few leads either way (under {FIT_MIN_GROUP})"
        elif lift >= FIT_RAISE_LIFT:
            suggestion, suggestions = "raise by 1 point", suggestions + 1
        elif lift <= FIT_LOWER_LIFT:
            suggestion, suggestions = "lower by 1 point", suggestions + 1
        else:
            suggestion = "keep"
        add("signal", f"{PRODUCT_NAMES[product]}: {signal} earned points", list(with_keys), suggestion)

    lines = [f"Contacted leads with a known outcome: {contacted}; replied: {replied} ({_rate(replied, contacted) or '0%'})."]
    if not contacted:
        lines.append("No outcomes yet: set landr_outcome in HubSpot and run --sync-outcomes, "
                     "or add rows to data/outcomes.csv.")
    elif not ready:
        lines.append(f"Not enough data to suggest weights yet (needs {FIT_MIN_CONTACTED} contacted and "
                     f"{FIT_MIN_REPLIES} replies). Reply rates by product and score band are below.")
    else:
        lines.append(f"{suggestions} weight changes suggested (signals whose reply rate is at least "
                     f"{FIT_RAISE_LIFT}x or at most {FIT_LOWER_LIFT}x the average). Edit them in "
                     "config/vertical_rules.json and raise scoring_version; nothing is changed for you.")
    for row in table:
        if row["section"] in ("best_product", "score_band", "exploration") or row["suggestion"] in (
            "raise by 1 point", "lower by 1 point"
        ):
            note = f" -> {row['suggestion']}" if row["section"] == "signal" else ""
            lines.append(f"  {row['section']}: {row['group']}: {row['replied']}/{row['contacted']} replied "
                         f"({row['reply_rate'] or '-'}){note}")
    return lines, table


FIT_COLUMNS = ("section", "group", "contacted", "replied", "reply_rate", "lift", "suggestion")


def fit_report_path(output_path: str | Path) -> Path:
    return related_path(output_path, "fit_report.csv")


def write_fit_report(output_path: str | Path, outcomes: str | Path | None = None,
                     products: dict | None = None) -> list[str]:
    lines, table = fit_report(output_path, outcomes, products)
    path = fit_report_path(output_path)
    _write_csv(path, FIT_COLUMNS, table)
    return lines + [f"Full table: {path}"]
