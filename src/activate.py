import csv
from pathlib import Path

import requests

OUTPUT_COLUMNS = (
    "name",
    "website_url",
    "target_type",
    "page_title",
    "storefront_links",
    "release_signals",
    "score",
    "bottleneck",
    "custom_hook",
)
WEBHOOK_TIMEOUT = 15


def _activation_record(record: dict[str, object]) -> dict[str, object]:
    return {column: record[column] for column in OUTPUT_COLUMNS}


def write_enriched_creators(
    path: str | Path, records: list[dict[str, object]]
) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as destination:
        writer = csv.DictWriter(destination, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        for record in records:
            row = _activation_record(record)
            row["storefront_links"] = " | ".join(record["storefront_links"])
            row["release_signals"] = " | ".join(record["release_signals"])
            writer.writerow(row)


def send_activation_webhook(
    records: list[dict[str, object]], webhook_url: str
) -> None:
    payload = {"creators": [_activation_record(record) for record in records]}
    response = requests.post(webhook_url, json=payload, timeout=WEBHOOK_TIMEOUT)
    response.raise_for_status()
