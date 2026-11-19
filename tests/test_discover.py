import csv
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from src import discover
from src.ingest import load_creators

TODAY = date(2026, 11, 18)
GENRES = [{"name": "Hip-hop", "discogs": {"genre": "Hip Hop"}, "musicbrainz": {"tag": "hip hop"}}]


def config(*searches, **extra):
    return {"genres": GENRES, "countries": {"discogs": ["Canada"], "musicbrainz": ["CA"]},
            "years_back": 0, "major_labels": ["Universal Music"], "searches": list(searches), **extra}


ARTISTS = {"name": "self-released", "source": "discogs", "kind": "artist",
           "params": {"label": "Not On Label"}, "products": ["Distribution", "Mastering"]}
LABELS = {"name": "labels", "source": "discogs", "kind": "label", "products": ["Distribution"]}


def fake_discogs(pages):
    """A stand-in for labels._discogs_get answering from a dict of path -> response."""
    calls = []

    def get(path, params, token):
        calls.append((path, params))
        if path == "database/search" and params.get("type") in ("artist", "label"):
            return {"results": [{"title": params["q"], "id": params["q"].lower().replace(" ", "-")}]}
        return pages.get(path, {"results": []})

    return get, calls


class SortLinksTests(unittest.TestCase):
    def test_each_link_goes_to_its_column(self):
        links = discover.sort_links([
            "https://www.discogs.com/artist/1", "https://open.spotify.com/artist/x",
            "https://www.youtube.com/@maple", "https://www.instagram.com/maple/",
            "https://maple.bandcamp.com", "https://www.tiktok.com/@maple",
            "https://maple.ca", "https://other.ca", "mailto:hi@maple.ca",
        ])
        self.assertEqual(links, {
            "youtube_url": "https://www.youtube.com/@maple", "instagram": "https://www.instagram.com/maple/",
            "storefront_url": "https://maple.bandcamp.com", "tiktok": "https://www.tiktok.com/@maple",
            "website_url": "https://maple.ca",
        })


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(os.environ, {"DISCOGS_TOKEN": "T"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_discogs(self, pages, cfg, known_rows=(), limit=None):
        get, calls = fake_discogs(pages)
        with patch("src.discover._discogs_get", side_effect=get):
            leads = discover.discover(cfg, discover.Known(list(known_rows)), limit, TODAY)
        return leads, calls

    def test_finds_self_released_artists_with_their_links(self):
        pages = {
            "database/search": {"results": [
                {"title": "Maple Kid - First Snow", "label": ["Not On Label (Maple Kid Self-released)"]},
                {"title": "Various - Toronto Tapes"},
                {"title": "Known Act - Old News"},
                {"title": "Maple Kid - Second Snow"},
                {"title": "No Links (2) - Quiet"},
            ]},
            "artists/maple-kid": {"name": "Maple Kid", "urls": ["https://www.youtube.com/@maplekid",
                                                                "https://www.instagram.com/maplekid"]},
            "artists/no-links": {"name": "No Links", "urls": ["https://open.spotify.com/artist/1"]},
        }
        leads, calls = self.run_discogs(pages, config(ARTISTS), known_rows=[{"name": "Known Act"}])

        self.assertEqual([lead["name"] for lead in leads], ["Maple Kid"])
        lead = leads[0]
        self.assertEqual(lead["target_type"], "creator")
        self.assertEqual(lead["youtube_url"], "https://www.youtube.com/@maplekid")
        self.assertEqual(lead["instagram"], "https://www.instagram.com/maplekid")
        self.assertEqual(lead["lead_source"], "discovered:self-released")
        self.assertEqual(lead["product_fit"], "Distribution; Mastering")
        release_search = calls[0][1]
        self.assertEqual((release_search["type"], release_search["genre"], release_search["country"],
                          release_search["year"], release_search["label"]),
                         ("release", "Hip Hop", "Canada", 2026, "Not On Label"))

    def test_labels_get_an_email_and_majors_are_partnerships(self):
        pages = {
            "database/search": {"results": [
                {"title": "A - B", "label": ["Snowfall Records", "Universal Music Canada"]},
                {"title": "C - D", "label": ["Not On Label"]},
            ]},
            "labels/snowfall-records": {"name": "Snowfall Records", "urls": ["https://snowfall.ca"],
                                        "contact_info": "Demos: demos@snowfall.ca"},
        }
        leads, _ = self.run_discogs(pages, config(LABELS))
        self.assertEqual(leads[0]["email"], "demos@snowfall.ca")
        self.assertEqual(leads[0]["target_type"], "label")

        pages["database/search"]["results"][0]["label"] = ["Universal Music Canada"]
        pages["labels/universal-music-canada"] = {"name": "Universal Music Canada", "urls": ["https://umusic.ca"]}
        leads, _ = self.run_discogs(pages, config(LABELS))
        self.assertEqual(leads[0]["product_fit"], "Partnership")

    def test_limits_and_missing_token(self):
        results = [{"title": f"Artist {i} - Song"} for i in range(5)]
        pages = {"database/search": {"results": results},
                 **{f"artists/artist-{i}": {"name": f"Artist {i}", "urls": [f"https://a{i}.example"]}
                    for i in range(5)}}
        leads, _ = self.run_discogs(pages, config({**ARTISTS, "max_leads": 2}, {**ARTISTS, "name": "again"}))
        self.assertEqual([lead["name"] for lead in leads], ["Artist 0", "Artist 1", "Artist 2", "Artist 3", "Artist 4"])
        self.assertEqual({lead["lead_source"] for lead in leads[2:]}, {"discovered:again"})
        leads, _ = self.run_discogs(pages, config(ARTISTS), limit=1)
        self.assertEqual(len(leads), 1)

        with patch.dict(os.environ, {"DISCOGS_TOKEN": ""}), self.assertLogs("src.discover", "WARNING") as logs:
            leads, calls = self.run_discogs(pages, config(ARTISTS))
        self.assertEqual((leads, calls), ([], []))
        self.assertIn("DISCOGS_TOKEN", logs.output[0])

    def test_musicbrainz_query_and_url_relations(self):
        search = {"name": "mb-labels", "source": "musicbrainz", "kind": "label",
                  "query": 'type:"Original Production"', "founded_since": True, "products": ["Distribution"]}
        calls = []

        def get(path, params):
            calls.append((path, params))
            if path == "label":
                return {"labels": [{"id": "1", "name": "North Loop"}]}
            return {"relations": [{"url": {"resource": "https://northloop.bandcamp.com"}},
                                  {"url": {"resource": "https://www.instagram.com/northloop"}}]}

        with patch("src.discover._musicbrainz_get", side_effect=get):
            leads = discover.discover(config(search, founded_within_years=3), discover.Known([]), today=TODAY)
        self.assertEqual(calls[0][1]["query"],
                         'type:"Original Production" AND tag:"hip hop" AND country:CA AND begin:[2023 TO *]')
        self.assertEqual(calls[1], ("label/1", {"inc": "url-rels"}))
        self.assertEqual(leads[0]["storefront_url"], "https://northloop.bandcamp.com")
        self.assertEqual(leads[0]["lead_source"], "discovered:mb-labels")


class InputFileTests(unittest.TestCase):
    def test_new_leads_are_appended_and_old_rows_marked_as_input(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            path.write_text("﻿name,target_type,website_url,youtube_url,storefront_url,instagram,tiktok,email\n"
                            "Mine,creator,,https://www.youtube.com/@mine,,,,me@mine.ca\n", encoding="utf-8")
            lead = discover._lead("Maple Kid", "artist", ["https://maplekid.ca"], ARTISTS)
            discover.append_leads(path, [lead])

            with path.open(encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source)
                self.assertEqual(reader.fieldnames[-3:], ["lead_source", "product_fit", "self_released"])
                rows = list(reader)
            self.assertEqual(rows[0]["email"], "me@mine.ca")
            self.assertEqual([row["lead_source"] for row in rows], ["input", "discovered:self-released"])

            creators = load_creators(path)
            self.assertEqual(creators[1]["website_url"], "https://maplekid.ca")
            self.assertEqual(creators[1]["product_fit"], "Distribution; Mastering")

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "input.csv").write_text("name,target_type,website_url\nMine,creator,https://mine.ca\n")
            (root / "discovery.json").write_text('{"searches": []}')
            before = (root / "input.csv").read_text()
            lead = discover._lead("New", "artist", ["https://new.ca"], ARTISTS)
            with patch("src.discover.discover", return_value=[lead]), patch("src.discover.load_dotenv"):
                discover.main(["--config", str(root / "discovery.json"), "--input", str(root / "input.csv"),
                               "--output", str(root / "out.csv"), "--dry-run"])
            self.assertEqual((root / "input.csv").read_text(), before)
            with patch("src.discover.discover", return_value=[lead]), patch("src.discover.load_dotenv"):
                discover.main(["--config", str(root / "discovery.json"), "--input", str(root / "input.csv"),
                               "--output", str(root / "out.csv")])
            self.assertEqual(len(load_creators(root / "input.csv")), 2)

    def test_config_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "discovery.json"
            path.write_text('{"searches": [{"name": "x", "source": "spotify", "kind": "artist"}]}')
            with self.assertRaisesRegex(ValueError, "discogs or musicbrainz"):
                discover.load_config(path)
        discover.load_config(Path(__file__).parent.parent / "config" / "discovery.json")


class ScoringHookupTests(unittest.TestCase):
    def test_social_triggers_feed_product_signals(self):
        from src.scoring import detect_signals
        found = detect_signals("New Drake type beat, cooked up in FL Studio. Beats for sale!",
                               ["https://www.distrokid.com/hyperfollow/x"])
        self.assertIn("producer_engineer", found)
        self.assertIn("production_content", found)
        self.assertIn("distributor:distrokid", found)
        self.assertIn("producer_engineer", detect_signals("New drop", ["https://www.beatstars.com/maple"]))
        self.assertEqual(detect_signals("Out with the release today", []), [])

    def test_discovered_self_release_is_scored_and_lead_source_kept(self):
        import json

        from tests.test_engine import NoEmails, RULES, SCRAPE_OK, enrichment
        from src.main import run_pipeline

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rules.json").write_text(json.dumps(RULES))
            (root / "input.csv").write_text(
                "name,target_type,website_url,lead_source,product_fit,self_released\n"
                "Found,creator,https://found.example,discovered:self-released,Distribution,yes\n"
                "Mine,creator,https://mine.example,,,\n")

            with (
                patch("src.main.collect_evidence", return_value={**SCRAPE_OK, "product_signals": []}),
                patch("src.main.enrich_creator", side_effect=lambda c, *a, **k: enrichment(0)),
            ):
                rows = run_pipeline(root / "input.csv", root / "rules.json",
                                    root / "out.csv", client=object(), enrichment="apollo",
                                    email_finder=NoEmails())
            by_name = {row["name"]: row for row in rows}
            self.assertIn("self-released 3/3", by_name["Found"]["product_breakdowns"])
            self.assertNotIn("self-released 3/3", by_name["Mine"]["product_breakdowns"])
            with (root / "out.csv").open(encoding="utf-8", newline="") as source:
                stored = {row["name"]: row for row in csv.DictReader(source)}
            self.assertEqual(stored["Found"]["lead_source"], "discovered:self-released")
            self.assertEqual(stored["Mine"]["lead_source"], "input")


if __name__ == "__main__":
    unittest.main()
