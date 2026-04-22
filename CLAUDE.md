# Cooking Brain — Claude Context

## What This Project Is

A **self-organizing AI cooking knowledge base** built on the Karpathy LLM Wiki pattern.
No vector databases. No RAG. Just plain markdown files in an Obsidian vault,
maintained and interlinked by a multi-agent Gemini pipeline.

**Core loop:**
1. User drops raw cooking content into `inbox/`
2. File watcher (`agent/watch.py`) detects it automatically
3. **Orchestrator** coordinates sub-agents in parallel:
   - Classifier → Writer → Cross-Linker (parallel page updates) → Logger → Indexer
4. `wiki/` grows with new pages, cross-references, and an updated `index.md`
5. User browses the growing knowledge graph in Obsidian

---

## Architecture

```
agent/
├── orchestrator.py          # Coordinates all sub-agents; parallel file processing
├── gemini.py                # Gemini client: init_gemini(), call_gemini()
├── utils.py                 # Shared: slugify, load_config, collect_wiki_pages, CATEGORY_DIR/PROMPT
├── compile.py               # CLI: ingest inbox (uses Orchestrator)
├── watch.py                 # CLI: file watcher (uses Orchestrator)
├── query.py                 # CLI: ask questions from the wiki
├── lint.py                  # CLI: wiki health check
└── agents/
    ├── classifier.py        # ClassifierAgent    — determines content type + title
    ├── writer.py            # WriterAgent        — generates + writes wiki pages
    ├── standardizer.py      # StandardizerAgent  — checks completeness, enriches if needed (no LLM cost if page is already complete)
    ├── cross_linker.py      # CrossLinkerAgent   — updates existing pages (parallel)
    ├── logger.py            # LoggerAgent      — appends to wiki/log.md
    ├── indexer.py           # IndexerAgent     — regenerates wiki/index.md
    ├── query_agent.py       # QueryAgent       — answers questions from wiki content
    └── lint_agent.py        # LintAgent        — vault health check
```

### Parallelism model
- **Outer pool** (`orchestrator.py`): up to 3 inbox files processed simultaneously
- **Inner pool** (`cross_linker.py`): up to 5 page-update Gemini calls per file, in parallel

---

## Project Structure

```
cooking-brain/
├── inbox/                  # Drop zone for raw content
│   ├── processed/          # Files moved here after processing
│   └── examples/           # Demo files (carbonara.txt, emulsification.txt)
├── wiki/
│   ├── index.md            # Master index — AI-generated
│   ├── log.md              # Timestamped changelog — auto-maintained
│   ├── recipes/
│   ├── ingredients/
│   ├── techniques/
│   ├── cuisines/
│   ├── tools/
│   └── people/
├── _templates/             # Obsidian manual entry templates
├── agent/                  # (see Architecture above)
│   └── prompts/
│       ├── classify.txt
│       ├── extract_recipe.txt
│       ├── extract_ingredient.txt
│       ├── extract_technique.txt
│       ├── update_index.txt
│       ├── cross_link_scan.txt    # NEW: finds pages to cross-link
│       ├── cross_link_update.txt  # NEW: inserts link into existing page
│       ├── query.txt              # NEW: answers questions from wiki
│       └── lint.txt               # NEW: wiki health check
├── .obsidian/
├── requirements.txt
├── .env.example
└── README.md
```

---

## Tech Stack

| Component   | Technology |
|---|---|
| AI model    | Google Gemini 2.5 Pro (`gemini-2.5-pro`) |
| SDK         | `google-genai` (NOT the deprecated `google-generativeai`) |
| File watch  | `watchdog` 6.0+ |
| Config      | `pyyaml` |
| Parallelism | `concurrent.futures.ThreadPoolExecutor` |
| Frontend    | Obsidian v1.12.7 |
| Language    | Python 3.14 |

**API key env var:** `GEMINI_API_KEY`

---

## Key Modules

### `agent/orchestrator.py`
Central coordinator. Key methods:
- `process_file(file_path)` — full pipeline for one file
- `process_inbox()` — parallel processing of all inbox files
- `query(question, file_answer=False)` — ask the wiki a question
- `lint()` — run health check on the vault
- `reindex()` — rebuild index.md only

### `agent/gemini.py`
- `init_gemini(cfg)` — returns a `genai.Client`
- `call_gemini(client, gemini_cfg, system_prompt, user_content)` — single blocking call (thread-safe)

### `agent/utils.py`
- `load_config()` — reads `agent/config.yaml`, resolves paths relative to project root
- `slugify(text)` — lowercase hyphenated slug
- `collect_wiki_pages(wiki_root, include_content=False)` — metadata list of all pages
- `CATEGORY_DIR` / `CATEGORY_PROMPT` — mappings used by WriterAgent

### `agent/agents/cross_linker.py`
Two-step cross-linking:
1. **Scan** (1 Gemini call): given new page + existing page list, return which pages to update
2. **Update** (N parallel Gemini calls): insert `[[WikiLink]]` into each target page

### `agent/agents/query_agent.py`
- Keyword-scores all wiki pages to find candidates (no extra LLM call)
- Passes top 10 candidates to Gemini for synthesis
- Detects `NEEDS_RESEARCH:` signal for gap reporting
- Optionally writes answer as a new `general_note` page

### `agent/agents/lint_agent.py`
- Passes full wiki content to Gemini
- Returns structured JSON: orphans, stubs, missing_links, gaps, contradictions

---

## Wiki Page Format

All pages use Obsidian-compatible frontmatter. Cross-references use `[[WikiLink]]` syntax.

### Category → Subfolder
| Category | Folder | Prompt |
|---|---|---|
| recipe | `wiki/recipes/` | `extract_recipe.txt` |
| ingredient | `wiki/ingredients/` | `extract_ingredient.txt` |
| technique | `wiki/techniques/` | `extract_technique.txt` |
| cuisine | `wiki/cuisines/` | `extract_recipe.txt` |
| tool | `wiki/tools/` | `extract_ingredient.txt` |
| person | `wiki/people/` | `extract_ingredient.txt` |
| general_note | `wiki/` (root) | `extract_recipe.txt` |

Slug format: `slugify(title)` → lowercase, hyphens, no special chars.

---

## CLI Commands

```bash
# Ingest inbox (automatic parallel processing)
python agent/compile.py

# Preview without writing
python agent/compile.py --dry-run

# Process one specific file
python agent/compile.py --file "inbox/my-note.txt"

# Rebuild the index only
python agent/compile.py --reindex

# Start the file watcher (recommended for continuous use)
python agent/watch.py

# Ask a question
python agent/query.py "How do I make a proper roux?"

# Ask and file the answer back into the wiki
python agent/query.py "What is emulsification?" --file

# Run a wiki health check
python agent/lint.py

# Health check as JSON
python agent/lint.py --json
```

---

## How to Test It

1. Set env var: `$env:GEMINI_API_KEY = "your_key"`
2. Copy an example file: `cp inbox/examples/carbonara.txt inbox/`
3. Run: `python agent/compile.py`
4. Check `wiki/recipes/` for the generated page
5. Check `wiki/log.md` for the timestamped changelog entry
6. Run: `python agent/query.py "How do I make carbonara?"`
7. Open the vault in Obsidian to browse the graph

---

## Known Issues / Gotchas

- Use `google-genai` SDK (NOT `google-generativeai` which is deprecated)
- `call_gemini()` is thread-safe — each call is an independent HTTP request
- PDF support is listed in config but not yet implemented — PDFs fail silently
- The watcher only watches the **top-level** `inbox/` — files in subdirs are ignored
- `collect_wiki_pages()` excludes `index.md` and `log.md` from all operations

---

## Potential Next Steps

- [ ] PDF text extraction (`pypdf` or `pdfminer`)
- [ ] URL ingestion — paste a URL, agent fetches and processes the page
- [ ] Web search backfill — when query hits a gap, auto-search and file results
- [ ] Lint auto-fix — not just report issues but fix orphans and missing links
- [ ] GitHub auto-commit after each ingest
- [ ] Dataview query examples in `wiki/index.md`
- [ ] Meal planning page type
- [ ] Shopping list generation from selected recipes
