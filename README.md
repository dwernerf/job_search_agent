# jobagent-local

`jobagent-local` is a read-only autonomous job-discovery agent for Ubuntu 24.x. It browses public company career pages and public job-portal result pages, extracts likely jobs, scores them against `config/profile.md` with a local OpenAI-compatible LLM server, and autonomously explores the web.

The repo is designed to work with a local llama.cpp server or similar.

## Quick start

```bash
# 1. Install dependencies and browser
make install && make browsers

# 2. Edit config files for your setup
#    - config/profile.md  → your job-search intent (roles, expertise, exclusions)
#    - config/intent.yaml → personal overrides (target city, company whitelist/blacklist)
#    - config/config.yaml → match llm.base_url, context_window_tokens, and other knobs to your server

# 3. Ensure a local LLM server is running (check the port in config/config.yaml)
curl http://127.0.0.1:<PORT>/v1/models

# 4. Run
scripts/run.sh
```

Outputs end up in `data/jobs.csv`, `data/jobs.jsonl`, and `data/jobs.sqlite`.

**Before running:** set `context_window_tokens` in `config/config.yaml` to your server's actual context size. Tune `batch_size_for_llm` to match your context window if you exceed it.

## Core design

These are the main config files to tailor the agent to your needs:

```text
config/profile.md   target roles, aliases, expertise, industries, seniority, exclusions
config/intent.yaml  personal overrides: target city, coordinates, company whitelist/blacklist, languages
config/config.yaml  operational settings: LLM endpoint, token budget, crawl limits, scoring, memory, exploration, logging
config/prompts.yaml generic LLM instructions; no target-role-specific content
config/seeds.txt    optional public starting URLs
```

The agent derives search terms, role-signal terms, positive-fit terms, avoid terms, query vocabulary, and score guardrails from `profile.md`. You should not need to maintain separate role lists in YAML.

## What it does

```text
seed/search URL
  -> open public page in Playwright
  -> extract visible text, links, and structured JobPosting data
  -> rank job/career/discovery links
  -> ask the local LLM whether the loaded page itself is a job-detail page
  -> save jobs only from loaded job-detail pages, not overview/search pages
  -> use overview/search pages only to discover follow-up job-detail links
   -> apply deterministic score/location/safety/company guardrails
   -> skip exploration URLs that visibly point to cities outside the target radius
   -> save matched jobs to SQLite + CSV + JSONL
   -> respect per-source page limits configured in config.yaml
  -> update persistent source memory
  -> prioritize future crawling using learned source quality
  -> optionally ask the LLM for new exploratory search queries
  -> print structured STEP/RESULT lines to the terminal
```

The LLM is used for judgement. Python enforces boundaries: crawl limits, depth limits, URL validation, deduplication, safety filters, radius checks, source-memory scoring, prompt-size control, and persistence.


## Install

From the repo root:

```bash
scripts/install_ubuntu24.sh
```


## Configure

### 1. Edit `config/profile.md`

This is where you describe what you want. It contains normal Markdown sections such as:

```text
Target roles and acceptable titles
Target role signals
Relevant expertise and positive fit factors
Especially relevant industries
Avoid and exclude
```

The parser accepts ordinary bullet lists. For example, if you add a new acceptable title under `Target roles and acceptable titles`, the agent uses it for query generation, link ranking, LLM prompting, heuristic extraction, and score consistency.

### 2. Edit `config/intent.yaml`

Personal overrides that differ between users. Contains:

- `location` — target city, coordinates, radius_km, acceptable languages
- `companies.blacklist` — drop exported jobs from these employers
- `companies.whitelist` — proactively search for these companies (~50% of bootstrap queries)

### 3. Edit `config/config.yaml` for runtime behavior

Key areas to review and adjust:

- **`llm`** — endpoint, model name, `context_window_tokens` (must match your server), timeout, temperature, thinking mode, JSON response format
- **`scoring`** — minimum export score, high-fit threshold
- **`browser`** — headless mode
- **`run`** — backlog reset behavior, ordering, delays, debug mode
- **`crawler`** — per-source page limits, error retry, domain expansion, batch size for LLM
- **`seeding`** — mode (`seeds`, `bootstrap`, or `both`), search URL templates
- **`job_validation`** — whether to require loaded job-detail pages
- **`memory`** — source scoring parameters
- **`exploration`** — enable URL discovery beyond seeds.txt
- **`logging`** — log level, console/file output

**Important:** set `context_window_tokens` to your server's actual context size. If your server has a smaller context, reduce this value. Tune `max_pages_per_source_key` to match your available resources.

The long internal lists that used to live in YAML have moved to Python defaults or are derived from `profile.md`. This avoids maintaining the same role/search/scoring vocabulary in multiple places.

### 4. Optional: edit `config/seeds.txt`

Add public career pages or job-board result pages. Example:

```text
https://some-company.example/careers
https://jobs.lever.co/some-company
https://boards.greenhouse.io/some-company
https://some-job-board.example/jobs/procurement/muenchen
```

If `seeds.txt` is empty, the agent generates simple `Role+City` queries and renders them into the configured `search_url_templates`.

### 5. Optional: edit company filters

Company filtering is in two places: `config/intent.yaml` (blacklist + whitelist) and `config/config.yaml` (validation toggle `drop_if_company_blacklisted`). The blacklist drops matched jobs from unwanted employers. Leave it empty unless needed.

The default `run.reset_backlog_on_start: true` clears stale queued URLs at the start of each run, but keeps saved jobs and learned source memory.

## Run

Start your local LLM server first, then run. By default the agent checks `llm.base_url + /models` before opening browser pages. If the model server is down, the run stops with a clear `llm_unavailable_stop` message instead of crawling hundreds of pages without LLM judgement.

```bash
scripts/run.sh
```

Or:

```bash
. .venv/bin/activate
python -m jobagent
```

Use another config file with:

```bash
JOBAGENT_CONFIG=/absolute/path/to/config.yaml python -m jobagent
```

## Terminal output

The agent now prints structured progress lines at process completion points rather than interval-based summaries. Normal output uses `logging.level: info`; set it to `debug` to also see skipped URLs and finer detail.

Logging is configured in `config/config.yaml` under `logging:` (level, console, file).

## Where the agent accumulates experience

Experience is accumulated in `data/jobs.sqlite`. The most important tables are:

| Table | Purpose |
|---|---|
| `source_memory` | Persistent quality score per source, usually `domain/path-prefix`. This is the main learned memory. |
| `pages` | Every visited page, final URL, status, title, source key, and how many jobs it produced. |
| `jobs` | Saved jobs and their final calibrated score. Exported to CSV/JSONL. |
| `backlog`    | Queue of URLs to visit, and discovery reason. |
| `queries` | Bootstrap and LLM-generated search queries, reuse count, and metadata. |
| `events` | Optional structured events. |

The source memory update happens after each page:

```text
jobs found          -> source score increases
high-fit jobs       -> source score increases more
high LLM quality    -> source score increases
no jobs             -> source score decreases slightly
errors              -> source score decreases
robots blocks       -> source score decreases
repeated no-job run -> additional penalty
```

At the start of each run, memory is slightly decayed toward the neutral initial score. This prevents ancient evidence from dominating forever.

The crawler priority combines:

```text
source memory score
- depth penalty
+ link hint score
+ small random jitter
```

Good sources rise in the queue. Weak or failing sources fall. Very weak sources are skipped once they fall below `memory.blacklist_below_score`.

Memory is also sent back into the LLM prompt as a compact summary of good sources, weak sources, and recent matched jobs. That is the autoregressive loop: the agent's previous browsing results influence future browsing, query generation, and source prioritization.

Inspect memory:

```bash
sqlite3 data/jobs.sqlite '
select source_key, score, visits, jobs_found, high_fit_jobs, no_job_streak, notes
from source_memory
order by score desc
limit 25;
'
```

Reset all learned memory and results:

```bash
rm -f data/jobs.sqlite data/jobs.sqlite-* data/jobs.csv data/jobs.jsonl
```

## Detail-page-only extraction

By default, a row is saved to `jobs.csv` only when the browser has actually loaded a concrete job-detail page. Listing pages, search pages, city pages, and overview pages are used only for discovery.

This is controlled by `job_validation.require_loaded_job_detail_page` in `config/config.yaml`.

With this enabled, the LLM must return the loaded page's `Final URL` as the job URL. Candidate links from an overview page go into `follow_urls`; they are not saved directly as jobs. This prevents CSV links from leading back to search/result pages.

## Fit score in the CSV

`jobs.csv` does not calculate the score. It exports the score already saved in SQLite.

There are two origins:

| `score_source` | Meaning |
|---|---|
| `llm` | The local model produced the raw fit score. |
| `llm_guarded` | The model produced the score, then Python capped it for consistency. |
| `heuristic_structured` | Optional fallback from structured `schema.org/JobPosting`; disabled by default. |
| `heuristic_link` | Optional fallback from a strong job-detail link; disabled by default. |

The LLM is allowed to generate scores, but deterministic guardrails in `agent.py` and `models.py` cap or drop scores before saving. These guardrails use terms derived from `profile.md`:

```text
missing target-role signal -> cap below save threshold
profile exclusion match    -> cap below save threshold
unclear/wrong location     -> cap or drop
initiative/talent-pool URL -> drop
city/filter title          -> drop
blacklisted company        -> drop
weak evidence/reason       -> cap
```

This prevents unrelated roles from being saved with medium-high scores just because the employer, city, or industry looks attractive.

## Location radius

Location filtering is configured in `config/intent.yaml` under `location:` (target city, coordinates, radius_km) and enforces:

- Non-remote jobs must name a city inside the radius
- Broad-only locations like `Germany`, `Deutschland`, or `Bayern` are insufficient unless the posting says Germany-remote
- Remote jobs are accepted if they can be performed from Germany
- URL text and link text are evaluated independently from the originating search query — a Munich search-results page cannot make a `/pforzheim-...` URL look acceptable

Known Munich-area towns and common outside German cities are stored as internal defaults in `src/jobagent/config.py`. Normal users should not need to override them.

## LLM context window

The token settings are in `config/config.yaml` under `llm:`. The two key values are:

```text
context_window_tokens   → set this to your server's actual context size
output_tokens           → max tokens the model is allowed to produce
```

The script derives the internal prompt budget automatically:

```text
usable prompt budget = context_window_tokens - output_tokens - automatic safety margin
```

If a page is too large, the prompt builder trims page text, profile text, source memory, and candidate links before sending the request. This prevents llama.cpp errors like:

```text
request (...) exceeds the available context size (...)
```

For Qwen thinking models, `thinking_enabled: true` may improve judgement on ambiguous postings, but the default is `false` because strict JSON is more important for this crawler. Turn it on only if your server remains JSON-reliable.

## Outputs

```text
data/jobs.sqlite   full state: jobs, pages, backlog, queries, source memory
data/jobs.csv      spreadsheet-friendly results; `title` is the third column
data/jobs.jsonl    machine-readable results
data/jobagent.log  run log
```

`jobs.csv` and `jobs.jsonl` are checkpointed after each processed page by default, so partial results should appear while the crawler is still running.

Inspect top jobs:

```bash
sqlite3 data/jobs.sqlite '
select fit_score, score_source, title, company, location, posting_language, url
from jobs
order by fit_score desc, last_seen_at desc
limit 25;
'
```

## Tests

Run:

```bash
scripts/test.sh
```

The suite covers config loading, profile-derived vocabulary, URL normalization, safety filtering, multilingual link ranking, LLM JSON parsing, SQLite memory, backlog seeding, follow-link exploration, heuristic fallback extraction, query generation, Munich radius filtering, job validation, prompt budgeting, structured logging, and score guardrails.

Validation performed in the build environment:

```bash
PYTHONPATH=src pytest -q
PYTHONPATH=src python -m compileall -q src tests
```

Tests use mocked browser and LLM components. Live crawling still depends on your network, target websites, and local LLM server.

## Troubleshooting

### `seeded_backlog=0 queued=0`

The agent has no usable starting URLs and did not enqueue bootstrap search URLs. Check `seeding.mode` in `config/config.yaml` — set it to `both` to use both seeds.txt and bootstrap queries. Also check that `config/seeds.txt` contains active URLs. Stale backlog state is normally cleared automatically because `run.reset_backlog_on_start` defaults to `true`. For a completely clean run, remove the database and exported files:

```bash
rm -f data/jobs.sqlite data/jobs.sqlite-* data/jobs.csv data/jobs.jsonl
```

### LLM connection errors

Check the endpoint in `config/config.yaml`, then verify:

```bash
curl <llm.base_url>/v1/models
```

Confirm `llm.base_url`, `llm.model`, and `llm.chat_endpoint` match your server.

### LLM page analysis fails

The agent logs compact details and keeps crawling. Because heuristic extraction is disabled by default, an LLM failure normally means no jobs are saved from that page. Fallback follow-link expansion is disabled by default so an invalid JSON response cannot flood the queue with weak links:

```text
RESULT llm_page_analysis_failed_using_configured_fallback error='LLM response was not valid JSON: ...'
```

Set `llm.thinking_enabled: false` if JSON reliability is poor. Enable `heuristic_extraction.enabled: true` only if you explicitly want fallback extraction.

### Too many irrelevant jobs

Edit `config/profile.md`, especially:

```text
Target roles and acceptable titles
Target role signals
Avoid and exclude
```

The guardrails are derived from those sections. You usually should not edit YAML lists or code for this.
