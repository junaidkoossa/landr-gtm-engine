"""Enrichment: emails and extra figures for leads that score well enough.

ENRICHMENT_PROVIDER picks who does it (default clay):

- clay: leads scoring ENRICH_MIN_SCORE or more go to a Clay table, through
  CLAY_WEBHOOK_URL when it is set (Clay's webhooks need a paid plan) and otherwise in a
  to_clay CSV to upload by hand. Clay finds the email and the extra figures. Its CSV
  export comes back with --import-clay, and those leads are scored again on the richer
  data; only then are hooks written and the leads sent to HubSpot.
- apollo / hunter: the email is looked up during the run (src/email_finders.py). They
  find emails only, so the score doesn't change.

Columns read from a Clay export (names are matched ignoring case, spaces and dashes):
the lead's links (website_url and so on, to match it back), email,
spotify_monthly_listeners, followers, roster_size, clay_research and clay_score.

When a lead has a clay_score, Clay's score is its final score (scaled from 0 to
CLAY_SCORE_MAX onto 1 to 10). Leads without one are scored again by the engine instead.

Every enriched lead gets a row in the enrichment report (score before and after), which
--enrichment-report sums up, along with the cutoff experiment (--clay-experiment).
"""
import csv
import json
import os
import random
import re
from datetime import datetime, timezone
from pathlib import Path

from src.settings import (
    CLAY_EXPERIMENT_SAMPLE,
    CLAY_SCORE_MAX,
    CLAY_EXPERIMENT_SCORE,
    CLAY_EXPERIMENT_THRESHOLD,
    ENRICH_MIN_SCORE,
    ENRICHMENT_PROVIDER,
    HOOK_MIN_SCORE,
    HUBSPOT_MIN_SCORE,
)
from src.scoring import PRODUCTS, yes_no
from src.store import LIST_COLUMNS, LIST_SEPARATOR, _write_csv, creator_key, now_iso, related_path

PROVIDERS = ("clay", "apollo", "hunter")

# enrichment_status values
AWAITING = "awaiting_clay"  # sent to (or listed for) Clay; its results aren't imported yet
SEND_FAILED = "send_failed"  # the Clay webhook didn't take it; sent again next run
ENRICHED = "enriched"
NOT_FOUND = "not_found"  # the email finder had no email
NO_DOMAIN = "no_domain"  # no website of the lead's own to look up
LOOKUP_FAILED = "lookup_failed"  # the email lookup failed; tried again next run
HAS_EMAIL = "has_email"  # the input already had an email, so no lookup was needed
BELOW_CUTOFF = "below_cutoff"
# Leads in these states are enriched when they reach the cutoff.
READY = ("", BELOW_CUTOFF, SEND_FAILED, LOOKUP_FAILED)

# What goes to Clay in the CSV (the webhook sends the whole scored row).
CLAY_COLUMNS = (
    "name", "target_type", "website_url", "youtube_url", "storefront_url", "instagram", "tiktok",
    "email", "score", "best_product", "score_mastering", "score_distribution", "score_samples",
    "score_plugins", "score_breakdown", "limited_data", "bottleneck", "enrichment_experiment",
)
# Figures Clay can fill in (yes/no answers, counts and a date), kept on the lead.
CLAY_FLAGS = ("self_released", "mastering_credit", "producer_or_engineer", "makes_production_content")
CLAY_COUNTS = ("spotify_monthly_listeners", "followers", "roster_size", "pack_count")
CLAY_PRODUCT_SCORES = tuple(f"clay_score_{p}" for p in PRODUCTS)
# Clay export column -> our name. Keys are normalised (lowercase, underscores).
CLAY_ALIASES = {
    "email": "email", "email_address": "email", "work_email": "email",
    "spotify_monthly_listeners": "spotify_monthly_listeners", "monthly_listeners": "spotify_monthly_listeners",
    "spotify_listeners": "spotify_monthly_listeners",
    "followers": "followers", "follower_count": "followers", "followers_count": "followers",
    "clay_followers": "followers",
    "roster_size": "roster_size", "roster": "roster_size", "artist_count": "roster_size",
    "active_artists": "roster_size",
    "clay_research": "clay_research", "research": "clay_research", "research_notes": "clay_research",
    "self_released": "self_released", "independent": "self_released",
    "mastering_credit": "mastering_credit", "has_mastering_credit": "mastering_credit",
    "pack_count": "pack_count", "packs": "pack_count", "sample_packs": "pack_count",
    "latest_pack_date": "latest_pack_date", "last_pack_date": "latest_pack_date",
    "producer_or_engineer": "producer_or_engineer", "producer_engineer": "producer_or_engineer",
    "makes_production_content": "makes_production_content", "production_content": "makes_production_content",
    "clay_score_version": "clay_score_version", "score_version": "clay_score_version",
    # Clay's own lead scores, one per product. Not "score": the export also carries the
    # engine's score. A single clay_score counts for the engine's best product.
    "clay_score": "clay_score", "lead_score": "clay_score", "clay_lead_score": "clay_score",
    "fit_score": "clay_score",
    **{f"clay_score_{p}": f"clay_score_{p}" for p in PRODUCTS},
    **{f"{p}_score": f"clay_score_{p}" for p in PRODUCTS},
    "samples_supplier_score": "clay_score_samples", "clay_score_samples_supplier": "clay_score_samples",
}
LINK_COLUMNS = ("name", "website_url", "youtube_url", "storefront_url", "instagram", "tiktok")
MAX_RESEARCH_LENGTH = 4_000

REPORT_COLUMNS = (
    "name", "website_url", "target_type", "best_product", "enrichment_provider", "experiment", "score_before",
    "score_after", "change", "reached_hook_score", "email_found", "email_source", "enriched_at",
)


def enrichment_provider(value: str | None = None) -> str:
    provider = (value or os.getenv("ENRICHMENT_PROVIDER") or ENRICHMENT_PROVIDER).strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"ENRICHMENT_PROVIDER must be one of {', '.join(PROVIDERS)}, not {provider!r}")
    return provider


def _score_setting(name: str, default: int) -> int:
    value = os.getenv(name) or default
    try:
        score = int(value)
    except ValueError:
        raise ValueError(f"{name} must be a whole number from 1 to 10, not {value!r}") from None
    if not 1 <= score <= 10:
        raise ValueError(f"{name} must be a whole number from 1 to 10, not {score}")
    return score


def enrich_min_score() -> int:
    return _score_setting("ENRICH_MIN_SCORE", ENRICH_MIN_SCORE)


def hubspot_min_score() -> int:
    return _score_setting("HUBSPOT_MIN_SCORE", HUBSPOT_MIN_SCORE)


def is_awaiting(row: dict[str, object]) -> bool:
    return row.get("enrichment_status") == AWAITING


def score_of(row: dict[str, object]) -> int | None:
    try:
        return int(row.get("score"))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def parse_number(value: object) -> int | None:
    """12345, "12,345", "12.3K" or "1.2M" as a whole number; None when blank or unreadable."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip().lower().replace(",", "").replace(" ", "")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([km]?)", text)
    if not match:
        return None
    number = float(match.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2)]
    return int(round(number))


def parse_score(value: object) -> float | None:
    """A score like 8, "8.5", "82" or "82%"; None when blank or unreadable."""
    text = str(value or "").strip().rstrip("%").replace(",", ".")
    try:
        return float(text) if text else None
    except ValueError:
        return None


def clay_score_max() -> float:
    value = os.getenv("CLAY_SCORE_MAX") or CLAY_SCORE_MAX
    try:
        top = float(value)
    except ValueError:
        raise ValueError(f"CLAY_SCORE_MAX must be a number such as 10 or 100, not {value!r}") from None
    if top <= 0:
        raise ValueError("CLAY_SCORE_MAX must be more than 0")
    return top


def final_clay_score(value: float, top: float | None = None) -> int:
    """Clay's score on the engine's 1-10 scale (8.5 of 10 -> 9; 82 of 100 -> 8)."""
    top = clay_score_max() if top is None else top
    return max(1, min(10, int(value / top * 10 + 0.5)))


def _normalise(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (header or "").strip().lower()).strip("_")


def read_clay_export(path: str | Path) -> list[dict[str, object]]:
    """The rows of a Clay CSV export, with our column names and numbers parsed."""
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        rows = []
        for raw in reader:
            values = {_normalise(key): (value or "").strip() for key, value in raw.items() if key}
            row: dict[str, object] = {column: values.get(column, "") for column in LINK_COLUMNS}
            for header, value in values.items():
                ours = CLAY_ALIASES.get(header)
                if ours and value and not row.get(ours):
                    row[ours] = value
            for column in CLAY_COUNTS:
                row[column] = parse_number(row.get(column))
            for column in CLAY_FLAGS:
                row[column] = yes_no(row.get(column))
            row["latest_pack_date"] = str(row.get("latest_pack_date") or "")
            row["clay_score_version"] = str(row.get("clay_score_version") or "")
            for column in CLAY_PRODUCT_SCORES:
                row[column] = parse_score(row.get(column))
            row["email"] = str(row.get("email") or "").strip()
            if "@" not in row["email"]:
                row["email"] = ""
            row["clay_research"] = str(row.get("clay_research") or "")[:MAX_RESEARCH_LENGTH]
            row["clay_score"] = parse_score(row.get("clay_score"))
            rows.append(row)
    if rows and not any(row.get(c) for row in rows for c in LINK_COLUMNS[1:]):
        raise ValueError(f"{path} has no website_url, youtube_url or other link column to match leads by")
    return rows


def apply_figures(record: dict[str, object], figures: dict[str, object]) -> dict[str, object]:
    """The record with enrichment figures added. Our own figures win: a follower count
    from enrichment only fills in when no source of ours had one."""
    record = dict(record)
    for column in ("spotify_monthly_listeners", "roster_size", "pack_count"):
        if figures.get(column) is not None:
            record[column] = figures[column]
    for column in CLAY_FLAGS:
        if figures.get(column) is not None:
            record[column] = "yes" if figures[column] else "no"
    for column in ("latest_pack_date", "clay_score_version"):
        if figures.get(column):
            record[column] = figures[column]
    followers = figures.get("followers")
    if followers is not None:
        record["clay_followers"] = followers
        if record.get("audience_size") is None:
            record["audience_size"] = followers
    if figures.get("clay_research"):
        record["clay_research"] = figures["clay_research"]
    return record


def stored_figures(row: dict[str, object]) -> dict[str, object]:
    """Enrichment figures kept in a stored row, ready for apply_figures."""
    return {
        "spotify_monthly_listeners": parse_number(row.get("spotify_monthly_listeners")),
        "roster_size": parse_number(row.get("roster_size")),
        "followers": parse_number(row.get("clay_followers")),
        "pack_count": parse_number(row.get("pack_count")),
        "latest_pack_date": row.get("latest_pack_date") or "",
        "clay_score_version": row.get("clay_score_version") or "",
        "clay_research": row.get("clay_research") or "",
        **{column: yes_no(row.get(column)) for column in CLAY_FLAGS},
    }


NUMBER_COLUMNS = (
    "audience_size", "release_count", "releases_last_12_months", "active_months_last_12",
    "views_last_12_months", "median_views_per_release", "steady_views",
)


def record_from_row(row: dict[str, object]) -> dict[str, object]:
    """A stored CSV row turned back into an evidence record (used when the full evidence
    saved at send time is missing). Page text isn't in the CSV, so it is left out."""
    record: dict[str, object] = dict(row)
    for column in NUMBER_COLUMNS:
        record[column] = parse_number(row.get(column))
    try:
        record["top_video_share"] = float(row["top_video_share"]) if row.get("top_video_share") else None
    except ValueError:
        record["top_video_share"] = None
    for column in LIST_COLUMNS:
        value = row.get(column)
        record[column] = [i for i in value.split(LIST_SEPARATOR) if i] if isinstance(value, str) else value or []
    return apply_figures(record, stored_figures(row))


class EvidenceCache:
    """The full evidence of leads waiting for Clay, so they can be scored again on it
    (plus Clay's figures) without collecting everything a second time."""

    def __init__(self, path: Path):
        self.path = path
        self.records: dict[str, dict] = (
            json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        )

    def add(self, key: str, record: dict[str, object]) -> None:
        self.records[key] = {k: v for k, v in record.items() if k not in ("replace_key", "review_matches")}

    def pop(self, key: str) -> dict | None:
        return self.records.pop(key, None)

    def save(self) -> None:
        if self.records:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.records, ensure_ascii=False, default=str), encoding="utf-8")
        elif self.path.exists():
            self.path.unlink()


def clay_csv_path(output_path: str | Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return related_path(output_path, f"to_clay.{stamp}.csv")


def write_clay_csv(path: Path, rows: list[dict[str, object]]) -> None:
    _write_csv(path, CLAY_COLUMNS, rows)


def pick_experiment(rows: list[dict[str, object]], sample: int = CLAY_EXPERIMENT_SAMPLE,
                    score: int = CLAY_EXPERIMENT_SCORE, rng: random.Random | None = None) -> list[dict[str, object]]:
    """A random sample of not-yet-enriched leads scoring exactly `score` (all of them if fewer)."""
    pool = [row for row in rows if score_of(row) == score and row.get("enrichment_status", "") in READY]
    if len(pool) <= sample:
        return pool
    return (rng or random).sample(pool, sample)


class EnrichmentReport:
    """One row per enriched lead (its latest enrichment): score before and after, email found."""

    def __init__(self, path: Path):
        self.path = path
        self.rows: dict[str, dict[str, object]] = {}
        if path.exists() and path.stat().st_size:
            with path.open(encoding="utf-8", newline="") as source:
                for row in csv.DictReader(source):
                    self.rows[creator_key(row)] = {c: row.get(c, "") for c in REPORT_COLUMNS}

    def add(self, row: dict[str, object], before: int, hook_score: int) -> None:
        after = score_of(row) or before
        self.rows[creator_key(row)] = {
            "name": row.get("name", ""),
            "website_url": row.get("website_url", "") or row.get("youtube_url", "") or row.get("storefront_url", ""),
            "target_type": row.get("target_type", ""),
            "best_product": row.get("best_product", ""),
            "enrichment_provider": row.get("enrichment_provider", ""),
            "experiment": row.get("enrichment_experiment", ""),
            "score_before": before,
            "score_after": after,
            "change": f"{after - before:+d}",
            "reached_hook_score": "yes" if after >= hook_score else "",
            "email_found": "yes" if row.get("email_source") not in ("", None, "input") and row.get("email") else "",
            "email_source": row.get("email_source", "") if row.get("email") else "",
            "enriched_at": now_iso(),
        }

    def save(self) -> None:
        if self.rows:
            _write_csv(self.path, REPORT_COLUMNS, list(self.rows.values()))


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def summarize(store_rows: list[dict[str, object]], report_rows: list[dict[str, object]],
              hook_score: int = HOOK_MIN_SCORE, hubspot_score: int = HUBSPOT_MIN_SCORE) -> list[str]:
    """The enrichment summary, one line per point."""
    lines: list[str] = []
    enriched = [r for r in report_rows if _int(r["score_before"]) is not None]
    regular = [r for r in enriched if r.get("experiment") != "yes"]
    if regular:
        before = sum(1 for r in regular if _int(r["score_before"]) >= hook_score)
        after = sum(1 for r in regular if _int(r["score_after"]) >= hook_score)
        emails = sum(1 for r in regular if r.get("email_found") == "yes")
        change = sum(_int(r["score_after"]) - _int(r["score_before"]) for r in regular) / len(regular)
        lines.append(
            f"Enriched leads: {len(regular)}. At {hook_score}+: {before} before enrichment, {after} after. "
            f"Emails found by enrichment: {emails}. Average score change: {change:+.1f}."
        )
    else:
        lines.append("Enriched leads: none yet.")

    experiment = [r for r in enriched if r.get("experiment") == "yes"]
    if experiment:
        reached = sum(1 for r in experiment if _int(r["score_after"]) >= hook_score)
        share = reached / len(experiment)
        if len(experiment) < CLAY_EXPERIMENT_SAMPLE:
            verdict = f"too small to call (fewer than {CLAY_EXPERIMENT_SAMPLE} leads)"
        elif share > CLAY_EXPERIMENT_THRESHOLD:
            verdict = f"over {CLAY_EXPERIMENT_THRESHOLD:.0%}: lower ENRICH_MIN_SCORE to {CLAY_EXPERIMENT_SCORE}"
        else:
            verdict = f"not over {CLAY_EXPERIMENT_THRESHOLD:.0%}: keep ENRICH_MIN_SCORE where it is"
        lines.append(
            f"Experiment (leads scored {CLAY_EXPERIMENT_SCORE}): {len(experiment)} enriched, "
            f"{reached} reached {hook_score}+ ({share:.0%}). Verdict: {verdict}."
        )

    scored = [r for r in store_rows if score_of(r) is not None]
    spread = []
    for cutoff in (7, 8, 9):
        at = [r for r in scored if score_of(r) >= cutoff]
        with_email = sum(1 for r in at if str(r.get("email") or "").strip())
        spread.append(f"{cutoff}+: {len(at)} ({with_email} with an email)")
    waiting = sum(1 for r in store_rows if is_awaiting(r))
    lines.append(f"Score spread over {len(scored)} scored leads: " + "; ".join(spread)
                 + f". Waiting for Clay: {waiting}.")

    def qualifies(score: int | None, email: bool) -> bool:
        return score is not None and score >= hubspot_score and email

    without = with_ = 0
    for row in scored:
        has_email = bool(str(row.get("email") or "").strip())
        own_email = has_email and row.get("email_source") in ("", None, "input")
        before = _int(row.get("score_before_enrichment"))
        without += qualifies(before if before is not None else score_of(row), own_email)
        with_ += qualifies(score_of(row), has_email) and not is_awaiting(row)
    lines.append(
        f"HubSpot ({hubspot_score}+ with an email): {without} leads without enrichment, {with_} with it."
    )
    return lines


def report_path(output_path: str | Path) -> Path:
    return related_path(output_path, "enrichment_report.csv")


def summary_path(output_path: str | Path) -> Path:
    return related_path(output_path, "enrichment_summary.txt")


def cache_path(output_path: str | Path) -> Path:
    return related_path(output_path, "awaiting_clay.json")
