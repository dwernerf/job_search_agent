# jobagent-local

`jobagent-local` is a read-only autonomous job-discovery agent for Ubuntu 24.x. It browses public company career pages and public job-portal result pages, ranks candidate links against `config/profile.md` with a local OpenAI-compatible LLM, and learns which sources are worth revisiting.

The agent is designed for a local llama.cpp server or similar. It does not require command-line arguments.

## Quick start

```bash
# 1. Install dependencies and browser
make install && make browsers

# 2. Edit the three config files
#    config/profile.md   → your job-search intent (roles, expertise, exclusions)
#    config/intent.yaml  → personal overrides (city, company whitelist/blacklist)
#    config/config.yaml  → match llm.base_url, context_window_tokens to your server

# 3. Start your local LLM server, then check the endpoint in config/config.yaml
curl http://127.0.0.1:<PORT>/v1/models

# 4. Run
scripts/run.sh
```

Outputs end up in `data/jobs.csv`, `data/jobs.jsonl`, and `data/jobs.sqlite`.

**Before running:** set `context_window_tokens` in `config/config.yaml` to your server's actual context size. Tune `batch_size_for_llm` if you exceed it.

## Core design

The agent uses five config files:

| File | Purpose |
|---|---|
| `config/profile.md` | Single source of truth for roles, signals, expertise, industries, exclusions |
| `config/intent.yaml` | Personal overrides: target city, coordinates, company whitelist/blacklist, languages |
| `config/config.yaml` | Operational knobs: LLM endpoint, token budget, crawl limits, scoring, memory, exploration, logging |
| `config/prompts.yaml` | Generic LLM instructions — no role-specific content |
| `config/seeds.txt` | Optional starting URLs (company career pages, job-board results) |

All role vocabulary, query generation, and score guardrails are derived from `profile.md`. You should not need to maintain separate lists in YAML.

## What it does

The agent follows a single main loop:

1. **Health check** — verifies the local LLM is reachable before opening any pages
2. **Backlog seeding** — populates the URL queue from seeds.txt, bootstrap queries, or both (see below)
3. **Fetch page** — opens an overview/search/listing page in Playwright
4. **Extract links + page_context** — collects candidate URLs and fetches brief text extracts for each
5. **Batch-classify links** — sends all links-with-context to the LLM; each link is tagged as `job_listing`, `explore`, or `skip`
6. **Process results** — `job_listing` links are saved as jobs; `explore` links are enqueued for later
7. **Update memory** — source quality scores are adjusted based on how many jobs each page produced
8. **Repeat** — until the backlog is exhausted or resource limits are reached

The LLM makes all judgment calls. Python enforces boundaries: per-source page limits, URL validation, deduplication, safety filters, radius checks, and prompt-size control.

### Seeding vs. bootstrapping searches

The `seeding.mode` setting in `config/config.yaml` controls how the URL queue is populated at startup:

- **`seeds`** — populate only from `config/seeds.txt`. Use when you have a curated list of career pages or job-board URLs to start from.
- **`bootstrap`** — generate `Role+City` queries from `profile.md` and render them into `search_url_templates`. Use when you want the agent to discover sources autonomously.
- **`both`** — combine seeds.txt URLs and bootstrap queries. This is the default.

The `search_url_templates` in `config/config.yaml` define which search engines to query. By default it uses Brave Search. Search engines might enforce Bot checks which is currently not somthing the agent can circumvent.


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

The parser accepts ordinary bullet lists. Adding or removing items in any section updates query generation, link ranking, LLM prompting, and score consistency automatically.

### 2. Edit `config/intent.yaml`

Personal overrides. Contains:

- `location` — target city, coordinates, radius_km, acceptable languages
- `companies.blacklist` — drop exported jobs from these employers
- `companies.whitelist` — proactively search for these companies (~50% of bootstrap queries)

### 3. Edit `config/config.yaml`

Key areas to review:

- **`llm`** — endpoint, model name, `context_window_tokens`, timeout, temperature, thinking mode, JSON response format
- **`scoring`** — minimum export score, high-fit threshold
- **`browser`** — headless mode
- **`run`** — backlog reset, ordering, delays, debug mode
- **`crawler`** — per-source page limits, error retry, domain expansion, batch size
- **`seeding`** — mode, search URL templates
- **`job_validation`** — whether to require the LLM to return the current page URL (not invented URLs)
- **`memory`** — source scoring parameters
- **`exploration`** — enable URL discovery beyond seeds.txt
- **`logging`** — log level, console/file output

Match `context_window_tokens` to your server's actual context size. If your server has a smaller context, reduce this value. Tune `max_pages_per_source_key` to match your available resources.

### 4. Optional: edit `config/seeds.txt`

Add public career pages or job-board result pages. Example:

```text
https://some-company.example/careers
https://jobs.lever.co/some-company
https://boards.greenhouse.io/some-company
https://some-job-board.example/jobs/procurement/muenchen
```

## Run

Start your local LLM server first. The agent checks `llm.base_url + /models` on startup and stops with `llm_unavailable_stop` if the server is unreachable.

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



## Where the agent accumulates experience

Experience is stored in `data/jobs.sqlite`. Key tables:

| Table | Purpose |
|---|---|
| `source_memory` | Persistent quality score per source (usually `domain/path-prefix`) — the main learned memory |
| `pages` | Every visited page: final URL, status, title, source key, jobs produced |
| `jobs` | Saved jobs and their final calibrated score |
| `backlog` | Queue of URLs to visit and discovery reason |
| `events` | Structured log events |

The source memory updates after each page:

- jobs found → score increases
- high-fit jobs → score increases more
- no jobs → score decreases slightly
- errors / robots blocks → score decreases
- repeated no-job runs → additional penalty

At the start of each run, scores decay slightly toward the neutral initial score to prevent ancient evidence from dominating.

Crawler priority combines: `source score - depth penalty + link hint score + random jitter`. Good sources rise in the queue. Weak sources fall. Sources below `memory.blacklist_below_score` are skipped.

Memory is also sent back into the LLM prompt as a compact summary of good sources, weak sources, and recent matched jobs — the autoregressive loop where past results influence future browsing and query generation.

Inspect memory:

```bash
sqlite3 data/jobs.sqlite '
select source_key, score, visits, jobs_found, high_fit_jobs, no_job_streak, notes
from source_memory order by score desc limit 25;
'
```

Reset all memory and results:

```bash
rm -f data/jobs.sqlite data/jobs.sqlite-* data/jobs.csv data/jobs.jsonl
```

## Job detail validation

`job_validation.require_loaded_job_detail_page` in `config/config.yaml` controls how job URLs are validated. When enabled, the LLM prompt instructs the model to return the **current page's URL** as the job URL — not candidate links from the page. This prevents the model from inventing URLs or returning search/result pages.

Candidate links from overview pages go into `follow_urls` and are not saved directly as jobs. The model must confirm the current page looks like a concrete posting before its URL is accepted.

## Fit score in the CSV

`jobs.csv` exports the `fit_score` saved in SQLite. The score comes from the LLM and is capped by deterministic guardrails in `agent.py` and `models.py` before saving. These guardrails use terms derived from `profile.md`:

- missing target-role signal → cap below save threshold
- profile exclusion match → cap below save threshold
- unclear or wrong location → cap or drop
- initiative / talent-pool URL → drop
- blacklisted company → drop
- weak evidence or reason → cap

This prevents unrelated roles from being saved with medium-high scores just because the employer or city looks attractive.

## Location radius

Location filtering is configured in `config/intent.yaml` under `location:` (target city, coordinates, radius_km). Enforces:

- Non-remote jobs must name a city inside the radius
- Broad-only locations like `Germany`, `Deutschland`, or `Bayern` are insufficient unless the posting says Germany-remote
- Remote jobs are accepted if they can be performed from Germany
- URL text and link text are evaluated independently from the originating search query — a Munich search-results page cannot make a `/pforzheim-...` URL look acceptable

## LLM context window

The token settings are in `config/config.yaml` under `llm:`:

- `context_window_tokens` — set to your server's actual context size
- `thinking_enabled` — set to `true` only if your server returns clean JSON; strict JSON is more important than reasoning for this crawler

The agent derives an internal prompt budget automatically (`context_window_tokens - output_tokens - safety margin`). If a page is too large, the agent trims page text, profile text, source memory, and candidate links to fit.

## Outputs

| File | Description |
|---|---|
| `data/jobs.sqlite` | Full state: jobs, pages, backlog, queries, source memory |
| `data/jobs.csv` | Spreadsheet-friendly results |
| `data/jobs.jsonl` | Machine-readable results |
| `data/jobagent.log` | Run log |

CSV and JSONL are checkpointed after each page, so partial results appear while the crawler runs.

Inspect top jobs:

```bash
sqlite3 data/jobs.sqlite '
select fit_score, score_source, title, company, location, posting_language, url
from jobs order by fit_score desc, last_seen_at desc limit 25;
'
```

## Tests

```bash
scripts/test.sh
```

The suite uses mocked browser and LLM components. It covers config loading, profile-derived vocabulary, URL normalization, safety filtering, multilingual link ranking, LLM JSON parsing, SQLite memory, backlog seeding, exploration, query generation, radius filtering, job validation, prompt budgeting, structured logging, and score guardrails.

Run in your environment:

```bash
PYTHONPATH=src pytest -q
PYTHONPATH=src python -m compileall -q src tests
```

## Troubleshooting

### `seeded_backlog=0 queued=0`

The agent has no usable starting URLs. Check `seeding.mode` in `config/config.yaml` — set to `both` to use seeds.txt and bootstrap queries. Also verify `config/seeds.txt` contains active URLs. Stale backlog is cleared automatically when `run.reset_backlog_on_start` is true (default). For a completely clean run:

```bash
rm -f data/jobs.sqlite data/jobs.sqlite-* data/jobs.csv data/jobs.jsonl
```

### LLM connection errors

Check the endpoint in `config/config.yaml`, then verify:

```bash
curl <llm.base_url>/v1/models
```

Confirm `llm.base_url`, `llm.model`, and `llm.chat_endpoint` match your server.

### Too many irrelevant jobs

Edit `config/profile.md`, especially:

- Target roles and acceptable titles
- Target role signals
- Avoid and exclude

The guardrails are derived from these sections. You usually should not edit YAML for this.
