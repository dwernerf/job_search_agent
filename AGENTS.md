# AGENTS.md — jobagent-local

## Run
```bash
make install            # create venv + pip install -e '.[dev]'
make browsers           # playwright install --with-deps chromium
scripts/run.sh          # activate venv + run agent
scripts/test.sh         # pytest + compileall (make test only runs pytest)
```

## Architecture
Single-process Python app. Entry: `src/jobagent/agent.py:main()` → `JobAgent.run()`.

Key modules (all in `src/jobagent/`):
- `config.py` — strict Pydantic config loading; copies personal intent values into runtime config and derives role/relevance terms from the profile.
- `discover.py` — seed-file and bootstrap URL generation plus startup backlog seeding.
- `db.py` — strict SQLite v3 persistence for `jobs`, `pages`, and the rated backlog; migrates valid v2 databases and synchronizes CSV on open and after each non-empty job save.
- `company_filters.py` — company-blacklist matching only.
- `llm.py` — OpenAI-compatible local LLM client and link-classification prompt rendering.
- `browser.py` — centrally paced Playwright loading, structured fetch errors, anchor extraction, and `JobPosting` JSON-LD extraction.
- `extract.py` — page-text compaction and LLM JSON parsing.
- `urltools.py` — the shared URL-only filtering/canonicalization policy and page-link deduplication.
- `prompts.py` — prompt-template rendering.
- `reporting.py` — structured progress logging. User-facing agent progress goes through `reporter.action()`; `self.logger` is for debug internals.

## Config (never edit profile content in YAML)
- `config/profile.md` — candidate profile and detailed search intent. Its complete text is sent to the LLM; parsed target roles feed bootstrap searches and parsed terms feed text compaction.
- `config/intent.yaml` — personal local-area value, company blacklist, and bootstrap company whitelist.
- `config/config.yaml` — operational knobs and defaults only.
- `config/prompts.yaml` — generic LLM instructions. Never add role-specific content here.
- `config/seeds.txt` — optional starting URLs.

### Config file boundaries (never cross them)
- `load_config()` validates the files separately and copies non-empty intent values into the runtime model; it does not merge YAML dictionaries.

## Key operational facts
- **LLM must be running first.** The agent checks `llm.base_url + /models` on startup; if unavailable it stops with `llm_unavailable_stop` rather than crawling blindly.
- Pipeline: rated seed/bootstrap URL -> browser overview -> URL filter/dedup -> persisted-page and run-local candidate checks -> fetch each unblocked outbound destination context -> LLM `link_classifications` -> persist `job_listing`/`skip` -> score threshold/company blacklist/URL dedup -> jobs; rated `explore` -> backlog.
- LLM batches contain up to `crawler.batch_size_for_llm` successfully fetched, URL-approved destinations. Dropped and failed candidates do not consume slots. Each destination context is compacted directly to `crawler.max_page_context_chars`; source-page body text is not sent to the LLM.
- Outbound destinations are fetched before classification but are not queued unless classified as `explore` at or above `scoring.min_score_to_explore`; their `fit_score` becomes the backlog rating. The threshold is enforced only in Python and is not included in the LLM prompt.
- URL filtering does not inspect link text, block submission endpoints, cap candidates per page, or contain provider-specific rules.
- `run.min_delay_seconds` and `run.max_delay_seconds` control the single pacing interval between actual Playwright navigations.
- Playwright uses the application user agent configured under `app.user_agent`.
- HTTP and navigation failures are logged through structured `BrowserFetchError` diagnostics. Candidate URLs already present in `pages` are dropped before fetching; a run-local requested/final URL set handles additional same-run deduplication. Successfully fetched candidates are persisted only when the LLM classifies them as `job_listing` or `skip`. Fetch failures persist `error:*` markers and are excluded before the LLM call.
- Runtime job acceptance is limited to a successfully fetched destination context, the LLM type/score threshold, company blacklist, and URL deduplication. Location and exclusions remain LLM judgments; Python does not rescore them.
- SQLite schema v3 contains only `jobs`, `pages`, and `backlog`. The backlog fields are `url`, `status`, `queued_at`, `rating`, and `queue_position`; valid v2 databases migrate automatically with rating 80.
- Backlog enqueueing never consults `pages`. Seed/bootstrap URLs are authoritative startup work items with rating 89, while outbound candidate deduplication still happens through `pages` before fetching and LLM classification.
- `run.backlog_order` supports `fifo`, `shuffle`, and `rating`; rating order is descending with FIFO as the tie-breaker. Rediscovering a queued URL retains the maximum rating and its original queue position.
- Successful sources are deleted from backlog without being persisted in `pages`; matching stale page-error markers are removed. Source errors remain in backlog. Startup removes legacy `done` and `skipped_visited` rows.
- Job fields are `url`, `title`, `company`, `location`, `fit_score`, `reason`, `evidence`, `source_key`, `first_seen_at`, `last_seen_at`, and `original_url`. `url` is the final fetched URL; `original_url` is the discovered pre-redirect URL.
- CSV fields are `fit_score`, `title`, `company`, `location`, `url`, `reason`, `evidence`, `source_key`, `first_seen_at`, `last_seen_at`, and `original_url`.
- Database startup and each non-empty `save_jobs()` call rewrite the CSV export from all current jobs.
- `crawler.retry_error_pages` clears transient HTTP page-error markers (408, 425, 429, and 5xx) once at startup; existing page rows are never bypassed continuously during a run.
- `run.reset_backlog_on_start` clears backlog rows only. `run.reset_pages_on_start` clears classification and page-error rows only. Jobs remain in both cases.
- Reset all persisted state with: `rm -f data/jobs.sqlite data/jobs.sqlite-* data/jobs.csv`
- Config path override: `JOBAGENT_CONFIG=/abs/path/to/config/config.yaml python -m jobagent`; keep `intent.yaml` beside it and resolve other configured paths from the containing project root.

## Tests
- Run: `scripts/test.sh` (pytest + compileall)
- `make test` runs pytest only.
- Tests are network-isolated; agent tests use fake browser/LLM implementations.
- `temp_loaded` copies `config/`, selects seed-only mode, sets delays to zero, disables log outputs, and uses temporary data paths.
- New tests should use the `temp_loaded` fixture from `tests/conftest.py` for isolated config.
- No linter or formatter is configured.

## Environment
- Python ≥3.11, venv at `.venv` (gitignored)
- Playwright Chromium required (`playwright install --with-deps chromium`)
- LLM server must expose OpenAI-compatible `/v1/chat/completions` and `/v1/models`
