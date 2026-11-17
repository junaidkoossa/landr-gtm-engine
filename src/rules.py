import json
from pathlib import Path

REQUIRED_RULE_FIELDS = (
    "target_vertical",
    "ideal_customer_profile",
    "signal_keywords",
    "evaluation_criteria",
)


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
    return rules
