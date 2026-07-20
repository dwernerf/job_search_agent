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
- `db.py` — strict SQLite v1 persistence for `jobs`, `pages`, and `backlog`; synchronizes CSV/JSONL on open and after each non-empty job save.
- `company_filters.py` — company-blacklist matching only.
- `llm.py` — OpenAI-compatible local LLM client, link-classification prompt rendering, and context-size estimation.
- `browser.py` — Playwright page loading, anchor extraction, and `JobPosting` JSON-LD extraction.
- `extract.py` — page-text compaction and LLM JSON parsing.
- `prompts.py` — prompt-template rendering.
- `reporting.py` — structured progress logging. User-facing agent progress goes through `reporter.action()`; `self.logger` is for debug internals.

## Config (never edit profile content in YAML)
- `config/profile.md` — candidate profile and detailed search intent. Its complete text is sent to the LLM; parsed target roles feed bootstrap searches and parsed terms feed text compaction.
- `config/intent.yaml` — personal short local-area/language values, company blacklist, and bootstrap company whitelist.
- `config/config.yaml` — operational knobs and defaults only.
- `config/prompts.yaml` — generic LLM instructions. Never add role-specific content here.
- `config/seeds.txt` — optional starting URLs.

### Config file boundaries (never cross them)
- `load_config()` validates the files separately and copies non-empty intent values into the runtime model; it does not merge YAML dictionaries.

## Key operational facts
- **LLM must be running first.** The agent checks `llm.base_url + /models` on startup; if unavailable it stops with `llm_unavailable_stop` rather than crawling blindly.
- Pipeline: seed/bootstrap URL -> browser overview -> fetch each outbound destination context -> LLM `link_classifications` -> score threshold/company blacklist/URL dedup -> jobs; `explore` -> URL-only backlog.
- Outbound destinations are fetched before classification but are not queued unless classified as `explore`.
- Runtime job acceptance is limited to the LLM type/score threshold, company blacklist, and URL deduplication. Location and exclusions remain LLM judgments; Python does not rescore them.
- SQLite schema v1 contains only `jobs`, `pages`, and `backlog`. The backlog fields are `url`, `status`, and `queued_at`.
- Job fields are `url`, `title`, `company`, `location`, `fit_score`, `reason`, `evidence`, `source_key`, `first_seen_at`, and `last_seen_at`.
- CSV/JSONL fields are `fit_score`, `title`, `company`, `location`, `url`, `reason`, `evidence`, `source_key`, `first_seen_at`, and `last_seen_at`.
- Database startup and each non-empty `save_jobs()` call rewrite both exports from all current jobs.
- LLM `source_quality` and `source_notes` affect reporting only, not persistence or queue order.
- `run.reset_backlog_on_start` clears backlog rows only. Jobs and visited-page rows remain.
- Reset all persisted state with: `rm -f data/jobs.sqlite data/jobs.sqlite-* data/jobs.csv data/jobs.jsonl`
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
