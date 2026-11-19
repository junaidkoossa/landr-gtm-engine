import csv
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src import costs, db
from src.enricher import enrich_creator
from src.hooks import HookWriter
from src.store import OUTPUT_COLUMNS, _write_csv

try:
    import pandas  # noqa: F401
except ImportError:
    pandas = None

RULES = {"target_vertical": "Music", "ideal_customer_profile": "Creators", "signal_keywords": [],
         "evaluation_criteria": "Score fit."}
PRICES = {
    "token_prices": {"claude-sonnet-5-5": {"input_per_million": 2.0, "output_per_million": 10.0},
                     "free-model": {"input_per_million": 0.0, "output_per_million": 0.0}},
    "row_prices": {"clay": 0.25, "apollo": 0.0},
    "batch_discount": 0.5,
}
BREAKDOWN = (
    "Mastering 8: 14 releases in 12 months 3/3; latest release 5 days ago 2/2; "
    "mastering credit unknown (half of 2 counted); self-released 1/1; reach 50K 2/2 | "
    "Distribution 5: label status unknown (half of 3 counted); no distributor found 1/2; "
    "14 releases in 12 months 2/2; latest release 5 days ago 1/1; Spotify listeners unknown (half of 2 counted); "
    "promotes releases (out now, pre-save, link in bio) 2/2"
)


def lead(name, score, **extra):
    return {"name": name, "youtube_url": f"https://www.youtube.com/@{name.lower()}", "target_type": "creator",
            "score": str(score), "best_product": "mastering", "enriched_at": "2026-11-18T10:00:00+00:00", **extra}


def claude_reply(text='{"custom_hook": "Nice drops"}', model="claude-sonnet-5-5", tokens=(1000, 100)):
    return SimpleNamespace(
        stop_reason="end_turn", model=model,
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=tokens[0], output_tokens=tokens[1],
                              cache_creation_input_tokens=0, cache_read_input_tokens=None),
    )


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.output = self.dir / "enriched_creators.csv"
        self.addCleanup(costs.finish_run)

    def write_output(self, rows):
        _write_csv(self.output, OUTPUT_COLUMNS, rows)

    def start(self, run_id="run-1"):
        prices = self.dir / "costs.json"
        prices.write_text(json.dumps(PRICES))
        return costs.start_run(self.output, run_id, prices)

    def cost_lines(self):
        return costs.read_costs(costs.costs_path(self.output))


class CostLedgerTests(Workspace):
    def test_nothing_is_logged_outside_a_run(self):
        costs.log_tokens("score", "claude", "claude-sonnet-5-5", 100, 10)
        costs.log_rows("clay", 1)
        self.assertFalse(costs.costs_path(self.output).exists())

    def test_tokens_and_rows_are_priced_into_one_file(self):
        ledger = self.start()
        costs.log_tokens("score", "claude", "claude-sonnet-5-5", 10_000, 500, lead=lead("A", 8))
        costs.log_tokens("score", "claude", "claude-sonnet-5-5", 10_000, 500, lead=lead("B", 5), batch=True)
        costs.log_rows("clay", 2)
        lines = self.cost_lines()
        self.assertEqual([line["kind"] for line in lines], ["tokens", "tokens", "rows"])
        self.assertEqual(lines[0]["cost_usd"], "0.025000")  # 10K * $2/M + 500 * $10/M
        self.assertEqual(lines[1]["cost_usd"], "0.012500")  # half price in a batch
        self.assertEqual(lines[2]["cost_usd"], "0.500000")
        self.assertEqual(lines[0]["lead_key"], "youtube.com/@a")
        self.assertEqual({line["run_id"] for line in lines}, {"run-1"})
        self.assertAlmostEqual(ledger.total, 0.5375)

    def test_unknown_models_and_unset_row_prices_have_a_blank_cost(self):
        ledger = self.start()
        costs.log_tokens("score", "gemini", "some-new-model", 100, 10)
        costs.log_rows("apollo", 1)  # price 0 means not set yet
        costs.log_tokens("score", "ollama", "free-model", 100, 10)
        self.assertEqual([line["cost_usd"] for line in self.cost_lines()], ["", "", "0.000000"])
        self.assertEqual(ledger.unpriced, {"some-new-model", "apollo"})

    def test_a_new_run_appends_to_the_same_file(self):
        self.start("run-1")
        costs.log_rows("clay", 1)
        costs.finish_run()
        self.start("run-2")
        costs.log_rows("clay", 1)
        self.assertEqual([line["run_id"] for line in self.cost_lines()], ["run-1", "run-2"])

    def test_claude_scoring_and_hooks_log_their_tokens(self):
        self.start()
        reply = '{"adjustment": 0, "adjustment_reason": "", "bottleneck": "No storefront"}'
        create = Mock(side_effect=[claude_reply(reply, tokens=(2000, 200)), claude_reply(tokens=(1500, 50))])
        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)),
                                 messages=SimpleNamespace(create=create))
        creator = {"name": "A", "youtube_url": "https://www.youtube.com/@a", "target_type": "creator"}
        enrich_creator(creator, {}, RULES, client, "claude", with_hook=False)
        HookWriter(client, min_score=7).write(creator, {}, RULES, {}, 8)
        lines = self.cost_lines()
        self.assertEqual([(l["task"], l["input_tokens"], l["output_tokens"]) for l in lines],
                         [("score", "2000", "200"), ("hook", "1500", "50")])

    def test_usage_readers_tolerate_missing_usage(self):
        self.assertEqual(costs.claude_usage(SimpleNamespace()), (0, 0))
        usage = SimpleNamespace(prompt_token_count=900, candidates_token_count=80, thoughts_token_count=20)
        self.assertEqual(costs.gemini_usage(SimpleNamespace(usage_metadata=usage)), (900, 100))


class DatabaseTests(Workspace):
    def query(self, sql, *params):
        with closing(sqlite3.connect(db.db_path(self.output))) as connection:
            return connection.execute(sql, params).fetchall()

    def write_outcomes(self, rows):
        with (self.dir / "outcomes.csv").open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=db.OUTCOME_COLUMNS)
            writer.writeheader()
            writer.writerows(rows)

    def test_sync_loads_leads_with_the_full_row(self):
        self.write_output([lead("A", 8, email="a@x.com", spotify_monthly_listeners="1200")])
        counts = db.sync(self.output)
        self.assertEqual(counts["leads"], 1)
        self.assertEqual(self.query("SELECT lead_key, score, email FROM leads"),
                         [("youtube.com/@a", 8, "a@x.com")])
        self.assertEqual(self.query("SELECT json_extract(row_json, '$.spotify_monthly_listeners') FROM leads"),
                         [("1200",)])

    def test_score_history_grows_and_never_duplicates(self):
        self.write_output([lead("A", 6)])
        db.sync(self.output, run={"run_id": "run-1", "command": "pipeline"})
        db.sync(self.output)  # nothing new
        self.write_output([lead("A", 7, enriched_at="2026-12-01T10:00:00+00:00")])
        db.sync(self.output)
        self.assertEqual(self.query("SELECT run_id, score FROM score_events ORDER BY scored_at"),
                         [("run-1", 6), (None, 7)])
        self.assertEqual(self.query("SELECT command FROM runs"), [("pipeline",)])

    def test_enriched_leads_get_a_before_and_after_event(self):
        self.write_output([lead("A", 8, enrichment_status="enriched", enrichment_provider="clay",
                                score_before_enrichment="6", enrichment_sent_at="2026-11-17T10:00:00+00:00")])
        db.sync(self.output)
        self.assertEqual(self.query("SELECT stage, score FROM score_events ORDER BY scored_at"),
                         [("before_enrichment", 6), ("after_enrichment", 8)])

    def test_components_are_named_by_the_rules_signals(self):
        self.write_output([lead("A", 8, score_mastering="8", score_distribution="5", clay_score_mastering="9",
                                product_breakdowns=BREAKDOWN)])
        db.sync(self.output)
        rows = self.query("SELECT product, component, points, max_points, known FROM score_components")
        self.assertIn(("mastering", "total", 8.0, 10.0, 1), rows)
        self.assertIn(("clay_mastering", "total", 9.0, 10.0, 1), rows)
        self.assertIn(("mastering", "releases", 3.0, 3.0, 1), rows)
        self.assertIn(("mastering", "no_mastering_credit", 1.0, 2.0, 0), rows)  # unknown: half counted
        self.assertIn(("distribution", "competitor_distributor", 1.0, 2.0, 1), rows)
        self.assertIn(("distribution", "release_promo", 2.0, 2.0, 1), rows)

    def test_breakdown_that_doesnt_match_the_rules_keeps_its_text(self):
        parts = db.parse_breakdowns("Mastering 6: releases unknown (half of 3 counted); something new 1/1")
        self.assertEqual([p[1] for p in parts], ["releases unknown", "something new"])

    def test_outcomes_match_by_email_or_link_and_imply_earlier_steps(self):
        self.write_output([lead("A", 8, email="a@x.com"), lead("B", 5)])
        self.write_outcomes([
            {"email": "A@X.com", "sent_at": "2026-11-20", "converted": "yes"},
            {"youtube_url": "https://youtube.com/@b/", "sent_at": "2026-11-20", "replied": "no"},
            {"name": "Nobody", "sent_at": "2026-11-20"},
        ])
        counts = db.sync(self.output)
        self.assertEqual(counts["unmatched_outcomes"], 1)
        self.assertEqual(
            self.query("SELECT lead_key, replied, interested, converted FROM outcomes ORDER BY lead_key"),
            [("youtube.com/@a", 1, 1, 1), ("youtube.com/@b", 0, 0, 0)],
        )

    def test_outcomes_file_needs_sent_at(self):
        self.write_output([lead("A", 8)])
        (self.dir / "outcomes.csv").write_text("email,replied\na@x.com,yes\n")
        with self.assertRaises(ValueError):
            db.sync(self.output)

    def test_costs_are_reloaded_from_the_cost_file(self):
        self.write_output([lead("A", 8)])
        self.start("run-1")
        costs.log_rows("clay", 1, lead("A", 8))
        costs.finish_run()
        db.sync(self.output)
        db.sync(self.output)
        self.assertEqual(self.query("SELECT run_id, provider, cost_usd FROM costs"), [("run-1", "clay", 0.25)])
        self.assertEqual(self.query("SELECT run_id FROM runs"), [("run-1",)])

    def test_sync_quietly_only_warns(self):
        self.output.write_text("not,a,pipeline,file\n")
        with self.assertLogs("src.db", "WARNING"):
            db.sync_quietly(self.output)


@unittest.skipIf(pandas is None, "pandas is not installed (pip install -r requirements-reports.txt)")
class ReportTests(Workspace):
    def setUp(self):
        super().setUp()
        from src import reports
        self.reports = reports

    def build(self):
        db.sync(self.output)
        connection = sqlite3.connect(db.db_path(self.output))
        self.addCleanup(connection.close)
        return self.reports.build_reports(connection, hook_score=7)

    def test_empty_database_gives_empty_reports(self):
        self.write_output([])
        built = self.build()
        self.assertTrue(built["pass_rates"].empty)
        self.assertTrue(built["reply_rates"].empty)
        lines = self.reports.write_reports(built, self.dir / "reports")
        self.assertIn("No outcomes yet", "\n".join(lines))
        self.assertTrue((self.dir / "reports" / "pass_rates.csv").exists())

    def test_pass_rates_by_threshold(self):
        self.write_output([lead("A", 9), lead("B", 7), lead("C", 5), lead("D", 8, target_type="label",
                                                                          best_product="samples")])
        table = self.build()["pass_rates"]
        everyone = table[table["target_type"] == "all"].iloc[0]
        self.assertEqual((everyone["leads"], everyone["score_7_plus"], everyone["score_8_plus"],
                          everyone["score_9_plus"]), (4, 3, 2, 1))
        self.assertEqual(everyone["pct_7_plus"], 75.0)
        creators = table[table["target_type"] == "creator"].iloc[0]
        self.assertEqual(creators["leads"], 3)

    def test_enrichment_lift(self):
        enriched = {"enrichment_status": "enriched", "enrichment_provider": "clay",
                    "enrichment_sent_at": "2026-11-17T10:00:00+00:00"}
        self.write_output([
            lead("A", 8, score_before_enrichment="6", **enriched),
            lead("B", 5, score_before_enrichment="6", **enriched),
            lead("C", 7, score_before_enrichment="5", enrichment_experiment="yes", **enriched),
        ])
        table = self.build()["enrichment_lift"].set_index("experiment")
        self.assertEqual(table.loc["no", "leads"], 2)
        self.assertEqual(table.loc["no", "avg_change"], 0.5)
        self.assertEqual(table.loc["no", "reached_7"], 1)
        self.assertEqual(table.loc["no", "pct_reached_7"], 50.0)
        self.assertEqual(table.loc["yes", "reached_7"], 1)

    def test_cost_per_lead_and_per_qualified_lead(self):
        self.write_output([lead("A", 8), lead("B", 5)])
        self.start()
        costs.log_tokens("score", "claude", "claude-sonnet-5-5", 100_000, 10_000)  # $0.30
        costs.log_rows("clay", 2)  # $0.50
        costs.finish_run()
        totals = self.build()["cost_totals"].iloc[0]
        self.assertAlmostEqual(totals["total_cost_usd"], 0.8)
        self.assertAlmostEqual(totals["cost_per_lead_scored"], 0.4)
        self.assertAlmostEqual(totals["cost_per_qualified_lead"], 0.8)

    def test_reply_rate_uses_the_score_when_contacted(self):
        self.write_output([lead("A", 9, email="a@x.com", enriched_at="2026-12-01T00:00:00+00:00"),
                           lead("B", 6, email="b@x.com")])
        db.sync(self.output)  # A's history: first synced at 9 on 2026-12-01 ...
        self.write_output([lead("A", 9, email="a@x.com", enriched_at="2026-12-01T00:00:00+00:00"),
                           lead("B", 6, email="b@x.com")])
        with closing(sqlite3.connect(db.db_path(self.output))) as connection, connection:
            connection.execute("INSERT INTO score_events (lead_key, stage, scored_at, score) "
                               "VALUES ('youtube.com/@a', 'initial', '2026-11-01T00:00:00+00:00', 6)")
        with (self.dir / "outcomes.csv").open("w", newline="") as handle:
            handle.write("email,sent_at,replied\na@x.com,2026-11-20,yes\nb@x.com,2026-11-20,\n")
        table = self.build()["reply_rates"].set_index("score_band")
        # ... but A was contacted on 2026-11-20, when its score was 6.
        self.assertEqual(table.loc["5-6", "sent"], 2)
        self.assertEqual(table.loc["5-6", "reply_rate"], 50.0)
        self.assertNotIn("9-10", table.index)


if __name__ == "__main__":
    unittest.main()
