# Agent Directory

Quick reference for every sub-agent. All agents receive a `client` (Gemini pool) and `gemini_cfg` dict; they're wired together by `orchestrator.py`.

```
agents/
├── processing/     # Ingest pipeline — transforms raw content into wiki pages
├── filing/         # Writes and maintains wiki structure
├── retrieval/      # Reads the wiki to answer questions
├── maintenance/    # Health checks and housekeeping
└── procurer/       # Discovers and scores new content leads
```

---

## `processing/` — Ingest Pipeline

Run in this order for every file or URL ingested.

### `cleaner.py` — CleanerAgent
First step. Extracts clean text from any raw input — PDF, URL, YouTube, plain text — stripping boilerplate before anything else touches it. Never writes files; returns a string. Falls back to raw text if extraction fails so the pipeline never hard-stops.
- **Gemini calls:** 1 (boilerplate removal). YouTube with no transcript: 1 video call.

### `router.py` — RouterAgent
Replaces the old classifier. Reads cleaned text and returns a strict JSON routing plan based on a Substance Score (0-10). It acts as a quality gatekeeper: automatically rejecting low-substance fluff (like sitemaps or marketing filler), and intelligently splitting dense "hub" articles into multiple distinct pages (1-to-N). Returns categories and suggested wiki titles for accepted content.
- **Gemini calls:** 1

### `writer.py` — WriterAgent
Generates the full Obsidian-compatible markdown page from cleaned text using the category-specific extract prompt. Writes the file to the correct subfolder (`wiki/recipes/`, `wiki/techniques/`, etc.) using a slugified title. Receives an optional `source_url` injected into the prompt so Gemini produces an APA citation.
- **Gemini calls:** 1

### `standardizer.py` — StandardizerAgent
Quality gate. First runs zero-cost Python checks (required frontmatter fields, section headers, word count, WikiLink density, See Also section). Only calls Gemini if issues are found — and only to fill in what's missing, never to rewrite existing content.
- **Gemini calls:** 0 if page passes, 1 if enrichment needed

### `wiki_linker.py` — WikiLinkerAgent
Dedicated link-annotation pass after the writer and standardizer. Aggressively wraps every culinary entity in `[[WikiLinks]]` — ingredients, techniques, tools, dishes, people, science terms, etc. Kept separate from the writer so prose quality and link density are optimised independently.
- **Gemini calls:** 1

---

## `filing/` — Wiki Structure

### `cross_linker.py` — CrossLinkerAgent
After a new page is written, finds existing pages that mention the new page's title terms but don't already link to it. Uses a zero-cost Python keyword filter to isolate the specific paragraph where the term is mentioned. It then passes *only* that paragraph to Gemini across 10 parallel threads to insert the `[[WikiLink]]` without altering the prose.
- **Gemini calls:** 0–N (one per candidate updated, capped at 12 candidates, up to 10 parallel workers)

### `logger.py` — LoggerAgent
Appends a timestamped entry to `wiki/log.md` after each ingest, query, or lint run. Pure Python — no Gemini calls.
- **Gemini calls:** 0

### `indexer.py` — IndexerAgent
Regenerates `wiki/index.md` from the current vault state by reading each page's `## Overview` section. Pure Python — flat cost regardless of wiki size.
- **Gemini calls:** 0

---

## `retrieval/` — Query

### `query_agent.py` — QueryAgent
Answers natural-language questions from wiki content. Uses a three-layer hybrid retrieval: keyword scoring → category/intent boost → WikiLink graph expansion (follows `[[links]]` from top seed pages). Passes up to 15 context pages to Gemini for synthesis. Emits a `NEEDS_RESEARCH:` signal if the wiki has a gap.
- **CLI:** `python agent/cli/query.py "your question"`
- **Gemini calls:** 1

---

## `maintenance/` — Health & Housekeeping

Both agents are also callable via `orchestrator.lint()` / `orchestrator.sort()` by other parts of the system (e.g. `procure.py` feeds lint output into gap analysis).

### `lint_agent.py` — LintAgent
Health check on the full vault. Identifies orphan pages, stubs, missing cross-references, topic gaps, and contradictions. Returns structured JSON. Called by `procure.py --lint` to seed gap analysis with real vault issues.
- **CLI:** `python agent/cli/lint.py` / `python agent/cli/lint.py --json`
- **Gemini calls:** 1

### Sort — `orchestrator.sort()`
Moves misplaced wiki root files to their correct subfolders based on frontmatter tags. Detects and optionally deletes garbled files (Gemini error messages saved as page content). Triggered automatically after `procure.py --approve` and `compile.py` batch runs; also runnable standalone.
- **CLI:** `python agent/cli/sort.py` (dry-run) / `python agent/cli/sort.py --apply`
- **Gemini calls:** 0

---

## `procurer/` — Procurement

Full pipeline: GapAnalyzer → Crawlers → Deduplicator → LeadScorer → LeadsWriter.
- **CLI:** `python agent/cli/procure.py`
- Crawlers available: `WebCrawler`, `JournalScraper`, `MultiLinkCrawler`
