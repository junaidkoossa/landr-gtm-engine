import argparse
import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv
from openai import OpenAI, OpenAIError

from src.activate import send_activation_webhook, write_enriched_creators
from src.enricher import enrich_creator
from src.ingest import load_creators
from src.rules import load_vertical_rules
from src.scraper import scrape_creator

logger = logging.getLogger(__name__)


def run_pipeline(
    input_path: str | Path,
    rules_path: str | Path,
    output_path: str | Path,
    client: OpenAI | None = None,
    webhook_url: str | None = None,
) -> list[dict[str, object]]:
    creators = load_creators(input_path)
    rules = load_vertical_rules(rules_path)
    if client is None:
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("Set OPENAI_API_KEY before running the enrichment pipeline")
        client = OpenAI(api_key=api_key)

    enriched_creators = []
    for creator in creators:
        scrape_result = scrape_creator(creator, rules["signal_keywords"])
        enrichment = enrich_creator(creator, scrape_result, rules, client)
        enriched_creators.append({**creator, **scrape_result, **enrichment})

    enriched_creators.sort(key=lambda record: record["score"], reverse=True)
    write_enriched_creators(output_path, enriched_creators)
    if webhook_url:
        send_activation_webhook(enriched_creators, webhook_url)
    return enriched_creators


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Enrich creator prospects for a GTM workflow.")
    parser.add_argument("--input", default="data/input_creators.csv")
    parser.add_argument("--rules", default="config/vertical_rules.json")
    parser.add_argument("--output", default="data/enriched_creators.csv")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    try:
        records = run_pipeline(
            args.input,
            args.rules,
            args.output,
            webhook_url=os.getenv("ACTIVATION_WEBHOOK_URL") or None,
        )
    except (OSError, ValueError, RuntimeError, requests.RequestException, OpenAIError) as error:
        parser.exit(1, f"error: {error}\n")

    logger.info("Wrote %d enriched creators to %s", len(records), args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
