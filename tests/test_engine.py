import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.activate import send_activation_webhook, write_enriched_creators
from src.enricher import enrich_creator
from src.ingest import load_creators
from src.main import run_pipeline
from src.rules import load_vertical_rules
from src.scraper import scrape_creator


class IngestionTests(unittest.TestCase):
    def test_loads_complete_rows_and_reports_incomplete_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "creators.csv"
            path.write_text(
                "name,website_url,target_type\n"
                "Incomplete,,creator\n"
                "Complete,https://example.com,creator\n",
                encoding="utf-8",
            )

            with self.assertLogs("src.ingest", level="WARNING") as logs:
                creators = load_creators(path)

        self.assertEqual(creators, [{
            "name": "Complete",
            "website_url": "https://example.com",
            "target_type": "creator",
        }])
        self.assertIn("Skipping input row 2", logs.output[0])

    def test_rejects_missing_required_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "creators.csv"
            path.write_text("name,website_url\nArtist,https://example.com\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "target_type"):
                load_creators(path)


class ScraperTests(unittest.TestCase):
    def test_extracts_page_and_storefront_signals(self):
        response = Mock()
        response.text = """
            <html><head><title>New Sample Pack</title></head>
            <body><script>ignore me</script><p>New release available.</p>
            <a href="https://artist.bandcamp.com/album/release">Listen</a>
            <a href="https://gumroad.com/l/pack">Buy</a></body></html>
        """
        with patch("src.scraper.requests.get", return_value=response) as get:
            result = scrape_creator(
                {"name": "Artist", "website_url": "https://artist.example", "target_type": "creator"},
                ["sample pack", "new release"],
            )

        get.assert_called_once()
        self.assertEqual(result["page_title"], "New Sample Pack")
        self.assertEqual(len(result["storefront_links"]), 2)
        self.assertEqual(result["release_signals"], ["sample pack", "new release"])
        self.assertNotIn("ignore me", result["source_text"])

    def test_rejects_non_http_urls(self):
        with self.assertRaisesRegex(ValueError, "HTTP"):
            scrape_creator(
                {"name": "Artist", "website_url": "file:///tmp/site", "target_type": "creator"},
                [],
            )


class RulesAndEnrichmentTests(unittest.TestCase):
    def test_loads_vertical_rules(self):
        rules = load_vertical_rules("config/vertical_rules.json")
        self.assertEqual(rules["target_vertical"], "Music Creator Ecosystem & Boutique Sample Labels")

    def test_enrichment_validates_structured_response(self):
        message = SimpleNamespace(
            content=json.dumps({
                "score": 8,
                "bottleneck": "Limited distribution",
                "custom_hook": "Expand the catalog with a direct storefront.",
            })
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(return_value=SimpleNamespace(
                        choices=[SimpleNamespace(message=message)]
                    ))
                )
            )
        )

        result = enrich_creator(
            {"name": "Artist"},
            {"page_title": "", "source_text": "", "storefront_links": [], "release_signals": []},
            {},
            client,
        )

        self.assertEqual(result["score"], 8)
        self.assertEqual(result["bottleneck"], "Limited distribution")

    def test_enrichment_rejects_out_of_range_score(self):
        message = SimpleNamespace(content='{"score": 11, "bottleneck": "x", "custom_hook": "y"}')
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=Mock(return_value=SimpleNamespace(
                        choices=[SimpleNamespace(message=message)]
                    ))
                )
            )
        )

        with self.assertRaisesRegex(ValueError, "invalid score"):
            enrich_creator(
                {"name": "Artist"},
                {"page_title": "", "source_text": "", "storefront_links": [], "release_signals": []},
                {},
                client,
            )


class ActivationTests(unittest.TestCase):
    def test_writes_csv_and_posts_activation_payload(self):
        record = {
            "name": "Artist",
            "website_url": "https://artist.example",
            "target_type": "creator",
            "page_title": "Artist",
            "source_text": "private scrape text",
            "storefront_links": ["https://artist.bandcamp.com"],
            "release_signals": ["sample pack"],
            "score": 8,
            "bottleneck": "Limited reach",
            "custom_hook": "Offer distribution.",
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "output" / "creators.csv"
            write_enriched_creators(path, [record])
            with path.open(encoding="utf-8", newline="") as source:
                output = next(csv.DictReader(source))

        self.assertEqual(output["score"], "8")
        self.assertEqual(output["storefront_links"], "https://artist.bandcamp.com")

        response = Mock()
        with patch("src.activate.requests.post", return_value=response) as post:
            send_activation_webhook([record], "https://hooks.example/activate")

        post.assert_called_once()
        self.assertNotIn("source_text", post.call_args.kwargs["json"]["creators"][0])
        response.raise_for_status.assert_called_once()


class PipelineTests(unittest.TestCase):
    def test_runs_and_sorts_the_pipeline_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "input.csv"
            input_path.write_text(
                "name,website_url,target_type\n"
                "Lower,https://lower.example,creator\n"
                "Higher,https://higher.example,creator\n",
                encoding="utf-8",
            )
            rules_path = root / "rules.json"
            rules_path.write_text(json.dumps({
                "target_vertical": "Music",
                "ideal_customer_profile": "Independent creators",
                "signal_keywords": ["sample pack"],
                "evaluation_criteria": "Score fit.",
            }), encoding="utf-8")
            output_path = root / "enriched.csv"

            with (
                patch("src.main.scrape_creator", return_value={
                    "page_title": "Artist",
                    "source_text": "A sample pack",
                    "storefront_links": [],
                    "release_signals": ["sample pack"],
                }),
                patch("src.main.enrich_creator", side_effect=[
                    {"score": 3, "bottleneck": "Reach", "custom_hook": "Hook one"},
                    {"score": 9, "bottleneck": "Catalog", "custom_hook": "Hook two"},
                ]),
                patch("src.main.send_activation_webhook") as webhook,
            ):
                results = run_pipeline(
                    input_path,
                    rules_path,
                    output_path,
                    client=object(),
                    webhook_url="https://hooks.example/activate",
                )
            self.assertTrue(output_path.exists())

        self.assertEqual([record["score"] for record in results], [9, 3])
        webhook.assert_called_once()


if __name__ == "__main__":
    unittest.main()
