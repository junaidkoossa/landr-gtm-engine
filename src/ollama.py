"""Score with a model running on this computer through Ollama (SCORING_PROVIDER=ollama).

Free and offline: nothing leaves the computer and there is no API key. Scoring is live,
one creator at a time. Everything around the model is the same as with Claude: the
code-computed base score, the +/-1 adjustment, the system prompt with its
prompt-injection rule, the JSON schema (sent as Ollama's "format", so the model is held
to it) and the checks on the reply.

A small model sometimes still returns broken JSON or breaks the output rules, so such
a reply is asked for again OLLAMA_MAX_RETRIES times before the creator counts as a
failed attempt. If Ollama isn't running, doesn't have the model, or keeps timing out,
the run stops without counting it against the creator.

Settings: OLLAMA_HOST (default http://localhost:11434) and OLLAMA_MODEL (default
llama3.1:8b), as env vars or in src/settings.py.
"""
import json
import logging
import os
import re

import requests

from src.enricher import ScoringUnavailable, build_payload, scoring_prompt, validate_result
from src.settings import (
    OLLAMA_HOST,
    OLLAMA_MAX_OUTPUT_TOKENS,
    OLLAMA_MAX_RETRIES,
    OLLAMA_MODEL,
    OLLAMA_NUM_CTX,
    OLLAMA_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)


def ollama_host() -> str:
    """The Ollama URL. Accepts the forms the Ollama app itself uses for OLLAMA_HOST,
    such as "localhost:11434" or "0.0.0.0" (listen everywhere, so connect locally)."""
    host = (os.getenv("OLLAMA_HOST") or OLLAMA_HOST).strip().rstrip("/")
    if "://" not in host:
        host = f"http://{host}"
    host = host.replace("://0.0.0.0", "://127.0.0.1")
    if not re.search(r":\d+$", host.split("://", 1)[1]):
        host += ":11434"
    return host


def ollama_model() -> str:
    return (os.getenv("OLLAMA_MODEL") or OLLAMA_MODEL).strip()


def _not_running(error: Exception) -> ScoringUnavailable:
    return ScoringUnavailable(
        f"Ollama isn't running at {ollama_host()}. Open the Ollama app (or run `ollama serve`) "
        f"and run again; nothing was counted against the creators ({error.__class__.__name__})"
    )


def _no_model(model: str) -> ScoringUnavailable:
    return ScoringUnavailable(
        f"Ollama doesn't have the model {model}. Run `ollama pull {model}` or set OLLAMA_MODEL "
        f"to a model listed by `ollama list`"
    )


def check_ready(session=None) -> None:
    """Stop before any creator is collected if Ollama isn't up or lacks the model."""
    session = session or requests
    model = ollama_model()
    try:
        response = session.get(f"{ollama_host()}/api/tags", timeout=10)
        response.raise_for_status()
        names = {entry.get("name") for entry in response.json().get("models", [])}
    except (requests.ConnectionError, requests.Timeout) as error:
        raise _not_running(error) from error
    except (requests.RequestException, ValueError) as error:
        raise ScoringUnavailable(f"Ollama at {ollama_host()} gave an unexpected answer: {error}") from error
    if model not in names and f"{model}:latest" not in names:
        raise _no_model(model)


def parse_reply(content: str | None) -> str | None:
    """Pull the JSON object out of a reply that may be wrapped in a code fence or text,
    and fix the slips a small model makes (an adjustment of "1", "+1" or 1.0)."""
    if not content:
        return content
    text = content.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        return text  # validate_result reports it as invalid JSON
    if isinstance(result, dict):
        adjustment = result.get("adjustment")
        if isinstance(adjustment, str) and re.fullmatch(r"\s*[+-]?\d+\s*", adjustment):
            result["adjustment"] = int(adjustment)
        elif isinstance(adjustment, float) and adjustment.is_integer():
            result["adjustment"] = int(adjustment)
    return json.dumps(result)


def score_with_ollama(
    creator: dict[str, str],
    evidence: dict[str, object],
    rules: dict[str, object],
    session=None,
    with_hook: bool = True,
) -> dict[str, object]:
    """Score one creator with the local model. Raises ValueError for this creator's
    failure and ScoringUnavailable when no creator could be scored right now."""
    session = session or requests
    name, model = creator["name"], ollama_model()
    system, schema = scoring_prompt(with_hook)
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": build_payload(creator, evidence, rules)},
        ],
        "format": schema,
        "stream": False,
        "options": {
            "temperature": 0,
            "num_ctx": OLLAMA_NUM_CTX,
            "num_predict": OLLAMA_MAX_OUTPUT_TOKENS,
        },
    }
    tries = OLLAMA_MAX_RETRIES + 1
    for attempt in range(1, tries + 1):
        try:
            response = session.post(f"{ollama_host()}/api/chat", json=body, timeout=OLLAMA_TIMEOUT_SECONDS)
        except requests.ConnectionError as error:
            raise _not_running(error) from error
        except requests.Timeout as error:
            if attempt == tries:
                raise ScoringUnavailable(
                    f"Ollama didn't answer within {OLLAMA_TIMEOUT_SECONDS}s, {tries} times in a row. "
                    f"The computer may be busy or low on memory; close other apps or try a smaller "
                    f"OLLAMA_MODEL. Nothing was counted against the creators"
                ) from error
            logger.info("Ollama timed out scoring %s; trying again (%d of %d)", name, attempt + 1, tries)
            continue

        if response.status_code == 404:
            raise _no_model(model)
        if response.status_code >= 500:
            message = _error_message(response)
            if attempt == tries:
                raise ScoringUnavailable(f"Ollama failed {tries} times in a row: {message}")
            logger.info("Ollama error scoring %s (%s); trying again", name, message)
            continue
        if response.status_code >= 400:
            raise ValueError(f"Ollama couldn't score {name}: {_error_message(response)}")

        try:
            data = response.json()
            content = (data.get("message") or {}).get("content")
            if data.get("done_reason") == "length":
                raise ValueError(f"Ollama ran out of output tokens scoring {name}")
            result = validate_result(name, parse_reply(content), "Ollama", with_hook)
        except ValueError as error:
            if attempt == tries:
                raise ValueError(f"{error} ({tries} tries)") from error
            logger.info("%s; asking again (%d of %d)", error, attempt + 1, tries)
            continue
        return {**result, "scored_by": model}
    raise AssertionError("unreachable")


def _error_message(response) -> str:
    try:
        return str(response.json().get("error") or response.status_code)
    except ValueError:
        return f"{response.status_code} {response.text[:200]}"
