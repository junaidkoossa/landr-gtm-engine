"""Score with Google Gemini instead of Claude (SCORING_PROVIDER=gemini).

Meant for small test runs on Gemini's free tier, not full-scale runs: scoring is live
only (one request per creator, no batches), paced to GEMINI_REQUESTS_PER_MINUTE.
Everything around the model is the same as with Claude: the code-computed base score,
the +/-1 adjustment, the system prompt with its prompt-injection rule, the JSON
schema, and the checks on the reply.

Needs GEMINI_API_KEY (or GOOGLE_API_KEY), from https://aistudio.google.com/apikey.
The model is GEMINI_MODEL in src/settings.py, or the GEMINI_MODEL env var.
"""
import json
import logging
import os
import re
import time

from src.enricher import MAX_TOKENS, OUTPUT_SCHEMA, SYSTEM_PROMPT, build_payload, validate_result
from src.labels import Throttle
from src.settings import GEMINI_MAX_RETRIES, GEMINI_MODEL, GEMINI_REQUESTS_PER_MINUTE

logger = logging.getLogger(__name__)
throttle = Throttle(60 / GEMINI_REQUESTS_PER_MINUTE)
_client = None


def gemini_model() -> str:
    return os.getenv("GEMINI_MODEL") or GEMINI_MODEL


def has_api_key() -> bool:
    return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))


def _get_client():
    global _client
    if _client is None:
        from google import genai  # reads GEMINI_API_KEY / GOOGLE_API_KEY

        _client = genai.Client()
    return _client


def _retry_delay(error, attempt: int) -> float:
    """The wait the API asks for (RetryInfo "retryDelay": "17s"), else a growing backoff."""
    match = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', json.dumps(getattr(error, "details", None) or {}))
    return float(match.group(1)) + 1 if match else min(60.0, 5.0 * 2 ** attempt)


def score_with_gemini(
    creator: dict[str, str], evidence: dict[str, object], rules: dict[str, object], client=None
) -> dict[str, object]:
    """Score one creator with Gemini. Raises ValueError for this creator's failure and
    RuntimeError when no later creator could succeed either (bad key, daily quota used up)."""
    from google.genai import errors

    client = client or _get_client()
    model = gemini_model()
    config = {
        "system_instruction": SYSTEM_PROMPT,
        "response_mime_type": "application/json",
        "response_json_schema": OUTPUT_SCHEMA,
        "max_output_tokens": MAX_TOKENS,
    }
    contents = build_payload(creator, evidence, rules)
    for attempt in range(GEMINI_MAX_RETRIES + 1):
        throttle.wait()
        try:
            response = client.models.generate_content(model=model, contents=contents, config=config)
            break
        except errors.APIError as error:
            message = str(getattr(error, "message", "") or error)
            if error.code in (401, 403) or "API_KEY_INVALID" in json.dumps(getattr(error, "details", None) or {}):
                raise RuntimeError(f"GEMINI_API_KEY was rejected: {message}") from error
            if error.code == 429 and "PerDay" in json.dumps(getattr(error, "details", None) or {}) + message:
                raise RuntimeError(
                    f"Gemini's daily free-tier limit for {model} is used up (it resets at midnight "
                    f"Pacific time): {message}"
                ) from error
            if (error.code == 429 or error.code >= 500) and attempt < GEMINI_MAX_RETRIES:
                delay = _retry_delay(error, attempt)
                logger.info("Gemini %s (%s); retrying in %.0fs", error.code, message[:120], delay)
                time.sleep(delay)
                continue
            raise ValueError(f"Gemini couldn't score {creator['name']}: {error.code} {message}") from error

    candidates = getattr(response, "candidates", None) or []
    finish = str(getattr(candidates[0], "finish_reason", "") or "") if candidates else ""
    if "MAX_TOKENS" in finish:
        raise ValueError(f"Gemini ran out of output tokens scoring {creator['name']}")
    if "SAFETY" in finish or "PROHIBITED" in finish or "BLOCKLIST" in finish:
        raise ValueError(f"Gemini declined to score {creator['name']} ({finish})")
    return {**validate_result(creator["name"], response.text, "Gemini"), "scored_by": model}
