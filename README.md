# jobagent-local

`jobagent-local` is a read-only autonomous job-discovery agent for Ubuntu 24.x. It browses public company career pages and public job-portal result pages, extracts likely jobs, scores them against `config/profile.md` with a local OpenAI-compatible LLM server, and learns which sources are worth revisiting.

The repo is designed for a local `llama.cpp` / Qwen server. It does not require command-line arguments.

## Core design

The configuration has been simplified so that there is one source of truth for job-search intent:

```text
config/profile.md   target roles, aliases, expertise, industries, seniority, exclusions
config/config.yaml  operational settings only: LLM endpoint, crawl limits, search mode, radius, memory, logging
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
  -> update persistent source memory
  -> prioritize future crawling using learned source quality
  -> optionally ask the LLM for new exploratory search queries
  -> print structured STEP/RESULT lines to the terminal
```

The LLM is used for judgement. Python enforces boundaries: crawl limits, depth limits, URL validation, deduplication, safety filters, radius checks, source-memory scoring, prompt-size control, and persistence.

## Safety boundaries

The agent is read-only. It does not log in, submit applications, bypass CAPTCHA, auto-apply, upload documents, or click submit buttons.

`robots.txt` handling is controlled by:

```yaml
crawler:
  respect_robots_txt: false
```

Set this to `true` if you want stricter crawler behavior.

## Install

From the repo root:

```bash
scripts/install_ubuntu24.sh
```

Manual equivalent:

```bash
sudo apt update
sudo apt install -y python3-venv python3-pip sqlite3
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
pip install -e '.[dev]'
playwright install --with-deps chromium
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

### 2. Edit `config/config.yaml` only for runtime behavior

Important settings:

```yaml
llm:
  base_url: http://127.0.0.1:8080/v1
  context_window_tokens: 12000
  output_tokens: 5000
  thinking_enabled: false
  require_available_on_start: true
  stop_run_on_connection_error: true

run:
  reset_frontier_on_start: true
  max_pages: 80
  max_depth: 3
  min_delay_seconds: 0.2
  max_delay_seconds: 0.8

location_radius:
  target_city: Munich
  latitude: 48.137154
  longitude: 11.576124
  radius_km: 30.0
  allowed_country_url_segments: [de, de-de, de_de, deutschland, germany]
  blocked_country_url_segments: [at, ch, cn, tw, zh_cn, zh_tw, fr, it, es, nl, pl, cz]

matching:
  min_fit_score_to_save: 55
  high_fit_score: 80

companies:
  blacklist: []

heuristic_extraction:
  enabled: false

job_validation:
  require_loaded_job_detail_page: true
```

The long internal lists that used to live in YAML have moved to Python defaults or are derived from `profile.md`. This avoids maintaining the same role/search/scoring vocabulary in multiple places.

### 3. Optional: edit `config/seeds.txt`

Add public career pages or job-board result pages. Example:

```text
https://some-company.example/careers
https://jobs.lever.co/some-company
https://boards.greenhouse.io/some-company
https://some-job-board.example/jobs/procurement/muenchen
```

If `seeds.txt` is empty, the agent creates bootstrap search URLs from `profile.md` and `config/config.yaml`.

### 4. Optional: edit company filters

`config/config.yaml` contains a small company filter section:

```yaml
companies:
  blacklist: []
```

`blacklist` drops matched jobs from unwanted employers. Leave it empty unless needed.

The default `run.reset_frontier_on_start: true` clears stale queued URLs at the start of each run, but keeps saved jobs and learned source memory.

LinkedIn is explicitly included in the default search URL templates and is not globally blocked. Public LinkedIn job-search or job-view URLs can be used as seeds or discovered URLs. LinkedIn signup, legal, authwall, and login URLs are blocked because they waste crawl budget and cannot produce jobs. The agent still does not log in, bypass CAPTCHA, or submit forms.

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

Example:

```text
STEP run_start local_area='Munich, Germany' roles='...' max_pages='80' max_depth='3'
RESULT seed_frontier added='42' queued='42'
STEP open_page depth='1' priority='56.42' source_key='example.com/jobs' url='https://example.com/jobs'
RESULT page_fetched title='Search results' candidate_links='18' final_url='https://example.com/jobs'
RESULT page_analyzed jobs='0' saved='0' high_fit='0' source_quality='55' source_notes='Overview page; follow job detail links.'
RESULT enqueue_exploration added='7' next_depth='2' queued='48'
RESULT page_complete saved='0' kept_jobs='0' enqueued='7' queued='48' source_quality='55' title='Search results'
RESULT export_results reason='page' csv='data/jobs.csv' jsonl='data/jobs.jsonl'
RESULT run_complete pages_done='80' jobs_saved_total='12' queued='130'
RESULT run_summary pages=80 jobs_seen=12 jobs_saved=12 high_fit_jobs=4 generated_queries=6 enqueued_urls=246 blocked=0 errors=3 avg_source_quality=58.2 queued=130 elapsed_seconds=912.4 actions=411
```

Configure only this:

```yaml
logging:
  # info or debug
  level: info
  console: true
  file: true
```

## Where the agent accumulates experience

Experience is accumulated in `data/jobs.sqlite`. The most important tables are:

| Table | Purpose |
|---|---|
| `source_memory` | Persistent quality score per source, usually `domain/path-prefix`. This is the main learned memory. |
| `pages` | Every visited page, final URL, status, title, source key, and how many jobs it produced. |
| `jobs` | Saved jobs and their final calibrated score. Exported to CSV/JSONL. |
| `frontier` | Queue of URLs to visit, with priority and discovery reason. |
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

This is controlled by:

```yaml
job_validation:
  require_loaded_job_detail_page: true
```

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

The LLM is allowed to generate scores, but `src/jobagent/scoring.py` applies deterministic guardrails before saving. These guardrails use terms derived from `profile.md`:

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

## Prompt files are generic

`config/prompts.yaml` intentionally contains no procurement-, purchasing-, optics-, laser-, or Munich-specific role instructions. It only says how to extract jobs, score against the profile, return JSON, and generate search queries.

All target-specific information comes from:

```text
config/profile.md
config/config.yaml location/radius settings
SQLite source memory
current page text and candidate links
```

## Location radius

The default build enforces:

```yaml
location_radius:
  enabled: true
  target_city: Munich
  radius_km: 30.0
  hard_drop_outside_radius: true
  require_location_for_non_remote: true
  allow_remote_if_country_match: true
```

Non-remote jobs must name a city/location inside the radius. Broad-only locations such as `Germany`, `Deutschland`, `Bavaria`, or `Bayern` are treated as insufficient unless the posting clearly allows Germany-remote work.

Known Munich-area towns and common outside German cities are stored as internal defaults in `src/jobagent/config.py`. You can still override `location_radius.city_coordinates` in YAML if needed, but normal users should not need to.

The same radius logic is also used before opening exploration URLs. URL text and link text are evaluated independently from the originating search query, so a Munich search-results page can no longer make `/pforzheim-technischer-einkaeufer...`, `/buchloe-...`, or `/fridolfing-...` look acceptable. Unknown-location company root pages are still allowed because many career pages do not encode a city in the URL.

## LLM context window

The visible token settings are deliberately simple:

```yaml
llm:
  context_window_tokens: 12000
  output_tokens: 5000
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
data/jobs.sqlite   full state: jobs, pages, frontier, queries, source memory
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

The suite covers config loading, profile-derived vocabulary, URL normalization, safety filtering, multilingual link ranking, LLM JSON parsing, SQLite memory, frontier seeding, follow-link exploration, heuristic fallback extraction, query generation, Munich radius filtering, job validation, prompt budgeting, structured logging, and score guardrails.

Validation performed in the build environment:

```text
PYTHONPATH=src pytest -q
 76 passed

PYTHONPATH=src python -m compileall -q src tests
compileall_ok
```

Tests use mocked browser and LLM components. Live crawling still depends on your network, target websites, and local LLM server.

## Troubleshooting

### `seeded_frontier=0 queued=0`

The agent has no usable starting URLs and did not enqueue bootstrap search URLs. Check:

```yaml
exploration:
  seeding_mode: both
```

Also check that `config/seeds.txt` contains active URLs. Stale frontier state is normally cleared automatically because `run.reset_frontier_on_start` defaults to `true`. For a completely clean run, remove the database and exported files:

```bash
rm -f data/jobs.sqlite data/jobs.sqlite-* data/jobs.csv data/jobs.jsonl
```

### LLM connection errors

Check:

```bash
curl http://127.0.0.1:8080/v1/models
```

Then confirm `llm.base_url`, `llm.model`, and `llm.chat_endpoint`.

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
