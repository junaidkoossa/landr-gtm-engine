import csv
import json
from datetime import date, timedelta
import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.email_finders import EmailFinder, EmailLookupError, find_with_apollo, find_with_hunter, lead_domain
from src.enrichment import (
    AWAITING,
    apply_figures,
    parse_number,
    pick_experiment,
    read_clay_export,
    summarize,
)
from src.hooks import HookWriter
from src.main import enrichment_report, import_clay, run_files, run_pipeline
from src.scoring import DEFAULT_PRODUCTS

RULES = {
    "target_vertical": "Music", "ideal_customer_profile": "Creators", "signal_keywords": ["sample pack"],
    "evaluation_criteria": "Fit", "activation_min_score": 1, "products": DEFAULT_PRODUCTS,
}


def evidence(releases, days_ago, steady_views=None, signals=()):
    return {
        "page_title": "Artist", "source_text": "A sample pack", "release_signals": ["sample pack"],
        "storefront_links": [], "data_sources": ["website"], "product_signals": list(signals),
        "audience_size": None, "release_count": None, "releases_last_12_months": releases,
        "latest_release_date": (date.today() - timedelta(days=days_ago)).isoformat(),
        "recent_releases": [], "review_matches": [], "active_months_last_12": None,
        "steady_views": steady_views,
    }


# Best product is Mastering for all three. STRONG: releases 1/3, recency 1/2, reach 2/2,
# unknown credit 1 + label 0.5 = 5.5 -> 6. MEDIUM: no reach figure (half) -> 5.
# WEAK: Distribution 4, its best. After Clay (150K listeners, no mastering credit,
# self-released), STRONG's Mastering is 1 + 1 + 2 + 1 + 2 = 7.
STRONG = evidence(8, 60, steady_views=300_000)
MEDIUM = evidence(8, 60)
WEAK = evidence(1, 400, steady_views=100)
TOP = evidence(12, 10, steady_views=300_000)  # Mastering 8
REPLY = {"score_adjustment": 0, "adjustment_reason": "", "bottleneck": "Reach", "scored_by": "model"}


def fake_hook(self, creator, record, rules, enrichment, score):
    return {"custom_hook": f"Hook for {creator['name']}", "hook_by": "claude-test"}


class NumbersAndExportTests(unittest.TestCase):
    def test_parse_number(self):
        for text, expected in (("12,345", 12_345), ("12.3K", 12_300), ("1.2M", 1_200_000), (" 7 ", 7),
                               ("", None), ("n/a", None), (None, None), (42, 42)):
            with self.subTest(text=text):
                self.assertEqual(parse_number(text), expected)

    def test_reads_clay_columns_by_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.csv"
            path.write_text(
                "Name,Website URL,Work Email,Monthly Listeners,Follower Count,Roster,Research Notes,Other\n"
                "A,https://a.example,a@a.example,1.2M,15K,,Runs a label too,x\n"
                "B,https://b.example,not an email,,,12,,\n",
                encoding="utf-8",
            )
            a, b = read_clay_export(path)
        self.assertEqual((a["email"], a["spotify_monthly_listeners"], a["followers"]), ("a@a.example", 1_200_000, 15_000))
        self.assertEqual(a["clay_research"], "Runs a label too")
        self.assertEqual((b["email"], b["roster_size"], b["spotify_monthly_listeners"]), ("", 12, None))

    def test_export_without_links_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.csv"
            path.write_text("Name,Email\nA,a@a.example\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no website_url"):
                read_clay_export(path)

    def test_our_follower_count_wins(self):
        mine = apply_figures({"audience_size": 50_000}, {"followers": 9_000, "spotify_monthly_listeners": 20_000})
        self.assertEqual((mine["audience_size"], mine["clay_followers"]), (50_000, 9_000))
        self.assertEqual(mine["spotify_monthly_listeners"], 20_000)
        self.assertEqual(apply_figures({"audience_size": None}, {"followers": 9_000})["audience_size"], 9_000)


class ExperimentAndSummaryTests(unittest.TestCase):
    def report_rows(self, reached, total):
        return [{"score_before": 5, "score_after": 7 if i < reached else 5, "experiment": "yes",
                 "email_found": ""} for i in range(total)]

    def test_sample_takes_only_unenriched_fives(self):
        rows = [{"score": "5", "enrichment_status": ""} for _ in range(30)]
        rows += [{"score": "5", "enrichment_status": "enriched"}, {"score": "4", "enrichment_status": ""}]
        picked = pick_experiment(rows, rng=random.Random(1))
        self.assertEqual(len(picked), 20)
        self.assertTrue(all(row["score"] == "5" and row["enrichment_status"] == "" for row in picked))
        self.assertEqual(len(pick_experiment(rows[:3])), 3)  # fewer than the sample: all of them

    def test_verdicts(self):
        lower = summarize([], self.report_rows(11, 20))
        self.assertIn("11 reached the hook cutoff (55%; creators 11/20). Verdict: over 50%: lower the enrich cutoff to 5", lower[1])
        keep = summarize([], self.report_rows(10, 20))
        self.assertIn("not over 50%: keep the enrich cutoff", keep[1])
        small = summarize([], self.report_rows(5, 6))
        self.assertIn("too small to call", small[1])

    def test_spread_and_hubspot_with_and_without_enrichment(self):
        store = [
            {"score": "9", "email": "a@x", "email_source": "input"},
            {"score": "8", "email": "b@x", "email_source": "clay", "score_before_enrichment": "6"},
            {"score": "7", "email": ""},
            {"score": "4", "email": ""},
        ]
        lines = summarize(store, [])
        self.assertIn("4 scored creators: 7+: 3 (2 with an email); 8+: 2 (2 with an email); 9+: 1 (1 with an email)",
                      lines[-3])
        self.assertIn("1 leads without enrichment, 2 with it", lines[-1])


class HookProductTests(unittest.TestCase):
    def test_hook_request_names_the_best_product_and_its_angle(self):
        writer = HookWriter(client=object(), min_score=7)
        request = writer.build_request({"name": "A"}, {}, RULES, {"best_product": "plugins", "bottleneck": "x"}, 8)
        review = json.loads(request["messages"][0]["content"])["review"]
        self.assertEqual(review["product"], "Plugins")
        self.assertIn("LANDR's own plugins", review["pitch_angle"])


class EmailFinderTests(unittest.TestCase):
    def test_lead_domain_skips_platforms(self):
        self.assertEqual(lead_domain({"website_url": "https://www.Artist.com/shop"}), "artist.com")
        self.assertIsNone(lead_domain({"website_url": "https://artist.bandcamp.com"}))
        self.assertEqual(lead_domain({"website_url": "https://youtube.com/@a", "storefront_url": "shop.a.io"}), "shop.a.io")

    def test_apollo(self):
        response = Mock(status_code=200)
        response.json.return_value = {"person": {"email": "jo@artist.com"}}
        with patch("src.email_finders.requests.post", return_value=response) as post:
            self.assertEqual(find_with_apollo({"name": "Jo", "website_url": "https://artist.com"}, "K"), "jo@artist.com")
        self.assertEqual(post.call_args.kwargs["json"]["domain"], "artist.com")
        self.assertEqual(post.call_args.kwargs["headers"]["x-api-key"], "K")
        response.json.return_value = {"person": {"email": "email_not_unlocked@domain.com"}}
        with patch("src.email_finders.requests.post", return_value=response):
            self.assertIsNone(find_with_apollo({"name": "Jo", "website_url": "https://artist.com"}, "K"))

    def test_hunter_picks_the_most_confident_email(self):
        response = Mock(status_code=200)
        response.json.return_value = {"data": {"emails": [{"value": "info@a.com", "confidence": 60},
                                                          {"value": "jo@a.com", "confidence": 95}]}}
        with patch("src.email_finders.requests.get", return_value=response):
            self.assertEqual(find_with_hunter({"website_url": "https://a.com"}, "K"), "jo@a.com")
        with patch("src.email_finders.requests.get", return_value=Mock(status_code=401)):
            with self.assertRaisesRegex(EmailLookupError, "HUNTER_API_KEY was rejected"):
                find_with_hunter({"website_url": "https://a.com"}, "K")

    def test_finder_needs_its_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "APOLLO_API_KEY"):
                EmailFinder("apollo")


class FakeFinder:
    provider = "hunter"

    def __init__(self, emails):
        self.emails = emails
        self.looked_up = []

    def find(self, row):
        self.looked_up.append(row["name"])
        return self.emails.get(row["name"])


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.rules = self.root / "rules.json"
        self.rules.write_text(json.dumps(RULES))
        self.output = self.root / "out.csv"
        self.files = run_files(self.output)
        for patcher in (
            patch.dict(os.environ, {}, clear=True),
            patch.object(HookWriter, "write", fake_hook),
            patch("src.activate.time.sleep"),
            patch("src.hubspot.time.sleep"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_input(self, rows):
        path = self.root / "in.csv"
        path.write_text("name,website_url,target_type,email\n"
                        + "".join(f"{n},https://{n.lower()}.example,creator,{e}\n" for n, e in rows), encoding="utf-8")
        return path

    def rows(self, path=None):
        with (path or self.output).open(encoding="utf-8", newline="") as source:
            return {row["name"]: row for row in csv.DictReader(source)}

    def run_engine(self, names, by_name, **kwargs):
        with (
            patch("src.main.collect_evidence", side_effect=lambda c, k, t=None: by_name[c["name"]]),
            patch("src.main.enrich_creator", return_value=REPLY),
        ):
            return run_pipeline(self.write_input(names), self.rules, self.output, client=object(), **kwargs)

    def test_clay_round_trip(self):
        hubspot_posts = []

        def hubspot_post(url, json=None, **kwargs):
            hubspot_posts.append(json)
            return Mock(status_code=200)

        self.run_engine([("Strong", ""), ("Weak", "")], {"Strong": STRONG, "Weak": WEAK}, hubspot="token")
        rows = self.rows()
        self.assertEqual((rows["Strong"]["score"], rows["Strong"]["enrichment_status"]), ("6", AWAITING))
        self.assertEqual(rows["Weak"]["enrichment_status"], "below_cutoff")
        self.assertEqual(rows["Strong"]["hubspot_status"], "awaiting_enrichment")
        self.assertEqual(rows["Strong"]["custom_hook"], "")  # no hook before Clay's results are in
        sent = list(self.root.glob("out.to_clay.*.csv"))
        self.assertEqual(len(sent), 1)
        self.assertEqual([r["name"] for r in csv.DictReader(sent[0].open(encoding="utf-8"))], ["Strong"])
        self.assertIn("strong.example", json.loads(self.files["clay_cache"].read_text()))

        export = self.root / "clay_export.csv"
        export.write_text("Name,Website URL,Email,Spotify Monthly Listeners,Followers,Mastering Credit,"
                          "Self Released,Clay Research\n"
                          "Strong,https://strong.example,jo@strong.example,150K,20000,no,yes,Tours every summer\n"
                          "Stranger,https://nobody.example,x@y.z,1M,,\n", encoding="utf-8")
        with (
            patch("src.main.enrich_creator", return_value=REPLY) as rescore,
            patch("src.hubspot.requests.post", side_effect=hubspot_post),
            self.assertLogs("src.main", level="INFO") as logs,
        ):
            imported = import_clay(export, self.rules, self.output, client=object(), hubspot="token")

        self.assertEqual([row["name"] for row in imported], ["Strong"])
        record = rescore.call_args.args[1]
        self.assertEqual(record["source_text"], "A sample pack")  # the saved evidence, not the CSV
        self.assertEqual(record["clay_research"], "Tours every summer")
        row = self.rows()["Strong"]
        self.assertEqual((row["score"], row["score_before_enrichment"]), ("7", "6"))
        self.assertEqual((row["best_product"], row["mastering_credit"], row["self_released"]), ("mastering", "no", "yes"))
        self.assertEqual((row["email"], row["email_source"]), ("jo@strong.example", "clay"))
        self.assertEqual((row["enrichment_status"], row["spotify_monthly_listeners"]), ("enriched", "150000"))
        self.assertEqual(row["custom_hook"], "Hook for Strong")
        self.assertEqual(row["hubspot_status"], "synced")
        self.assertEqual(hubspot_posts[0]["inputs"][0]["id"], "jo@strong.example")
        self.assertFalse(self.files["clay_cache"].exists())
        report = list(csv.DictReader(self.files["report"].open(encoding="utf-8")))
        self.assertEqual((report[0]["score_before"], report[0]["score_after"], report[0]["change"]), ("6", "7", "+1"))
        self.assertEqual((report[0]["reached_hook_score"], report[0]["email_found"]), ("yes", "yes"))
        self.assertTrue(any("1 not found in the output" in line for line in logs.output))
        summary = enrichment_report(self.output)
        self.assertIn("At the hook cutoff (7+): 0 before enrichment, 1 after", summary[0])
        self.assertIn("0 leads without enrichment, 1 with it", summary[-1])

        # Scored again 90 days later: Clay's figures still count and it isn't sent again.
        with patch("src.main.is_stale", return_value=True):
            self.run_engine([("Strong", ""), ("Weak", "")], {"Strong": STRONG, "Weak": WEAK})
        row = self.rows()["Strong"]
        self.assertEqual((row["score"], row["enrichment_status"]), ("7", "enriched"))
        self.assertEqual(len(list(self.root.glob("out.to_clay.*.csv"))), 1)

    def test_clay_score_is_the_final_score(self):
        self.run_engine([("Strong", ""), ("Other", "")], {"Strong": STRONG, "Other": STRONG})
        export = self.root / "clay_export.csv"
        export.write_text("Website URL,Email,Lead Score,Monthly Listeners\n"
                          "https://strong.example,jo@strong.example,82,150K\n"
                          "https://other.example,,,\n", encoding="utf-8")
        with (
            patch.dict(os.environ, {"CLAY_SCORE_MAX": "100"}),
            patch("src.main.enrich_creator", return_value=REPLY) as rescore,
        ):
            import_clay(export, self.rules, self.output, client=object())
        rows = self.rows()
        self.assertEqual(rescore.call_count, 1)  # only Other, which had no Clay score
        strong = rows["Strong"]
        self.assertEqual((strong["score"], strong["scored_by"], strong["score_before_enrichment"]), ("8", "clay", "6"))
        self.assertEqual(strong["base_score"], "6")  # the engine's own view, kept for comparison
        self.assertEqual(strong["bottleneck"], "Reach")
        self.assertEqual(strong["custom_hook"], "Hook for Strong")
        self.assertEqual((rows["Other"]["scored_by"], rows["Other"]["score"]), ("model", "6"))

    def test_clay_scores_per_product_pick_the_best_product(self):
        self.run_engine([("Strong", "")], {"Strong": STRONG})
        export = self.root / "clay_export.csv"
        export.write_text("Website URL,Mastering Score,Distribution Score,Samples Score,Plugins Score,Score Version\n"
                          "https://strong.example,6,9,2,3,clay-v2\n", encoding="utf-8")
        with patch("src.main.enrich_creator", return_value=REPLY) as rescore:
            import_clay(export, self.rules, self.output, client=object())
        rescore.assert_not_called()
        row = self.rows()["Strong"]
        self.assertEqual((row["score"], row["best_product"], row["scored_by"]), ("9", "distribution", "clay"))
        self.assertEqual((row["clay_score_mastering"], row["clay_score_distribution"]), ("6", "9"))
        self.assertEqual(row["clay_score_version"], "clay-v2")
        self.assertEqual(row["custom_hook"], "Hook for Strong")

    def test_limited_data_leads_go_to_clay_whatever_their_score(self):
        unknown = {**WEAK, "releases_last_12_months": None, "latest_release_date": "", "steady_views": None,
                   "product_signals": None}
        self.run_engine([("Thin", ""), ("Weak", "")], {"Thin": unknown, "Weak": WEAK})
        rows = self.rows()
        self.assertTrue(rows["Thin"]["limited_data"])
        self.assertEqual(rows["Thin"]["enrichment_status"], AWAITING)
        self.assertEqual(rows["Weak"]["enrichment_status"], "below_cutoff")

    def test_clay_score_scaling(self):
        from src.enrichment import final_clay_score, parse_score
        self.assertEqual(final_clay_score(8.5, 10), 9)
        self.assertEqual(final_clay_score(82, 100), 8)
        self.assertEqual(final_clay_score(0, 100), 1)
        self.assertEqual(final_clay_score(140, 100), 10)
        self.assertEqual(parse_score("82%"), 82.0)
        self.assertIsNone(parse_score("n/a"))

    def test_clay_webhook_failures_are_sent_again(self):
        down = Mock(status_code=500)
        down.raise_for_status.side_effect = __import__("requests").HTTPError("down")
        with patch("src.activate.requests.post", return_value=down):
            self.run_engine([("Strong", "")], {"Strong": STRONG}, clay_webhook_url="https://clay.example")
        self.assertEqual(self.rows()["Strong"]["enrichment_status"], "send_failed")
        with patch("src.activate.requests.post", return_value=Mock(status_code=200)) as post:
            self.run_engine([("Other", "")], {"Other": WEAK}, clay_webhook_url="https://clay.example")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(json.loads(post.call_args.kwargs["data"])["name"], "Strong")
        self.assertEqual(self.rows()["Strong"]["enrichment_status"], AWAITING)

    def test_experiment_sends_fives_below_the_cutoff(self):
        names = [(f"M{i}", "") for i in range(3)]
        self.run_engine(names, {n: MEDIUM for n, _ in names}, clay_experiment=True)
        rows = self.rows()
        self.assertEqual({row["enrichment_status"] for row in rows.values()}, {AWAITING})
        self.assertEqual({row["enrichment_experiment"] for row in rows.values()}, {"yes"})

    def test_experiment_needs_clay(self):
        with self.assertRaisesRegex(RuntimeError, "ENRICHMENT_PROVIDER=clay"):
            self.run_engine([("Strong", "")], {"Strong": STRONG}, clay_experiment=True,
                     enrichment="hunter", email_finder=FakeFinder({}))

    def test_email_finder_flow(self):
        finder = FakeFinder({"Strong": "jo@strong.example"})
        top = TOP
        with patch("src.hubspot.requests.post", return_value=Mock(status_code=200)):
            self.run_engine([("Strong", ""), ("Known", "k@known.example"), ("Weak", "")],
                     {"Strong": top, "Known": top, "Weak": WEAK},
                     enrichment="hunter", email_finder=finder, hubspot="token")
        self.assertEqual(finder.looked_up, ["Strong"])  # Known had an email; Weak is below the cutoff
        rows = self.rows()
        self.assertEqual((rows["Strong"]["email"], rows["Strong"]["email_source"]), ("jo@strong.example", "hunter"))
        self.assertEqual(rows["Known"]["enrichment_status"], "has_email")
        self.assertEqual({rows[n]["hubspot_status"] for n in ("Strong", "Known")}, {"synced"})
        self.assertEqual(rows["Strong"]["custom_hook"], "Hook for Strong")  # 7+: hook in the same run
        self.assertEqual(rows["Weak"]["custom_hook"], "")


if __name__ == "__main__":
    unittest.main()
