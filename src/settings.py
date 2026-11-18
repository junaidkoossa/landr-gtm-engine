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

# Outreach hooks. HOOK_PROVIDER "same" has the scoring model write the hook in the same
# reply (the default for Claude and Gemini); "claude" has Claude write it in a separate
# call, only for creators scoring at least HOOK_MIN_SCORE (the default for Ollama);
# "none" writes no hooks.
HOOK_MIN_SCORE = 7

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
