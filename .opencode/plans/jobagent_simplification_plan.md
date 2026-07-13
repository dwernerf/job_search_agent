# JobAgent Simplification Workplan

## Goal
Remove scoring complexity and heuristic scoring. Move to a two-stage LLM architecture where the LLM first classifies which links to follow (no pre-filters), then scores each opened page individually. Increase context from 12K to 128K tokens. Remove queue priority, source memory, and robots.txt entirely.

## Architecture: Before vs After

### Before (current)
```
Seed URLs → priority queue (memory-weighted) → open page
  → LLM extracts jobs + recommends follow_urls (8 max)
  → heuristic scoring (disabled)
  → normalize_job_score() applies 12+ deterministic caps
  → job validation (10 gates)
  → save to DB
  → candidate links ranked (link_hint_score) → capped at 80 → enqueued
  → source memory updated → affects next round priority
  → query generation (every 12 pages, max 6 per run)
```

### After (simplified)
```
Seed URLs → FIFO queue → open page
  → Stage 1: LLM classifies ALL candidate links as worth_opening/not_worth_opening
  → follow_urls enqueued for later
  → For each worth_opening:
      → Stage 2: LLM scores the job detail page (fit_score)
      → save if fit_score >= min_fit_score (55)
  → Repeat until queue empty
```

---

## Deleted Files

1. **`src/jobagent/heuristics.py`** — Heuristic scoring. Replaced by LLM-only scoring.
2. **`src/jobagent/scoring.py`** — Deterministic guardrails/caps. Replaced by inlined min-score gate in `_clean_jobs()`.
3. **`src/jobagent/robots.py`** — Robots.txt enforcement. Removed entirely.

---

## Config Changes

### config.yaml — Remove these blocks/fields

| Block / Field | Location |
|---------------|----------|
| `score_consistency:` (entire block) | Line 133 |
| `heuristic_extraction:` (entire block) | Line 194 |
| `location_radius:` (entire block) | Line 228 |
| `max_pages: 500` | `run:` block |
| `max_depth: 3` | `run:` block |
| `max_links_per_page_for_llm: 80` | `crawler:` block |
| `filter_exploration_urls` | `exploration:` block |
| `drop_urls_with_outside_city` | `exploration:` block |
| `require_role_signal_for_job_detail_urls` | `exploration:` block |
| `require_role_signal_for_human_readable_company_job_urls` | `exploration:` block |
| `drop_avoid_only_job_detail_urls` | `exploration:` block |
| `respect_robots_txt` | `crawler:` block |
| `strict_robots_when_unavailable` | `crawler:` block |
| `robots_timeout_seconds` | `crawler:` block |
| `retry_previously_blocked_when_robots_disabled` | `crawler:` block |

### config.yaml — Modify these fields

| Field | Old Value | New Value |
|-------|-----------|-----------|
| `llm.context_window_tokens` | `12000` | `128000` |

### config.yaml — Add this block

```yaml
scoring:
  min_fit_score: 55
  high_fit_score: 80
```

### config.yaml — Keep these

| Field | Reason |
|-------|--------|
| `crawler.max_pages_per_source_key: 25` | Safety limit to prevent one company from consuming the entire run |

---

## Config Model Changes (config.py)

### Delete these classes
- `ScoreConsistencyConfig` (line 503)
- `LocationRadiusConfig` (line 468)
- `HeuristicExtractionConfig` (line 574)

### Delete these helper functions
- `default_city_coordinates()`
- `default_weak_location_terms()`
- `default_remote_terms()`
- `default_broad_location_terms()`
- `_location_terms()`

### Add this class

```python
class ScoringConfig(StrictModel):
    min_fit_score: int = Field(default=55, ge=0, le=100)
    high_fit_score: int = Field(default=80, ge=0, le=100)
```

### Modify `JobAgentConfig` — Remove these fields
- `score_consistency: ScoreConsistencyConfig`
- `location_radius: LocationRadiusConfig`
- `heuristic_extraction: HeuristicExtractionConfig`

### Modify `JobAgentConfig` — Add this field
- `scoring: ScoringConfig`

### Modify `CrawlerConfig` — Remove these fields
- `max_links_per_page_for_llm`
- `respect_robots_txt`
- `strict_robots_when_unavailable`
- `robots_timeout_seconds`
- `retry_previously_blocked_when_robots_disabled`

### Modify `RunConfig` — Remove these fields
- `max_pages`
- `max_depth`

### Modify `ExplorationConfig` — Remove these fields
- `filter_exploration_urls`
- `drop_urls_with_outside_city`
- `require_role_signal_for_job_detail_urls`
- `require_role_signal_for_human_readable_company_job_urls`
- `drop_avoid_only_job_detail_urls`
- `max_generated_queries_per_run`
- `query_generation_every_pages`
- `seed_search_when_empty` (query generation removed entirely)

---

## Prompt Changes (prompts.yaml)

### Stage 1 prompt (page_classification_user / page_analysis_user) — ADD to output schema

```yaml
link_classification:
  - url: "candidate link URL"
    decision: "worth_opening" | "not_worth_opening"
    reason: "why this link is worth opening or not"
```

Add instructions to the prompt:
> For each candidate link, classify it as "worth_opening" or "not_worth_opening" with a brief reason. All classified as "worth_opening" will be opened and analyzed individually.

### Remove from output schema
- `score_source` field from job entries

### Stage 2 prompt (job scoring)
- Same `page_analysis_user` template — no changes needed to job scoring logic within the prompt. The LLM still assigns `fit_score` 0-100 and provides `reason`/`evidence`.

---

## Model Changes (models.py)

### Delete
- `SourceMemoryRow` dataclass
- `clamp_int()` function

### Modify `FrontierItem`
- Remove `priority: float` field

### Modify `JobMatch`
- Remove `score_source` field (or set default to empty string for DB compatibility)
- Remove `score_basis` field (or set default to empty string for DB compatibility)

### Add
- `LinkClassification` dataclass:
  ```python
  @dataclass(slots=True)
  class LinkClassification:
      url: str
      decision: Literal["worth_opening", "not_worth_opening"]
      reason: str
  ```

### Modify `PageDecision`
- Add `link_classification: list[LinkClassification]`
- Keep `jobs`, `follow_urls`, `source_quality`, `source_notes`

---

## Agent Changes (agent.py)

### Delete imports
- `from .heuristics import heuristic_jobs_from_page`
- `from .scoring import normalize_job_score`
- `from .robots import RobotsCache`
- `from .urltools import link_hint_score` (remove if no longer used)

### Delete fields/methods
- `_early_page_decision()` — delete entirely (removed zero-LinkedIn check, keep nothing)
- `RobotsLike` protocol class
- `self.robots` init parameter and field
- `pages_done`, `jobs_saved_total`, `generated_queries` counters
- `_llm_health_check()` — keep for now (useful startup check)

### Rewrite `_clean_jobs()` — inline min-score gate
```python
def _clean_jobs(self, jobs, base_url, allowed_job_urls):
    cleaned = []
    seen = set()
    score_guard_dropped = 0
    
    for job in jobs:
        # ... URL cleanup, safety, dedup, validation gates ...
        
        # Inlined min-score gate (was normalize_job_score)
        score = max(0, min(100, int(job.fit_score)))
        if score < self.config.scoring.min_fit_score:
            score_guard_dropped += 1
            continue
        
        cleaned.append(replace(job, fit_score=score))
    
    if score_guard_dropped:
        self.reporter.action("score_guard_dropped", count=score_guard_dropped)
    
    return cleaned
```

### Rewrite `run()` — new two-stage loop
```python
def run(self) -> int:
    self.reporter.action("run_start", ...)
    
    queue = self._seed_initial_queue()
    source_limit = self.config.crawler.max_pages_per_source_key
    jobs_saved_total = 0
    
    while queue:
        item = queue.popleft()
        
        # Skip visited
        if self.db.was_visited(item.url):
            self.db.mark_frontier(item.url, "skipped_visited")
            continue
        
        # Check source limit
        if self.db.source_visit_count(item.source_key) >= source_limit:
            self.db.mark_frontier(item.url, "skipped_source_limit")
            continue
        
        # Fetch page
        snapshot = browser.fetch(item.url)
        final_url = snapshot.final_url or snapshot.url
        source_domain = domain_from_url(final_url)
        self.db.ensure_source(item.source_key, source_domain)
        
        # Rank candidate links (no length cap — all links sent to LLM)
        candidate_links = rank_candidate_links(snapshot, self.config)
        
        # Stage 1: LLM classifies all links
        classification = llm_client.analyze_page(snapshot, candidate_links, memory_summary="")
        
        # Enqueue follow_urls for later processing
        for url in classification.follow_urls:
            queue.append(url)
        
        source_quality = classification.source_quality
        source_notes = classification.source_notes
        
        # Stage 2: Open each worth_opening, score it
        saved = 0
        for link in classification.worth_opening:
            try:
                detail_snapshot = browser.fetch(link.url)
                detail_final_url = detail_snapshot.final_url or detail_snapshot.url
                
                detail_candidate_links = rank_candidate_links(detail_snapshot, self.config)
                detail_decision = llm_client.analyze_page(detail_snapshot, detail_candidate_links, memory_summary="")
                
                allowed_urls = self._allowed_job_urls(detail_snapshot, detail_candidate_links)
                filtered_jobs = self._clean_jobs(detail_decision.jobs + detail_decision.heuristic_jobs, detail_final_url, allowed_urls)
                
                saved += self.db.save_jobs(filtered_jobs, detail_final_url, item.source_key)
                self.db.update_source_memory(item.source_key, source_domain, "ok", len(filtered_jobs), ...)
                self.db.record_page(item.url, detail_final_url, detail_snapshot.title, item.source_key, 0, "ok", ...)
            except Exception as exc:
                self.reporter.action("stage2_page_failed", url=link.url, error=str(exc))
        
        jobs_saved_total += saved
        
        # Record source visit
        self.db.record_page(item.url, final_url, snapshot.title, item.source_key, 0, "ok", 0, ...)
        self.db.mark_frontier(item.url, "done")
        
        self.logger.debug("page_complete jobs=%s queued=%s", saved, len(queue))
    
    self._checkpoint_export("run_complete", force=True)
    
    self.reporter.action("run_complete", pages_done=..., jobs_saved_total=jobs_saved_total)
    self.reporter.maybe_summary(queued=len(queue), force=True)
    return 0
```

### Modify `_enqueue_exploration()`
- Remove `candidate_links` parameter (no longer needed — all links are in the prompt)
- Remove link expansion logic
- Remove `max_follow_urls_without_llm` logic
- Keep only: enqueue `follow_urls` from classification

### Modify `_allowed_job_urls()`
- Keep as-is (used in Stage 2 for URL validation)

### Remove `score_source` handling in job merging
- Remove `+ heuristic_jobs` from filtered_jobs (heuristics deleted)
- Remove `high_fit` counting that referenced heuristic jobs
- Remove `score_source` defaulting logic for heuristic sources

---

## LLM Client Changes (llm.py)

### Modify `analyze_page()`
- Parse `link_classification` from LLM JSON output
- Return extended result with `worth_opening: list[LinkClassification]`
- Remove dependency on `candidate_links` length limits

### Remove truncation loop in `_render_page_prompts()`
- Delete lines 115-147 (the 18-iteration retry loop)
- No longer needed — 128K context means nothing exceeds budget
- Keep initial render with full text (no truncation)

### Remove `_estimate_tokens` guard in `chat_json()`
- Delete lines 197-200 (the budget check that raises LLMResponseError)
- No longer needed — 128K context means the guard is unnecessary

### Modify `_links_json_for_prompt()`
- Remove `_clip()` on link text/url — send raw values
- Remove link count limit parameter — send all links

### Modify `_clip()` in `_render_page_prompts()`
- Keep `compact_text()` for page text in Stage 1 prompt
- Remove `_clip()` on memory/profile — send at full size

### Modify `_common_values()`
- Remove `location_policy` and `location_radius_policy` (no location config)
- Keep `profile`, `local_area`, `roles`, `language_policy`
- Remove `memory_summary` — no source memory

---

## Extract Changes (extract.py)

### Modify `rank_candidate_links()`
- Remove `max_links_per_page_for_llm` cap — send ALL valid links
- Remove `link_hint_score()` import and use — no link ranking needed
- Keep: URL cleanup, safety check, domain check, dedup, same-domain bonus

### Modify `page_decision_from_dict()`
- Remove `score_source` parsing — no longer part of LLM output schema

### Keep `compact_text()`
- Used in Stage 1 prompt — page text is compacted to readable format

### Remove `score_source` from `parse_json_object` defaults
- `score_source` in `JobMatch` → remove from `page_decision_from_dict()`

---

## Discover Changes (discover.py)

### Delete these functions
- `priority_for_url()`
- `make_frontier_item()` — remove `priority` parameter (keep URL, depth, source_key, reason, discovered_from)
- `should_generate_queries()`
- `_exploration_role_focus_allowed()`
- `_unfocused_listing_url()`
- `_human_readable_job_slug()`
- `exploration_scope_allowed()`
- `exploration_url_allowed()`

### Modify `seed_frontier()`
- Remove `exploration_url_allowed()` check from bootstrap query URLs
- Keep: seed file reading, bootstrap query generation

### Modify `enqueue_links()`
- Remove `exploration_url_allowed()` check
- Keep: URL cleaning, dedup

### Modify `enqueue_query_suggestions()`
- Remove `exploration_url_allowed()` check
- Remove `max_generated_queries_per_run` limit (query generation deleted)

### Simplify `make_frontier_item()`
- Remove `link_hint` parameter
- Remove `priority` from `FrontierItem`
- Keep: url, depth, source_key, reason, discovered_from

---

## Location Changes (location.py)

### Delete these functions/classes
- `evaluate_job_location()`
- `evaluate_exploration_url_location()`
- `LocationVerdict` dataclass
- `is_location_only_title()`
- `haversine_km()`
- `location_radius_summary()`
- `job_location_text()`
- `_city_matches()`
- `_city_match_groups()`
- `_format_city_reason()`

### Keep
- None — delete entire file

---

## Database Changes (db.py)

### Delete these methods
- `apply_decay()`
- `recalibrate_existing_jobs()`
- `source_score()`
- `update_source_memory()`
- `source_visit_count()`
- `reset_frontier()`
- `reapply_guardrails()` (if exists)

### Delete table references
- Remove `source_memory` table DDL and management
- Remove `queries` table DDL and management (query generation deleted)

### Modify `pop_frontier()`
- Change from priority-sorted to simple FIFO dequeue
- Remove `order by score desc` — use `order by queued_at asc` instead

### Modify `export_csv()` / `export_jsonl()`
- Remove `score_source` column from output
- Remove `score_basis` column from output
- Remove `source_memory` related output

### Modify `save_jobs()`
- Remove `score_source` and `score_basis` from INSERT/UPDATE
- Keep `fit_score` as-is

### Modify `init_schema()`
- Remove `source_memory` table
- Remove `queries` table
- Keep: `pages`, `jobs`, `frontier`
- Remove `score_source` and `score_basis` from `jobs` table (or keep as nullable/empty for compatibility)
- Remove `score` from `frontier` table (no priority)

### Modify `ensure_source()`
- Keep for `max_pages_per_source_key` safety check
- Remove reward/decay logic

---

## URL Tools Changes (urltools.py)

### Keep
- `clean_url()` — URL normalization
- `denied_by_safety()` — safety URL blocking
- `source_key()` — source identification for safety limit
- `domain_from_url()` — domain extraction
- `career_candidate_urls()` — career path URL generation

### Delete
- `link_hint_score()` — no longer needed (no link ranking)

### Keep unchanged
- `query_slug()`, `render_query_url()`, `same_domain()`, `root_url()` — used by seed/query generation

---

## Language Changes (language.py)

### Delete
- `_role_query_text()` helper (no longer needed without role signal filter)

### Keep
- `multilingual_role_terms()`
- `multilingual_job_terms()`
- `bootstrap_template_values()`
- `language_policy_summary()`

---

## Company Filters Changes (company_filters.py)

### Keep unchanged
- `normalize_text()`, `compact_text()`, `company_aliases()`
- `company_matches_text()`
- `match_blacklist_company()`
- `CompanyMatch` dataclass

---

## Reporting Changes (reporting.py)

### Remove reporter actions that reference deleted concepts
- `score_guard` — simplify to `score_guard_dropped`
- `heuristic_jobs` — remove
- `source_memory_decay` — remove
- `recalibrate` — remove
- `query_generation` — remove

### Keep
- All page/open/follow/saved actions

---

## Profile Knowledge Changes (profile_knowledge.py)

### Minimal changes — mostly keep as-is
- Keep `extract_profile_knowledge()` — profile is still needed for LLM prompt
- The profile-derived terms still feed into the LLM prompt

---

## Browser Changes (browser.py)

### Keep unchanged
- All browser/fetch logic remains the same

---

## Test Changes (tests/)

### Update tests to match new architecture
- Remove tests for deleted modules: `test_heuristics.py`, `test_scoring.py`, `test_robots.py`, `test_location.py`
- Update tests for `agent.py` — new two-stage loop
- Update tests for `extract.py` — no link hint score
- Update tests for `discover.py` — no priority queue, no exploration filters
- Update tests for `db.py` — no source_memory, no queries table
- Update tests for `config.py` — no score_consistency, no location_radius, no heuristic_extraction

---

## Execution Order

### Phase 1: Delete and remove
1. Delete `heuristics.py`, `scoring.py`, `robots.py`
2. Delete deleted config fields from `config.yaml`
3. Delete deleted config classes from `config.py`
4. Delete `SourceMemoryRow`, remove `score_source`/`score_basis` from models
5. Delete `location.py` (all functions)
6. Delete `link_hint_score()` from `urltools.py`

### Phase 2: Rewrite agent loop
7. Rewrite `agent.py` `run()` method — two-stage loop
8. Inline min-score gate in `_clean_jobs()`
9. Remove all references to deleted modules
10. Delete `_early_page_decision()`

### Phase 3: Rewrite supporting modules
11. Rewrite `llm.py` — remove truncation loop, remove token guard, parse link_classification
12. Rewrite `extract.py` — remove link cap, remove link_hint_score
13. Rewrite `discover.py` — remove priority, remove exploration filters, remove query generation
14. Rewrite `db.py` — FIFO queue, remove source_memory/queries, remove score columns from export

### Phase 4: Update prompts and output
15. Update `prompts.yaml` — add link_classification schema
16. Update `models.py` — add LinkClassification, remove priority from FrontierItem
17. Update `reporting.py` — remove deleted action types

### Phase 5: Clean up
18. Update `config.py` — add ScoringConfig, remove deleted configs
19. Run `scripts/test.sh` — fix any test failures
20. Run `make install` if needed

---

## Final State Summary

### What remains
- `agent.py` — two-stage loop (classify → open → score)
- `llm.py` — LLM client (no truncation, no token budget check)
- `config.py` — `ScoringConfig`, `CrawlerConfig` (simplified), `RunConfig` (no max_pages/depth)
- `models.py` — `JobMatch`, `PageDecision` (with link_classification), `LinkClassification`, `FrontierItem` (no priority)
- `db.py` — FIFO queue, save_jobs, export_csv/jsonl (no score_source/score_basis)
- `discover.py` — seed_frontier, enqueue_follow_urls (no priority, no exploration filters)
- `extract.py` — compact_text, rank_candidate_links (no length cap)
- `urltools.py` — clean_url, denied_by_safety, source_key, career_candidate_urls
- `company_filters.py` — blacklist matching
- `language.py` — multilingual terms, bootstrap templates
- `reporting.py` — ActionReporter
- `browser.py` — Playwright wrapper
- `prompts.yaml` — Stage 1 + Stage 2 prompts with link_classification
- `scoring.py` — DELETED (inlined in agent.py)
- `heuristics.py` — DELETED
- `robots.py` — DELETED
- `location.py` — DELETED

### What's deleted from config
- `score_consistency` — all cap values
- `heuristic_extraction` — all heuristic config
- `location_radius` — all location config
- `max_pages`, `max_depth` — removed
- `respect_robots_txt`, `strict_robots_when_unavailable`, `robots_timeout_seconds` — removed
- `filter_exploration_urls`, `drop_urls_with_outside_city` — removed
- `require_role_signal_*` — removed
- `max_links_per_page_for_llm` — removed
- `max_generated_queries_per_run` — removed
- `query_generation_every_pages` — removed
- `seed_search_when_empty` — removed

### What remains in config
- `scoring.min_fit_score: 55` — the only score gate
- `scoring.high_fit_score: 80` — for source memory rewards (if any)
- `crawler.max_pages_per_source_key: 25` — safety limit
- `llm.context_window_tokens: 128000` — expanded
- All other operational config: LLM endpoint, browser settings, safety, job_validation, etc.
