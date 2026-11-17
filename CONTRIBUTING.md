# Reading Git Commit Messages

Each commit starts with a short label that tells you what kind of change it
contains:

```text
type(scope): summary
```

The scope is optional. When present, it names the part of the project being
changed. The summary describes the change in a few words.

## Labels used in this project

- **`chore`** — repository setup or maintenance. For example,
  `chore: initialize the LANDR creator GTM engine` marks the starting point
  for the project history.
- **`feat`** — adds or changes project behavior. A scope narrows down the
  area: `feat(ingest): load and validate creator CSV targets` is about reading
  and checking input records, while `feat(scraper): extract website text,
  storefronts, and signals` is about collecting website information.
- **`test`** — adds or updates automated checks. For example,
  `test: cover ingestion, scraping, enrichment, and activation` describes
  which pipeline areas are tested.
- **`docs`** — explains the project or how to use it. For example,
  `docs: explain setup, pipeline stages, and webhook options` updates the
  project guidance.

## Reading the history

Git displays the newest commit first. Read each summary to follow the project
from its initial setup through ingestion, scraping, configuration, enrichment,
activation, orchestration, tests, and documentation. To inspect the exact
files changed by a commit, open it in your Git history viewer or run:

```bash
git show <commit>
```

Commit titles should be concise, use an action-oriented summary, and focus on
one logical change. Add a body only when more context is needed to explain why
the change was made.
