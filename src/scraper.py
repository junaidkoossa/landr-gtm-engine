from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

STOREFRONT_DOMAINS = ("gumroad.com", "bandcamp.com")
REQUEST_TIMEOUT = 15
MAX_SOURCE_TEXT_LENGTH = 12_000


def scrape_creator(creator: dict[str, str], signal_keywords: list[str]) -> dict[str, object]:
    """Fetch a creator site and return text, storefront links, and keyword signals."""
    url = creator["website_url"]
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError(f"Creator website must be an absolute HTTP(S) URL: {url}")

    response = requests.get(
        url,
        headers={"User-Agent": "creator-gtm-signal-engine/1.0"},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    for element in soup(("script", "style", "noscript")):
        element.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    page_text = soup.get_text(" ", strip=True)
    storefront_links = []
    for anchor in soup.select("a[href]"):
        link = urljoin(url, anchor["href"])
        hostname = (urlparse(link).hostname or "").lower()
        if any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in STOREFRONT_DOMAINS
        ):
            if link not in storefront_links:
                storefront_links.append(link)

    searchable_text = f"{page_text} {' '.join(storefront_links)}".casefold()
    release_signals = [
        keyword for keyword in signal_keywords if keyword.casefold() in searchable_text
    ]

    return {
        "page_title": title,
        "source_text": page_text[:MAX_SOURCE_TEXT_LENGTH],
        "storefront_links": storefront_links,
        "release_signals": release_signals,
    }