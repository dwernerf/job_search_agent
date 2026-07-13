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
- `config.py` — Pydantic models + YAML loading. `load_config()` merges `config.yaml` defaults with `config/intent.yaml` personal overrides.
- `discover.py` — backlog seeding from seeds.txt/profile, URL enqueue, exploration scope filtering.
- `db.py` — SQLite persistence. `export_csv`/`export_jsonl` export all rows unfiltered.
- `scoring.py` — deterministic cap/drop guardrails applied **after** LLM scoring. Never trust raw LLM scores.
- `company_filters.py` — blacklist matching only.
- `location.py` — 30 km Munich radius enforcement.
- `llm.py` — OpenAI-compatible local LLM client. Prompt rendering + token budgeting.
- `browser.py` — Playwright wrapper.
- `extract.py` — link ranking.
- `prompts.py` — template rendering.

## Config (never edit profile content in YAML)
- `config/profile.md` — **single source of truth** for job-search intent. Roles, signals, expertise, exclusions, industries. All query vocabulary, score guardrails, and positive-fit terms derived from this file.
- `config/intent.yaml` — personal overrides: blacklist, target city/coords/radius, company whitelist. Values are read directly from `IntentConfig` — **never** merged into `config.yaml` fields.
- `config/config.yaml` — operational knobs and defaults.
- `config/prompts.yaml` — **generic** LLM instructions. Never add role-specific content here.
- `config/seeds.txt` — optional starting URLs.

### Config file boundaries (never cross them)
- `intent.yaml` and `config.yaml` must not share parameter names. No YAML merge overwrites.

## Key operational facts
- **LLM must be running first.** The agent checks `llm.base_url + /models` on startup; if unavailable it stops with `llm_unavailable_stop` rather than crawling blindly.
- `job_validation.require_loaded_job_detail_page: true` — CSV/JSONL rows are saved **only** from actually loaded job-detail pages. Overview/search pages contribute follow URLs only.
- `scoring.py` applies deterministic cap/drop rules on every LLM score before saving.
- Location filter defaults to 30 km around Munich (48.137154, 11.576124). Non-remote jobs must name a city inside the radius. Broad locations ("Germany", "Bayern") are insufficient unless the posting says Germany-remote.
- `data/jobs.sqlite` persists all state (jobs, pages, source memory, backlog, queries). Reset with: `rm -f data/jobs.sqlite data/jobs.sqlite-* data/jobs.csv data/jobs.jsonl`
- `run.reset_backlog_on_start: true` (default) clears stale queue URLs each run but keeps source memory and saved jobs.
- Config path override: `JOBAGENT_CONFIG=/abs/path/to/config.yaml python -m jobagent`

## Tests
- Run: `scripts/test.sh` (pytest + compileall)
- Tests use mocked browser and LLM. No live crawling.
- Test config copies `config/config.yaml` with `max_pages=4`, delays=0, logging disabled.
- New tests should use the `temp_loaded` fixture from `tests/conftest.py` for isolated config.
- No linter or formatter is configured.

## Environment
- Python ≥3.11, venv at `.venv` (gitignored)
- Playwright Chromium required (`playwright install --with-deps chromium`)
- LLM server must expose OpenAI-compatible `/v1/chat/completions` and `/v1/models`
