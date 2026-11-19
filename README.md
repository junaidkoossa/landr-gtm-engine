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
| `lead_source` | no | `input`, or `discovered:<search>` for rows added by `src.discover` | Tells inputted and discovered leads apart (blank = `input`) |
| `product_fit` | no | LANDR products a discovered lead points to, or `Partnership` for a major label | Filled by `src.discover`; a hint for review (the scored `best_product` is what counts) |
| `self_released` | no | `yes` when discovery found them on Discogs' "Not On Label" | Counts as the self-released signal in scoring |

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

Every lead gets a score out of 10 for each LANDR product, worked out in code from the
weights under `products` in `config/vertical_rules.json` (logic in `src/scoring.py`):

| Product | What LANDR sells them | Scored for |
| --- | --- | --- |
| Mastering | AI mastering plugin and subscriptions | Creators |
| Distribution | Release to digital streaming platforms | Creators and labels |
| Samples supplier | Supply their packs to LANDR Samples, building a catalogue to rival Splice | Creators and labels |
| Plugins | LANDR's own plugins, for musicians, producers and mixing engineers | Creators |

The lead's `score` is its best product's score (`best_product`), and all four are kept
(`score_mastering` and so on, with every point in `product_breakdowns`). The model then
reads the evidence and may move the best score by at most 1 point, giving a reason, and
names the bottleneck. (Scoring can also run on Gemini or Ollama; see below.) Claude
writes the outreach hook later, for the best product, once the score is final and only
for leads scoring 7 or more.

| Mastering | Points |
| --- | --- |
| Releases in the last 12 months: 6 / 12 / 24+ | 1 / 2 / 3 |
| Latest release within 90 / 30 days | 1 / 2 |
| No mastering credit on their releases (Clay) | 2 |
| Self-released (Clay, "self-released" on their page, or found self-released by `src.discover`) | 1 |
| Reach (Spotify listeners or steady views): 10K–1M / over 1M | 2 / 1 |

| Distribution | Points |
| --- | --- |
| Self-released (as in Mastering) | 3 |
| Links to or names a competitor distributor (DistroKid, TuneCore, CD Baby, Amuse, Ditto...) / none found | 2 / 1 |
| Releases in the last 12 months: 6 / 12+ | 1 / 2 |
| Latest release within 30 days | 1 |
| Spotify monthly listeners: 1K / 10K+ (Clay) | 1 / 2 |
| Promotes their releases: "out now", "pre-save", "link in bio", or a smart link (Linktree, feature.fm, ffm.to, lnk.to...) | 2 |

| Samples supplier | Points |
| --- | --- |
| Sells sample packs, drum kits, loop kits or presets (page text, or Clay's pack count) | 3 |
| Packs in catalogue: 10 / 50+ (Clay) | 1 / 2 |
| New pack within 90 days (Clay) | 2 |
| Sells direct (a storefront link) | 1 |
| Followers: 10K / 100K+ | 1 / 2 |

| Plugins | Points |
| --- | --- |
| Produces or mixes for others ("mixing engineer", "producer", "beatmaker", "type beat", "beats for sale", a BeatStars/Airbit/Traktrain link, or Clay) | 3 |
| Makes production content: tutorials, beat-making, beat breakdowns, DAW sessions (FL Studio, Ableton, Logic Pro...), gear or plugin reviews (or Clay) | 2 |
| Mentions third-party plugins (VST, Serum, FabFilter, Waves...) | 2 |
| Releases in the last 12 months: 6 / 12+ | 1 / 2 |
| Latest release within 30 days | 1 |

These weights are a starting point: they'll be tuned once replies show which signals
predict interest. Points can be changed, and a signal dropped, in the rules file; change
`scoring_version` there when you do, as it is stamped on every row.

- **Unknown counts half.** A signal no source could check earns half its points, so a
  lead is neither buried nor promoted for missing data; the breakdown says "half of N
  counted". Page phrases are found in `product_signals` (`src/scoring.py` lists them).
  They are read from website text, YouTube titles, and Instagram and TikTok captions
  and bios, so the social triggers (competitor distributor, release promotion,
  beatmaker, production content, self-released) feed the product they point to.
  Distribution has 12 points in all; like every product, its score is scaled to 10.
- **Limited data goes to Clay.** When under 60% of the points our own sources can check
  were checked for the best product (Clay-only signals are left out of that share),
  `limited_data` says so and the lead is sent to Clay whatever its score.
- **Labels** get Distribution and Samples supplier scores only; Mastering and Plugins are 0.
- **The big-act test.** Acts over 1M listeners or views get 1 reach point in Mastering
  instead of 2, on the theory that they already have an engineer. 10% of them
  (`BIG_ACT_TEST_SHARE`, picked by a stable hash so a lead stays in or out) get the full
  2 points and are marked `big_act_test`, so their replies can test the theory.

**Steady views** = median views per release × number of releases with views, over the
last 12 months, added up per platform. Releases from the last 14 days count as releases
but not in view figures. The median means one breakout video barely moves it.
`top_video_share` (the biggest release's share of views) is shown to the model too.

Existing rows keep their old score until they are re-scored: run
`python -m src.main --rescore-all` once after this change.

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
4. Work out the weighted base score, then have the model review it (±1 at most) and
   name the bottleneck.
5. Update the output CSV. A re-scored creator's row is replaced, so there is one row
   per creator; rows are never removed.
6. Enrich leads at or above the enrich cutoff (6; see "Cutoffs") with
   `ENRICHMENT_PROVIDER` (see "Enrichment"). With Clay, those leads wait for
   `--import-clay`, which scores them again on Clay's data before going on.
7. Claude writes the outreach hook for leads at or above the hook cutoff (7) whose
   score is final, and for the exploration sample (see "Fine-tuning from replies").
8. Send creators scoring at least `activation_min_score` (in the rules file) to
   `ACTIVATION_WEBHOOK_URL`, and those at or above the HubSpot cutoff (7) with an email,
   plus the exploration sample, to HubSpot, and record the result in each row.

### Cutoffs

Creators and labels each have their own three cutoffs, in `config/vertical_rules.json`:

```json
"cutoffs": {
  "creator": {"enrich": 6, "hook": 7, "hubspot": 7},
  "label":   {"enrich": 6, "hook": 7, "hubspot": 7}
}
```

`enrich` sends leads to enrichment, `hook` gets them a Claude hook, `hubspot` sends them
to HubSpot. Both start at the same values; the enrichment report splits its numbers by
creators and labels so each pair can be set from its own results.
`ENRICH_MIN_SCORE`, `HOOK_MIN_SCORE` and `HUBSPOT_MIN_SCORE` in `.env` override the
rules file for both types when set (handy for a one-off run).

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
| `enriched_creators.to_clay.<date-time>.csv` | Leads to upload to Clay, one file per run that had any (when `CLAY_WEBHOOK_URL` is blank) |
| `enriched_creators.awaiting_clay.json` | The full evidence of leads waiting for Clay, for their re-score |
| `enriched_creators.enrichment_report.csv` | One row per enriched lead: score before and after, change, reached 7+, email found |
| `enriched_creators.enrichment_summary.txt` | The latest enrichment summary (see "Enrichment report") |
| `enriched_creators.costs.csv` | Every model call and Clay row, priced: what each run cost (see "Costs, database and reports") |
| `gtm.db` | SQLite copy of the above for SQL and pandas reports, updated after every run |
| `outcomes.csv` | Outreach results (sent, replied, interested, converted, bounced), filled in by you or by `--sync-outcomes` |
| `enriched_creators.scoring_features.csv` | One row per scoring: the points each signal earned per product, the raw figures and Clay's scores (see "Fine-tuning from replies") |
| `enriched_creators.fit_report.csv` | The latest `--fit-report` table |

### Enrichment

Enrichment finds emails, and with Clay extra figures, for leads worth the cost: those
at or above the enrich cutoff (default 6). `ENRICHMENT_PROVIDER` picks who does it,
so a client can use the email finder they already pay for:

| `ENRICHMENT_PROVIDER` | Finds | When | Needs |
| --- | --- | --- | --- |
| `clay` (default) | email, Spotify monthly listeners, follower count, roster size, research notes | after you import Clay's CSV export | a Clay table; `CLAY_WEBHOOK_URL` optional |
| `apollo` | email (People Enrichment by name and domain) | during the run | `APOLLO_API_KEY` |
| `hunter` | email (Domain Search, most confident address) | during the run | `HUNTER_API_KEY` |

Every lead at or above the cutoff that hasn't been enriched yet is enriched, including
leads scored in earlier runs. Apollo and Hunter look leads up by the domain of their own
website or storefront, so leads with only YouTube, Bandcamp or social links are marked
`no_domain`, and leads that already have an email aren't looked up (`has_email`). They
only find emails, so the score doesn't change, and hooks and HubSpot follow in the same
run. Each lookup can use a credit.

**The Clay flow:**

1. `python -m src.main` scores every lead. Leads scoring 6+, and leads with limited data, go to Clay: by POST to
   `CLAY_WEBHOOK_URL` when it's set (Clay webhooks need a paid plan), otherwise in
   `data/enriched_creators.to_clay.<date-time>.csv` to upload to a Clay table by hand.
   They are marked `awaiting_clay`, get no hook yet, and HubSpot shows them as
   `awaiting_enrichment`.
2. In Clay, add columns named `email`, `spotify_monthly_listeners`, `followers`,
   `self_released`, `mastering_credit`, `producer_or_engineer`,
   `makes_production_content` (yes/no), `pack_count`, `latest_pack_date`, `roster_size`
   (labels) and `clay_research` (a few lines of research), and keep the
   link columns (`website_url` and so on) so leads match back. Names are matched
   ignoring case, spaces and dashes; `Work Email`, `Monthly Listeners`, `Follower Count`,
   `Roster` and `Research Notes` work too. Numbers like `12.3K` or `1.2M` are fine.
3. Export the table as CSV and run
   `python -m src.main --import-clay path/to/export.csv`. Each lead waiting for Clay is
   scored again with Clay's figures (our own figures win where both exist) and Clay's
   research in front of the model, then Claude writes hooks for 7+ and leads with 7+
   and an email go to HubSpot. `score_before_enrichment` keeps the earlier score.
   Leads not waiting for Clay, or not found, are counted and left alone. If a lead
   can't be scored, it stays waiting; import the same file again.
4. **Clay's lead scores.** Clay rescores the enriched list with its built-in lead
   scoring, one score per product: `clay_score_mastering`, `clay_score_distribution`,
   `clay_score_samples` and `clay_score_plugins` (`Mastering Score` and so on work too).
   On import Clay's best score is the final score and names `best_product`: no model
   call, `scored_by` is `clay`, each scaled onto 1-10 from 0 to `CLAY_SCORE_MAX` (default
   10; set 100 for a 0-100 score). Add a `clay_score_version` column when you change
   Clay's formula. A single `clay_score` column counts for the engine's best product.
   The engine's own product scores, with Clay's figures, stay alongside for comparison.
   Leads with no Clay score are scored again by the engine as above.

Clay's figures are kept when a lead is scored again later, and an enriched lead isn't
sent to Clay again. A failed webhook send is marked `send_failed` and sent next run.

**The cutoff experiment.** `python -m src.main --clay-experiment` also sends a random
20 leads scored exactly 5 (`CLAY_EXPERIMENT_SCORE`, `CLAY_EXPERIMENT_SAMPLE` in
`src/settings.py`) to Clay. After the import, the report says what share reached 7+: over
50% means the enrich cutoff should go down to 5. With fewer than 20 leads scored 5,
all of them are sent and the result is marked too small to call.

### Enrichment report

`python -m src.main --enrichment-report` prints the summary (it is also logged at the
end of every run and import, and saved in `enrichment_summary.txt`):

```text
Enriched leads: 34. At the hook cutoff (7+): 12 before enrichment, 19 after. Emails found by enrichment: 21. Average score change: +0.6.
Experiment (leads scored 5): 20 enriched, 11 reached the hook cutoff (55%; creators 9/16; labels 2/4). Verdict: over 50%: lower the enrich cutoff to 5.
Score spread over 380 scored creators: 7+: 36 (28 with an email); 8+: 13 (10 with an email); 9+: 3 (3 with an email).
Score spread over 32 scored labels: 7+: 4 (3 with an email); 8+: 2 (2 with an email); 9+: 0 (0 with an email).
Waiting for Clay: 0.
HubSpot (7+ with an email): 9 leads without enrichment, 24 with it. Exploration sample below the cutoff: 11 (creators 10; labels 1).
```

The score spread is there to pick the hook and HubSpot cutoffs (both 7 for now) for
creators and labels once real runs show how many leads land at each score. "Without enrichment" counts
leads by their score before enrichment and the emails that came with the input.
`enrichment_report.csv` has the lead-by-lead detail.

### Webhook (activation)

`ACTIVATION_WEBHOOK_URL` is an optional final delivery (Zapier, Make, your own
endpoint) for leads scoring at least `activation_min_score`. Leads waiting for Clay are
held back until their import. It is separate from `CLAY_WEBHOOK_URL`, which sends leads
*to* Clay for enrichment; don't set both to the same Clay table.

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
  12 months, latest release date, scored-at, Spotify monthly listeners, roster size,
  score before enrichment and research notes go into custom contact properties in a
  "LANDR GTM engine" group (`landr_fit_score` and so on, listed in `src/hubspot.py`).
- Only creators at or above the HubSpot cutoff (default 7) are synced, plus the
  exploration sample (`landr_exploration` says which group). Leads waiting for Clay are
  marked `awaiting_enrichment` and synced after the import.
- `--create-properties` also creates `landr_outcome`, a dropdown (no reply, replied,
  interested, converted, bounced) for you or a workflow to set once a contact answers;
  `--sync-outcomes` reads it back (see "Fine-tuning from replies").
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

### Ollama: free local scoring

Scoring is high-volume, mechanical work (a ±1 adjustment, a reason and a bottleneck
label as JSON), so it can run on a model on your own computer through
[Ollama](https://ollama.com), free and offline. The outreach hook is the sentence a
creator actually reads, so it is written by Claude (see "Outreach hooks"), and only for creators
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

### Outreach hooks

Whichever model scores, the hook is written by Claude (`CLAUDE_MODEL`) in a separate
call once the lead's score is final: in the same run with Apollo or Hunter, or after
`--import-clay` for leads sent to Clay. Only leads at or above the hook cutoff (default 7)
get one. `HOOK_PROVIDER=none` turns hooks off (`claude` is the default; the
older `same` setting now means `claude`).

Claude sees the same evidence plus the score and bottleneck, and leads below the
threshold get an empty `custom_hook`. It needs `ANTHROPIC_API_KEY`, also when Gemini or
Ollama scores (or set `HOOK_PROVIDER=none`). If Claude can't write one creator's hook,
the score is still saved with an empty hook and a warning; a Claude account problem (no
credit, bad key) stops the run as usual. The `hook_by` column records which model wrote
each hook.

## Finding new leads (Discogs and MusicBrainz)

`python -m src.discover` searches Discogs and MusicBrainz for artists and labels that
are not in your lists yet and adds them to `data/input_creators.csv`. The next
`python -m src.main` collects evidence and scores them like any other row.

```bash
python -m src.discover --dry-run     # list what would be added
python -m src.discover               # add up to 100 new leads
python -m src.discover --limit 20
```

The saved searches are in `config/discovery.json`:

| Search | Finds | `product_fit` |
| --- | --- | --- |
| `discogs-self-released-artists` | Artists with digital releases on "Not On Label" (Discogs' mark for self-released) this year or last | Distribution; Mastering |
| `discogs-active-labels` | Labels with digital releases this year or last | Distribution; Mastering; Samples |
| `musicbrainz-new-labels` | Production labels founded in the last 5 years | Distribution; Mastering |
| `musicbrainz-artists` | Artists tagged with each genre | Plugins; Samples; Distribution |

- Every search runs for each genre (hip-hop, R&B, pop, country, rock, indie, alternative,
  electronic, house, afrobeats, latin, dancehall, lo-fi) and country (US and Canada;
  `suggested_countries` lists more to move in). Genres map to Discogs genres/styles and
  MusicBrainz tags; MusicBrainz tags are added by volunteers, so coverage is patchy.
- Each lead's links come from its own Discogs or MusicBrainz page: website, YouTube,
  Bandcamp and other storefronts, Instagram and TikTok, plus an email when a Discogs
  label lists one. Leads with no link are skipped, as are leads already in the input
  or output CSV.
- Labels whose name contains one in `major_labels` stay in, marked `Partnership`.
- `lead_source` is copied to the scored CSV, so discovered and inputted leads can be
  compared. Self-released artists get `self_released` = `yes`, which scoring counts.
- `max_leads` per search and `max_new_leads` per run keep the run short: Discogs allows
  60 calls a minute (each lead takes about 2) and MusicBrainz one a second.
- Discogs needs `DISCOGS_TOKEN`; without it the Discogs searches are skipped.
- Licensing: MusicBrainz's web service is free for non-commercial use only, and some
  Discogs data is restricted under its API Terms of Use. Fine for this portfolio project;
  check both before using the leads commercially.

Not tested against the live APIs yet (the build machine couldn't reach them).

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

- `ANTHROPIC_API_KEY` (required unless scoring with Gemini or Ollama and
  `HOOK_PROVIDER=none`, since Claude writes the hooks).
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
- `SCORING_PROVIDER=ollama`, and optionally `OLLAMA_HOST` and `OLLAMA_MODEL`, to score
  on this computer (see "Ollama" above). `HOOK_PROVIDER` and `HOOK_MIN_SCORE` for hooks.
- `ENRICHMENT_PROVIDER` (`clay`, `apollo` or `hunter`). `ENRICH_MIN_SCORE`,
  `HUBSPOT_MIN_SCORE` and `HOOK_MIN_SCORE` override the rules file's cutoffs for creators
  and labels alike (see "Cutoffs"); `EXPLORE_SHARE` (default 0.05) sizes the exploration
  sample; `CLAY_WEBHOOK_URL` to send leads to Clay without
  a CSV (paid Clay plan); `APOLLO_API_KEY` or `HUNTER_API_KEY` for those email finders
  (see "Enrichment").
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
- `ACTIVATION_WEBHOOK_URL` and `ACTIVATION_WEBHOOK_FORMAT` for an optional final
  delivery webhook (see "Webhook (activation)").
- `HUBSPOT_ACCESS_TOKEN` to sync contacts into HubSpot (see "HubSpot contacts").

Run the pipeline from the repository root:

```bash
python -m src.main
python -m src.main --scoring batch
python -m src.main --resend-unsent
python -m src.main --rescore-all   # score everyone again, e.g. after changing the weights
python -m src.main --provider gemini   # small test run scored by Gemini
python -m src.main --provider ollama   # score on this computer
python -m src.main --retry-failed      # try creators that failed earlier runs again
python -m src.main --clay-experiment   # also send 20 leads scored 5 to Clay
python -m src.main --import-clay data/clay_export.csv   # Clay's results: re-score, hooks, HubSpot
python -m src.main --enrichment-report # before/after scores, experiment verdict, score spread
python -m src.db sync                  # reload the database from the CSV files
python -m src.reports                  # pass rates, Clay lift, costs, reply rates (needs pandas)
```

Input, rules, and output paths can be overridden with `--input`, `--rules`,
and `--output`. The sample input is a starter dataset, not the planned
production list.

Run settings shared by every vertical (models, batch size, 90-day re-score, 3
attempts, pages read) are in `src/settings.py`.

## Costs, database and reports

The CSV files stay the source of truth. Next to them, every run now keeps a cost file
and a SQLite database, so you can answer questions like "what is the reply rate of
leads scored 8?", "what does a qualified lead cost?" or "how much does Clay lift a
score?" with SQL and pandas.

### Costs

Each model call logs its tokens (scoring and hooks, Claude, Gemini and Ollama, live or
batch), and each lead sent to Clay, or looked up on Apollo or Hunter, logs one row.
Both go to one file, `data/enriched_creators.costs.csv`, priced as they're written:

| Column | Meaning |
| --- | --- |
| `run_id` | The run (its start time, UTC), shared by every line it logged |
| `lead_key`, `name` | The lead the line is about |
| `kind` | `tokens` (a model call) or `rows` (a flat per-row charge) |
| `task` | `score`, `hook` or `enrich` |
| `provider`, `model`, `batch` | Who was paid; `batch` is `yes` for Message Batches (half price) |
| `input_tokens`, `output_tokens`, `rows` | What was used |
| `cost_usd` | The price of that line; blank when it has no price |

Prices are in `config/costs.json`: token prices per million for each model, the batch
discount, and a flat price per row for Clay, Apollo and Hunter. **Set the Clay row
price there** (your plan's price per credit times the credits one row uses in your Clay
table); until it is set, Clay rows are logged with a blank cost and the report warns
about it. Gemini is priced at 0 because test runs use the free tier, and Ollama is
always free; their tokens are still logged. At the end of each run the log says what
the run cost.

### Database

After every run (`python -m src.main`, `--import-clay` and `--resend-unsent`), the CSV
files are loaded into `data/gtm.db` (SQLite, built into Python; nothing to install or
run). To load them by hand, e.g. after editing `outcomes.csv`:

```bash
python -m src.db sync
```

Tables:

| Table | One row per | Notes |
| --- | --- | --- |
| `leads` | lead in the output CSV | Main columns, plus the whole CSV row as JSON in `row_json`: `json_extract(row_json, '$.spotify_monthly_listeners')` reads any column, so new CSV columns need no schema change |
| `score_events` | score a lead has had | Grows with every sync, so score history builds up from the first sync. `stage` is `initial`, or `before_enrichment` / `after_enrichment` for leads Clay enriched |
| `score_components` | part of a score event | Each product's total (`component = 'total'`), Clay's per-product scores (`product = 'clay_mastering'`, ...) and each signal with its points, named as in the rules file (`releases`, `self_released`, ...). `known = 0` marks a signal no source could check (half its points counted). New products or signals are new rows |
| `runs` | pipeline run | Command, provider, start and end, leads scored |
| `costs` | line of the cost file | Reloaded from the file on every sync |
| `outcomes` | outreach sent | Reloaded from `outcomes.csv` on every sync |

`leads`, `costs` and `outcomes` are copies of their files; `score_events`,
`score_components` and `runs` only grow. Deleting `gtm.db` loses only score history the
CSV no longer shows. Open it in any SQLite tool (e.g. DB Browser for SQLite), or:

```bash
sqlite3 data/gtm.db "SELECT best_product, COUNT(*), AVG(score) FROM leads GROUP BY best_product"
```

### Outreach outcomes

Record outreach in `data/outcomes.csv` (copy `data/outcomes.example.csv` to start):

| Column | Meaning |
| --- | --- |
| `email`, or `website_url` / `youtube_url` / `instagram` / `tiktok` / `name` | Which lead; matched like the input file. Rows matching no lead are listed as warnings and left out |
| `channel` | e.g. `email` (the default), `instagram_dm` |
| `sent_at` | Date sent, `YYYY-MM-DD` (required) |
| `replied`, `interested`, `converted` | `yes` or blank. Each implies the ones before it |
| `bounced` | `yes` if the email never arrived; the row is left out of the reports and the fit |
| `notes` | Anything |

`python -m src.main --sync-outcomes` fills the email rows in from HubSpot's
`landr_outcome` property: a row with the same email is updated (your notes and other
channels are kept) and new ones are added. `sent_at` is the contact's last email send
date in HubSpot. The database is updated straight after.

The file holds names and emails, so it is in `.gitignore` and stays on your computer.

### Reports

The reports use pandas, which the pipeline itself doesn't need (about 60 MB with
numpy). Install it once, in the same virtual environment:

```bash
pip install -r requirements-reports.txt
python -m src.reports          # add --sync to update the database first
```

It prints four reports and writes each to a CSV in `data/reports/`:

| Report | Answers |
| --- | --- |
| `pass_rates` | How many leads score 7, 8 and 9 or more, by lead type and best product |
| `enrichment_lift` | Average score before and after Clay, how many went up or down, and how many reached the hook score (7), split into regular leads and the cutoff experiment |
| `costs` and `cost_totals` | Spend by provider, task and model; total, cost per lead scored, cost per qualified lead (score 7+) |
| `reply_rates` | Reply, interest and conversion rates by score band (1-4, 5-6, 7, 8, 9-10), using the score each lead had **when it was contacted**, so later re-scores don't blur the result |

The SQL behind each report is at the top of `src/reports.py`; pandas does the grouping
and the rates. Once outcomes come in, `score_components` joined to `outcomes` shows
which signals actually predict a reply, which is how the weights in
`config/vertical_rules.json` can be tuned from real responses.

## Fine-tuning from replies

The product weights in the rules file are starting guesses. Four pieces move them toward
the signals that actually predict replies:

1. **Outcomes.** Set `landr_outcome` in HubSpot as contacts answer (or fill in
   `outcomes.csv`), then run `python -m src.main --sync-outcomes`.
2. **Feature file.** Every scoring adds a row to `scoring_features.csv` with the points
   each signal earned per product (`points_<product>_<signal>`, half points for an
   unknown signal), the raw figures, the model's adjustment, Clay's four scores, and
   the `scoring_version` and `clay_score_version` behind them.
3. **Exploration sample.** 5% of the leads below the HubSpot cutoff that have an email
   (`EXPLORE_SHARE`) go to HubSpot anyway, with a hook, marked `below_cutoff` in the
   `exploration` column, or `spot_check_5` for leads scored 5. The draw is stable per
   lead (a hash of its link), so the same lead isn't drawn in and out between runs.
   Without it you would only learn about the leads you already rated highly.
4. **Fit report.** `python -m src.main --fit-report` compares reply rates (replied,
   interested or converted; bounces left out) for each best product, score band, lead
   type, exploration group, big-act test group, scorer (Clay or the engine) and scoring
   version, and for every product signal it compares the leads where it earned points
   with the average. It writes `fit_report.csv` and prints the headline rows.

Weights are only suggested once there are 200 contacted leads and 30 replies
(`FIT_MIN_CONTACTED`, `FIT_MIN_REPLIES` in `src/settings.py`). Then a signal whose
leads reply at 1.5 times the average or more gets "raise by 1 point", and one at 0.67
times or less gets "lower by 1 point" (with at least 10 leads on each side). Before
that, the product and score-band rates are enough to spot something clearly not
working. Nothing is changed for you: edit the points in `config/vertical_rules.json`,
raise `scoring_version`, and run `--rescore-all`. The same table shows which signals to
weight in Clay's lead scoring.

## Development

Run the test suite with:

```bash
python -m unittest discover -s tests
```

The report tests are skipped when pandas isn't installed.

For a guide to reading the project history and commit labels, see
[CONTRIBUTING.md](CONTRIBUTING.md).
