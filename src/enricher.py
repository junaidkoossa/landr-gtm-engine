import json

from openai import OpenAI

MODEL = "gpt-4o-mini"
RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "creator_fit",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "score": {"type": "integer", "minimum": 1, "maximum": 10},
                "bottleneck": {"type": "string"},
                "custom_hook": {"type": "string"},
            },
            "required": ["score", "bottleneck", "custom_hook"],
            "additionalProperties": False,
        },
    },
}


def enrich_creator(
    creator: dict[str, str],
    scrape_result: dict[str, object],
    rules: dict[str, object],
    client: OpenAI | None = None,
) -> dict[str, object]:
    """Score one creator against vertical rules and validate the model response."""
    if client is None:
        client = OpenAI()

    evidence = {
        "creator": creator,
        "website_title": scrape_result["page_title"],
        "website_text": scrape_result["source_text"],
        "storefront_links": scrape_result["storefront_links"],
        "release_signals": scrape_result["release_signals"],
    }
    completion = client.chat.completions.create(
        model=MODEL,
        temperature=0,
        response_format=RESPONSE_FORMAT,
        messages=[
            {
                "role": "system",
                "content": (
                    "Evaluate creator fit using only the supplied evidence and rules. "
                    "Do not invent facts. Return a JSON object with exactly three fields: "
                    "score (integer from 1 to 10), bottleneck (the main growth or "
                    "distribution constraint), and custom_hook (one concise, evidence-based "
                    "outreach angle)."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"vertical_rules": rules, "evidence": evidence},
                    ensure_ascii=False,
                ),
            },
        ],
    )

    content = completion.choices[0].message.content
    if not content:
        raise ValueError(f"OpenAI returned no enrichment for {creator['name']}")
    try:
        result = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValueError(f"OpenAI returned invalid JSON for {creator['name']}") from error

    if not isinstance(result, dict):
        raise ValueError(f"OpenAI returned a non-object result for {creator['name']}")
    score = result.get("score")
    if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 10:
        raise ValueError(f"OpenAI returned an invalid score for {creator['name']}")
    for field in ("bottleneck", "custom_hook"):
        if not isinstance(result.get(field), str) or not result[field].strip():
            raise ValueError(f"OpenAI returned an invalid {field} for {creator['name']}")

    return {
        "score": score,
        "bottleneck": result["bottleneck"].strip(),
        "custom_hook": result["custom_hook"].strip(),
    }
