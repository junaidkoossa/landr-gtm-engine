"""A SQLite copy of the pipeline's data, for reporting with SQL and pandas.

The CSV files stay the source of truth. The database (default data/gtm.db, next to the
output CSV) is filled from them:

- leads: one row per lead from the output CSV, plus the whole CSV row as JSON (row_json),
  so a new CSV column is available to SQL (json_extract) without changing the schema.
- score_events: each score a lead has had. Every sync adds the current score if it is
  new, so history builds up from the first sync on. Stages: "initial" (scored by the
  engine) and, for leads enriched by Clay, "before_enrichment" and "after_enrichment".
- score_components: the parts of each score event, one row each: every product's total
  (component "total"), Clay's per-product scores (product "clay_<product>"), and each
  signal from the breakdown with its points (e.g. mastering / self_released / 1 of 1).
  New products or signals are new rows, not new columns.
- runs: one row per pipeline run, with what it did.
- costs: every line of the cost file (src/costs.py): tokens and per-row charges, priced.
- outcomes: outreach results from data/outcomes.csv (sent, replied, interested, converted).

leads, costs and outcomes are reloaded from their files on every sync; score_events,
score_components and runs only ever grow. Deleting gtm.db loses only score history that
the CSV no longer shows, so it is safe to rebuild with `python -m src.db sync`.

Usage:
    python -m src.db sync [--output data/enriched_creators.csv] [--outcomes data/outcomes.csv]
"""
import argparse
import csv
import json
import logging
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from src.costs import costs_path, read_costs
from src.rules import load_vertical_rules
from src.scoring import PRODUCT_NAMES, PRODUCTS, default_products
from src.store import CreatorStore, creator_key, now_iso

logger = logging.getLogger(__name__)

DB_NAME = "gtm.db"
DEFAULT_RULES = "config/vertical_rules.json"
OUTCOME_FILE = "outcomes.csv"
OUTCOME_COLUMNS = (
    "email", "website_url", "youtube_url", "instagram", "tiktok", "name",
    "channel", "sent_at", "replied", "interested", "converted", "notes",
)
YES = {"yes", "y", "true", "1", "x"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    lead_key            TEXT PRIMARY KEY,
    name                TEXT,
    target_type         TEXT,
    email               TEXT,
    score               INTEGER,
    best_product        TEXT,
    scored_by           TEXT,
    scoring_version     TEXT,
    enrichment_provider TEXT,
    enrichment_status   TEXT,
    hubspot_status      TEXT,
    scored_at           TEXT,
    row_json            TEXT        -- the full CSV row; query any column with json_extract
);

CREATE TABLE IF NOT EXISTS score_events (
    event_id        INTEGER PRIMARY KEY,
    lead_key        TEXT NOT NULL,
    run_id          TEXT,
    stage           TEXT NOT NULL,   -- initial | before_enrichment | after_enrichment
    scored_at       TEXT NOT NULL,
    score           INTEGER,
    base_score      INTEGER,
    score_adjustment INTEGER,
    best_product    TEXT,
    scored_by       TEXT,
    scoring_version TEXT,
    UNIQUE (lead_key, stage, scored_at)
);

CREATE TABLE IF NOT EXISTS score_components (
    event_id    INTEGER NOT NULL REFERENCES score_events (event_id),
    product     TEXT NOT NULL,       -- mastering, distribution, ... or clay_<product>
    component   TEXT NOT NULL,       -- "total", or a signal name from the rules (e.g. self_released)
    points      REAL,
    max_points  REAL,
    detail      TEXT,                -- the breakdown text, e.g. "14 releases in 12 months"
    known       INTEGER,             -- 0 when no source could check it (half its points counted)
    PRIMARY KEY (event_id, product, component)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    command      TEXT,
    provider     TEXT,
    started_at   TEXT,
    finished_at  TEXT,
    leads_scored INTEGER
);

CREATE TABLE IF NOT EXISTS costs (
    logged_at     TEXT,
    run_id        TEXT,
    lead_key      TEXT,
    name          TEXT,
    kind          TEXT,              -- tokens | rows
    task          TEXT,              -- score | hook | enrich
    provider      TEXT,
    model         TEXT,
    batch         INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    rows          INTEGER,
    cost_usd      REAL               -- NULL when config/costs.json has no price
);

CREATE TABLE IF NOT EXISTS outcomes (
    lead_key    TEXT NOT NULL,
    channel     TEXT,
    sent_at     TEXT NOT NULL,
    replied     INTEGER NOT NULL DEFAULT 0,
    interested  INTEGER NOT NULL DEFAULT 0,
    converted   INTEGER NOT NULL DEFAULT 0,
    notes       TEXT,
    PRIMARY KEY (lead_key, sent_at)
);

CREATE INDEX IF NOT EXISTS score_events_by_lead ON score_events (lead_key, scored_at);
CREATE INDEX IF NOT EXISTS costs_by_run ON costs (run_id);
"""

# "self-released 1/1", "releases 3/3 (14 in 12 months)" or "recency unknown (half of 2 counted)"
_SIGNAL = re.compile(r"^(?P<name>.*?)\s+(?P<got>\d+(?:\.\d+)?)/(?P<max>\d+(?:\.\d+)?)\b")
_HALF = re.compile(r"^(?P<name>.*?)\s*\(half of (?P<max>\d+(?:\.\d+)?) counted\)")
_PRODUCT_BY_NAME = {name.lower(): product for product, name in PRODUCT_NAMES.items()}


def db_path(output_path: str | Path) -> Path:
    return Path(output_path).with_name(DB_NAME)


def outcomes_path(output_path: str | Path) -> Path:
    return Path(output_path).with_name(OUTCOME_FILE)


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.executescript(SCHEMA)
    return connection


def _int(value: object) -> int | None:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return None


def _float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _yes(value: object) -> int:
    return 1 if str(value or "").strip().lower() in YES else 0


def parse_breakdowns(text: str, products: dict | None = None) -> list[tuple]:
    """(product, signal, points, max_points, detail, known) for each signal in
    product_breakdowns, e.g. "Mastering 7: 14 releases in 12 months 3/3; latest release
    unknown (half of 2 counted) | Samples supplier 4: ...".

    The breakdown lists signals in the order of the rules file, so each one is named
    by its key there (releases, recency, ...). If the counts don't line up (the rules
    changed since), the breakdown text is used as the name instead. A signal no source
    could check gets half its points, as in the score."""
    products = products or default_products()
    parts = []
    for block in str(text or "").split(" | "):
        head, _, body = block.partition(":")
        match = re.match(r"^(?P<name>.+?)\s+\d+$", head.strip())
        product = _PRODUCT_BY_NAME.get((match.group("name") if match else head).strip().lower())
        if not product or not body:
            continue
        items = []
        for item in (piece.strip() for piece in body.split(";")):
            half, signal = _HALF.match(item), _SIGNAL.match(item)
            if half:
                top = float(half.group("max"))
                items.append((half.group("name").strip(), top / 2, top, 0))
            elif signal:
                items.append((signal.group("name").strip(), float(signal.group("got")), float(signal.group("max")), 1))
        names = list(products.get(product, {}))
        named = len(names) == len(items)
        for index, (detail, points, top, known) in enumerate(items):
            parts.append((product, names[index] if named else detail, points, top, detail, known))
    return parts


def _components(row: dict, products: dict | None = None) -> list[tuple]:
    parts = [(p, "total", _float(row.get(f"score_{p}")), 10.0, "", 1)
             for p in PRODUCTS if row.get(f"score_{p}") not in (None, "")]
    parts += [(f"clay_{p}", "total", _float(row.get(f"clay_score_{p}")), 10.0, "", 1)
              for p in PRODUCTS if row.get(f"clay_score_{p}") not in (None, "")]
    return parts + parse_breakdowns(str(row.get("product_breakdowns") or ""), products)


def _events(row: dict, products: dict | None = None) -> list[dict[str, object]]:
    """The score events a CSV row shows: its current score, and the score before
    enrichment when the lead has been enriched."""
    scored_at = str(row.get("enriched_at") or "")
    if not scored_at or _int(row.get("score")) is None:
        return []
    current = {
        "stage": "initial", "scored_at": scored_at, "score": _int(row.get("score")),
        "base_score": _int(row.get("base_score")), "score_adjustment": _int(row.get("score_adjustment")),
        "best_product": row.get("best_product") or "", "scored_by": row.get("scored_by") or "",
        "scoring_version": row.get("scoring_version") or "", "components": _components(row, products),
    }
    before = _int(row.get("score_before_enrichment"))
    if row.get("enrichment_status") != "enriched" or before is None:
        return [current]
    current["stage"] = "after_enrichment"
    earlier = {
        "stage": "before_enrichment",
        "scored_at": str(row.get("enrichment_sent_at") or row.get("enrichment_returned_at") or scored_at),
        "score": before, "base_score": None, "score_adjustment": None, "best_product": "",
        "scored_by": "", "scoring_version": "", "components": [],
    }
    return [earlier, current]


def _sync_leads(connection: sqlite3.Connection, rows: list[dict], run_id: str | None,
                products: dict | None = None) -> int:
    """Replace the leads table with the CSV and add any new score events. Returns how many
    events were new."""
    connection.execute("DELETE FROM leads")
    new_events = 0
    for row in rows:
        key = creator_key(row)
        connection.execute(
            """INSERT OR REPLACE INTO leads
               (lead_key, name, target_type, email, score, best_product, scored_by, scoring_version,
                enrichment_provider, enrichment_status, hubspot_status, scored_at, row_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, row.get("name"), row.get("target_type"), row.get("email") or None, _int(row.get("score")),
             row.get("best_product"), row.get("scored_by"), row.get("scoring_version"),
             row.get("enrichment_provider"), row.get("enrichment_status"), row.get("hubspot_status"),
             row.get("enriched_at"), json.dumps(row, ensure_ascii=False, default=str)),
        )
        for event in _events(row, products):
            cursor = connection.execute(
                """INSERT OR IGNORE INTO score_events
                   (lead_key, run_id, stage, scored_at, score, base_score, score_adjustment,
                    best_product, scored_by, scoring_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (key, run_id, event["stage"], event["scored_at"], event["score"], event["base_score"],
                 event["score_adjustment"], event["best_product"], event["scored_by"], event["scoring_version"]),
            )
            if cursor.rowcount:
                new_events += 1
                connection.executemany(
                    "INSERT OR IGNORE INTO score_components VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [(cursor.lastrowid, *part) for part in event["components"]],
                )
    return new_events


def _sync_costs(connection: sqlite3.Connection, lines: list[dict]) -> None:
    connection.execute("DELETE FROM costs")
    connection.executemany(
        "INSERT INTO costs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(line.get("logged_at"), line.get("run_id"), line.get("lead_key"), line.get("name"), line.get("kind"),
          line.get("task"), line.get("provider"), line.get("model"), _yes(line.get("batch")),
          _int(line.get("input_tokens")), _int(line.get("output_tokens")), _int(line.get("rows")),
          _float(line.get("cost_usd"))) for line in lines],
    )
    # Runs logged only in the cost file (e.g. a DB rebuilt from scratch) still get a row.
    connection.execute(
        """INSERT OR IGNORE INTO runs (run_id, started_at, finished_at)
           SELECT run_id, MIN(logged_at), MAX(logged_at) FROM costs WHERE run_id <> '' GROUP BY run_id"""
    )


def read_outcomes(path: str | Path) -> list[dict[str, str]]:
    path = Path(path)
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        fields = {field.strip().lower() for field in reader.fieldnames or ()}
        if "sent_at" not in fields:
            raise ValueError(f"{path} needs a sent_at column (columns: {', '.join(OUTCOME_COLUMNS)})")
        return [{k.strip().lower(): (v or "").strip() for k, v in row.items() if k} for row in reader]


def match_outcome(store: CreatorStore, by_email: dict[str, str], outcome: dict[str, str]) -> str | None:
    """The lead an outcome row is about: by email, then by its links or name."""
    email = outcome.get("email", "").lower()
    if email and email in by_email:
        return by_email[email]
    if not any(outcome.get(c) for c in ("website_url", "youtube_url", "instagram", "tiktok", "name")):
        return None
    found = store.find(outcome)
    return found[0] if found else None


def _sync_outcomes(connection: sqlite3.Connection, store: CreatorStore, outcomes: list[dict]) -> int:
    """Replace the outcomes table with the file. Returns how many rows matched no lead."""
    connection.execute("DELETE FROM outcomes")
    by_email = {str(row.get("email") or "").strip().lower(): key for key, row in store.rows.items() if row.get("email")}
    unmatched = 0
    for outcome in outcomes:
        if _yes(outcome.get("bounced")):
            continue  # the email never arrived, so it says nothing about the lead
        key = match_outcome(store, by_email, outcome)
        if key is None or not outcome.get("sent_at"):
            unmatched += 1
            logger.warning("Outcome not matched to a lead: %s", outcome.get("email") or outcome.get("name")
                           or outcome.get("website_url") or "(blank row)")
            continue
        replied, interested, converted = (_yes(outcome.get(c)) for c in ("replied", "interested", "converted"))
        # Each step implies the ones before it.
        interested = interested or converted
        replied = replied or interested
        connection.execute(
            "INSERT OR REPLACE INTO outcomes VALUES (?, ?, ?, ?, ?, ?, ?)",
            (key, outcome.get("channel") or "email", outcome["sent_at"], replied, interested, converted,
             outcome.get("notes") or ""),
        )
    return unmatched


def _products(rules_path: str | Path) -> dict:
    try:
        return load_vertical_rules(rules_path)["products"]
    except (OSError, ValueError):
        return default_products()


def sync(output_path: str | Path, database: str | Path | None = None, outcomes: str | Path | None = None,
         run: dict | None = None, rules_path: str | Path = DEFAULT_RULES) -> dict[str, int]:
    """Load the CSV files into the database. run (run_id, command, provider, started_at,
    leads_scored) records the pipeline run doing the sync and tags its new score events."""
    store = CreatorStore(output_path)
    rows = list(store.rows.values())
    run_id = (run or {}).get("run_id")
    outcome_rows = read_outcomes(outcomes or outcomes_path(output_path))
    with closing(connect(database or db_path(output_path))) as connection, connection:
        new_events = _sync_leads(connection, rows, run_id, _products(rules_path))
        _sync_costs(connection, read_costs(costs_path(output_path)))
        unmatched = _sync_outcomes(connection, store, outcome_rows)
        if run:
            connection.execute(
                """INSERT OR REPLACE INTO runs (run_id, command, provider, started_at, finished_at, leads_scored)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_id, run.get("command"), run.get("provider"), run.get("started_at"), now_iso(),
                 run.get("leads_scored")),
            )
    counts = {"leads": len(rows), "new_score_events": new_events, "outcomes": len(outcome_rows) - unmatched,
              "unmatched_outcomes": unmatched}
    logger.info("Database %s: %d leads, %d new score events, %d outcomes (%d unmatched)",
                database or db_path(output_path), counts["leads"], new_events, counts["outcomes"], unmatched)
    return counts


def sync_quietly(output_path: str | Path, run: dict | None = None, rules_path: str | Path = DEFAULT_RULES) -> None:
    """sync() for the end of a pipeline run: a database problem is a warning, since the
    CSV files (the source of truth) are already saved."""
    try:
        sync(output_path, run=run, rules_path=rules_path)
    except (OSError, ValueError, sqlite3.Error) as error:
        logger.warning("Couldn't update the database (the CSV files are saved; run "
                       "`python -m src.db sync` to retry): %s", error)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Load the pipeline's CSV files into SQLite.")
    sub = parser.add_subparsers(dest="command", required=True)
    sync_parser = sub.add_parser("sync", help="load the output CSV, cost file and outcomes into the database")
    sync_parser.add_argument("--output", default="data/enriched_creators.csv")
    sync_parser.add_argument("--db", help=f"database file (default: {DB_NAME} next to the output CSV)")
    sync_parser.add_argument("--rules", default=DEFAULT_RULES)
    sync_parser.add_argument("--outcomes", help=f"outcomes CSV (default: {OUTCOME_FILE} next to the output CSV)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        sync(args.output, args.db, args.outcomes, rules_path=args.rules)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
