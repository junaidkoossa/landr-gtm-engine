import json
from pathlib import Path

from src.scoring import default_products, validate_products

REQUIRED_RULE_FIELDS = (
    "target_vertical",
    "ideal_customer_profile",
    "signal_keywords",
    "evaluation_criteria",
)
# Creators scoring below this are written to the CSV but not sent to the webhook.
# Scores run 1-10, so 1 sends everyone.
DEFAULT_ACTIVATION_MIN_SCORE = 1


def load_vertical_rules(path: str | Path) -> dict[str, object]:
    with Path(path).open(encoding="utf-8") as source:
        try:
            rules = json.load(source)
        except json.JSONDecodeError as error:
            raise ValueError(f"{path} is not valid JSON: {error}") from error

    if not isinstance(rules, dict):
        raise ValueError(f"{path} must contain a JSON object")

    missing_fields = [field for field in REQUIRED_RULE_FIELDS if field not in rules]
    if missing_fields:
        raise ValueError(f"{path} is missing rule fields: {', '.join(missing_fields)}")
    for field in ("target_vertical", "ideal_customer_profile", "evaluation_criteria"):
        if not isinstance(rules[field], str) or not rules[field].strip():
            raise ValueError(f"{path} field {field!r} must be a non-empty string")

    keywords = rules["signal_keywords"]
    if not isinstance(keywords, list) or not all(
        isinstance(keyword, str) and keyword.strip() for keyword in keywords
    ):
        raise ValueError(f"{path} field 'signal_keywords' must be a list of non-empty strings")

    min_score = rules.setdefault("activation_min_score", DEFAULT_ACTIVATION_MIN_SCORE)
    if isinstance(min_score, bool) or not isinstance(min_score, int) or not 1 <= min_score <= 10:
        raise ValueError(f"{path} field 'activation_min_score' must be an integer from 1 to 10")
    # "scoring" (the single creator/label score) was replaced by per-product scores.
    rules.pop("scoring", None)
    rules["products"] = validate_products(rules.get("products", default_products()))
    version = rules.setdefault("scoring_version", "")
    if not isinstance(version, str):
        raise ValueError(f"{path} field 'scoring_version' must be a string")
    return rules


def scoring_rules(rules: dict[str, object]) -> dict[str, object]:
    """The part of the rules the model scores against (pipeline settings left out)."""
    return {field: rules[field] for field in REQUIRED_RULE_FIELDS}
