import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from src import instagram, tiktok
from src.collect import merge_evidence
from src.rules import load_vertical_rules
from src.scoring import (
    DEFAULT_PRODUCTS,
    big_act_test,
    detect_signals,
    final_score,
    score_products,
    validate_products,
)

TODAY = date(2026, 11, 17)


def youtube_part(views_per_release: list[int]) -> dict:
    """Releases spread evenly over the last year (all older than 14 days)."""
    releases = [
        {"date": (TODAY - timedelta(days=20 + (index * 340) // len(views_per_release))).isoformat(), "views": views}
        for index, views in enumerate(views_per_release)
    ]
    return {"source": "youtube", "title": "", "text": "", "links": [], "audience_size": 20_000,
            "release_count": len(releases), "release_dates": [r["date"] for r in releases], "releases": releases}


def evidence_for(views_per_release):
    return merge_evidence([youtube_part(views_per_release)], [], today=TODAY)


class SteadyViewsTests(unittest.TestCase):
    """Junaid's examples: consistency of views beats volume, and one hit doesn't carry a creator."""

    def setUp(self):
        self.a = evidence_for([3_333] * 300)                 # 300 beats, ~1M views
        self.b = evidence_for([33_333] * 150)                # 150 beats, ~5M views
        self.c = evidence_for([4_900_000] + [671] * 149)     # 150 beats, 5M views, one hit

    def test_steady_views_ranks_b_then_a_then_c(self):
        self.assertGreater(self.b["steady_views"], self.a["steady_views"])
        self.assertGreater(self.a["steady_views"], self.c["steady_views"])
        self.assertAlmostEqual(self.c["top_video_share"], 0.98, places=2)
        self.assertEqual(self.c["views_last_12_months"], self.b["views_last_12_months"] - 50 + 79)

    def test_steady_views_feed_reach(self):
        # A (1M) earns full Mastering reach; C (100K) too; B (5M) is over the cap.
        reach = {name: score_products(ev, False, today=TODAY)["product_breakdowns"] for name, ev in
                 (("A", self.a), ("B", self.b), ("C", self.c))}
        self.assertIn("reach 100.7K 2/2", reach["C"])
        self.assertRegex(reach["B"], r"reach 5(\.\d)?M, (over the cap 1/2|big-act test 2/2)")

    def test_new_releases_count_but_not_their_views(self):
        part = youtube_part([10_000] * 10)
        part["releases"].append({"date": (TODAY - timedelta(days=2)).isoformat(), "views": 5})
        part["release_dates"].append(part["releases"][-1]["date"])
        evidence = merge_evidence([part], [], today=TODAY)
        self.assertEqual(evidence["releases_last_12_months"], 11)
        self.assertEqual(evidence["median_views_per_release"], 10_000)

    def test_active_months_and_no_view_data(self):
        part = {"source": "musicbrainz", "title": "", "text": "", "links": [], "audience_size": None,
                "release_count": 300, "release_dates": ["2026-10-01", "2026-10-20", "2026-03-01", "2019-01-01"]}
        evidence = merge_evidence([part], [], today=TODAY)
        self.assertEqual(evidence["active_months_last_12"], 2)
        self.assertIsNone(evidence["steady_views"])


def lead(**fields):
    base = {"releases_last_12_months": 12, "latest_release_date": (TODAY - timedelta(days=10)).isoformat(),
            "product_signals": [], "storefront_links": []}
    return {**base, **fields}


class ProductScoreTests(unittest.TestCase):
    def test_mastering_breakdown_and_unknowns_count_half(self):
        row = score_products(lead(steady_views=300_000), False, today=TODAY)
        # releases 2/3, recency 2/2, reach 2/2, mastering credit 1 of 2, self-released 0.5 of 1
        self.assertEqual((row["best_product"], row["score_mastering"]), ("mastering", 8))
        self.assertIn("mastering credit unknown (half of 2 counted)", row["score_breakdown"])
        self.assertTrue(row["score_breakdown"].startswith("Mastering: 12 releases in 12 months 2/3"))
        self.assertEqual(row["limited_data"], "")

    def test_each_product_rewards_its_own_signals(self):
        plugins = score_products(lead(product_signals=["producer_engineer", "production_content",
                                                       "third_party_plugins"]), False, today=TODAY)
        self.assertEqual((plugins["best_product"], plugins["score_plugins"]), ("plugins", 10))
        samples = score_products(lead(product_signals=["sells_sample_packs"], storefront_links=["x"],
                                      pack_count=60, latest_pack_date=TODAY.isoformat(), audience_size=150_000,
                                      releases_last_12_months=1), False, today=TODAY)
        self.assertEqual((samples["best_product"], samples["score_samples"]), ("samples", 10))
        distribution = score_products(lead(product_signals=["distributor:distrokid", "release_promo"], self_released="yes",
                                           spotify_monthly_listeners=20_000), False, today=TODAY)
        self.assertEqual(distribution["score_distribution"], 10)
        self.assertIn("uses distrokid 2/2", distribution["product_breakdowns"])

    def test_labels_only_get_distribution_and_samples(self):
        row = score_products(lead(), True, today=TODAY)
        self.assertEqual((row["score_mastering"], row["score_plugins"]), (0, 0))
        self.assertIn(row["best_product"], ("distribution", "samples"))

    def test_clay_answers_override_page_text(self):
        no_credit = score_products(lead(mastering_credit="no", self_released="yes"), False, today=TODAY)
        credit = score_products(lead(mastering_credit="yes", self_released="no"), False, today=TODAY)
        self.assertGreater(no_credit["score_mastering"], credit["score_mastering"])
        self.assertIn("has a label 0/1", credit["product_breakdowns"])

    def test_big_acts_are_capped_except_the_test_group(self):
        keys = [f"artist{i}.example" for i in range(400)]
        in_test = [k for k in keys if big_act_test(k)]
        self.assertTrue(20 <= len(in_test) <= 60)  # about 10%
        outside = next(k for k in keys if not big_act_test(k))
        capped = score_products(lead(steady_views=5_000_000), False, key=outside, today=TODAY)
        tested = score_products(lead(steady_views=5_000_000), False, key=in_test[0], today=TODAY)
        self.assertIn("over the cap 1/2", capped["product_breakdowns"])
        self.assertIn("big-act test 2/2", tested["product_breakdowns"])
        self.assertEqual((capped["big_act_test"], tested["big_act_test"]), ("", "yes"))
        self.assertEqual(big_act_test(in_test[0]), big_act_test(in_test[0]))  # stable

    def test_detect_signals(self):
        found = detect_signals("Mixing engineer and producer. New drum kit out! Serum presets. Self-released.",
                               ["https://distrokid.com/hyperfollow/x"])
        self.assertEqual(set(found), {"producer_engineer", "sells_sample_packs", "third_party_plugins",
                                      "self_released", "distributor:distrokid", "release_promo"})
        self.assertEqual(detect_signals("We love the reproducer word", []), [])

    def test_minimum_and_adjustment(self):
        self.assertEqual(final_score(9, 1), 10)
        self.assertEqual(final_score(10, 1), 10)
        self.assertEqual(final_score(1, -1), 1)
        self.assertEqual(final_score(5, 3), 6)

    def test_rules_file_products_are_validated(self):
        validate_products(json.loads(json.dumps(DEFAULT_PRODUCTS)))
        bad = json.loads(json.dumps(DEFAULT_PRODUCTS))
        bad["plugins"]["made_up"] = {"points": 1}
        with self.assertRaisesRegex(ValueError, "unknown signals: made_up"):
            validate_products(bad)
        bad = json.loads(json.dumps(DEFAULT_PRODUCTS))
        bad["mastering"]["releases"]["bands"] = [12, 6]
        with self.assertRaisesRegex(ValueError, "must rise"):
            validate_products(bad)
        lighter = json.loads(json.dumps(DEFAULT_PRODUCTS))
        del lighter["plugins"]["third_party_plugins"]  # dropping a signal is allowed
        validate_products(lighter)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rules.json"
            path.write_text(json.dumps({"target_vertical": "M", "ideal_customer_profile": "C",
                                        "signal_keywords": [], "evaluation_criteria": "E",
                                        "scoring": {"old": "weights"}}))
            self.assertEqual(load_vertical_rules(path)["products"], DEFAULT_PRODUCTS)
        rules = load_vertical_rules("config/vertical_rules.json")
        self.assertEqual(rules["products"], DEFAULT_PRODUCTS)
        self.assertTrue(rules["scoring_version"])

def response(status, data):
    mock = Mock(status_code=status)
    mock.json.return_value = data
    return mock


class InstagramTests(unittest.TestCase):
    def setUp(self):
        instagram.throttle.interval = 0

    def test_reads_profile_and_pages_posts(self):
        recent = (date.today() - timedelta(days=40)).isoformat()
        first = {"business_discovery": {
            "username": "beats", "name": "Beats", "biography": "Kits at https://beats.gumroad.com",
            "followers_count": 25_000, "media_count": 300,
            "media": {"data": [
                {"caption": "New drum kit out now", "timestamp": f"{recent}T10:00:00+0000",
                 "media_type": "VIDEO", "view_count": 12_000, "like_count": 900, "comments_count": 40},
                {"caption": "Holiday pics", "timestamp": f"{recent}T09:00:00+0000", "media_type": "IMAGE"},
            ], "paging": {"cursors": {"after": "CUR"}}},
        }}
        second = {"business_discovery": {"media": {"data": [
            {"caption": "Old sample pack", "timestamp": "2020-01-01T00:00:00+0000", "media_type": "IMAGE",
             "like_count": 10},
        ]}}}
        with patch("src.instagram.requests.get", side_effect=[response(200, first), response(200, second)]) as get:
            part = instagram.fetch_instagram_profile("beats", "TOKEN", "123")
        self.assertIn("business_discovery.username(beats)", get.call_args_list[0].kwargs["params"]["fields"])
        self.assertIn("media.after(CUR)", get.call_args_list[1].kwargs["params"]["fields"])
        self.assertTrue(get.call_args_list[0].args[0].endswith("/v25.0/123"))
        self.assertEqual(part["audience_size"], 25_000)
        self.assertEqual(part["releases"], [{"date": recent, "views": 12_000}, {"date": "2020-01-01", "views": None}])
        self.assertIn("https://beats.gumroad.com", part["links"])

    def test_personal_account_and_bad_token(self):
        personal = response(400, {"error": {"code": 110, "message": "Invalid user id"}})
        with patch("src.instagram.requests.get", return_value=personal):
            with self.assertRaises(instagram.InstagramUnavailable):
                instagram.fetch_instagram_profile("someone", "T", "1")
        expired = response(400, {"error": {"code": 190, "message": "Session expired"}})
        with patch("src.instagram.requests.get", return_value=expired):
            with self.assertRaisesRegex(RuntimeError, "INSTAGRAM_ACCESS_TOKEN"):
                instagram.fetch_instagram_profile("someone", "T", "1")


class TikTokTests(unittest.TestCase):
    def test_profiles_for_all_posts_only_for_bigger_accounts(self):
        recent = (date.today() - timedelta(days=30)).isoformat()
        profiles = [
            {"url": "https://www.tiktok.com/@big", "nickname": "Big", "followers": 50_000, "videos_count": 400,
             "biography": "beats", "bio_link": "big.beatstars.com"},
            {"url": "https://www.tiktok.com/@small", "nickname": "Small", "followers": 900},
            {"input": {"url": "https://www.tiktok.com/@gone"}, "error": "Profile not found"},
        ]
        posts = [
            {"profile_url": "https://www.tiktok.com/@big", "description": "New type beat #producer",
             "create_time": f"{recent}T12:00:00.000Z", "play_count": 80_000},
            {"profile_url": "https://www.tiktok.com/@big", "description": "Day in the life",
             "create_time": f"{recent}T12:00:00.000Z", "play_count": 1_000_000},
        ]
        triggered = []

        def post(url, params=None, json=None, **_):
            triggered.append((params, json))
            return response(200, {"snapshot_id": f"s{len(triggered)}"})

        def get(url, **_):
            if "/progress/" in url:
                return response(200, {"status": "ready"})
            return response(200, profiles if url.endswith("/s1") else posts)

        with patch("src.tiktok.requests.post", side_effect=post), patch("src.tiktok.requests.get", side_effect=get):
            jobs = tiktok.TikTokJobs("TOKEN", poll_seconds=0)
            jobs.start(["big", "small", "gone"])
            results = jobs.finish()

        self.assertEqual(triggered[0][0]["dataset_id"], tiktok.PROFILES_DATASET)
        self.assertEqual(len(triggered[0][1]), 3)
        self.assertEqual(triggered[1][0]["discover_by"], "profile_url")
        self.assertEqual(triggered[1][1], [{"url": "https://www.tiktok.com/@big", "num_of_posts": 30}])
        self.assertEqual(results["big"]["releases"], [{"date": recent, "views": 80_000}])
        self.assertIn("https://big.beatstars.com", results["big"]["links"])
        self.assertIsNone(results["small"]["release_count"])
        self.assertIn("not collected", results["small"]["text"])
        self.assertIsInstance(results["gone"], ValueError)


class TikTokPipelineTests(unittest.TestCase):
    def test_tiktok_creators_wait_for_the_job_and_others_go_first(self):
        from src.main import run_pipeline
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rules.json").write_text(json.dumps({
                "target_vertical": "M", "ideal_customer_profile": "C", "signal_keywords": [],
                "evaluation_criteria": "E"}))
            (root / "in.csv").write_text(
                "name,website_url,target_type,tiktok\n"
                "Site,https://site.example,creator,\n"
                "Tok,https://www.tiktok.com/@tok,creator,\n")
            order = []

            def collect(creator, _keywords, tiktok_result=None):
                order.append((creator["name"], tiktok_result))
                return merge_evidence([{"source": "website", "title": "", "text": "", "links": [],
                                        "audience_size": None, "release_count": None, "release_dates": []}], [])

            jobs = Mock()
            jobs.finish.return_value = {"tok": "TIKTOK-PART"}
            reply = {"score_adjustment": 0, "adjustment_reason": "", "bottleneck": "b", "custom_hook": "h"}
            with (
                patch.dict(os.environ, {"BRIGHTDATA_API_TOKEN": "T"}),
                patch("src.main.TikTokJobs", return_value=jobs),
                patch("src.main.collect_evidence", side_effect=collect),
                patch("src.main.enrich_creator", return_value=reply),
            ):
                run_pipeline(root / "in.csv", root / "rules.json", root / "out.csv", client=object(), scoring="live")
            jobs.start.assert_called_once_with(["tok"])
            self.assertEqual(order, [("Site", None), ("Tok", "TIKTOK-PART")])


if __name__ == "__main__":
    unittest.main()


class FollowUpTests(unittest.TestCase):
    def test_tiktok_year_estimated_from_posting_rate(self):
        from src.tiktok import estimate_year
        today = date(2026, 11, 17)
        # 30 posts over the last 30 days, 15 of them releases: about 182 a year.
        dates = [(today - timedelta(days=day)).isoformat() for day in range(1, 31)]
        estimate = estimate_year(dates, 15, 30, today=today)
        self.assertEqual(estimate["releases_last_12_months"], 182)
        self.assertEqual(estimate["active_months_last_12"], 12)
        self.assertAlmostEqual(estimate["factor"], 182 / 15)
        # A sample that wasn't cut off, or already covers the year, isn't estimated.
        self.assertIsNone(estimate_year(dates[:10], 5, 10, today=today))
        old = dates[:29] + [(today - timedelta(days=400)).isoformat()]
        self.assertIsNone(estimate_year(old, 15, 30, today=today))
        # A 3-month-old account is scaled to its age, not a full year.
        young = estimate_year(dates, 15, 30, account_created=(today - timedelta(days=90)).isoformat(), today=today)
        self.assertEqual(young["releases_last_12_months"], 45)
        self.assertEqual(young["active_months_last_12"], 3)

    def test_estimates_feed_the_evidence(self):
        part = youtube_part([20_000] * 10)
        part.update(source="tiktok", estimate={"releases_last_12_months": 120, "active_months_last_12": 12,
                                               "factor": 12.0, "sample_days": 30})
        evidence = merge_evidence([part], [], today=TODAY)
        self.assertEqual(evidence["releases_last_12_months"], 120)
        self.assertEqual(evidence["active_months_last_12"], 12)
        self.assertEqual(evidence["steady_views"], 20_000 * 10 * 12)
        self.assertEqual(evidence["estimated_sources"], ["tiktok"])

    def test_limited_data(self):
        from src.scoring import score_products
        known = {"releases_last_12_months": 8, "latest_release_date": TODAY.isoformat(), "product_signals": []}
        self.assertEqual(score_products(known, False, today=TODAY)["limited_data"], "")
        self.assertIn("only 0%", score_products({}, False, today=TODAY)["limited_data"])

    def test_discogs_newest_releases_get_exact_dates(self):
        from src import labels
        labels.discogs_throttle.interval = 0
        this_year = date.today().year
        releases = {"pagination": {"items": 3, "pages": 1}, "releases": [
            {"id": 1, "type": "release", "year": this_year, "title": "New"},
            {"id": 2, "type": "master", "year": this_year, "title": "Master"},
            {"id": 3, "type": "release", "year": 2015, "title": "Old"},
        ]}

        def get(url, params=None, **_):
            data = {
                "/database/search": {"results": [{"id": 7, "title": "Hospital Records"}]},
                "/labels/7/releases": releases,
                "/labels/7": {"name": "Hospital Records"},
                "/releases/1": {"released": f"{this_year}-03-00"},
            }
            for fragment, body in data.items():
                if url.endswith(fragment):
                    return response(200, body)
            raise AssertionError(url)

        with patch("src.labels.requests.get", side_effect=get) as mocked:
            part = labels.fetch_discogs_label("Hospital Records", "T")
        called = [call.args[0] for call in mocked.call_args_list]
        self.assertEqual(sum("/releases/" in url for url in called), 1)  # not the master or 2015
        self.assertEqual(part["release_dates"], [f"{this_year}-03", str(this_year), "2015"])

    def test_instagram_creators_go_after_others(self):
        from src.main import run_pipeline
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rules.json").write_text(json.dumps({
                "target_vertical": "M", "ideal_customer_profile": "C", "signal_keywords": [],
                "evaluation_criteria": "E"}))
            (root / "in.csv").write_text(
                "name,website_url,target_type,instagram\n"
                "Insta,https://insta.example,creator,@insta\n"
                "Site,https://site.example,creator,\n")
            order = []

            def collect(creator, _keywords, _tiktok=None):
                order.append(creator["name"])
                return merge_evidence([{"source": "website", "title": "", "text": "", "links": [],
                                        "audience_size": None, "release_count": None, "release_dates": []}], [])

            reply = {"score_adjustment": 0, "adjustment_reason": "", "bottleneck": "b", "custom_hook": "h"}
            with (
                patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "T", "INSTAGRAM_BUSINESS_ID": "1"}),
                patch("src.main.collect_evidence", side_effect=collect),
                patch("src.main.enrich_creator", return_value=reply),
            ):
                run_pipeline(root / "in.csv", root / "rules.json", root / "out.csv", client=object(), scoring="live")
            self.assertEqual(order, ["Site", "Insta"])
