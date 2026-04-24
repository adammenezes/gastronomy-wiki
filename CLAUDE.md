# Cooking Brain — Claude Context

## What This Project Is

A **self-organizing AI cooking knowledge base** built on the Karpathy LLM Wiki pattern.
No vector databases. No RAG. Just plain markdown files in an Obsidian vault,
maintained and interlinked by a multi-agent Gemini pipeline.

**Core loop:**
1. User drops raw cooking content into `inbox/` (or adds URLs to `inbox/urls.txt`)
2. File watcher (`agent/watch.py`) detects it automatically
3. **Orchestrator** runs the full pipeline: Clean → Classify → Write → Standardize → WikiLink → Cross-Link → Log → Index
4. `wiki/` grows with new pages, cross-references, and an updated `index.md`
5. User browses the growing knowledge graph in Obsidian

---

## Architecture

```
agent/
├── orchestrator.py          # Central coordinator — wires all sub-agents, parallel file processing
├── gemini.py                # GeminiPool (round-robin key pool), call_gemini(), call_gemini_video()
├── utils.py                 # load_config, slugify, load_prompt, inject_date, collect_wiki_pages, CATEGORY_DIR/PROMPT
├── compile.py               # CLI: process inbox (wraps Orchestrator)
├── watch.py                 # CLI: file watcher (wraps Orchestrator)
├── query.py                 # CLI: ask questions from the wiki
├── lint.py                  # CLI: wiki health check
├── procure.py               # CLI: discover new knowledge leads (gap analysis → crawl → score → leads.md)
├── sort.py                  # CLI: move misplaced wiki root files to correct subfolders
├── server.py                # HTTP server wrapper (optional)
├── benchmark.py             # Benchmarking / stress tests
├── config.yaml              # All settings (model, paths, watcher, standardizer rules)
├── sources.yaml             # Crawl sources for the procurement agent
├── taxonomy.yaml            # Cooking knowledge taxonomy used by gap analysis
└── agents/
    ├── AGENTS.md            # Quick-reference doc for all sub-agents
    ├── processing/          # Ingest pipeline
    │   ├── cleaner.py       # CleanerAgent — extract clean text from file, URL, or YouTube
    │   ├── classifier.py    # ClassifierAgent — returns {category, title_suggestion, confidence}
    │   ├── writer.py        # WriterAgent — generates + writes the wiki page markdown
    │   ├── standardizer.py  # StandardizerAgent — quality gate (Python checks first, Gemini only if needed)
    │   └── wiki_linker.py   # WikiLinkerAgent — aggressive [[WikiLink]] annotation pass
    ├── filing/
    │   ├── cross_linker.py  # CrossLinkerAgent — updates existing pages to link back to new page (parallel)
    │   ├── logger.py        # LoggerAgent — appends to wiki/log.md (pure Python, 0 Gemini calls)
    │   └── indexer.py       # IndexerAgent — regenerates wiki/index.md (pure Python, 0 Gemini calls)
    ├── retrieval/
    │   └── query_agent.py   # QueryAgent — keyword + graph retrieval → Gemini synthesis
    ├── maintenance/
    │   └── lint_agent.py    # LintAgent — vault health check, returns structured JSON
    └── procurer/            # Full procurement sub-system: GapAnalyzer → Crawlers → Deduplicator → LeadScorer → LeadsWriter
```

### Parallelism model
- **Outer pool** (`orchestrator.py`): `MAX_FILE_WORKERS = 3` inbox files processed simultaneously via `ThreadPoolExecutor`
- **Inner pool** (`cross_linker.py`): up to 5 parallel Gemini calls per ingested file

---

## Project Structure

```
cooking-brain/
├── inbox/
│   ├── urls.txt            # Add URLs here (one per line) — processed automatically
│   ├── processed/          # Files moved here after processing
│   └── examples/           # Demo files (carbonara.txt, emulsification.txt)
├── wiki/
│   ├── index.md            # Master index — auto-generated (pure Python, no LLM)
│   ├── log.md              # Timestamped changelog — auto-maintained (pure Python)
│   ├── recipes/
│   ├── ingredients/
│   ├── techniques/
│   ├── cuisines/
│   ├── tools/
│   ├── people/
│   ├── safety/             # Food safety content
│   ├── science/            # Food science / chemistry
│   ├── management/         # Kitchen management, mise en place, etc.
│   └── other/              # Catch-all for unclassified content
├── _templates/             # Obsidian manual entry templates
├── agent/                  # (see Architecture above)
├── .obsidian/
├── requirements.txt
├── .env                    # Not committed — set GEMINI_API_KEY here
├── .env.example
└── README.md
```

---

## Tech Stack

| Component   | Technology |
|---|---|
| AI model    | `gemini-2.5-flash` (default) / `gemini-2.5-pro` (writer + cross_linker) |
| SDK         | `google-genai` — use `from google import genai`. **NOT** the deprecated `google-generativeai` |
| File watch  | `watchdog` 6.0+ |
| Config      | `pyyaml`, optional `python-dotenv` (auto-loads `.env` if present) |
| Parallelism | `concurrent.futures.ThreadPoolExecutor` |
| Frontend    | Obsidian v1.12.7 |
| Language    | Python 3.14 |

**API key env var:** `GEMINI_API_KEY` (or add to `.env` — loaded automatically by `utils.py`)

---

## Key Modules — Detailed

### `agent/gemini.py`
- `GeminiPool` — thread-safe round-robin pool of `genai.Client` instances. Supports multiple API keys via `extra_api_key_envs` in config to spread rate limits.
- `init_gemini(cfg) → GeminiPool` — reads `GEMINI_API_KEY` + any extra key env vars
- `call_gemini(pool, gemini_cfg, system_prompt, user_content) → str` — standard blocking call
- `call_gemini_video(pool, gemini_cfg, system_prompt, video_url) → str` — passes YouTube URL directly to Gemini as `FileData` (fallback when no transcript available)
- Supports `thinking_budget` in `gemini_cfg` — set to `0` to disable internal reasoning (used by lint to avoid token budget exhaustion before the JSON output)

### `agent/utils.py`
- `load_config() → dict` — reads `agent/config.yaml`, resolves all paths relative to project root, auto-loads `.env`
- `load_prompt(prompts_dir, name) → str` — reads `<name>.txt` from prompts dir
- `inject_date(text) → str` — replaces `<YYYY-MM-DD>` placeholder with today's date
- `slugify(text) → str` — lowercase hyphenated slug for filenames
- `collect_wiki_pages(wiki_root, include_content=False) → list[dict]` — walks wiki, returns metadata; **excludes `index.md` and `log.md`**
- `CATEGORY_DIR` — maps category string → subfolder name
- `CATEGORY_PROMPT` — maps category string → prompt filename (without `.txt`)

### `agent/orchestrator.py` — `Orchestrator` class
Per-file pipeline (step order matters):
1. `cleaner.run(file_path)` or `cleaner.run_url(url)` → cleaned text string
2. `classifier.run(text)` → `{category, title_suggestion, confidence}`
3. `writer.generate(category, text, source_url)` → markdown string
4. `standardizer.run(category, content)` → `(content, issues_fixed)` — 0 Gemini calls if page already passes
5. `wiki_linker.run(content)` → `(content, links_added)`
6. `writer.write(category, title, content, dry_run)` → Path to written file
7. `cross_linker.run(wiki_page_path, content, dry_run)` → count of pages updated
8. Archive source file to `inbox/processed/`
9. `logger.log_ingest(...)` → appends to `wiki/log.md`

Key orchestrator methods:
- `process_file(file_path) → bool`
- `process_url(url) → bool`
- `process_inbox()` — parallel, also processes `inbox/urls.txt`
- `query(question, file_answer=False) → dict`
- `lint() → dict`
- `sort(dry_run, delete_garbled) → (moved, flagged)` — moves misplaced root files; detects garbled Gemini output
- `reindex()` — rebuilds `wiki/index.md`

### `agent/agents/processing/cleaner.py` — `CleanerAgent`
- Handles: plain text files, PDF (text extraction), plain URLs (via `requests`), YouTube URLs (transcript → Gemini video fallback)
- Always returns a string — falls back to raw file text if extraction fails so the pipeline never hard-stops

### `agent/agents/processing/standardizer.py` — `StandardizerAgent`
- **Zero-cost Python checks first**: required frontmatter fields, required section headers, min body word count, min `[[WikiLink]]` density
- Only calls Gemini (1 call) **if** issues are found — fills in what's missing without rewriting existing content
- Config-driven: `config.yaml` specifies `required_frontmatter` and `required_sections` per category, plus `min_body_words` and `min_wiki_links`

### `agent/agents/procurer/` — Procurement sub-system
Full pipeline:
1. **GapAnalyzer** — identifies knowledge gaps from: missing `[[WikiLinks]]`, lint report stubs/gaps, taxonomy coverage, orphaned terms
2. **Crawlers** — `WebCrawler`, `JournalScraper`, `MultiLinkCrawler`; sources defined in `agent/sources.yaml`
3. **Deduplicator** — filters out URLs already processed or already in the wiki
4. **LeadScorer** — Gemini scores each lead for relevance, novelty, quality
5. **LeadsWriter** — writes `inbox/leads.md` as a checklist for human approval

Approval workflow:
```bash
python agent/procure.py           # → generates inbox/leads.md
# Open leads.md, mark [x] to approve
python agent/procure.py --approve # → ingests approved leads, sorts, reindexes
```

---

## Wiki Page Format

All pages use Obsidian-compatible frontmatter + body. Cross-references use `[[WikiLink]]` syntax.

### Full Category → Subfolder → Prompt mapping
| Category | Folder | Prompt file |
|---|---|---|
| recipe | `wiki/recipes/` | `extract_recipe.txt` |
| ingredient | `wiki/ingredients/` | `extract_ingredient.txt` |
| technique | `wiki/techniques/` | `extract_technique.txt` |
| cuisine | `wiki/cuisines/` | `extract_recipe.txt` |
| tool | `wiki/tools/` | `extract_ingredient.txt` |
| person | `wiki/people/` | `extract_ingredient.txt` |
| safety | `wiki/safety/` | `extract_safety.txt` |
| management | `wiki/management/` | `extract_management.txt` |
| science | `wiki/science/` | `extract_science.txt` |
| other | `wiki/other/` | `extract_other.txt` |
| general_note | `wiki/` (root) | `extract_recipe.txt` |

Slug format: `slugify(title)` → lowercase, hyphens, no special chars.

### Per-agent Gemini config overrides
Set in `config.yaml` under `gemini.agents.<agent_name>`:
```yaml
gemini:
  model: gemini-2.5-flash    # default
  agents:
    writer:
      model: gemini-2.5-pro  # override for this agent only
    cross_linker:
      model: gemini-2.5-pro
```
The orchestrator's `_acfg(name)` helper merges the base config with per-agent overrides (stripping the `agents` sub-dict before passing to `call_gemini`).

---

## CLI Commands

```bash
# ── Ingest ──────────────────────────────────────────────────────────
python agent/compile.py                   # process entire inbox (parallel)
python agent/compile.py --dry-run         # preview without writing
python agent/compile.py --file PATH       # process one specific file
python agent/compile.py --url URL         # process a web page or YouTube video
python agent/compile.py --reindex         # rebuild wiki/index.md only

# ── Watcher (recommended for continuous use) ─────────────────────────
python agent/watch.py                     # auto-process on every inbox drop

# ── Query ────────────────────────────────────────────────────────────
python agent/query.py "How do I make a roux?"
python agent/query.py "What is emulsification?" --file   # save answer to wiki

# ── Lint ─────────────────────────────────────────────────────────────
python agent/lint.py                      # human-readable health check
python agent/lint.py --json               # structured JSON output

# ── Sort ─────────────────────────────────────────────────────────────
python agent/sort.py                      # dry-run: show what would move
python agent/sort.py --apply              # actually move misplaced files
python agent/sort.py --apply --delete-garbled  # also delete garbled pages

# ── Procure ──────────────────────────────────────────────────────────
python agent/procure.py                   # full run: gaps → crawl → score → leads.md
python agent/procure.py --gaps-only       # print gaps, skip crawling
python agent/procure.py --lint            # run lint first, feed gaps into procurer
python agent/procure.py --approve         # ingest [x]-marked leads from leads.md
python agent/procure.py --source NAME     # run one source only
python agent/procure.py --estimate        # show projected token cost, then exit
python agent/procure.py --dry-run         # analyse + crawl, don't write leads.md
python agent/procure.py --page PATH       # use [[WikiLinks]] from a page as gap list
```

---

## How to Test It

```bash
# 1. Set API key (or add to .env file)
$env:GEMINI_API_KEY = "your_key"

# 2. Test one example file
cp inbox/examples/carbonara.txt inbox/
python agent/compile.py

# 3. Check output
# wiki/recipes/pasta-carbonara.md   ← generated page
# wiki/log.md                       ← timestamped changelog entry

# 4. Test query
python agent/query.py "How do I make carbonara?"

# 5. Test URL ingestion
python agent/compile.py --url "https://www.seriouseats.com/the-food-lab-the-best-way-to-make-a-steak"

# 6. Run a health check
python agent/lint.py

# 7. Open in Obsidian → browse graph view
```

---

## Known Issues / Gotchas

- **SDK**: Use `from google import genai` (the `google-genai` package). **Never** `import google.generativeai` — that's deprecated.
- **`call_gemini` signature**: `call_gemini(pool, gemini_cfg, system_prompt, user_content)` — first arg is a `GeminiPool`, not a plain client.
- **`thinking_budget`**: The lint agent sets `thinking_budget: 0` in config to prevent Gemini's internal reasoning from consuming the output token budget before the JSON response (causing mid-JSON truncation).
- **`collect_wiki_pages`** always excludes `index.md` and `log.md` — don't add special files to subdirs.
- Watcher watches **top-level `inbox/` only** — files in subdirs (e.g. `inbox/processed/`) are ignored.
- PDF support is in the watcher's extension list but `cleaner.py`'s PDF path may need `pypdf` installed.
- **Garbled files**: If the Gemini API returns an error message instead of markdown, `sort()` detects it by checking for known error prefixes and can delete it with `delete_garbled=True`.
- Config's `agents` sub-dict is stripped before any `call_gemini()` call via `_acfg()` in the orchestrator — don't pass raw `cfg["gemini"]` anywhere; always use `_acfg(name)` or strip it manually.

---

## Brave Search — Current State & Optimization Priorities

### How Brave is used today
`SearchCrawler` (`agents/procurer/crawlers/search_crawler.py`) is one of four crawler types wired into the `ProcurementAgent`. It is the only **dynamic discovery** source — all other crawlers have fixed seed URLs.

**Current flow per `procure.py` run:**
1. `GapAnalyzer` produces a list of gap topics (broken `[[WikiLinks]]`, lint stubs, mention frequency, taxonomy)
2. `SearchCrawler.discover(topics)` receives that gap list, does:
   - Rule-based noise filter (stop words, `_CULINARY_STOP_WORDS`, titles already in wiki)
   - **1 Gemini Flash call** → `search_queries.txt` prompt refines raw gap terms into better search queries
   - Brave API calls (sequential, `_REQUEST_DELAY = 0.5s` between each)
   - **Parallel HTTP fetches** (6 workers) to confirm each result is an article + extract metadata
3. All Brave leads are returned with `access="verify"` — never auto-ingested

**Configured in `sources.yaml`:**
```yaml
- name: web_search
  crawler: SearchCrawler
  api_key_env: BRAVE_API_KEY      # BRAVE_API_KEY must be set in .env
  search_suffix: "culinary food science cooking"
  queries_per_gap: 1              # currently unused — see optimization #7
  max_results: 5                  # URLs returned per Brave query (API max: 20)
  max_gaps: 25                    # how many gap terms are searched
```

**Free tier limits:** 2,000 queries/month → ~66/day → ~13 full `procure.py` runs/day at `max_gaps=25` + `queries_per_gap=1`.

---

### Brave API — what we're not using (from the official docs)

The API returns far more data than we currently extract. `_brave_search()` only reads `item["url"]` and `item["title"]` from each result and discards everything else.

**Fields available on every `web.results[]` item that we currently discard:**

| Field | What it is | Use in SearchCrawler |
|---|---|---|
| `description` | Snippet/excerpt from the page | **Replace most HTTP fetches** — use as `content_preview` directly |
| `page_age` | Page's published/last-modified date | **Replace date extraction** — no HTTP fetch needed |
| `article` | Structured metadata: author, date, publisher, `isAccessibleForFree` | Confirm article type + get author/date/paywall status without a second request |
| `language` | Detected page language | Filter non-English results without fetching the page |
| `family_friendly` | Content safety flag | Pre-filter low-quality/adult results |
| `extra_snippets` | Up to 5 additional excerpts per result (requires `extra_snippets=true` param) | Richer preview for the LeadScorer, no HTTP fetch |

**Request parameters we're not using:**

| Param | Currently | Better | Why |
|---|---|---|---|
| `count` | 5 | 10–20 | API max is **20**. Free query budget, double results at zero cost |
| `result_filter` | *(all)* | `web` | Strips news/video/discussions — smaller payload, cleaner results |
| `freshness` | *(none)* | `py` / `pm` | Filter by page age **server-side** instead of post-fetching |
| `extra_snippets` | false | true | Up to 5 extra excerpts per result — eliminates most HTTP fetches |
| `goggles_id` | — | **DEPRECATED** | Must use `goggles` param instead (API docs say `goggles_id` will break) |
| `spellcheck` | — | true | Better query matching for culinary terms |
| `search_lang` | — | `en` | Restrict to English results server-side |

---

### Optimization Plan

#### 1. ⚡ Eliminate most HTTP fetches using Brave's response body (biggest win)
The `_fetch_article_meta()` parallel HTTP fetch (6 workers, BeautifulSoup, `og:` tag extraction) gets three things: article confirmation, date, and preview text. **The Brave API already returns all three** in the response:
- `item["article"]` → confirms it's an article; gives author, date, publisher, and `isAccessibleForFree`
- `item["description"]` → preview text, no page fetch needed
- `item["page_age"]` → publication date, no page fetch needed
- `item["extra_snippets"]` (with `extra_snippets=true`) → up to 5 alternative excerpts

**New flow:** extend `_brave_search()` to return the full enriched result dict. In `discover()`, skip `_fetch_article_meta()` when `article` is present or description is non-empty. Only fall back to HTTP fetch for results that have neither.

**File:** `search_crawler.py` → `_brave_search()` return a richer tuple/dict; update `discover()` loop
**Expected impact:** eliminates ~70–80% of HTTP fetches; faster, fewer failures on paywalled/bot-protected pages

#### 2. Fix `goggles_id` → `goggles` (deprecated, will break)
`goggles_id` is explicitly marked deprecated in the official API docs — use `goggles` instead. We don't currently pass a value but any future goggle usage must use the right param name.

**File:** `search_crawler.py` → `_brave_search()` params dict — rename key from `goggles_id` to `goggles`

#### 3. Raise `count` from 5 → 10–20 and add `result_filter=web`
We set `max_results: 5` but the API max is **20**. Doubling to 10 doubles candidate URLs per query at zero extra API call cost. Adding `result_filter=web` drops news/video results from the response so we don't waste slots on non-article results.

**File:** `sources.yaml` → `max_results: 10`
**File:** `search_crawler.py` → `_brave_search()` params — add `"result_filter": "web"`, `"search_lang": "en"`, `"spellcheck": True`

#### 4. Add `extra_snippets=true`
Costs nothing extra; gives up to 5 alternative excerpts per result. Concatenate them as the lead's preview for richer LeadScorer input.

**File:** `search_crawler.py` → `_brave_search()` params — add `"extra_snippets": True`; collect `item.get("extra_snippets", [])` and join into preview

#### 5. Use `freshness` param for server-side age filtering
Currently: fetch all results → HTTP fetch → parse date → post-filter old articles. Instead set `freshness=py` in the Brave request to filter at source. Use `pm` for timely topics (lint/wikilink gaps), `py` for taxonomy.

**File:** `sources.yaml` → add `freshness: py` under `web_search` source
**File:** `search_crawler.py` → `_brave_search()` reads `self.config.get("freshness")` and adds to params if set

#### 6. Parallelize Brave API calls
Calls are sequential with `time.sleep(0.5)` between each. Replace with a `ThreadPoolExecutor(max_workers=3)` + exponential backoff on 429. Cuts wall-clock crawl time from ~12s to ~4s for 25 queries.

**File:** `search_crawler.py` → new `_run_brave_searches(queries)` method; same pattern already used by the article fetch pool

#### 7. Implement `queries_per_gap` (config key exists, is ignored)
`queries_per_gap` is in `sources.yaml` but the LLM refinement always emits a flat list — the per-gap quota is never enforced. Fix: restructure `_llm_refine_queries` to output `{gap: [query1, query2, ...]}`, then expand to `(gap, query)` pairs in `discover()`.

**File:** `search_crawler.py` → `_llm_refine_queries()` output format + `discover()` expansion loop
**File:** `agent/prompts/search_queries.txt` → output grouped JSON: `{"Maillard reaction": ["query A", "query B"], ...}`

#### 8. Use `article.isAccessibleForFree` to pre-label paywalled leads
When `item["article"]["isAccessibleForFree"]` is `false`, mark the lead `access="paywalled"` immediately. The `approve()` method already has a paywalled branch that prints a manual download list — this just routes them there automatically instead of dumping everything into `"verify"`.

**File:** `search_crawler.py` → `discover()` — read `isAccessibleForFree` from Brave response, set `Lead.access`

#### 9. Move `_SKIP_DOMAINS` to `sources.yaml` + add `preferred_domains`
`_SKIP_DOMAINS` is hardcoded. Move to config so it's tunable without touching code. Add `preferred_domains` (e.g. `seriouseats.com`, `chefsteps.com`) that receive a scorer bonus.

**File:** `search_crawler.py` → read `self.config.get("skip_domains", [])` and `self.config.get("preferred_domains", [])`
**File:** `sources.yaml` → add `skip_domains:` and `preferred_domains:` lists under `web_search`

#### 10. Weight query budget by gap signal confidence
All 25 gaps get equal budget. `GapAnalyzer` already groups by confidence: `wikilinks > lint > frequency > taxonomy`. Pass `gaps_by_signal` into `SearchCrawler` and allocate `queries_per_gap` proportionally.

**File:** `procurer/__init__.py` → inject `_gaps_by_signal` into SearchCrawler config dict (same pattern as `_page_path`)
**File:** `search_crawler.py` → `discover()` reads `self.config.get("_gaps_by_signal", {})` and calculates per-gap quota

---

### Recommended implementation order

| Priority | Item | Effort | Impact |
|---|---|---|---|
| 1 | **#1** — use Brave response body, eliminate HTTP fetches | Medium | ⚡⚡⚡ |
| 2 | **#2 + #3 + #4** — fix deprecated param, bump count, add extra_snippets | Trivial | ⚡⚡ |
| 3 | **#5** — freshness param | Trivial | ⚡ |
| 4 | **#6** — parallelize Brave calls | Medium | ⚡⚡ |
| 5 | **#7–#10** — query budget, paywall labeling, domain config | Medium | ⚡ |

---

### Key files for Brave work

| File | Role |
|---|---|
| `agent/agents/procurer/crawlers/search_crawler.py` | All Brave logic — queries, API calls, article confirmation |
| `agent/prompts/search_queries.txt` | System prompt for LLM query refinement |
| `agent/sources.yaml` | `web_search` source config (key, suffix, max_gaps, max_results, freshness, skip_domains) |
| `agent/agents/procurer/__init__.py` | Where SearchCrawler is instantiated + config injected |
| `agent/agents/procurer/deduplicator.py` | `known_urls` set — feed into SearchCrawler pre-filter |
| `.env` | `BRAVE_API_KEY=...` — must be set |
