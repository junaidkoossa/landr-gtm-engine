import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src import labels
from src.collect import MissingCredentials, UnsupportedSource, collect_evidence, merge_evidence
from src.main import run_files, run_pipeline
from src.youtube import channel_lookup_params, fetch_youtube_channel, is_youtube_url, looks_like_release


def json_response(data):
    response = Mock()
    response.json.return_value = data
    return response


def routed_get(routes):
    """A requests.get stand-in that answers by the first route found in the URL."""
    def get(url, params=None, **_kwargs):
        for fragment, data in routes:
            if fragment in url:
                return json_response(data(params) if callable(data) else data)
        raise AssertionError(f"unexpected request {url}")
    return get


class MatchingTests(unittest.TestCase):
    def test_exact_and_variant_names(self):
        from src.matching import match_type
        cases = [
            ("Ninja Tune", "Ninja Tunes", "variant"),
            ("Spice", "Splice", None),
            ("Loopmasters", "Loopmasters Ltd", "variant"),
            ("Hospital", "Hospital Records", "variant"),
            ("Monstercat", "Monster Cat", "exact"),
            ("Black Octopus Sound", "Black Octopus", "variant"),
            ("Cymatics", "Cymatics.fm", "variant"),
            ("Loopmasters", "Bassmasters", None),
            ("Drumcode", "Drum Code", "exact"),
            ("Toolroom", "Toolroom Records", "variant"),
            ("Ghost Syndicate", "Ghosthack", None),
            ("Sample Magic", "Samples Magic", "variant"),
            ("Glass", "Glas", None),
        ]
        for wanted, candidate, expected in cases:
            self.assertEqual(match_type(wanted, candidate), expected, (wanted, candidate))

    def test_best_match_prefers_exact(self):
        from src.matching import best_match
        found = best_match("Ninja Tune", [("Ninja Tunes", 1), ("Ninja Tune", 2)])
        self.assertEqual(found[1:], (2, "exact"))


class YouTubeTests(unittest.TestCase):
    def test_lookup_params_from_channel_urls(self):
        self.assertTrue(is_youtube_url("https://www.youtube.com/@bmoneysounds"))
        self.assertFalse(is_youtube_url("https://artist.example"))
        self.assertEqual(channel_lookup_params("https://www.youtube.com/@bmoneysounds"), {"forHandle": "@bmoneysounds"})
        self.assertEqual(channel_lookup_params("https://youtube.com/channel/UC123/videos"), {"id": "UC123"})
        self.assertEqual(channel_lookup_params("https://youtube.com/user/oldname"), {"forUsername": "oldname"})
        self.assertIsNone(channel_lookup_params("https://youtube.com/c/customname"))

    def test_release_title_filter(self):
        for title in ["New Sample Pack Vol. 2", "FREE Drum Kit 2026", "Travis type beat", "My new EP out now",
                      "Serum presets", "Lo-fi loops", "[FREE] One Shots"]:
            self.assertTrue(looks_like_release(title), title)
        for title in ["Studio vlog", "How I mix vocals", "Q&A with fans", "Keep it simple", "Packing for tour",
                      "Daft Punk bootleg"]:
            self.assertFalse(looks_like_release(title), title)

    def test_fetches_channel_and_counts_release_uploads(self):
        recent = (date.today() - timedelta(days=30)).isoformat()
        channel = {"items": [{
            "snippet": {"title": "bmoney sounds", "description": "Packs: https://bmoney.gumroad.com/l/drums"},
            "statistics": {"subscriberCount": "12000", "videoCount": "140"},
            "contentDetails": {"relatedPlaylists": {"uploads": "UU123"}},
        }]}
        page_one = {"nextPageToken": "p2", "items": [
            {"snippet": {"title": "New sample pack", "description": "Get it https://bmoney.bandcamp.com/album/x."},
             "contentDetails": {"videoId": "v1", "videoPublishedAt": f"{recent}T12:00:00Z"}},
            {"snippet": {"title": "Studio vlog"}, "contentDetails": {"videoPublishedAt": f"{recent}T10:00:00Z"}},
        ]}
        page_two = {"nextPageToken": "p3", "items": [
            {"snippet": {"title": "Beat tape"}, "contentDetails": {"videoId": "v3", "videoPublishedAt": "2020-01-05T12:00:00Z"}},
        ]}

        def playlist(params):
            return page_two if params.get("pageToken") == "p2" else page_one

        views = {"items": [{"id": "v1", "statistics": {"viewCount": "4200"}},
                           {"id": "v3", "statistics": {"viewCount": "90"}}]}
        get = routed_get([("/channels", channel), ("/playlistItems", playlist), ("/videos", views)])
        with patch("src.youtube.requests.get", side_effect=get) as mocked:
            part = fetch_youtube_channel(
                {"name": "bmoney sounds", "website_url": "https://www.youtube.com/@bmoneysounds"}, "KEY"
            )

        self.assertEqual(mocked.call_args_list[0].kwargs["params"]["forHandle"], "@bmoneysounds")
        self.assertEqual(mocked.call_count, 4)  # 2 pages (stopped once over a year old) + 1 views call
        self.assertEqual(mocked.call_args_list[-1].kwargs["params"]["id"], "v1,v3")
        self.assertEqual(part["releases"], [{"date": recent, "views": 4200}, {"date": "2020-01-05", "views": 90}])
        self.assertIn("4,200 views", part["text"])
        self.assertEqual(part["audience_size"], 12000)
        self.assertEqual(part["release_count"], 2)
        self.assertEqual(part["release_dates"], [recent, "2020-01-05"])
        self.assertIn("Studio vlog", part["text"])
        self.assertIn("not counted as releases", part["text"])
        self.assertIn("https://bmoney.gumroad.com/l/drums", part["links"])
        self.assertIn("https://bmoney.bandcamp.com/album/x", part["links"])

    def test_custom_url_uses_search(self):
        get = routed_get([
            ("/search", {"items": [{"id": {"channelId": "UC999"}}]}),
            ("/channels", lambda params: {"items": [{"snippet": {"title": params["id"]}, "statistics": {}, "contentDetails": {}}]}),
        ])
        with patch("src.youtube.requests.get", side_effect=get):
            part = fetch_youtube_channel({"name": "X", "website_url": "https://youtube.com/c/customname"}, "KEY")
        self.assertEqual(part["title"], "UC999")


class LabelSourceTests(unittest.TestCase):
    def setUp(self):
        labels.musicbrainz_throttle.interval = 0
        labels.discogs_throttle.interval = 0

    def test_musicbrainz_exact_match_pages_through_releases(self):
        def releases(params):
            if params["offset"] == 0:
                return {"release-count": 150, "releases": [{"title": "Old", "date": "2015-01-01"}] * 100}
            return {"release-count": 150, "releases": [{"title": "Newest", "date": "2026-03-01"}] + [{"title": "C"}] * 49}

        get = routed_get([
            ("/label/mb-1", {"name": "Loopmasters", "type": "Production", "country": "GB",
                             "relations": [{"url": {"resource": "https://loopmasters.bandcamp.com"}}]}),
            ("/ws/2/label", {"labels": [{"id": "mb-0", "name": "Bassmasters"}, {"id": "mb-1", "name": "loopmasters"}]}),
            ("/ws/2/release", releases),
        ])
        with patch("src.labels.requests.get", side_effect=get) as mocked:
            part = labels.fetch_musicbrainz_label("Loopmasters")

        self.assertEqual(mocked.call_count, 4)
        self.assertEqual(part["release_count"], 150)
        self.assertEqual(part["release_dates"][0], "2026-03-01")
        self.assertTrue(part["match"]["exact"])
        self.assertEqual(part["links"], ["https://loopmasters.bandcamp.com"])

    def test_musicbrainz_name_variant_is_flagged(self):
        get = routed_get([
            ("/label/mb-2", {"name": "Hospital Records"}),
            ("/ws/2/label", {"labels": [{"id": "mb-2", "name": "Hospital Records"}]}),
            ("/ws/2/release", {"release-count": 0, "releases": []}),
        ])
        with patch("src.labels.requests.get", side_effect=get):
            part = labels.fetch_musicbrainz_label("Hospital")
        self.assertFalse(part["match"]["exact"])
        self.assertEqual(part["match"]["match_type"], "variant")
        self.assertEqual(part["match"]["matched_name"], "Hospital Records")
        self.assertIn("flagged for review", part["text"])

    def test_musicbrainz_no_close_match_returns_none(self):
        get = routed_get([("/ws/2/label", {"labels": [{"id": "x", "name": "Something Else"},
                                                      {"id": "y", "name": "Bassmasters"}]})])
        with patch("src.labels.requests.get", side_effect=get):
            self.assertIsNone(labels.fetch_musicbrainz_label("Loopmasters"))

    def test_discogs_label_pages_through_releases(self):
        def releases(params):
            if params["page"] == 1:
                return {"pagination": {"items": 101, "pages": 2},
                        "releases": [{"title": "A", "artist": "X", "year": 2012}] * 100}
            return {"pagination": {"items": 101, "pages": 2}, "releases": [{"title": "B", "artist": "Y", "year": 2025}]}

        get = routed_get([
            ("/database/search", {"results": [{"id": 7, "title": "Loopmasters"}]}),
            ("/labels/7/releases", releases),
            ("/labels/7", {"name": "Loopmasters", "profile": "UK sample label", "urls": ["https://loopmasters.com"]}),
        ])
        with patch("src.labels.requests.get", side_effect=get) as mocked:
            part = labels.fetch_discogs_label("Loopmasters", "TOKEN")

        self.assertEqual(part["release_count"], 101)
        self.assertEqual(part["release_dates"][0], "2025")  # newest first
        self.assertEqual(mocked.call_args.kwargs["headers"]["Authorization"], "Discogs token=TOKEN")
        page_params = [call.kwargs["params"] for call in mocked.call_args_list if "/releases" in call.args[0]]
        self.assertEqual([params["page"] for params in page_params], [1, 2])
        self.assertEqual((page_params[0]["sort"], page_params[0]["sort_order"]), ("year", "desc"))


def part(source, **fields):
    return {"source": source, "title": source, "text": "", "links": [], "audience_size": None,
            "release_count": None, "release_dates": [], **fields}


class CollectTests(unittest.TestCase):
    def test_merge_counts_recent_releases(self):
        evidence = merge_evidence(
            [
                part("musicbrainz", release_count=40, release_dates=["2026-09-01", "2025-12", "2024-01-01"],
                     links=["https://x.bandcamp.com"], text="New release"),
                part("discogs", release_count=55, release_dates=["2026"]),
            ],
            ["new release"],
            today=date(2026, 11, 17),
        )
        self.assertEqual(evidence["data_sources"], ["musicbrainz", "discogs"])
        self.assertEqual(evidence["release_count"], 55)
        self.assertEqual(evidence["releases_last_12_months"], 2)
        self.assertEqual(evidence["latest_release_date"], "2026-09-01")
        self.assertEqual(evidence["storefront_links"], ["https://x.bandcamp.com"])
        self.assertEqual(evidence["release_signals"], ["new release"])

    def test_label_uses_website_musicbrainz_and_discogs(self):
        label = {"name": "Loopmasters", "website_url": "https://loopmasters.example", "target_type": "label"}
        with (
            patch("src.collect.scrape_website", return_value=part("website")),
            patch("src.collect.fetch_musicbrainz_label", return_value=None),
            patch("src.collect.fetch_discogs_label", return_value=part("discogs")) as discogs,
            patch.dict(os.environ, {"DISCOGS_TOKEN": "T"}),
        ):
            evidence = collect_evidence(label, [])
        self.assertEqual(evidence["data_sources"], ["website", "discogs"])
        discogs.assert_called_once_with("Loopmasters", "T")

    def test_label_with_no_source_fails(self):
        label = {"name": "Nobody", "website_url": "https://nobody.example", "target_type": "label"}
        with (
            patch("src.collect.scrape_website", side_effect=ValueError("down")),
            patch("src.collect.fetch_musicbrainz_label", return_value=None),
            patch.dict(os.environ, {}, clear=True),
        ):
            with self.assertRaisesRegex(ValueError, "no data source"):
                collect_evidence(label, [])

    def test_loose_matches_are_returned_for_review(self):
        match = {"source": "discogs", "searched_name": "Hospital", "matched_name": "Hospital Records",
                 "match_type": "variant", "exact": False, "matched_url": "u"}
        evidence = merge_evidence([part("discogs", match=match), part("website")], [])
        self.assertEqual(evidence["review_matches"], [match])

    def test_storefront_column_always_counts_and_failures_are_noted(self):
        creator = {"name": "X", "target_type": "creator", "website_url": "https://down.example",
                   "youtube_url": "", "storefront_url": "https://www.beatstars.com/x"}
        with (
            patch("src.collect.scrape_website", side_effect=ValueError("down")),
        ):
            evidence = collect_evidence(creator, [])
        self.assertEqual(evidence["data_sources"], ["storefront"])
        self.assertEqual(evidence["storefront_links"], ["https://www.beatstars.com/x"])
        # An unknown store site still counts, because it was given as the storefront.
        with patch("src.collect.scrape_website", return_value=part("website")):
            evidence = collect_evidence({**creator, "storefront_url": "https://shop.x.example"}, [])
        self.assertEqual(evidence["storefront_links"], ["https://shop.x.example"])

    def test_website_and_youtube_columns_both_used(self):
        creator = {"name": "X", "target_type": "creator", "website_url": "https://x.example",
                   "youtube_url": "https://www.youtube.com/@x"}
        with (
            patch("src.collect.scrape_website", return_value=part("website")),
            patch("src.collect.fetch_youtube_channel", return_value=part("youtube")),
            patch.dict(os.environ, {"YOUTUBE_API_KEY": "K"}),
        ):
            self.assertEqual(collect_evidence(creator, [])["data_sources"], ["website", "youtube"])

    def test_unreadable_social_urls_are_unsupported(self):
        for url in ["https://linktr.ee/x", "https://www.facebook.com/x", "https://x.com/x"]:
            with self.assertRaises(UnsupportedSource):
                collect_evidence({"name": "X", "website_url": url, "target_type": "creator"}, [])

    def test_instagram_and_tiktok_without_tokens_wait(self):
        with patch.dict(os.environ, {}, clear=True):
            for url in ["https://www.instagram.com/x", "https://www.tiktok.com/@x"]:
                with self.assertRaises(MissingCredentials):
                    collect_evidence({"name": "X", "website_url": url, "target_type": "creator"}, [])

    def test_website_plus_instagram_and_tiktok_columns(self):
        creator = {"name": "X", "website_url": "https://x.example", "target_type": "creator",
                   "instagram": "@x.beats", "tiktok": "https://www.tiktok.com/@xbeats"}
        with (
            patch("src.collect.scrape_website", return_value=part("website")),
            patch("src.collect.fetch_instagram_profile", return_value=part("instagram")) as instagram,
            patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "T", "INSTAGRAM_BUSINESS_ID": "1"}),
        ):
            evidence = collect_evidence(creator, [], part("tiktok"))
        self.assertEqual(evidence["data_sources"], ["website", "instagram", "tiktok"])
        instagram.assert_called_once_with("x.beats", "T", "1")

    def test_personal_instagram_alongside_a_website_is_noted_not_fatal(self):
        from src.instagram import InstagramUnavailable
        creator = {"name": "X", "website_url": "https://x.example", "target_type": "creator", "instagram": "x"}
        with (
            patch("src.collect.scrape_website", return_value=part("website")),
            patch("src.collect.fetch_instagram_profile", side_effect=InstagramUnavailable("personal")),
            patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "T", "INSTAGRAM_BUSINESS_ID": "1"}),
        ):
            self.assertEqual(collect_evidence(creator, [])["data_sources"], ["website"])
        with (
            patch("src.collect.fetch_instagram_profile", side_effect=InstagramUnavailable("personal")),
            patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "T", "INSTAGRAM_BUSINESS_ID": "1"}),
        ):
            with self.assertRaises(UnsupportedSource):
                collect_evidence({**creator, "website_url": "https://instagram.com/x"}, [])

    def test_youtube_creator_uses_youtube_api(self):
        creator = {"name": "B", "website_url": "https://www.youtube.com/@b", "target_type": "creator"}
        with (
            patch("src.collect.fetch_youtube_channel", return_value=part("youtube")) as youtube,
            patch("src.collect.scrape_website") as website,
            patch.dict(os.environ, {"YOUTUBE_API_KEY": "K"}),
        ):
            evidence = collect_evidence(creator, [])
        self.assertEqual(evidence["data_sources"], ["youtube"])
        youtube.assert_called_once_with({**creator, "youtube_url": "https://www.youtube.com/@b"}, "K")
        website.assert_not_called()


EVIDENCE = {
    "page_title": "Artist", "source_text": "text", "storefront_links": [], "release_signals": [],
    "data_sources": ["website"], "audience_size": None, "release_count": None,
    "releases_last_12_months": None, "latest_release_date": "", "recent_releases": [],
    "review_matches": [],
}
REPLY = '{"adjustment": %d, "adjustment_reason": "x", "bottleneck": "Reach", "custom_hook": "Hook"}'


def batch_client(replies_by_name):
    """A fake client; each batch returns a reply per creator, looked up by creator name."""
    state = {"batches": {}}

    def create(requests):
        batch_id = f"batch-{len(state['batches']) + 1}"
        state["batches"][batch_id] = requests
        return SimpleNamespace(id=batch_id)

    def results(batch_id):
        for request in state["batches"][batch_id]:
            name = json.loads(request["params"]["messages"][0]["content"])["evidence"]["creator"]["name"]
            reply = replies_by_name[name]
            if reply is None:
                result = SimpleNamespace(type="expired")
            else:
                message = SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=reply)])
                result = SimpleNamespace(type="succeeded", message=message)
            yield SimpleNamespace(custom_id=request["custom_id"], result=result)

    batches = SimpleNamespace(
        create=Mock(side_effect=create),
        retrieve=Mock(return_value=SimpleNamespace(processing_status="ended")),
        results=Mock(side_effect=results),
    )
    return SimpleNamespace(messages=SimpleNamespace(batches=batches)), state


class BatchPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.rules_path = self.root / "rules.json"
        self.rules_path.write_text(json.dumps({
            "target_vertical": "Music", "ideal_customer_profile": "Creators",
            "signal_keywords": [], "evaluation_criteria": "Score fit.",
        }), encoding="utf-8")
        self.input_path = self.root / "input.csv"
        self.output_path = self.root / "out.csv"
        self.pending_path = run_files(self.output_path)["pending"]

    def tearDown(self):
        self.directory.cleanup()

    def write_input(self, *names):
        self.input_path.write_text(
            "name,website_url,target_type\n"
            + "".join(f"{name},https://{name.lower()}.example,creator\n" for name in names),
            encoding="utf-8",
        )

    def test_batch_scoring_on_sonnet_skips_failures(self):
        self.write_input("Good", "Late")
        client, state = batch_client({"Good": REPLY % 1, "Late": None})
        with patch("src.main.collect_evidence", return_value=EVIDENCE):
            with self.assertLogs("src.main", level="WARNING"):
                results = run_pipeline(self.input_path, self.rules_path, self.output_path,
                                       client=client, scoring="batch", poll_seconds=0)

        self.assertEqual([r["name"] for r in results], ["Good"])
        requests = state["batches"]["batch-1"]
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["params"]["model"], "claude-sonnet-5-5")
        self.assertNotIn("fallbacks", requests[0]["params"])
        self.assertFalse(self.pending_path.exists())
        self.assertIn("Good", self.output_path.read_text(encoding="utf-8"))
        self.assertNotIn("Late", self.output_path.read_text(encoding="utf-8"))

    def test_submits_a_batch_every_batch_size_creators(self):
        names = ["A", "B", "C", "D", "E"]
        self.write_input(*names)
        client, state = batch_client({name: REPLY % 0 for name in names})
        with (
            patch("src.main.BATCH_SIZE", 2),
            patch("src.main.collect_evidence", return_value=EVIDENCE),
        ):
            results = run_pipeline(self.input_path, self.rules_path, self.output_path,
                                   client=client, scoring="auto", poll_seconds=0)

        self.assertEqual([len(requests) for requests in state["batches"].values()], [2, 2, 1])
        self.assertEqual(len(results), 5)

    def test_resumes_pending_batches(self):
        self.write_input("Good", "Late")
        client, state = batch_client({"Good": REPLY % 0, "Late": REPLY % -1})
        records = {
            "creator-0": {"name": "Good", "website_url": "https://good.example", "target_type": "creator", **EVIDENCE},
            "creator-1": {"name": "Late", "website_url": "https://late.example", "target_type": "creator", **EVIDENCE},
        }
        state["batches"]["batch-1"] = [
            {"custom_id": cid, "params": {"messages": [{"content": json.dumps(
                {"evidence": {"creator": {"name": record["name"]}}})}]}}
            for cid, record in records.items()
        ]
        self.pending_path.write_text(
            json.dumps([{"batch_id": "batch-1", "records": records}]), encoding="utf-8")

        with patch("src.main.collect_evidence") as collect:
            results = run_pipeline(self.input_path, self.rules_path, self.output_path,
                                   client=client, scoring="batch", poll_seconds=0)

        collect.assert_not_called()  # both creators were already scored by the resumed batch
        client.messages.batches.create.assert_not_called()
        self.assertEqual([r["name"] for r in results], ["Good", "Late"])
        self.assertFalse(self.pending_path.exists())

    def test_youtube_input_needs_api_key(self):
        self.input_path.write_text(
            "name,website_url,target_type\nB,https://www.youtube.com/@b,creator\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "YOUTUBE_API_KEY"):
                run_pipeline(self.input_path, self.rules_path, self.output_path, client=object())


if __name__ == "__main__":
    unittest.main()
