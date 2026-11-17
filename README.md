# LANDR Audio Creator GTM Engine

An extensible prospecting pipeline for finding and prioritizing independent music creators,
boutique sample labels, and audio-tool partners for LANDR Audio.

The pipeline keeps vertical-specific qualification criteria in
`config/vertical_rules.json`, separate from ingestion, scraping, and enrichment logic.
Switching the rules file lets the same workflow evaluate a different customer vertical.

## Pipeline

1. Load complete creator rows from `data/input_creators.csv`. Incomplete rows are
   reported and skipped.
2. Scrape the creator website for readable text, Gumroad/Bandcamp links, and
   configured signal keywords.
3. Use OpenAI `gpt-4o-mini` to return a 1-10 fit score, a bottleneck, and an
   evidence-based outreach hook.
4. Sort by score and write `data/enriched_creators.csv`.
5. Optionally POST the ranked records to `ACTIVATION_WEBHOOK_URL` for routing
   through Clay or HubSpot.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `OPENAI_API_KEY` in `.env`. Set `ACTIVATION_WEBHOOK_URL` only when a
downstream webhook is configured; CSV export works without it.

Run the pipeline from the repository root:

```bash
python -m src.main
```

Input, rules, and output paths can be overridden with `--input`, `--rules`,
and `--output`. The sample input is a starter dataset, not the planned
20-creator production list.

## Development

Run the test suite with:

```bash
python -m unittest discover -s tests
```
