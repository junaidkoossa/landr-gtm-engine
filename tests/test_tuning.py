import csv
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from src import db
from src.cutoffs import Cutoffs, default_cutoffs, validate_cutoffs
from src.hubspot import fetch_outcomes
from src.main import _sync_hubspot, main
from src.scoring import score_products
from src.store import CreatorStore, creator_key
from src.tuning import (
    BELOW_CUTOFF,
    SPOT_CHECK,
    FeatureLog,
    features_path,
    fit_report,
    in_exploration,
    mark_exploration,
    merge_outcomes,
)

CLEAN_ENV = {"ENRICH_MIN_SCORE": "", "HOOK_MIN_SCORE": "", "HUBSPOT_MIN_SCORE": "", "EXPLORE_SHARE": ""}


def scored_row(name: str, evidence: dict | None = None, target: str = "creator", **extra) -> dict:
    record = {"name": name, "target_type": target, "website_url": f"https://{name.lower()}.example",
              **(evidence or {})}
    row = {**record, **score_products(record, target == "label", key=creator_key(record))}
    return {**row, "score": row["base_score"], "enriched_at": "2026-11-19T00:00:00+00:00", **extra}


@patch.dict(os.environ, CLEAN_ENV)
class CutoffTests(unittest.TestCase):
    def test_defaults_and_partial_rules(self):
        self.assertEqual(validate_cutoffs(None), default_cutoffs())
        cutoffs = validate_cutoffs({"label": {"hubspot": 8}})
        self.assertEqual((cutoffs["label"]["hubspot"], cutoffs["label"]["enrich"], cutoffs["creator"]["hubspot"]),
                         (8, 6, 7))

    def test_bad_values_are_rejected(self):
        for bad in ({"band": {}}, {"label": {"send": 7}}, {"label": {"hook": 11}}, {"label": {"hook": "7"}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_cutoffs(bad)

    def test_each_type_gets_its_own_cutoff_and_the_environment_overrides_both(self):
        cutoffs = Cutoffs({"cutoffs": validate_cutoffs({"label": {"hubspot": 8}})})
        self.assertEqual(cutoffs.for_row({"target_type": "label"}, "hubspot"), 8)
        self.assertEqual(cutoffs.for_row({"target_type": "creator"}, "hubspot"), 7)
        self.assertEqual(cutoffs.describe("hubspot"), "7 for creators, 8 for labels")
        with patch.dict(os.environ, {"HUBSPOT_MIN_SCORE": "6"}):
            self.assertEqual(cutoffs.for_row({"target_type": "label"}, "hubspot"), 6)
            self.assertEqual(cutoffs.describe("hubspot"), "6")

    def test_rules_file_cutoffs_are_loaded(self):
        from src.rules import load_vertical_rules
        rules = load_vertical_rules("config/vertical_rules.json")
        self.assertEqual(rules["cutoffs"]["creator"], {"enrich": 6, "hook": 7, "hubspot": 7})


@patch.dict(os.environ, CLEAN_ENV)
class ExplorationTests(unittest.TestCase):
    def rows(self):
        return [
            {"name": "Five", "website_url": "https://five.example", "score": "5", "email": "a@x", "target_type": "creator"},
            {"name": "Four", "website_url": "https://four.example", "score": "4", "email": "b@x", "target_type": "creator"},
            {"name": "NoEmail", "website_url": "https://n.example", "score": "4", "email": "", "target_type": "creator"},
            {"name": "Top", "website_url": "https://top.example", "score": "8", "email": "c@x", "target_type": "creator"},
            {"name": "Waiting", "website_url": "https://w.example", "score": "4", "email": "d@x",
             "target_type": "creator", "enrichment_status": "awaiting_clay"},
        ]

    def test_the_draw_is_stable_and_close_to_the_share(self):
        keys = [f"lead{i}.example" for i in range(4000)]
        drawn = sum(in_exploration(key, 0.05) for key in keys)
        self.assertTrue(150 < drawn < 250, drawn)
        self.assertEqual([in_exploration(k, 0.05) for k in keys[:50]], [in_exploration(k, 0.05) for k in keys[:50]])

    def test_only_drawn_leads_below_the_cutoff_with_an_email_are_marked(self):
        rows = self.rows()
        marked = mark_exploration(rows, lambda row: 7, share=1.0)
        self.assertEqual(marked, 2)
        self.assertEqual([r.get("exploration", "") for r in rows], [SPOT_CHECK, BELOW_CUTOFF, "", "", ""])
        self.assertEqual(mark_exploration(self.rows(), lambda row: 7, share=0.0), 0)

    def test_exploration_leads_go_to_hubspot_below_the_cutoff(self):
        rows = self.rows()[:2]
        rows[0]["exploration"] = SPOT_CHECK
        store = Mock()
        with patch("src.main.upsert_contacts", side_effect=lambda r, t: [(row, None) for row in r]) as upsert:
            synced, failed = _sync_hubspot(store, rows, Cutoffs(), "token")
        self.assertEqual((synced, failed), (1, 0))
        self.assertEqual([r["name"] for r in upsert.call_args.args[0]], ["Five"])

    def test_labels_use_their_own_hubspot_cutoff(self):
        rules = {"cutoffs": validate_cutoffs({"label": {"hubspot": 8}})}
        rows = [{"name": n, "score": "7", "email": f"{n}@x", "target_type": t} for n, t in (("C", "creator"), ("L", "label"))]
        store = Mock()
        with patch("src.main.upsert_contacts", side_effect=lambda r, t: [(row, None) for row in r]) as upsert:
            _sync_hubspot(store, rows, Cutoffs(rules), "token")
        self.assertEqual([r["name"] for r in upsert.call_args.args[0]], ["C"])
        store.set_hubspot_status.assert_any_call(rows[1], "below_min_score")


@patch.dict(os.environ, CLEAN_ENV)
class HookCutoffTests(unittest.TestCase):
    def test_hooks_follow_each_type_and_include_the_exploration_sample(self):
        from src.main import Run
        from src.rules import load_vertical_rules
        rules = load_vertical_rules("config/vertical_rules.json")
        rules["cutoffs"] = validate_cutoffs({"label": {"hook": 8}})
        with tempfile.TemporaryDirectory() as root:
            run = Run(Path(root) / "out.csv", rules, client=object(), poll_seconds=0)
            rows = [
                {"name": "Creator7", "website_url": "https://c7.example", "target_type": "creator", "score": "7"},
                {"name": "Label7", "website_url": "https://l7.example", "target_type": "label", "score": "7"},
                {"name": "Label8", "website_url": "https://l8.example", "target_type": "label", "score": "8"},
                {"name": "Explore4", "website_url": "https://e4.example", "target_type": "creator", "score": "4",
                 "exploration": BELOW_CUTOFF},
                {"name": "Plain4", "website_url": "https://p4.example", "target_type": "creator", "score": "4"},
            ]
            with patch.object(run.hook_writer, "write", return_value={"custom_hook": "Hi", "hook_by": "m"}) as write:
                self.assertEqual(run.write_hooks(rows), 3)
        self.assertEqual([c.args[0]["name"] for c in write.call_args_list], ["Creator7", "Label8", "Explore4"])


class FeatureLogTests(unittest.TestCase):
    def test_rows_are_appended_with_points_per_signal(self):
        with tempfile.TemporaryDirectory() as root:
            path = features_path(Path(root) / "out.csv")
            log = FeatureLog(path)
            log.add(scored_row("Alpha", {"self_released": "yes", "releases_last_12_months": 8}))
            log.save()
            log.add(scored_row("Beta"))
            log.save()
            rows = list(csv.DictReader(path.open(encoding="utf-8")))
        self.assertEqual([r["name"] for r in rows], ["Alpha", "Beta"])
        self.assertEqual(rows[0]["points_distribution_self_released"], "3")
        self.assertEqual(rows[0]["points_mastering_releases"], "1")
        self.assertEqual(rows[1]["points_distribution_self_released"], "1.5")  # unknown: half counted
        self.assertEqual(rows[0]["lead_key"], "alpha.example")

    def test_new_signals_rewrite_the_header_once(self):
        with tempfile.TemporaryDirectory() as root:
            path = features_path(Path(root) / "out.csv")
            path.write_text("scored_at,name\n2026-01-01,Old\n", encoding="utf-8")
            log = FeatureLog(path)
            log.add(scored_row("New"))
            log.save()
            rows = list(csv.DictReader(path.open(encoding="utf-8")))
        self.assertEqual([r["name"] for r in rows], ["Old", "New"])
        self.assertIn("points_plugins_recency", rows[0])


class OutcomeTests(unittest.TestCase):
    def test_hubspot_outcomes_update_and_add_rows_and_keep_notes(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "outcomes.csv"
            path.write_text(
                "email,website_url,youtube_url,instagram,tiktok,name,channel,sent_at,replied,interested,converted,notes\n"
                "a@x,,,,,,email,2026-11-20,,,,Asked for pricing\n"
                ",,https://youtube.com/@b,,,,instagram_dm,2026-11-21,yes,,,\n",
                encoding="utf-8",
            )
            counts = merge_outcomes(path, [
                {"email": "A@x", "outcome": "interested", "sent_at": "2026-11-25"},
                {"email": "c@x", "outcome": "bounced", "sent_at": "2026-11-22"},
                {"email": "d@x", "outcome": "maybe", "sent_at": ""},
            ])
            rows = db.read_outcomes(path)
        self.assertEqual(counts, {"updated": 1, "added": 1, "skipped": 1})
        a = rows[0]
        self.assertEqual((a["replied"], a["interested"], a["converted"], a["sent_at"], a["notes"]),
                         ("yes", "yes", "", "2026-11-20", "Asked for pricing"))
        self.assertEqual(rows[1]["channel"], "instagram_dm")
        self.assertEqual((rows[2]["email"], rows[2]["bounced"]), ("c@x", "yes"))

    def test_fetch_outcomes_pages_through_the_search(self):
        pages = [
            Mock(status_code=200, json=lambda: {
                "results": [{"properties": {"email": "A@x", "landr_outcome": "replied",
                                            "hs_email_last_send_date": "2026-11-20T10:00:00Z"}}],
                "paging": {"next": {"after": "1"}}}),
            Mock(status_code=200, json=lambda: {
                "results": [{"properties": {"email": "b@x", "landr_outcome": "no_reply",
                                            "lastmodifieddate": "2026-11-21T10:00:00Z"}}]}),
        ]
        with patch("src.hubspot.requests.post", side_effect=pages) as post, patch("src.hubspot.time.sleep"):
            contacts = fetch_outcomes("token")
        self.assertEqual(contacts, [
            {"email": "a@x", "outcome": "replied", "sent_at": "2026-11-20"},
            {"email": "b@x", "outcome": "no_reply", "sent_at": "2026-11-21"},
        ])
        self.assertEqual(post.call_args_list[1].kwargs["json"]["after"], "1")
        self.assertEqual(post.call_args_list[0].kwargs["json"]["filterGroups"][0]["filters"][0]["operator"],
                         "HAS_PROPERTY")

    def test_bounced_rows_stay_out_of_the_database(self):
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "out.csv"
            store = CreatorStore(output)
            for name in ("Alpha", "Beta"):
                store.upsert({**scored_row(name), "email": f"{name.lower()}@x"})
            store.save()
            outcomes = Path(root) / "outcomes.csv"
            merge_outcomes(outcomes, [{"email": "alpha@x", "outcome": "bounced", "sent_at": "2026-11-20"},
                                      {"email": "beta@x", "outcome": "replied", "sent_at": "2026-11-20"}])
            database = Path(root) / "gtm.db"
            db.sync(output, database, outcomes)
            with closing(sqlite3.connect(database)) as connection:
                count = connection.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0]
        self.assertEqual(count, 1)


class FitReportTests(unittest.TestCase):
    def build(self, root: Path, leads: int, replies_from_self_released: int):
        output = root / "out.csv"
        store = CreatorStore(output)
        contacts = []
        for i in range(leads):
            self_released = i % 2 == 0
            row = scored_row(f"Lead{i}", {"self_released": "yes" if self_released else "no"},
                             email=f"lead{i}@x", exploration=SPOT_CHECK if i % 10 == 1 else "")
            store.upsert(row)
            replied = self_released and i // 2 < replies_from_self_released
            contacts.append({"email": f"lead{i}@x", "outcome": "replied" if replied else "no_reply",
                             "sent_at": "2026-11-20"})
        store.save()
        merge_outcomes(root / "outcomes.csv", contacts)
        return output

    def test_too_little_data_suggests_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            output = self.build(Path(root), 20, 5)
            lines, table = fit_report(output)
        self.assertIn("Contacted leads with a known outcome: 20; replied: 5 (25%).", lines[0])
        self.assertIn("Not enough data", lines[1])
        signals = [row for row in table if row["section"] == "signal"]
        self.assertTrue(signals and all(row["suggestion"] == "need more data" for row in signals))

    def test_a_signal_that_predicts_replies_gets_a_raise(self):
        with tempfile.TemporaryDirectory() as root:
            output = self.build(Path(root), 220, 60)
            lines, table = fit_report(output)
        by_group = {(row["section"], row["group"]): row for row in table}
        raise_row = by_group[("signal", "Distribution: self_released earned points")]
        self.assertEqual((raise_row["contacted"], raise_row["replied"], raise_row["suggestion"]),
                         (110, 60, "raise by 1 point"))
        self.assertEqual(by_group[("exploration", SPOT_CHECK)]["contacted"], 22)
        self.assertIn("weight changes suggested", lines[1])

    def test_cli_writes_the_table(self):
        with tempfile.TemporaryDirectory() as root:
            output = self.build(Path(root), 20, 5)
            out = StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["--fit-report", "--output", str(output)]), 0)
            self.assertTrue((Path(root) / "out.fit_report.csv").exists())
        self.assertIn("Full table:", out.getvalue())


if __name__ == "__main__":
    unittest.main()
