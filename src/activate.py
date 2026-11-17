"""Send scored creators to a webhook (Clay by default).

Clay's webhook source turns each POST into one table row (a JSON array becomes a single
row), accepts at most 100 KB per POST and 10 requests a second per workspace, drops
requests over the rate limit with a 429, and holds at most 50,000 records per webhook
source. Webhooks need a Clay Growth or Enterprise plan. So in "clay" format each creator
is its own POST, sent at most CLAY_REQUESTS_PER_SECOND, with a retry after a 429.

The "batched" format sends {"creators": [...]} in chunks, for receivers (Zapier, Make,
your own endpoint) that accept many records per request.
"""
import json
import logging
import time

import requests

from src.store import LIST_COLUMNS, LIST_SEPARATOR, OUTPUT_COLUMNS

logger = logging.getLogger(__name__)

WEBHOOK_FORMATS = ("clay", "batched")
WEBHOOK_TIMEOUT = 15
MAX_PAYLOAD_BYTES = 100_000
CLAY_REQUESTS_PER_SECOND = 8
RETRIES_AFTER_RATE_LIMIT = 3
BATCHED_CHUNK_SIZE = 100
PAYLOAD_COLUMNS = tuple(c for c in OUTPUT_COLUMNS if c not in ("webhook_status", "webhook_sent_at"))


def webhook_payload(row: dict[str, object]) -> dict[str, object]:
    payload = {column: row.get(column) for column in PAYLOAD_COLUMNS}
    for column in LIST_COLUMNS:
        value = payload[column]
        if isinstance(value, str):
            payload[column] = [item for item in value.split(LIST_SEPARATOR) if item]
    return payload


def _post(url: str, body: dict[str, object]) -> None:
    """POST once, waiting and retrying when the receiver says it's rate limited."""
    data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
    if len(data) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"payload is {len(data)} bytes, over the {MAX_PAYLOAD_BYTES}-byte limit")
    for attempt in range(RETRIES_AFTER_RATE_LIMIT + 1):
        response = requests.post(
            url, data=data, headers={"Content-Type": "application/json"}, timeout=WEBHOOK_TIMEOUT
        )
        if response.status_code == 429 and attempt < RETRIES_AFTER_RATE_LIMIT:
            try:
                delay = float(response.headers.get("Retry-After", 1))
            except ValueError:
                delay = 1.0
            time.sleep(delay)
            continue
        response.raise_for_status()
        return


def send_to_webhook(
    rows: list[dict[str, object]], url: str, webhook_format: str = "clay"
) -> list[tuple[dict[str, object], Exception | None]]:
    """Send each row; return every row with None if it arrived, or the error if it didn't."""
    if webhook_format not in WEBHOOK_FORMATS:
        raise ValueError(f"webhook format must be one of {', '.join(WEBHOOK_FORMATS)}")
    outcomes: list[tuple[dict[str, object], Exception | None]] = []

    if webhook_format == "clay":
        interval = 1 / CLAY_REQUESTS_PER_SECOND
        for row in rows:
            started = time.monotonic()
            try:
                _post(url, webhook_payload(row))
                outcomes.append((row, None))
            except (requests.RequestException, ValueError) as error:
                outcomes.append((row, error))
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
        return outcomes

    for start in range(0, len(rows), BATCHED_CHUNK_SIZE):
        chunk = rows[start:start + BATCHED_CHUNK_SIZE]
        try:
            _post(url, {"creators": [webhook_payload(row) for row in chunk]})
            outcomes.extend((row, None) for row in chunk)
        except (requests.RequestException, ValueError) as error:
            outcomes.extend((row, error) for row in chunk)
    return outcomes
