"""Pipeline settings that apply to every vertical."""

# Scoring models: live runs score a few creators at a time; batch runs are bulk.
LIVE_MODEL = "claude-sonnet-5-5"
BATCH_MODEL = "claude-sonnet-5-5"

# Gemini, for small test runs only (SCORING_PROVIDER=gemini): live scoring, no batches.
# Free-tier limits vary by model and project; check them in Google AI Studio.
GEMINI_MODEL = "gemini-3.8-flash"
# Tried when GEMINI_MODEL stays overloaded or rate-limited after its retries.
GEMINI_FALLBACK_MODEL = "gemini-3.5-flash-lite"
GEMINI_REQUESTS_PER_MINUTE = 10
GEMINI_MAX_RETRIES = 5  # waits 5, 10, 20, 40, 60s (or what the API asks for)

# Ollama, a model running on this computer (SCORING_PROVIDER=ollama): free, live scoring.
OLLAMA_HOST = "http://localhost:11434"
OLLAMA_MODEL = "llama3.1:8b"
# Context window in tokens. Ollama's own default is smaller than one creator's evidence
# (up to 12,000 characters of page text plus figures), which it would silently cut.
OLLAMA_NUM_CTX = 16_384
OLLAMA_MAX_OUTPUT_TOKENS = 1_024
# A reply that isn't valid JSON (or breaks the output rules) is asked for again this
# many times before the creator counts as a failed attempt.
OLLAMA_MAX_RETRIES = 2
# Seconds to wait for one reply; the first one also waits for the model to load.
OLLAMA_TIMEOUT_SECONDS = 300

# Outreach hooks. Claude writes them in a separate call once a lead's score is final
# (after enrichment), only for leads scoring at least HOOK_MIN_SCORE. HOOK_PROVIDER=none
# writes no hooks.
HOOK_MIN_SCORE = 7

# Enrichment (src/enrichment.py). ENRICHMENT_PROVIDER: "clay" (the default), "apollo" or
# "hunter". Leads scoring at least ENRICH_MIN_SCORE are enriched. The three *_MIN_SCORE
# values here are defaults: the rules file's "cutoffs" set them per creator and label.
ENRICHMENT_PROVIDER = "clay"
ENRICH_MIN_SCORE = 6
# Only leads scoring at least this (and with an email) go to HubSpot.
HUBSPOT_MIN_SCORE = 7
# The cutoff experiment (--clay-experiment): a random sample of leads scoring exactly
# CLAY_EXPERIMENT_SCORE is enriched anyway. If more than CLAY_EXPERIMENT_THRESHOLD of
# them reach HOOK_MIN_SCORE after enrichment, the report suggests lowering
# ENRICH_MIN_SCORE to CLAY_EXPERIMENT_SCORE. Fewer than CLAY_EXPERIMENT_SAMPLE is too
# small to call.
CLAY_EXPERIMENT_SCORE = 5
CLAY_EXPERIMENT_SAMPLE = 20
CLAY_EXPERIMENT_THRESHOLD = 0.5
# Clay's lead score (a clay_score column in its export) is the final score of the leads
# it enriched. CLAY_SCORE_MAX is the top of Clay's scale (10, or 100 for a 0-100 score);
# it is scaled onto 1-10.
CLAY_SCORE_MAX = 10
# Mastering caps reach points for acts over 1M listeners or views (they usually have an
# engineer already). This share of those acts, picked by a stable hash of the lead, get
# the full points instead and are marked big_act_test, to test that against replies.
BIG_ACT_TEST_SHARE = 0.10
# Exploration sample: this share of the leads below the HubSpot cutoff that have an email
# go to HubSpot anyway (with a hook), so you learn how lower-rated leads answer. Leads
# scored SPOT_CHECK_SCORE are reported as their own spot-check group.
EXPLORE_SHARE = 0.05
SPOT_CHECK_SCORE = 5
# --fit-report suggests weight changes only with at least FIT_MIN_CONTACTED contacted
# leads and FIT_MIN_REPLIES replies, for signals with FIT_MIN_GROUP leads on each side
# whose reply rate is at least FIT_RAISE_LIFT (or at most FIT_LOWER_LIFT) times the average.
FIT_MIN_CONTACTED = 200
FIT_MIN_REPLIES = 30
FIT_MIN_GROUP = 10
FIT_RAISE_LIFT = 1.5
FIT_LOWER_LIFT = 0.67
# Apollo and Hunter lookups, one at a time (each one can use a credit).
EMAIL_LOOKUPS_PER_SECOND = 1

# In "auto" scoring mode, runs scoring at least this many creators use the Batch API.
BATCH_THRESHOLD = 20
# A batch is submitted every time this many creators have evidence collected.
BATCH_SIZE = 500
BATCH_POLL_SECONDS = 60

# Creators scored longer ago than this are collected and scored again.
RESCORE_AFTER_DAYS = 90
# A creator that fails this many runs in a row is skipped until removed from the failures file.
MAX_ATTEMPTS = 3

# Pages of releases read per label (100 releases a page, about 1 second each) and of
# uploads per channel (50 a page, so up to 500 uploads in the last year).
MAX_RELEASE_PAGES = 50
MAX_UPLOAD_PAGES = 10
# Discogs only lists release years; the exact dates of this many of a label's newest
# releases are read one by one (about 1 second each) to place them in the last 12 months.
DISCOGS_DATED_RELEASES = 20

# Releases younger than this are counted as releases but left out of view figures,
# because they haven't had time to collect views.
VIEW_MATURITY_DAYS = 14

# Instagram Business Discovery (Graph API).
GRAPH_API_VERSION = "v25.0"
INSTAGRAM_MEDIA_LIMIT = 50  # posts per call
INSTAGRAM_MAX_PAGES = 4  # up to 200 posts, stopping once posts are over a year old
# Business Discovery calls count against your token's limit, about 200 an hour.
INSTAGRAM_CALLS_PER_HOUR = 190

# TikTok through Bright Data. Every TikTok profile is looked up (1 record each); the
# recent posts (about TIKTOK_POSTS_PER_PROFILE records each) are only collected for
# profiles with at least TIKTOK_POSTS_MIN_FOLLOWERS followers, to stay in the free tier.
TIKTOK_POSTS_MIN_FOLLOWERS = 10_000
TIKTOK_POSTS_PER_PROFILE = 30
BRIGHTDATA_POLL_SECONDS = 15
BRIGHTDATA_MAX_WAIT_SECONDS = 3600
