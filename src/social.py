"""Instagram and TikTok handles from a creator's input row."""
import re
from urllib.parse import unquote, urlparse

INSTAGRAM_HOSTS = ("instagram.com",)
TIKTOK_HOSTS = ("tiktok.com",)
HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9._]{1,30}$")
# Instagram paths that are not profiles.
INSTAGRAM_RESERVED = {"p", "reel", "reels", "stories", "explore", "tv", "accounts"}


def on_host(url: str, hosts: tuple[str, ...]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith(f".{h}") for h in hosts)


def _handle(value: str, hosts: tuple[str, ...]) -> str | None:
    """A bare handle from "@name", "name" or a profile URL; None if it isn't one."""
    value = (value or "").strip()
    if not value:
        return None
    as_url = value if "://" in value else f"https://{value}"
    if "/" in value or on_host(as_url, hosts):
        # A URL: it must be on this platform and point at a profile.
        if not on_host(as_url, hosts):
            return None
        parts = [unquote(part) for part in urlparse(as_url).path.split("/") if part]
        if not parts or parts[0].lower() in INSTAGRAM_RESERVED:
            return None
        value = parts[0]
    value = value.lstrip("@")
    return value.lower() if HANDLE_PATTERN.match(value) else None


def instagram_handle(creator: dict[str, str]) -> str | None:
    website = creator.get("website_url") or ""
    return _handle(creator.get("instagram", ""), INSTAGRAM_HOSTS) or (
        _handle(website, INSTAGRAM_HOSTS) if on_host(website, INSTAGRAM_HOSTS) else None
    )


def tiktok_handle(creator: dict[str, str]) -> str | None:
    website = creator.get("website_url") or ""
    return _handle(creator.get("tiktok", ""), TIKTOK_HOSTS) or (
        _handle(website, TIKTOK_HOSTS) if on_host(website, TIKTOK_HOSTS) else None
    )


def tiktok_url(handle: str) -> str:
    return f"https://www.tiktok.com/@{handle}"
