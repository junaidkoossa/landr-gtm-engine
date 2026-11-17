import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from google.genai import errors

from src import gemini
from src.enricher import OUTPUT_SCHEMA, SYSTEM_PROMPT, enrich_creator, scoring_provider

RULES = {"target_vertical": "Music", "ideal_customer_profile": "Creators", "signal_keywords": [],
         "evaluation_criteria": "Score fit."}
GOOD = json.dumps({"adjustment": 1, "adjustment_reason": "Strong store.", "bottleneck": "Reach",
                   "custom_hook": "Hook"})


def gemini_client(*outcomes):
    """A fake genai client whose generate_content returns or raises each outcome in turn."""
    def respond(text, finish="STOP"):
        return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=finish)])

    side_effect = [o if isinstance(o, Exception) else respond(*o) if isinstance(o, tuple) else respond(o)
                   for o in outcomes]
    return SimpleNamespace(models=SimpleNamespace(generate_content=Mock(side_effect=side_effect)))


def rate_limited(per_day=False):
    violation = "GenerateRequestsPerDayPerProjectPerModel-FreeTier" if per_day else "PerMinute"
    return errors.ClientError(429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded",
                                              "details": [{"violations": [{"quotaId": violation}]},
                                                          {"retryDelay": "3s"}]}})


class GeminiTests(unittest.TestCase):
    def setUp(self):
        gemini.throttle.interval = 0
        patcher = patch("src.gemini.time.sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def test_same_prompt_schema_and_checks_as_claude(self):
        client = gemini_client(GOOD)
        result = gemini.score_with_gemini({"name": "A"}, {"base_score": 6}, RULES, client)
        self.assertEqual(result["score_adjustment"], 1)
        self.assertEqual(result["scored_by"], "gemini-3.8-flash")
        call = client.models.generate_content.call_args.kwargs
        self.assertEqual(call["config"]["system_instruction"], SYSTEM_PROMPT)
        self.assertEqual(call["config"]["response_json_schema"], OUTPUT_SCHEMA)
        self.assertEqual(call["config"]["response_mime_type"], "application/json")
        self.assertEqual(json.loads(call["contents"])["evidence"]["base_score"], 6)

        bad = json.dumps({"adjustment": 3, "adjustment_reason": "", "bottleneck": "x", "custom_hook": "y"})
        with self.assertRaisesRegex(ValueError, "Gemini returned an invalid score adjustment"):
            gemini.score_with_gemini({"name": "A"}, {}, RULES, gemini_client(bad))

    def test_retries_rate_limits_using_the_given_delay(self):
        client = gemini_client(rate_limited(), GOOD)
        gemini.score_with_gemini({"name": "A"}, {}, RULES, client)
        self.assertEqual(client.models.generate_content.call_count, 2)
        self.sleep.assert_called_once_with(4.0)

    def test_daily_limit_and_bad_key_stop_the_run(self):
        with self.assertRaisesRegex(RuntimeError, "daily free-tier limit"):
            gemini.score_with_gemini({"name": "A"}, {}, RULES, gemini_client(rate_limited(per_day=True)))
        bad_key = errors.ClientError(400, {"error": {"code": 400, "message": "API key not valid",
                                                     "status": "INVALID_ARGUMENT",
                                                     "details": [{"reason": "API_KEY_INVALID"}]}})
        with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
            gemini.score_with_gemini({"name": "A"}, {}, RULES, gemini_client(bad_key))

    def test_gives_up_after_retries_and_reports_cut_off_replies(self):
        busy = errors.ServerError(503, {"error": {"code": 503, "message": "overloaded", "status": "UNAVAILABLE"}})
        with self.assertRaisesRegex(ValueError, "couldn't score A"):
            gemini.score_with_gemini({"name": "A"}, {}, RULES, gemini_client(*[busy] * 4))
        with self.assertRaisesRegex(ValueError, "ran out of output tokens"):
            gemini.score_with_gemini({"name": "A"}, {}, RULES, gemini_client(("{", "FinishReason.MAX_TOKENS")))

    def test_provider_switch(self):
        with patch.dict(os.environ, {"SCORING_PROVIDER": "Gemini"}):
            self.assertEqual(scoring_provider(), "gemini")
        self.assertEqual(scoring_provider("claude"), "claude")
        with self.assertRaisesRegex(ValueError, "SCORING_PROVIDER"):
            scoring_provider("openai")
        with patch("src.gemini.score_with_gemini", return_value={"score_adjustment": 0}) as score:
            enrich_creator({"name": "A"}, {}, RULES, provider="gemini")
        score.assert_called_once()

    def test_pipeline_with_gemini_scores_live_and_never_batches(self):
        from src.main import run_pipeline
        from src.collect import merge_evidence
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "rules.json").write_text(json.dumps(RULES))
            (root / "in.csv").write_text("name,target_type,website_url\n"
                                         + "".join(f"C{i},creator,https://c{i}.example\n" for i in range(25)))
            evidence = merge_evidence([{"source": "website", "title": "", "text": "", "links": [],
                                        "audience_size": None, "release_count": None, "release_dates": []}], [])
            with (
                patch.dict(os.environ, {"GEMINI_API_KEY": "K"}, clear=True),
                patch("src.main.collect_evidence", return_value=evidence),
                patch("src.gemini._get_client", return_value=gemini_client(*[GOOD] * 25)),
                patch("src.main.submit_batch") as submit,
            ):
                results = run_pipeline(root / "in.csv", root / "rules.json", root / "out.csv",
                                       scoring="batch", provider="gemini")
            submit.assert_not_called()
            self.assertEqual(len(results), 25)
            self.assertEqual({row["scored_by"] for row in results}, {"gemini-3.8-flash"})
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                    run_pipeline(root / "in.csv", root / "rules.json", root / "out.csv", provider="gemini")


if __name__ == "__main__":
    unittest.main()
