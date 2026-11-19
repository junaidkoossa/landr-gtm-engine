"""Reports from the SQLite database (src/db.py), with SQL for the data and pandas for the maths.

    python -m src.reports [--db data/gtm.db] [--out data/reports] [--sync]

Prints four reports and writes each one to a CSV in --out:

1. pass_rates: how many leads score 7, 8 and 9 or more, by lead type and best product.
2. enrichment_lift: scores before and after Clay, and how many crossed the hook score.
3. costs: what was spent by provider and task, per lead scored and per qualified lead.
4. reply_rates: reply, interest and conversion rates by score band, using the score
   each lead had when it was contacted.

pandas is only needed here (pip install -r requirements-reports.txt); the pipeline and
the database run without it.
"""
import argparse
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from src.db import DB_NAME, sync
from src.hooks import hook_min_score

THRESHOLDS = (7, 8, 9)
# (label, lowest score, highest score)
SCORE_BANDS = (("1-4", 1, 4), ("5-6", 5, 6), ("7", 7, 7), ("8", 8, 8), ("9-10", 9, 10))

# --- SQL -------------------------------------------------------------------------------
# Each query returns tidy rows; pandas does the grouping and the ratios.

LEAD_SCORES_SQL = """
SELECT lead_key,
       target_type,
       COALESCE(NULLIF(best_product, ''), 'unknown') AS best_product,
       score
FROM leads
WHERE score IS NOT NULL
"""

# One row per lead enriched by Clay (or Apollo/Hunter): its score just before and just
# after enrichment, from the score history.
ENRICHMENT_SQL = """
SELECT b.lead_key,
       l.enrichment_provider                                   AS provider,
       COALESCE(json_extract(l.row_json, '$.enrichment_experiment'), '') = 'yes' AS experiment,
       b.score                                                 AS score_before,
       a.score                                                 AS score_after
FROM score_events AS b
JOIN score_events AS a
  ON a.lead_key = b.lead_key
 AND a.stage = 'after_enrichment'
 AND a.scored_at = (SELECT MAX(scored_at) FROM score_events
                    WHERE lead_key = b.lead_key AND stage = 'after_enrichment')
JOIN leads AS l ON l.lead_key = b.lead_key
WHERE b.stage = 'before_enrichment'
  AND b.scored_at = (SELECT MAX(scored_at) FROM score_events
                     WHERE lead_key = b.lead_key AND stage = 'before_enrichment')
"""

COSTS_SQL = """
SELECT provider,
       task,
       kind,
       COALESCE(model, '')        AS model,
       COUNT(*)                   AS calls,
       COUNT(DISTINCT lead_key)   AS leads,
       SUM(input_tokens)          AS input_tokens,
       SUM(output_tokens)         AS output_tokens,
       SUM(rows)                  AS rows,
       SUM(cost_usd)              AS cost_usd,
       SUM(cost_usd IS NULL)      AS unpriced_lines
FROM costs
GROUP BY provider, task, kind, model
ORDER BY cost_usd DESC
"""

# Each outreach with the score the lead had when it was sent: the latest score event at
# or before sent_at, else the lead's current score (e.g. scored before history began).
OUTREACH_SQL = """
SELECT o.lead_key,
       o.channel,
       o.sent_at,
       o.replied,
       o.interested,
       o.converted,
       COALESCE(
           (SELECT e.score FROM score_events AS e
            WHERE e.lead_key = o.lead_key AND e.scored_at <= o.sent_at
            ORDER BY e.scored_at DESC LIMIT 1),
           l.score
       ) AS score_at_send
FROM outcomes AS o
LEFT JOIN leads AS l ON l.lead_key = o.lead_key
"""


def _pandas():
    try:
        import pandas
    except ImportError:
        sys.exit("Reports need pandas: pip install -r requirements-reports.txt")
    return pandas


def read_sql(connection: sqlite3.Connection, sql: str):
    return _pandas().read_sql_query(sql, connection)


# --- Reports ---------------------------------------------------------------------------

def pass_rates(connection: sqlite3.Connection):
    """Leads at or above each threshold, by target type and best product, with an "all" row."""
    pd = _pandas()
    leads = read_sql(connection, LEAD_SCORES_SQL)
    columns = ["target_type", "best_product", "leads"] + [
        name for t in THRESHOLDS for name in (f"score_{t}_plus", f"pct_{t}_plus")
    ]
    if leads.empty:
        return pd.DataFrame(columns=columns)
    for t in THRESHOLDS:
        leads[f"score_{t}_plus"] = leads["score"] >= t
    counts = [f"score_{t}_plus" for t in THRESHOLDS]
    grouped = leads.groupby(["target_type", "best_product"])[counts].agg(["sum", "count"])
    table = pd.DataFrame({"leads": grouped[(counts[0], "count")]})
    for t, column in zip(THRESHOLDS, counts):
        table[column] = grouped[(column, "sum")].astype(int)
    table = table.reset_index()
    total = {"target_type": "all", "best_product": "all", "leads": len(leads),
             **{column: int(leads[column].sum()) for column in counts}}
    table = pd.concat([table, pd.DataFrame([total])], ignore_index=True)
    for t in THRESHOLDS:
        table[f"pct_{t}_plus"] = (table[f"score_{t}_plus"] / table["leads"] * 100).round(1)
    return table[columns]


def enrichment_lift(connection: sqlite3.Connection, hook_score: int | None = None):
    """Average score before and after enrichment, and how many reached the hook score, by
    provider and whether the lead was in the cutoff experiment."""
    pd = _pandas()
    hook_score = hook_min_score() if hook_score is None else hook_score
    rows = read_sql(connection, ENRICHMENT_SQL)
    columns = ["provider", "experiment", "leads", "avg_before", "avg_after", "avg_change",
               "went_up", "went_down", f"reached_{hook_score}", f"pct_reached_{hook_score}"]
    if rows.empty:
        return pd.DataFrame(columns=columns)
    rows["experiment"] = rows["experiment"].map({1: "yes", 0: "no"})
    rows["change"] = rows["score_after"] - rows["score_before"]
    rows["went_up"] = rows["change"] > 0
    rows["went_down"] = rows["change"] < 0
    rows["reached"] = (rows["score_before"] < hook_score) & (rows["score_after"] >= hook_score)
    table = rows.groupby(["provider", "experiment"]).agg(
        leads=("lead_key", "count"),
        avg_before=("score_before", "mean"),
        avg_after=("score_after", "mean"),
        avg_change=("change", "mean"),
        went_up=("went_up", "sum"),
        went_down=("went_down", "sum"),
        reached=("reached", "sum"),
    ).reset_index()
    table = table.rename(columns={"reached": f"reached_{hook_score}"})
    # Share of the leads that were below the hook score before enrichment.
    below = rows[rows["score_before"] < hook_score].groupby(["provider", "experiment"]).size()
    table[f"pct_reached_{hook_score}"] = [
        round(r[f"reached_{hook_score}"] / below.get((r["provider"], r["experiment"]), 0) * 100, 1)
        if below.get((r["provider"], r["experiment"]), 0) else None
        for _, r in table.iterrows()
    ]
    return table.round({"avg_before": 2, "avg_after": 2, "avg_change": 2})[columns]


def cost_summary(connection: sqlite3.Connection, qualified_score: int = 7):
    """Spend by provider, task and model; then totals, cost per lead scored and per
    qualified lead (current score at least qualified_score). Returns (detail, totals)."""
    pd = _pandas()
    detail = read_sql(connection, COSTS_SQL)
    leads = read_sql(connection, LEAD_SCORES_SQL)
    scored = int(read_sql(connection, "SELECT COUNT(DISTINCT lead_key) AS n FROM score_events")["n"].iloc[0])
    qualified = int((leads["score"] >= qualified_score).sum()) if not leads.empty else 0
    total = float(detail["cost_usd"].fillna(0).sum()) if not detail.empty else 0.0
    by_kind = detail.groupby("kind")["cost_usd"].sum() if not detail.empty else {}
    totals = pd.DataFrame([{
        "total_cost_usd": round(total, 4),
        "model_cost_usd": round(float(by_kind.get("tokens", 0) or 0), 4),
        "row_cost_usd": round(float(by_kind.get("rows", 0) or 0), 4),
        "leads_scored": scored,
        f"qualified_{qualified_score}_plus": qualified,
        "cost_per_lead_scored": round(total / scored, 4) if scored else None,
        "cost_per_qualified_lead": round(total / qualified, 4) if qualified else None,
        "unpriced_lines": int(detail["unpriced_lines"].sum()) if not detail.empty else 0,
    }])
    if not detail.empty:
        detail["cost_usd"] = detail["cost_usd"].round(4)
        for column in ("input_tokens", "output_tokens", "rows"):
            detail[column] = detail[column].astype("Int64")
    return detail, totals


def reply_rates(connection: sqlite3.Connection):
    """Reply, interest and conversion rates by the score band each lead had when contacted."""
    pd = _pandas()
    outreach = read_sql(connection, OUTREACH_SQL)
    columns = ["score_band", "sent", "replied", "interested", "converted",
               "reply_rate", "interest_rate", "conversion_rate"]
    if outreach.empty:
        return pd.DataFrame(columns=columns)
    labels = [label for label, _, _ in SCORE_BANDS]
    edges = [SCORE_BANDS[0][1] - 0.5] + [high + 0.5 for _, _, high in SCORE_BANDS]
    outreach["score_band"] = pd.cut(outreach["score_at_send"], bins=edges, labels=labels)
    outreach["score_band"] = outreach["score_band"].cat.add_categories("unscored").fillna("unscored")
    table = outreach.groupby("score_band", observed=True).agg(
        sent=("lead_key", "count"),
        replied=("replied", "sum"),
        interested=("interested", "sum"),
        converted=("converted", "sum"),
    ).reset_index()
    for rate, column in (("reply_rate", "replied"), ("interest_rate", "interested"),
                         ("conversion_rate", "converted")):
        table[rate] = (table[column] / table["sent"] * 100).round(1)
    table["score_band"] = table["score_band"].astype(str)
    return table[columns]


def build_reports(connection: sqlite3.Connection, hook_score: int | None = None) -> dict:
    detail, totals = cost_summary(connection)
    return {
        "pass_rates": pass_rates(connection),
        "enrichment_lift": enrichment_lift(connection, hook_score),
        "costs": detail,
        "cost_totals": totals,
        "reply_rates": reply_rates(connection),
    }


TITLES = {
    "pass_rates": "Leads passing 7, 8 and 9",
    "enrichment_lift": "Scores before and after enrichment",
    "costs": "Spend by provider, task and model",
    "cost_totals": "Total cost, per lead and per qualified lead",
    "reply_rates": "Reply rate by score band (score when contacted)",
}
EMPTY_NOTES = {
    "enrichment_lift": "No enriched leads yet (import Clay's export with --import-clay).",
    "costs": "No costs logged yet: costs are logged from the next pipeline run on.",
    "reply_rates": "No outcomes yet: add rows to data/outcomes.csv, then run python -m src.db sync.",
}


def write_reports(reports: dict, out_dir: str | Path) -> list[str]:
    """Write each report to <out_dir>/<name>.csv and return the printable text."""
    pd = _pandas()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    with pd.option_context("display.width", 200, "display.max_columns", 30):
        for name, table in reports.items():
            table.to_csv(out_dir / f"{name}.csv", index=False)
            lines += ["", f"== {TITLES[name]} ==",
                      EMPTY_NOTES.get(name, "No data yet.") if table.empty else table.to_string(index=False)]
    totals = reports.get("cost_totals")
    if totals is not None and not totals.empty and totals["unpriced_lines"].iloc[0]:
        lines += ["", "Note: some cost lines have no price (a model missing from config/costs.json, "
                      "or a Clay/Apollo/Hunter row price still 0 there); they count as $0 above."]
    lines += ["", f"CSV copies are in {out_dir}/"]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the GTM reports from the SQLite database.")
    parser.add_argument("--output", default="data/enriched_creators.csv", help="the pipeline's output CSV")
    parser.add_argument("--db", help=f"database file (default: {DB_NAME} next to the output CSV)")
    parser.add_argument("--out", default="data/reports", help="folder for the report CSVs")
    parser.add_argument("--sync", action="store_true", help="update the database from the CSV files first")
    args = parser.parse_args(argv)
    database = Path(args.db) if args.db else Path(args.output).with_name(DB_NAME)
    if args.sync:
        sync(args.output, database)
    if not database.exists():
        parser.exit(1, f"error: {database} doesn't exist yet; run python -m src.db sync first\n")
    with closing(sqlite3.connect(database)) as connection:
        print("\n".join(write_reports(build_reports(connection), args.out)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
