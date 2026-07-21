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
- `llm.py` — OpenAI-compatible local LLM client, link-classification prompt rendering, and context-size estimation.
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
- Pipeline: rated seed/bootstrap URL -> browser overview -> URL filter/dedup -> atomically claim unseen candidates in `pages` -> fetch each newly claimed outbound destination context -> LLM `link_classifications` -> score threshold/company blacklist/URL dedup -> jobs; rated `explore` -> backlog.
- Outbound destinations are fetched before classification but are not queued unless classified as `explore`; their `fit_score` becomes the backlog rating.
- URL filtering does not inspect link text, block submission endpoints, cap candidates per page, or contain provider-specific rules.
- `run.min_delay_seconds` and `run.max_delay_seconds` control the single pacing interval between actual Playwright navigations.
- Playwright uses the application user agent configured under `app.user_agent`.
- HTTP and navigation failures are logged through structured `BrowserFetchError` diagnostics. Top-level statuses retain the `error:` prefix for configured later-run retries. Every candidate is inserted into `pages` with `status=ok` before its context fetch. Existing page entries are dropped except transient candidate HTTP failures (408, 425, 429, and 5xx) when retries are enabled. Candidate HTTP failures update the marker and are excluded before the LLM call.
- Runtime job acceptance is limited to a successfully fetched destination context, the LLM type/score threshold, company blacklist, and URL deduplication. Location and exclusions remain LLM judgments; Python does not rescore them.
- SQLite schema v3 contains only `jobs`, `pages`, and `backlog`. The backlog fields are `url`, `status`, `queued_at`, `rating`, and `queue_position`; valid v2 databases migrate automatically with rating 80.
- Backlog enqueueing never consults `pages`. Seed/bootstrap URLs are authoritative startup work items with rating 80, while outbound candidate deduplication still happens through `pages` before fetching and LLM classification.
- `run.backlog_order` supports `fifo`, `shuffle`, and `rating`; rating order is descending with FIFO as the tie-breaker. Rediscovering a queued URL retains the maximum rating and its original queue position.
- Successful sources are atomically recorded in `pages` and deleted from backlog; source errors remain in backlog. Startup removes legacy `done` and `skipped_visited` rows.
- Job fields are `url`, `title`, `company`, `location`, `fit_score`, `reason`, `evidence`, `source_key`, `first_seen_at`, `last_seen_at`, and `original_url`. `url` is the final fetched URL; `original_url` is the discovered pre-redirect URL.
- CSV fields are `fit_score`, `title`, `company`, `location`, `url`, `reason`, `evidence`, `source_key`, `first_seen_at`, `last_seen_at`, and `original_url`.
- Database startup and each non-empty `save_jobs()` call rewrite the CSV export from all current jobs.
- `run.reset_backlog_on_start` clears backlog rows only. Jobs and visited-page rows remain.
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
