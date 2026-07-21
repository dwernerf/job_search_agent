# jobagent-local

`jobagent-local` is a read-only job-discovery agent for Ubuntu 24.x. Playwright loads public career, search, and listing pages, fetches the destinations linked from each page, and sends those destination contexts to a local OpenAI-compatible LLM for classification against `config/profile.md`.

The agent is designed for llama.cpp or a similar local server. It does not require command-line arguments.

## Quick start

```bash
# 1. Install dependencies and browser
make install && make browsers

# 2. Edit the three user-facing config files
#    config/profile.md   - roles, expertise, exclusions, and detailed preferences
#    config/intent.yaml  - local area and company lists
#    config/config.yaml  - LLM endpoint, context size, batching, and runtime settings

# 3. Start the local LLM server and check its configured endpoint
curl http://127.0.0.1:<PORT>/v1/models

# 4. Run
scripts/run.sh
```

Results are written to `data/jobs.csv` and `data/jobs.sqlite`.

Before running, set `llm.context_window_tokens` in `config/config.yaml` to the server's actual context size. Reduce `crawler.batch_size_for_llm` or `crawler.max_page_context_chars` if a classification batch is too large.

## Core design

The agent uses five configuration inputs:

| File | Purpose |
|---|---|
| `config/profile.md` | Candidate profile supplied to the LLM; also provides target roles and text-relevance terms |
| `config/intent.yaml` | Personal values: local area, company blacklist, and bootstrap whitelist |
| `config/config.yaml` | Operational settings for the LLM, browser, queue, batching, score thresholds, exploration, and logging |
| `config/prompts.yaml` | Generic LLM instructions with no role-specific content |
| `config/seeds.txt` | Optional starting URLs for career pages and job-board results |

## Active pipeline

The agent runs one queue-driven loop:

1. **Check the LLM** - when enabled, request the configured `/models` endpoint before opening a browser.
2. **Seed the backlog** - enqueue URLs from `config/seeds.txt`, generated bootstrap searches, or both, each with rating 80.
3. **Open a backlog page** - Playwright captures its final URL, title, body text, links, and any `JobPosting` JSON-LD rendered as text.
4. **Claim and fetch outbound destinations** - URLs are normalized, checked against URL-only crawl rules, stripped of tracking parameters, and deduplicated. A URL already present in `pages` is dropped; a new URL is immediately recorded there with `status=ok` before Playwright fetches and compacts it into `page_context`. HTTP failures update that marker and are excluded before classification.
5. **Classify the links** - the LLM receives the overview page plus each link's text, URL, and destination context. It returns `link_classifications` with type `job_listing`, `explore`, or `skip`. For `explore`, `fit_score` estimates the likelihood of finding a suitable target-area job through that URL.
6. **Route classifications** - a `job_listing` at or above `scoring.min_score_to_export` becomes a job candidate. An `explore` URL enters the backlog with its score as the rating when exploration is enabled. A `skip` result is ignored.
7. **Filter and save** - Python applies the company blacklist, removes duplicate job URLs from the batch, and upserts jobs by URL. CSV is rewritten from SQLite after each non-empty save.
8. **Continue** - the successfully processed source is recorded in `pages`, its backlog row is deleted, and the loop runs until no queued URL remains.

Fetching an outbound destination for classification does not automatically queue it. Only links classified as `explore` enter the backlog. The `rating` queue mode processes the highest rating first and uses insertion order for ties.

Every Playwright navigation passes through one global pacing interval configured by `run.min_delay_seconds` and `run.max_delay_seconds`. Playwright uses the application user agent configured under `app.user_agent`.

### Seeds and bootstrap searches

`seeding.mode` controls startup:

- **`seeds`** - use only normalized, deduplicated URLs from `config/seeds.txt`.
- **`bootstrap`** - build one search phrase per target role using the role, the configured local area, a random job suffix, and sometimes a whitelisted company, then render it through each search URL template.
- **`both`** - combine both sources. The repository configuration currently uses this mode.

The configured template uses Brave Search. Public search services may present bot checks that the agent does not bypass. Startup URLs are authoritative work items and are enqueued on every run even if they already exist in `pages`; this lets a stable seed discover newly added outbound links.


## Install

```bash
# Option 1: make targets
make install && make browsers

# Option 2: install script
scripts/install_ubuntu24.sh
```


## Configure

### 1. Edit `config/profile.md`

Describe what you want in plain Markdown. Key sections:

- Target roles and acceptable titles
- Target role signals (keywords the agent looks for)
- Relevant expertise and positive fit factors
- Especially relevant industries
- Avoid and exclude (roles, titles, shift types to reject)

The complete profile is included in the LLM classification prompt. The Markdown parser also derives target roles for bootstrap searches and relevance terms used while compacting fetched text. Profile exclusions guide the LLM; they are not applied as a separate Python filter.

### 2. Edit `config/intent.yaml`

Personal overrides. Contains:

- `location.local_area` - short area description supplied to bootstrap search generation, prompts, and logs
- `companies.blacklist` - drop matching job classifications before persistence
- `companies.whitelist` - inject a company into approximately half of generated bootstrap searches

### 3. Edit `config/config.yaml`

Key areas to review:

- **`llm`** - endpoint, model, `context_window_tokens`, timeout, and temperature
- **`browser`** - headless mode and navigation behavior
- **`run`** - backlog reset, FIFO, shuffled, or rating-first ordering, and the interval between Playwright navigations
- **`crawler`** - URL normalization/denial rules, failed-page retries, LLM batch size, and destination-context size
- **`scoring`** - minimum saved score
- **`exploration`** - whether LLM-classified `explore` URLs enter the backlog
- **`seeding`** - seed/bootstrap mode, search URL templates, and job suffixes
- **`logging`** - info/debug level and console/file output

Match `llm.context_window_tokens` to the server's context size. If a prompt exceeds the configured budget, reduce `crawler.batch_size_for_llm` or `crawler.max_page_context_chars`.

### 4. Optional: edit `config/seeds.txt`

Add public career pages or job-board result pages. Example:

```text
https://some-company.example/careers
https://jobs.lever.co/some-company
https://boards.greenhouse.io/some-company
https://some-job-board.example/jobs/procurement/muenchen
```

## Run

Start your local LLM server first. When `llm.require_available_on_start` is enabled, the agent checks `llm.base_url + /models` and stops with `llm_unavailable_stop` if the server is unreachable.

```bash
scripts/run.sh
```

Or:

```bash
. .venv/bin/activate
python -m jobagent
```

Override the config file:

```bash
JOBAGENT_CONFIG=/absolute/path/to/config.yaml python -m jobagent
```

Relative profile, seed, prompt, and output paths are resolved from that configuration's project root. Keep `intent.yaml` in the root's `config/` directory.



## Persistence and filtering

SQLite schema version 3 contains exactly three tables:

| Table | Fields |
|---|---|
| `jobs` | `url`, `title`, `company`, `location`, `fit_score`, `reason`, `evidence`, `source_key`, `first_seen_at`, `last_seen_at`, `original_url` |
| `pages` | `url`, `final_url`, `status` |
| `backlog` | `url`, `status`, `queued_at`, `rating`, `queue_position` |

`pages` is the permanent attempted-URL set. Candidate deduplication normally checks only whether a requested or final URL exists there. When `crawler.retry_error_pages` is enabled, candidates with transient HTTP statuses (408, 425, 429, or 5xx) are the exception and may be claimed again when rediscovered. Backlog enqueueing does not consult `pages`: seed/bootstrap sources can run again, and an outbound destination already claimed for classification can still become an `explore` source. The backlog stores no depth or discovery context and retains only queued, active, or errored work plus its rating and stable queue position. Successful rows are deleted. Enabling `run.reset_backlog_on_start` clears only backlog rows, while saved jobs and attempted-page rows remain.

The same URL policy is used for seeds, generated searches, and page links. It accepts configured HTTP schemes; requires a host; rejects configured domains, file extensions, login/account URLs, and initiative/talent-pool/general-application URLs; removes configured tracking parameters and fragments; and normalizes paths. Page links resolving to the source page or to an already-seen canonical URL are removed.

Filtering is URL-only. Link text is not inspected, submission endpoints such as `/apply/submit` are not denied, and there is no per-page candidate limit or provider-specific rule. Every newly claimed URL that passes the policy is fetched before LLM classification.

Valid schema-v2 databases are migrated automatically to v3. Existing backlog rows receive rating 80 and stable queue positions in their prior FIFO order. Older and malformed schemas are not migrated. Interrupted `active` backlog rows are returned to `queued` when the database is reopened. Error rows are also requeued when `crawler.retry_error_pages` is enabled; old `done` and `skipped_visited` rows are removed.

For a saved job, `source_key` contains the normalized domain and first path segment of the backlog page being processed. An upsert preserves `first_seen_at`, updates the other job fields, and refreshes `last_seen_at`.

The LLM decides whether a destination is a job and supplies its score and fields. A job's `fit_score` measures concrete job fit; an explore classification's `fit_score` becomes its backlog rating. Runtime job acceptance then consists of:

- a successfully fetched destination context
- `type == "job_listing"`
- `fit_score >= scoring.min_score_to_export`
- no company-blacklist match across the returned job text and URL
- no duplicate URL in the current batch; SQLite subsequently upserts by URL

Location preferences and profile exclusions are LLM context. Python does not calculate geographic distance or alter the returned fit score.

## LLM context window

Set `llm.context_window_tokens` in `config/config.yaml` to the server's actual context size.

The client estimates prompt size with a character-based heuristic and reserves 3,000 tokens from `context_window_tokens`. Overview text and each destination context have fixed caps. If a batch exceeds the estimate, the agent defers its final link to a separate batch and retries the reduced batch once; reduce the batch or context setting if the retry still does not fit.

## Outputs

| File | Description |
|---|---|
| `data/jobs.sqlite` | SQLite v3 jobs, visited pages, and rated URL backlog |
| `data/jobs.csv` | Spreadsheet-friendly job rows |
| `data/jobagent.log` | Run log |

CSV uses these fields, in this order:

`fit_score`, `title`, `company`, `location`, `url`, `reason`, `evidence`, `source_key`, `first_seen_at`, `last_seen_at`, `original_url`

The export is synchronized from all SQLite job rows on database startup and after every `save_jobs()` call that writes at least one job. Rows are ordered by descending fit score and then most recent `last_seen_at`.

Failed browser navigations retain the requested/final URL, failure category, and HTTP status when available. Top-level failures persist concise statuses such as `error:http_429` or `error:navigation_timeout`. A candidate HTTP failure updates its `pages` marker and is excluded from the LLM request. Transient candidate HTTP failures can be fetched again when rediscovered and retries are enabled; other candidate failures remain terminal. Source failures retain an errored backlog row; successful sources delete their backlog row.

Inspect top jobs:

```bash
sqlite3 data/jobs.sqlite '
select fit_score, title, company, location, url
from jobs order by fit_score desc, last_seen_at desc limit 25;
'
```

Reset all persisted state and exports:

```bash
rm -f data/jobs.sqlite data/jobs.sqlite-* data/jobs.csv
```

## Tests

```bash
scripts/test.sh
```

This runs pytest followed by `compileall`; `make test` runs pytest only. Test files cover configuration and profile derivation, bootstrap seeding, URL handling, link-classification routing, company filtering, the SQLite v3 schema and exports, LLM JSON/context handling, and reporting. Agent tests use fake browser/LLM implementations where supplied.

The suite is network-isolated; agent tests use fake browser and LLM implementations.

Run in your environment:

```bash
PYTHONPATH=src pytest -q
PYTHONPATH=src python -m compileall -q src tests
```

## Troubleshooting

### `seed_backlog added=0 queued=0`

Check `seeding.mode` and verify that `config/seeds.txt` contains usable URLs. Previously visited seeds are still enqueued; an already queued duplicate is not counted as another addition. For a deliberately clean run:

```bash
rm -f data/jobs.sqlite data/jobs.sqlite-* data/jobs.csv
```

### LLM connection errors

Check the endpoint in `config/config.yaml`, then verify:

```bash
curl <llm.base_url>/models
```

Confirm `llm.base_url`, `llm.model`, and `llm.chat_endpoint` match your server.

### Too many irrelevant jobs

Edit `config/profile.md`, especially:

- Target roles and acceptable titles
- Target role signals
- Avoid and exclude

These sections guide the LLM's classification and score. You can also raise `scoring.min_score_to_export` or add employers to `companies.blacklist` in `config/intent.yaml`; Python otherwise preserves the LLM's score.
