"""Send scored creators into HubSpot as contacts, through the CRM API.

HubSpot has no inbound webhook that can create records: its "When a webhook is
received" workflow trigger needs a Professional or Enterprise plan and only updates
records that already exist. So the pipeline uses the CRM API instead, which works on
the free plan with a private app access token (HUBSPOT_ACCESS_TOKEN).

Contacts are created or updated by email (batch upsert, at most 100 per request; the
API allows 100 requests per 10 seconds on Free and Starter). Creators without an email
are held back with status "no_email" and go out on a later run, or with
--resend-unsent, once an email is added to the input CSV.

The custom properties (fit score, hook and so on) must exist in HubSpot first:
    python -m src.hubspot --create-properties
"""
import argparse
import os
import time

import requests
from dotenv import load_dotenv

API_URL = "https://api.hubapi.com"
TIMEOUT = 30
BATCH_LIMIT = 100
REQUESTS_PER_SECOND = 9
PROPERTY_GROUP = "landr_gtm"

# HubSpot property name -> (label, type, fieldType, output CSV column)
CUSTOM_PROPERTIES = {
    "landr_creator_name": ("LANDR creator name", "string", "text", "name"),
    "landr_target_type": ("LANDR target type", "string", "text", "target_type"),
    "landr_fit_score": ("LANDR fit score", "number", "number", "score"),
    "landr_bottleneck": ("LANDR bottleneck", "string", "textarea", "bottleneck"),
    "landr_outreach_hook": ("LANDR outreach hook", "string", "textarea", "custom_hook"),
    "landr_data_sources": ("LANDR data sources", "string", "text", "data_sources"),
    "landr_audience_size": ("LANDR audience size", "number", "number", "audience_size"),
    "landr_releases_last_12_months": ("LANDR releases in last 12 months", "number", "number", "releases_last_12_months"),
    "landr_latest_release_date": ("LANDR latest release date", "string", "text", "latest_release_date"),
    "landr_scored_at": ("LANDR scored at", "string", "text", "enriched_at"),
}


def access_token() -> str | None:
    return os.getenv("HUBSPOT_ACCESS_TOKEN") or None


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def contact_properties(row: dict[str, object]) -> dict[str, str]:
    properties = {"email": str(row.get("email") or "").strip(), "website": str(row.get("website_url") or "")}
    for name, (_, _, _, column) in CUSTOM_PROPERTIES.items():
        value = row.get(column)
        if isinstance(value, list):
            value = " | ".join(value)
        if value not in (None, ""):
            properties[name] = str(value)
    return properties


def _post_upsert(body: dict, token: str) -> requests.Response:
    for attempt in range(2):
        response = requests.post(
            f"{API_URL}/crm/v3/objects/contacts/batch/upsert",
            json=body,
            headers=_headers(token),
            timeout=TIMEOUT,
        )
        if response.status_code != 429 or attempt:
            return response
        time.sleep(float(response.headers.get("Retry-After", 10)))
    return response


def upsert_contacts(
    rows: list[dict[str, object]], token: str
) -> list[tuple[dict[str, object], Exception | None]]:
    """Create or update a contact per row (matched by email); return each row with its error.

    Rows sharing an email go out once (HubSpot rejects a batch with a repeated id), and a
    207 partial success marks only the contacts HubSpot returned as synced."""
    by_email: dict[str, list[dict[str, object]]] = {}
    for row in rows:
        by_email.setdefault(str(row.get("email") or "").strip().lower(), []).append(row)
    emails = list(by_email)

    outcomes: list[tuple[dict[str, object], Exception | None]] = []
    interval = 1 / REQUESTS_PER_SECOND
    for start in range(0, len(emails), BATCH_LIMIT):
        chunk = emails[start:start + BATCH_LIMIT]
        body = {
            "inputs": [
                {
                    "idProperty": "email",
                    "id": email,
                    "properties": {**contact_properties(by_email[email][-1]), "email": email},
                }
                for email in chunk
            ]
        }
        started = time.monotonic()
        try:
            response = _post_upsert(body, token)
            response.raise_for_status()
            if response.status_code == 207:
                data = response.json()
                done = {
                    str(result.get("properties", {}).get("email") or "").lower()
                    for result in data.get("results", [])
                }
                messages = "; ".join(error.get("message", "") for error in data.get("errors", []))
                failed = ValueError(f"HubSpot rejected this contact: {messages or 'no reason given'}")
            else:
                done, failed = set(chunk), None
            for email in chunk:
                error = None if email in done else failed
                outcomes.extend((row, error) for row in by_email[email])
        except (requests.RequestException, ValueError) as error:
            for email in chunk:
                outcomes.extend((row, error) for row in by_email[email])
        remaining = interval - (time.monotonic() - started)
        if remaining > 0:
            time.sleep(remaining)
    return outcomes


def create_properties(token: str) -> list[str]:
    """Create the property group and custom contact properties; existing ones are left alone."""
    created = []
    group = requests.post(
        f"{API_URL}/crm/v3/properties/contacts/groups",
        json={"name": PROPERTY_GROUP, "label": "LANDR GTM engine"},
        headers=_headers(token),
        timeout=TIMEOUT,
    )
    if group.status_code != 409:
        group.raise_for_status()
    for name, (label, kind, field_type, _) in CUSTOM_PROPERTIES.items():
        response = requests.post(
            f"{API_URL}/crm/v3/properties/contacts",
            json={"name": name, "label": label, "type": kind, "fieldType": field_type, "groupName": PROPERTY_GROUP},
            headers=_headers(token),
            timeout=TIMEOUT,
        )
        if response.status_code == 409:
            continue  # already exists
        response.raise_for_status()
        created.append(name)
    return created


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="HubSpot setup for the LANDR GTM engine.")
    parser.add_argument("--create-properties", action="store_true", help="create the custom contact properties")
    args = parser.parse_args()
    token = access_token()
    if not token:
        parser.exit(1, "error: set HUBSPOT_ACCESS_TOKEN first\n")
    if args.create_properties:
        created = create_properties(token)
        print(f"Created {len(created)} properties: {', '.join(created) or 'all already existed'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
