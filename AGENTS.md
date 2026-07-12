# AGENTS.md — jobagent-local

## Run
```bash
scripts/run.sh          # activate venv + run agent
scripts/test.sh         # pytest + compileall
make test               # same as scripts/test.sh
```

## Architecture
- `src/jobagent/agent.py` — main crawl loop (`JobAgent` class, `main()` entry)
- `src/jobagent/browser.py` — Playwright session wrapper
- `src/jobagent/llm.py` — OpenAI-compatible local LLM client
- `src/jobagent/discover.py` — frontier seeding, URL enqueue, exploration scope
- `src/jobagent/extract.py` — link ranking
- `src/jobagent/scoring.py` — deterministic score guardrails (applied after LLM scoring)
- `src/jobagent/db.py` — SQLite schema: `jobs`, `pages`, `frontier`, `source_memory`, `queries`
- `src/jobagent/location.py` — Munich 30 km radius enforcement
- `src/jobagent/company_filters.py` — whitelist/blacklist matching

## Config (two files)
- `config/profile.md` — **only place to edit job-search intent** (roles, signals, exclusions, industries). The agent derives all query vocab, score guardrails, and positive-fit terms from this file.
- `config/config.yaml` — operational settings only (LLM endpoint, crawl limits, radius, memory, logging). Default LLM: `http://127.0.0.1:8087/v1`.
- `config/seeds.txt` — optional starting URLs (company career pages, job-board results).
- `config/prompts.yaml` — generic LLM prompt templates; never add role-specific content here.

## Key operational facts
- **LLM must be running first.** The agent checks `llm.base_url + /models` on startup; if unavailable it stops with `llm_unavailable_stop` rather than crawling blindly.
- `run.reset_frontier_on_start: true` (default) clears stale queue URLs each run but keeps source memory and saved jobs.
- `job_validation.require_loaded_job_detail_page: true` — CSV rows are saved **only** from actually loaded job-detail pages. Overview/search pages contribute follow URLs only.
- `scoring.py` applies deterministic cap/drop rules on every LLM score before saving. Never trust an LLM score without guardrails.
- `explore.mode: both | whitelist_only | exploratory_only` controls discovery scope. `whitelist_only` enforces company-scope checks on queued URLs and saved jobs.
- Location filter: non-remote jobs must name a city inside 30 km of Munich. Broad locations ("Germany", "Bayern") are insufficient unless the posting says Germany-remote.
- `data/jobs.sqlite` persists all state (jobs, source memory, frontier, queries). Reset with: `rm -f data/jobs.sqlite data/jobs.sqlite-* data/jobs.csv data/jobs.jsonl`

## Tests
- Run: `scripts/test.sh` (pytest + compileall)
- Tests use mocked browser and LLM components. No live crawling.
- Test config is copied from `config/config.yaml` with `max_pages=4`, delays=0, console/file logging disabled.
- New tests should use the `temp_loaded` fixture from `tests/conftest.py` for isolated config.

## Environment
- Python ≥3.11, venv at `.venv` (gitignored)
- Playwright Chromium required (`playwright install --with-deps chromium`)
- LLM server must expose OpenAI-compatible `/v1/chat/completions` and `/v1/models`
- Config path override: `JOBAGENT_CONFIG=/abs/path/to/config.yaml python -m jobagent`
