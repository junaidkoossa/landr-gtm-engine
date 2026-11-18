# LANDR Audio Creator GTM Engine

An extensible prospecting pipeline for finding and prioritizing independent music creators,
boutique sample labels, and audio-tool partners for LANDR Audio.

The pipeline keeps vertical-specific qualification criteria in
`config/vertical_rules.json`, separate from data collection and scoring logic.
Switching the rules file lets the same workflow evaluate a different customer vertical.

## Input file

`data/input_creators.csv` has one row per creator:

| Column | Required | What goes in it | How it's used |
| --- | --- | --- | --- |
| `name` | yes | Creator or label name | Matching, MusicBrainz/Discogs search for labels |
| `target_type` | yes | `creator` or `label` | Picks the sources and the scoring weights |
| `website_url` | one link needed | Their own website | Scraped for text, links and storefront links |
| `youtube_url` | one link needed | YouTube channel URL | YouTube Data API |
| `storefront_url` | one link needed | Where they sell (Bandcamp, Beatstars, Gumroad, Shopify...) | Always counts as a storefront; the page is scraped when it can be |
| `instagram` | one link needed | `@handle` or profile URL | Instagram Business Discovery |
| `tiktok` | one link needed | `@handle` or profile URL | Bright Data |
| `email` | no | Contact email | HubSpot only |

Every link a row has is used, and the results are combined. A source that fails is
noted and skipped as long as another source worked. Creators are matched to earlier
runs by their first link in the order above, or by name. Older files with a single
`website_url` column still work: a YouTube, Instagram or TikTok link there is moved to
its own column when the file is read.

## Where the data comes from

This project uses free data sources:

| Input | Source | What it adds | Access |
| --- | --- | --- | --- |
| `youtube_url` (`youtube.com/@handle`, `/channel/…`, `/user/…`, `/c/…`) | [YouTube Data API v3](https://developers.google.com/youtube/v3) | Channel description, subscriber count, the last year of uploads (up to 500) with dates, titles, descriptions and views for release uploads, and storefront links found in them | Free API key (`YOUTUBE_API_KEY`). 10,000 quota units a day; up to about 21 units per channel (so roughly 500 channels a day), and `/c/` URLs also use one of the 100 searches allowed a day |
| `instagram` | [Instagram Business Discovery](https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery) | Followers, post count, bio and the last year of posts (up to 200) with likes, comments and views | Free. Needs your own Instagram professional account (see setup). About 200 calls an hour, 1 to 4 per creator. Business and Creator accounts only; personal and age-gated accounts return nothing |
| `tiktok` | [Bright Data](https://docs.brightdata.com/datasets/scrapers/tiktok/introduction) Web Scraper API | Followers, likes, video count and bio for every profile; the last 30 posts with plays for profiles with 10,000+ followers | Paid per record, with a monthly free allowance (`BRIGHTDATA_API_TOKEN`). 1 record per profile plus about 30 per profile whose posts are collected. Scraped, so TikTok's terms apply |
| `target_type` = `label` | [MusicBrainz](https://musicbrainz.org/doc/MusicBrainz_API) | Label type, country, start year, linked sites (Bandcamp, socials), release count and dated releases | Free, no key. About 1 request a second |
| `target_type` = `label` | [Discogs](https://www.discogs.com/developers) | Label profile, linked sites, release count and release years, plus exact dates for the newest 20 releases from this year or last | Free personal token (`DISCOGS_TOKEN`); skipped if not set. 60 requests a minute |
| `website_url` (and `storefront_url`) | The page itself | Page title, readable text and outbound links | None |
| Linktree, Facebook, X/Twitter, Threads, Snapchat as `website_url` | Not read | These pages show a login wall or need JavaScript, so the link is skipped. A row with no other link goes to the failures file | — |

**Social profiles.** Instagram and TikTok are combined with the creator's other
sources. If a token isn't set yet, creators with no other source are skipped for now
without counting as a failure.
A personal Instagram account is noted and skipped; if it was the creator's only
source, the creator goes to the failures file. TikTok profiles are collected as one
Bright Data job per run: it starts first, everyone else is collected while it runs,
and creators with a TikTok are finished once it's ready. Instagram allows about 200
calls an hour, so creators with an Instagram are collected after everyone else (and
before the TikTok group), so a big Instagram list doesn't hold up the rest.

**TikTok 12-month estimate.** Only the last 30 posts are collected, which for a daily
poster covers about a month. When the 30 posts don't reach back a year, the
pipeline estimates the year from the posting rate: release posts per day × 365 (or
the account's age, if younger). Steady views are scaled up the same way, and months
with a release assume releases are spread evenly. `estimated_sources` in the output
says which figures were estimated, and Claude is told too.

**Storefronts.** Links to Gumroad, Bandcamp, Beatstars, Airbit, Splice, Payhip,
Sellfy, Shopify (`*.myshopify.com`), Patreon, Beatport, ADSR and Plugin Boutique
count as a storefront (`STOREFRONT_DOMAINS` in `src/scraper.py`). Shopify stores on
their own domain aren't recognised.

**YouTube releases.** Only uploads whose titles look like releases count towards
release figures. The words are in `RELEASE_TITLE_WORDS` in `src/youtube.py` (pack,
sample, loop, kit, beat, type beat, instrumental, preset, plugin, VST, MIDI, stems,
one shot, EP, LP, album, single, mixtape, remix, out now, free download and similar).
Other uploads are still shown to Claude, labelled as not counted. The same words pick
out release posts on Instagram (captions) and TikTok (descriptions). "Bootleg" is not a
release word, since bootlegs are usually unofficial edits of other people's music.

**Label matching.** MusicBrainz and Discogs are searched by name, and a result is
only used if it is one of these:

- **Exact**: the same letters and numbers, ignoring case, spaces and punctuation
  ("Drumcode" = "Drum Code", "Monstercat" = "Monster Cat").
- **Variant**: the same words once plurals are made singular and filler words such as
  "Records", "Music", "Sounds", "Ltd" and ".fm" are set aside ("Ninja Tune" = "Ninja
  Tunes", "Hospital" = "Hospital Records", "Cymatics" = "Cymatics.fm").

Anything else is rejected, however similar it looks: "Spice" never matches "Splice",
"Loopmasters" never matches "Bassmasters", "Glass" never matches "Glas". An exact
result is always preferred over a variant. Variant matches are used, but marked
`needs_review` in the output and listed in the review file for a person to confirm.
The filler words are `LABEL_FILLER_WORDS` in `src/matching.py`.

**Label release history.** MusicBrainz returns a label's releases in no date order,
and Discogs is asked for newest year first but doesn't always honour it. So the
pipeline pages through the catalogue (100 releases a page, up to `MAX_RELEASE_PAGES`
= 50 pages, so 5,000 releases) and sorts everything newest to oldest itself before
working out the latest release and the last 12 months. If a catalogue is bigger than
that, the evidence says the latest dates may be incomplete. Each page takes about a
second because of the services' rate limits.

All sources are merged into one evidence record per creator: text, storefront links,
keyword signals, audience size, release count, releases in the last 12 months, the
latest release date, and the view figures below.

## Scoring

The score is worked out in code from fixed weights (`scoring` in
`config/vertical_rules.json`, logic in `src/scoring.py`). Claude then reads the
evidence and may move the score by at most 1 point, giving a reason
(`score_adjustment`, `adjustment_reason`), and writes the bottleneck and outreach hook.
(Scoring can also run on Gemini or on a free local model through Ollama, and the hook
can be written separately by Claude for top-scored creators only; see "Ollama" below.)
`score_breakdown` shows where each point came from.

The aim is to favour creators who release constantly and are consistently watched.

**Steady views** = median views per release × number of releases with views, over the
last 12 months, added up per platform. Releases from the last 14 days count as
releases but not in view figures, as they haven't had time to collect views. The
median means one breakout video barely moves it:

| Creator | Releases | Total views | Median per release | Steady views | Base score |
| --- | --- | --- | --- | --- | --- |
| A | 300 | ~1M | ~3.3K | ~1M | 6 |
| B | 150 | ~5M | ~33K | ~5M | 7 |
| C | 150 | ~5M, 4.9M from one video | ~670 | ~100K | 5 |

(All three: a release every month and 20K subscribers, no storefront.)
`top_video_share` (the biggest release's share of views) is shown to Claude too.

**Consistency** = months in the last 12 with at least one release.

| Creators (out of 10) | Points |
| --- | --- |
| Steady views: 50K / 250K / 1M / 5M+ | 1 / 2 / 3 / 4 |
| Consistency: share of the last 12 months with a release | up to 3 |
| Storefront link found | 2 |
| 10,000+ followers or subscribers | 1 |

| Labels (out of 10) | Points |
| --- | --- |
| Consistency: share of the last 12 months with a release | up to 5 |
| Catalogue size: 10 / 50 / 100 / 250 / 500+ releases | 1 / 2 / 3 / 4 / 5 |
| Storefront link found | +1 bonus (capped at 10; no store is never a penalty) |

Scores run from 1 to 10. A figure no source had scores 0 for that part and shows as
"unknown" in the breakdown. Those creators keep their score but get a `limited_data`
note (for example "no view figures, no dated releases") so they can be looked at
rather than buried.

Rows scored before weighted scoring keep their old score until they are re-scored
(after 90 days, or when you run `python -m src.main --rescore-all`, which scores every
creator in the input again however recently it was scored). The weights and bands can be changed in the rules file;
each band list needs one number per point.

**For production use**, free databases leave gaps: many beatmakers and sample-pack
sellers aren't in MusicBrainz or Discogs, and YouTube only covers YouTube. Connecting
music databases and analytics tools such as [Soundcharts](https://soundcharts.com/en/pricing/developers)
or [Chartmetric](https://chartmetric.com/plans/api) would give consistent cross-platform
data (Spotify, YouTube, TikTok, Instagram and more) for every artist, which means
better scraping and higher quality leads. A new source only has to return the same
evidence fields (see `src/collect.py`), so it can be added alongside the free ones.

## Pipeline

1. Load creator rows from `data/input_creators.csv` (see "Input file" below).
   Rows without a name, target type or any link are reported and skipped. The email is
   never sent to Claude.
2. Skip creators that need no work:
   - already in the output CSV and scored within the last 90 days
     (`RESCORE_AFTER_DAYS`). Creators match on website URL (ignoring `http`/`https`,
     `www.`, case, query strings and a trailing slash) or on name (ignoring case,
     spaces and punctuation);
   - repeated in the input, by URL or name;
   - failed 3 runs in a row (`MAX_ATTEMPTS`).
3. Collect evidence for the rest from the sources above.
4. Work out the weighted base score, then have Claude review it (±1 at most) and
   write a bottleneck and an outreach hook.
5. Update the output CSV. A re-scored creator's row is replaced, so there is one row
   per creator; rows are never removed.
6. Send creators scoring at least `activation_min_score` (in the rules file) to
   `ACTIVATION_WEBHOOK_URL` and to HubSpot, and record the result in each row.

If a creator's data can't be collected or Claude's reply can't be used, the pipeline
logs a warning, records it in the failures file and carries on. Problems with the
account or the service rather than the creator (a rejected API key, no credit left, a
rate limit or outage that outlasts the retries, Gemini's daily limit, Ollama not running) stop the run with
a message instead, since every creator would fail the same way. These are not counted
as failed attempts, and creators already scored are kept. `--retry-failed` empties the
failures file at the start of a run, so creators that failed before are tried again.

### Files the pipeline keeps

All sit next to the output CSV (default `data/`):

| File | What's in it |
| --- | --- |
| `enriched_creators.csv` | One row per creator, with `webhook_status` (`sent`, `failed`, `below_min_score`, `no_webhook`), `hubspot_status` (`synced`, `failed`, `below_min_score`, `no_email`, `not_configured`), when each was sent, and `needs_review` with a `review_reason` |
| `enriched_creators.unsent.csv` | Every creator that reached neither the webhook nor HubSpot, for review |
| `enriched_creators.failures.csv` | Creators that failed, with the attempt count and last error. After 3 attempts the status is `skipped`; delete the row, or run with `--retry-failed`, to try again |
| `enriched_creators.review_matches.csv` | Label-name variant matches to check |
| `enriched_creators.pending_batches.json` | Batches submitted but not yet collected (only while a run is waiting) |

### Webhook (Clay)

**Status: not active.** Clay webhooks need a paid Growth or Enterprise plan, and we
aren't paying for one for now. The code is ready: leave `ACTIVATION_WEBHOOK_URL`
blank until there is a plan, and rows are marked `no_webhook`; once a URL is set,
`--resend-unsent` sends them.

Creators that failed to send are never retried automatically. After reviewing the
unsent file, retry them in a separate run, which resends rows marked `failed` or
`no_webhook` that meet the minimum score, and does no scoring:

```bash
python -m src.main --resend-unsent
```

A creator already sent is not sent again when it is re-scored.

`ACTIVATION_WEBHOOK_FORMAT` sets how creators are sent:

- `clay` (default): one creator per POST, because Clay turns each POST into one table
  row (a JSON array would become a single row). Clay's limits: 100 KB per POST, 10
  requests a second per workspace (the pipeline sends 8 a second and waits and retries
  on a 429), and 50,000 records per webhook source. See
  [Clay's webhook guide](https://github.com/clay-run/public-docs/blob/main/docs/webhook-integration-guide.md).
- `batched`: `{"creators": [...]}` with up to 100 creators per POST, for tools that take
  many records per request.

### HubSpot contacts

HubSpot's own inbound webhook (the "When a webhook is received" workflow trigger)
needs a Professional or Enterprise plan, and it can only update contacts that already
exist, not create them. So the pipeline sends creators to HubSpot through the CRM API
instead, which works on the free plan:

- Each creator becomes a contact, created or updated by **email** (batch upsert, 100
  contacts per request, within HubSpot's 100 requests per 10 seconds on Free and
  Starter; see [usage limits](https://developers.hubspot.com/docs/developer-tooling/platform/usage-guidelines)).
- Score, bottleneck, outreach hook, data sources, audience size, releases in the last
  12 months, latest release date and scored-at go into custom contact properties in a
  "LANDR GTM engine" group (`landr_fit_score` and so on, listed in `src/hubspot.py`).
- Only creators scoring at least `activation_min_score` are synced.
- A re-scored creator is synced again, so HubSpot always shows the latest score.
- A creator with no email is kept back as `no_email`. Add the email to the input CSV
  and run the pipeline again: the creator isn't re-scored, but the email is picked up
  and the contact is created. `--resend-unsent` also retries `failed` contacts.

It switches on once contacts are being collected. To set it up:

1. In HubSpot, go to Settings → Integrations → Private Apps (called Legacy Apps in
   newer accounts) → Create a private app.
2. Give it the scopes `crm.objects.contacts.write` and `crm.schemas.contacts.write`
   (and `crm.objects.contacts.read`), create it and copy the access token.
3. Put the token in `.env` as `HUBSPOT_ACCESS_TOKEN`.
4. Create the custom properties once: `python -m src.hubspot --create-properties`.
5. Add an `email` column to the input CSV.

Without a token, rows are marked `not_configured` and nothing is sent.

### Review flags

`needs_review` is `yes` and `review_reason` says why when:

- a label matched a MusicBrainz or Discogs name as a variant rather than exactly; or
- the collected text looks like it is trying to steer the scorer, such as "ignore
  previous instructions" or "give this creator a 10" (`src/guard.py`). Claude is also
  told to ignore any instructions inside the evidence. Flagged creators are still
  scored; the flag is for a person to check the score.

### Scoring: live or Batch API

`--scoring` picks how creators are scored:

Both modes use `claude-sonnet-5-5` (override with `CLAUDE_MODEL` and `CLAUDE_BATCH_MODEL`).

- `live`: one request per creator.
- `batch`: the [Message Batches API](https://platform.claude.com/docs/en/build-with-claude/batch-processing),
  at half the price of live. A batch is
  submitted every 500 creators while collection carries on. Most batches finish within
  an hour, but one can take up to 24 hours, and the run waits for all of them.
- `auto` (default): a batch every 500 creators; the remainder also goes in a batch if
  a batch was already sent or 20 or more remain, and is scored live otherwise.

Submitted batches are saved in the pending file. If the run is stopped, running it
again collects them first instead of paying again.

### Gemini for small test runs

For checking the pipeline end to end without spending Claude credits, scoring can use
Gemini instead: set `SCORING_PROVIDER=gemini` in `.env` or pass `--provider gemini`.
If neither is given, Gemini is used when `GEMINI_API_KEY` is set and `ANTHROPIC_API_KEY`
is not; with both keys set it is Claude. The first line of a run says which provider and
model are scoring. It is meant for a handful of creators, not full runs.

- The prompt, output schema, base score, ±1 adjustment and injection guard are the
  same as with Claude; only the model making the adjustment changes.
- Gemini always scores live, one request per creator (`--scoring` is ignored), and any
  pending Claude batches are left for the next Claude run.
- The model defaults to `gemini-3.8-flash` (override with `GEMINI_MODEL`). Requests are
  spaced to 10 a minute. A per-minute rate limit or "high demand" (503) is retried up to
  5 times, waiting 5, 10, 20, 40 and 60 seconds (or as long as the API asks). If the
  model is still busy, `gemini-3.5-flash-lite` is tried the same way (change it with
  `GEMINI_FALLBACK_MODEL`, or set it to `none`). If that is busy too, or the daily
  free-tier limit is used up, the run stops with a message. Free-tier limits differ by
  model and project and are shown in [Google AI Studio](https://aistudio.google.com/).
- The `scored_by` column records which model scored each row.

### Ollama: free local scoring, Claude hooks for top leads

Scoring is high-volume, mechanical work (a ±1 adjustment, a reason and a bottleneck
label as JSON), so it can run on a model on your own computer through
[Ollama](https://ollama.com), free and offline. The outreach hook is the sentence a
creator actually reads, so with Ollama it is written by Claude, and only for creators
worth contacting.

Set `SCORING_PROVIDER=ollama` in `.env` (or pass `--provider ollama`), with the Ollama app
open. The first lines of a run say which model is scoring and who writes the hooks:

```text
INFO: Scoring with Ollama (llama3.1:8b at http://localhost:11434), live, one creator at a time
INFO: Outreach hooks: Claude (claude-sonnet-5-5), only for creators scoring 7 or more
```

- `OLLAMA_HOST` (default `http://localhost:11434`) and `OLLAMA_MODEL` (default
  `llama3.1:8b`; any model from `ollama list`). No API key.
- The prompt, base score, ±1 adjustment and injection guard are the same as with Claude.
  The output schema is sent as Ollama's JSON format, so the model is held to it, and the
  reply goes through the same checks. A small model still sometimes returns broken JSON
  or an out-of-range adjustment; that reply is asked for again (up to 3 tries in all),
  then the creator counts as a normal failed attempt.
- If Ollama isn't running, doesn't have the model (`ollama pull llama3.1:8b`), or keeps
  timing out, the run stops with a message before or instead of failing creators, the
  same as an account error with Claude. Creators already scored are kept.
- Scoring is live, one creator at a time (`--scoring` is ignored). Each request uses a
  16K-token context (`OLLAMA_NUM_CTX` in `src/settings.py`) so a full page of evidence
  isn't cut off; lower it if your computer runs short of memory.

Who writes the hook is set by `HOOK_PROVIDER`:

| `HOOK_PROVIDER` | Hook written by | For | Default for |
| --- | --- | --- | --- |
| `same` | the scoring model, in the same reply | every creator | Claude, Gemini |
| `claude` | Claude (`CLAUDE_MODEL`), in a separate call after scoring | creators scoring `HOOK_MIN_SCORE` (default 7) or more | Ollama |
| `none` | nobody | no one | |

With `claude`, the scoring model isn't asked for a hook at all, Claude sees the same
evidence plus the score and bottleneck, and creators below the threshold get an empty
`custom_hook`. It needs `ANTHROPIC_API_KEY`. If Claude can't write one creator's hook,
the score is still saved with an empty hook and a warning; a Claude account problem (no
credit, bad key) stops the run as usual. The `hook_by` column records which model wrote
each hook.

## Setup

On a Mac, use `python3` (macOS has no `python` or `pip` command until a virtual
environment is active):

```bash
python3 -m venv .venv          # once, in the repository folder
source .venv/bin/activate      # each new terminal; then `python` and `pip` work
pip install -r requirements.txt
cp .env.example .env
```

The code needs Python 3.10 or newer (`python3 --version`). If `python3` itself isn't
found, or is older, install a current Python from
[python.org](https://www.python.org/downloads/macos/) or with `brew install python`.

Set these in `.env`:

- `ANTHROPIC_API_KEY` (required unless scoring with Gemini, or with Ollama and
  `HOOK_PROVIDER=none`).
- `YOUTUBE_API_KEY` if the input has YouTube channels. In Google Cloud Console,
  enable "YouTube Data API v3" for a project and create an API key.
- `DISCOGS_TOKEN` to add Discogs for labels: Discogs settings → Developers →
  Generate new token.
- `DATA_SOURCE_CONTACT`: a URL or email MusicBrainz and Discogs can contact you at.
  MusicBrainz asks for this in every request's User-Agent.
- `CLAUDE_MODEL` / `CLAUDE_BATCH_MODEL` to override the live and batch scoring models.
- `SCORING_PROVIDER` (`claude` or `gemini`; blank picks by which key is set), `GEMINI_API_KEY` and
  optionally `GEMINI_MODEL` for Gemini test runs. Create the key at
  [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
- `SCORING_PROVIDER=ollama`, and optionally `OLLAMA_HOST`, `OLLAMA_MODEL`,
  `HOOK_PROVIDER` and `HOOK_MIN_SCORE`, to score on this computer (see "Ollama" above).
- `INSTAGRAM_ACCESS_TOKEN` and `INSTAGRAM_BUSINESS_ID` for Instagram:
  1. Switch an Instagram account you own to a Business or Creator account and link it
     to a Facebook Page.
  2. At [developers.facebook.com](https://developers.facebook.com/apps) create an app
     (type Business) and add the Instagram product ("API setup with Facebook login").
     Development mode is enough while only you use it.
  3. Get a token that doesn't expire, from a Business Manager system user:
     - In [Meta Business Suite](https://business.facebook.com/settings) → Business
       settings, make sure the Facebook Page and the app belong to your business.
     - Users → System users → Add an admin system user.
     - Assign assets: give it the Page (full control) and the app.
     - Generate new token: pick the app, set expiry to "Never", and tick
       `instagram_basic`, `instagram_manage_insights`, `pages_read_engagement`,
       `pages_show_list` and `business_management`.
     A personal user token from the Graph API Explorer also works, but it lasts at
     most about 60 days; when it expires the run stops with an error saying so.
  4. Call `GET https://graph.facebook.com/v25.0/me/accounts?fields=instagram_business_account`
     with the token; the `instagram_business_account.id` is `INSTAGRAM_BUSINESS_ID`.
- `BRIGHTDATA_API_TOKEN` for TikTok: create a Bright Data account and an API key under
  Account settings → API keys. Watch the record count in the Bright Data dashboard;
  `TIKTOK_POSTS_MIN_FOLLOWERS` and `TIKTOK_POSTS_PER_PROFILE` in `src/settings.py`
  control how many records a run uses.
- `ACTIVATION_WEBHOOK_URL` and `ACTIVATION_WEBHOOK_FORMAT` only when a Clay webhook
  exists (needs a paid Clay plan; not in use for now).
- `HUBSPOT_ACCESS_TOKEN` to sync contacts into HubSpot (see "HubSpot contacts").

Run the pipeline from the repository root:

```bash
python -m src.main
python -m src.main --scoring batch
python -m src.main --resend-unsent
python -m src.main --rescore-all   # score everyone again, e.g. after changing the weights
python -m src.main --provider gemini   # small test run scored by Gemini
python -m src.main --provider ollama   # score on this computer; Claude writes top hooks
python -m src.main --retry-failed      # try creators that failed earlier runs again
```

Input, rules, and output paths can be overridden with `--input`, `--rules`,
and `--output`. The sample input is a starter dataset, not the planned
production list.

Run settings shared by every vertical (models, batch size, 90-day re-score, 3
attempts, pages read) are in `src/settings.py`.

## Development

Run the test suite with:

```bash
python -m unittest discover -s tests
```

For a guide to reading the project history and commit labels, see
[CONTRIBUTING.md](CONTRIBUTING.md).
