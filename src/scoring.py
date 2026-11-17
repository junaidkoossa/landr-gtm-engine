"""The weighted base score, worked out in code from the evidence and the rules file.

Creators (out of 10, weights in config/vertical_rules.json under "scoring"):
- steady views: median views per release x releases with views, over the last 12
  months, so a steady creator beats one carried by a single breakout video;
- consistency: months in the last 12 with at least one release;
- storefront: a store link was found;
- audience: followers or subscribers above a minimum.

Labels have no view figures, so they are scored on consistency and catalogue size,
with a storefront as a small bonus that is never a penalty when missing.

Claude then reviews the evidence and may move the score by at most 1 point, saying why.
"""
DEFAULT_SCORING = {
    "creator": {
        "steady_views": {"points": 4, "bands": [50_000, 250_000, 1_000_000, 5_000_000]},
        "consistency": {"points": 3},
        "storefront": {"points": 2},
        "audience": {"points": 1, "min_followers": 10_000},
    },
    "label": {
        "consistency": {"points": 5},
        "catalogue_size": {"points": 5, "bands": [10, 50, 100, 250, 500]},
        "storefront_bonus": {"points": 1},
    },
}
MAX_ADJUSTMENT = 1


def validate_scoring(scoring: object) -> dict:
    """Check a "scoring" section from the rules file; raise ValueError if it's malformed."""
    if not isinstance(scoring, dict) or set(scoring) != {"creator", "label"}:
        raise ValueError("'scoring' must have exactly 'creator' and 'label' sections")
    for kind, defaults in DEFAULT_SCORING.items():
        section = scoring[kind]
        if not isinstance(section, dict) or set(section) != set(defaults):
            raise ValueError(f"'scoring.{kind}' must have exactly: {', '.join(defaults)}")
        for name, settings in section.items():
            points = settings.get("points") if isinstance(settings, dict) else None
            if isinstance(points, bool) or not isinstance(points, int) or points < 0:
                raise ValueError(f"'scoring.{kind}.{name}.points' must be a whole number, 0 or more")
            if "bands" in defaults[name]:
                bands = settings.get("bands")
                if (
                    not isinstance(bands, list)
                    or len(bands) != points
                    or not all(isinstance(b, (int, float)) and not isinstance(b, bool) for b in bands)
                    or bands != sorted(bands)
                ):
                    raise ValueError(
                        f"'scoring.{kind}.{name}.bands' must be {points} rising numbers "
                        "(one per point)"
                    )
    return scoring


def _short(number: float | int | None) -> str:
    if number is None:
        return "unknown"
    for limit, suffix in ((1_000_000, "M"), (1_000, "K")):
        if number >= limit:
            return f"{number / limit:.1f}".rstrip("0").rstrip(".") + suffix
    return str(int(number))


def _banded(value: float | int | None, bands: list) -> int:
    return 0 if value is None else sum(1 for band in bands if value >= band)


def _consistency(evidence: dict, points: int) -> tuple[int, str]:
    months = evidence.get("active_months_last_12")
    if months is None:
        return 0, f"consistency 0/{points} (no dated releases)"
    earned = int(points * min(months, 12) / 12 + 0.5)  # round half up
    return earned, f"consistency {earned}/{points} ({months} of 12 months with a release)"


def base_score(evidence: dict, is_label: bool, scoring: dict | None = None) -> tuple[int, str]:
    """(score from 1 to 10, a one-line breakdown of where the points came from)."""
    scoring = scoring or DEFAULT_SCORING
    parts: list[tuple[int, str]] = []
    has_store = bool(evidence.get("storefront_links"))

    if is_label:
        weights = scoring["label"]
        parts.append(_consistency(evidence, weights["consistency"]["points"]))
        catalogue = weights["catalogue_size"]
        count = evidence.get("release_count")
        earned = _banded(count, catalogue["bands"])
        parts.append((earned, f"catalogue {earned}/{catalogue['points']} ({_short(count)} releases)"))
        bonus = weights["storefront_bonus"]["points"] if has_store else 0
        parts.append((bonus, f"storefront bonus +{bonus}"))
    else:
        weights = scoring["creator"]
        steady = weights["steady_views"]
        views = evidence.get("steady_views")
        earned = _banded(views, steady["bands"])
        detail = f"steady views {earned}/{steady['points']} ({_short(views)}"
        if evidence.get("top_video_share") is not None:
            detail += f"; top release has {evidence['top_video_share']:.0%} of views"
        parts.append((earned, detail + ")"))
        parts.append(_consistency(evidence, weights["consistency"]["points"]))
        store = weights["storefront"]["points"]
        parts.append((store if has_store else 0, f"storefront {store if has_store else 0}/{store}"))
        audience = weights["audience"]
        followers = evidence.get("audience_size")
        earned = audience["points"] if followers is not None and followers >= audience["min_followers"] else 0
        parts.append((earned, f"audience {earned}/{audience['points']} ({_short(followers)})"))

    total = sum(points for points, _ in parts)
    return max(1, min(10, total)), "; ".join(text for _, text in parts)


def limited_data(evidence: dict, is_label: bool) -> str:
    """Why the score rests on thin data ("" if it doesn't). The score is kept as is."""
    missing = []
    if is_label:
        if evidence.get("release_count") is None:
            missing.append("no catalogue size")
        if evidence.get("active_months_last_12") is None:
            missing.append("no dated releases")
    else:
        if evidence.get("steady_views") is None:
            missing.append("no view figures")
        if evidence.get("active_months_last_12") is None:
            missing.append("no dated releases")
    return ", ".join(missing)


def final_score(base: int, adjustment: int) -> int:
    return max(1, min(10, int(base) + max(-MAX_ADJUSTMENT, min(MAX_ADJUSTMENT, int(adjustment)))))
