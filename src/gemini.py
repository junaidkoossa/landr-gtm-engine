"""Score with Google Gemini instead of Claude (SCORING_PROVIDER=gemini).

Meant for small test runs on Gemini's free tier, not full-scale runs: scoring is live
only (one request per creator, no batches), paced to GEMINI_REQUESTS_PER_MINUTE.
Everything around the model is the same as with Claude: the code-computed base score,
the +/-1 adjustment, the system prompt with its prompt-injection rule, the JSON
schema, and the checks on the reply.

Needs GEMINI_API_KEY (or GOOGLE_API_KEY), from https://aistudio.google.com/apikey.
The model is GEMINI_MODEL in src/settings.py, or the GEMINI_MODEL env var. When it
stays overloaded (503) or rate-limited after its retries, GEMINI_FALLBACK_MODEL is
tried ("none" turns that off). If neither answers, the run stops without counting it
against the creator.
"""
import json
import logging
import os
import re
import time

from src.enricher import MAX_TOKENS, ScoringUnavailable, OUTPUT_SCHEMA, SYSTEM_PROMPT, build_payload, validate_result
from src.labels import Throttle
from src.settings import GEMINI_FALLBACK_MODEL, GEMINI_MAX_RETRIES, GEMINI_MODEL, GEMINI_REQUESTS_PER_MINUTE

logger = logging.getLogger(__name__)
throttle = Throttle(60 / GEMINI_REQUESTS_PER_MINUTE)
_client = None


def gemini_model() -> str:
    return os.getenv("GEMINI_MODEL") or GEMINI_MODEL


def fallback_model() -> str | None:
    """The model to try when gemini_model() keeps failing; None when turned off."""
    value = (os.getenv("GEMINI_FALLBACK_MODEL") or GEMINI_FALLBACK_MODEL).strip()
    return None if value.lower() in ("", "none", "off") or value == gemini_model() else value


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
    ScoringUnavailable when no creator could be scored right now (bad key, quota used
    up, model overloaded), so the run stops without counting it against the creator."""
    client = client or _get_client()
    config = {
        "system_instruction": SYSTEM_PROMPT,
        "response_mime_type": "application/json",
        "response_json_schema": OUTPUT_SCHEMA,
        "max_output_tokens": MAX_TOKENS,
        "automatic_function_calling": {"disable": True},  # no tools; also quiets the "AFC" log line
    }
    contents = build_payload(creator, evidence, rules)
    models = [gemini_model()] + [model for model in [fallback_model()] if model]
    for index, model in enumerate(models):
        try:
            response = _generate(client, model, contents, config, creator["name"])
            break
        except _Busy as busy:
            if index + 1 < len(models):
                logger.warning("%s is still busy (%s); trying %s", model, busy, models[index + 1])
                continue
            tried = " and ".join(models)
            raise ScoringUnavailable(
                f"Gemini is too busy right now ({tried}: {busy}). Nothing was counted against "
                f"the creators; run again later or set GEMINI_MODEL to another model"
            ) from busy.__cause__

    candidates = getattr(response, "candidates", None) or []
    finish = str(getattr(candidates[0], "finish_reason", "") or "") if candidates else ""
    if "MAX_TOKENS" in finish:
        raise ValueError(f"Gemini ran out of output tokens scoring {creator['name']}")
    if "SAFETY" in finish or "PROHIBITED" in finish or "BLOCKLIST" in finish:
        raise ValueError(f"Gemini declined to score {creator['name']} ({finish})")
    return {**validate_result(creator["name"], response.text, "Gemini"), "scored_by": model}


class _Busy(Exception):
    """The model stayed overloaded or rate-limited through every retry."""


def _generate(client, model: str, contents: str, config: dict, name: str):
    from google.genai import errors

    for attempt in range(GEMINI_MAX_RETRIES + 1):
        throttle.wait()
        try:
            return client.models.generate_content(model=model, contents=contents, config=config)
        except errors.APIError as error:
            message = str(getattr(error, "message", "") or error)
            details = json.dumps(getattr(error, "details", None) or {})
            if error.code in (401, 403) or "API_KEY_INVALID" in details:
                raise ScoringUnavailable(f"GEMINI_API_KEY was rejected: {message}") from error
            if error.code == 429 and "PerDay" in details + message:
                raise ScoringUnavailable(
                    f"Gemini's daily free-tier limit for {model} is used up (it resets at midnight "
                    f"Pacific time): {message}"
                ) from error
            if error.code == 429 or error.code >= 500:
                if attempt == GEMINI_MAX_RETRIES:
                    raise _Busy(f"{error.code} {message[:120]}") from error
                delay = _retry_delay(error, attempt)
                logger.info("Gemini %s (%s); retrying in %.0fs", error.code, message[:120], delay)
                time.sleep(delay)
                continue
            raise ValueError(f"Gemini couldn't score {name}: {error.code} {message}") from error
