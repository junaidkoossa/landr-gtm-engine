import re
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Links to these sites (or their subdomains) count as a storefront. Shopify stores
# usually run on their own domain, so only *.myshopify.com links are caught.
STOREFRONT_DOMAINS = (
    "gumroad.com", "bandcamp.com", "beatstars.com", "airbit.com", "splice.com",
    "payhip.com", "sellfy.com", "myshopify.com", "patreon.com", "beatport.com",
    "adsrsounds.com", "pluginboutique.com",
)
REQUEST_TIMEOUT = 15
MAX_SOURCE_TEXT_LENGTH = 12_000
USER_AGENT = "creator-gtm-signal-engine/1.0"
URL_PATTERN = re.compile(r"https?://[^\s<>\"')\]]+")


def require_http_url(url: str) -> None:
    parsed_url = urlparse(url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ValueError(f"Creator website must be an absolute HTTP(S) URL: {url}")


def find_urls(text: str) -> list[str]:
    return [match.rstrip(".,;:!?") for match in URL_PATTERN.findall(text or "")]


def find_storefront_links(links: list[str]) -> list[str]:
    storefront_links = []
    for link in links:
        hostname = (urlparse(link).hostname or "").lower()
        if any(
            hostname == domain or hostname.endswith(f".{domain}")
            for domain in STOREFRONT_DOMAINS
        ):
            if link not in storefront_links:
                storefront_links.append(link)
    return storefront_links


def find_release_signals(
    text: str, storefront_links: list[str], signal_keywords: list[str]
) -> list[str]:
    searchable_text = f"{text} {' '.join(storefront_links)}".casefold()
    return [keyword for keyword in signal_keywords if keyword.casefold() in searchable_text]


def scrape_website(url: str) -> dict[str, object]:
    """Fetch one web page and return its title, readable text and outbound links."""
    require_http_url(url)
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    for element in soup(("script", "style", "noscript")):
        element.decompose()

    return {
        "source": "website",
        "title": soup.title.get_text(" ", strip=True) if soup.title else "",
        "text": soup.get_text(" ", strip=True),
        "links": [urljoin(url, anchor["href"]) for anchor in soup.select("a[href]")],
        "audience_size": None,
        "release_count": None,
        "release_dates": [],
    }
