# Gastronomy Wiki

> An AI-powered, self-organising culinary knowledge base — built on the **Karpathy LLM Wiki pattern**.

Drop in raw cooking content. The pipeline reads it, writes a structured wiki page, cross-links it across your entire vault, cites the source in APA format, and updates the master index — automatically. Browse the result as an interconnected knowledge graph in Obsidian.

---

## How It Works

```
inbox/ ──► Orchestrator ─────────────────────────────────────────► wiki/ ──► Obsidian
              │
              ├─ processing/
              │    cleaner ──► classifier ──► writer ──► standardizer ──► wiki_linker
              │
              ├─ filing/
              │    cross_linker (parallel) ──► logger ──► indexer
              │
              └─ maintenance/
                   sort (auto) ──► lint (on demand)
```

**Ingest loop:**
1. Drop any cooking content into `inbox/` — text, PDF, URL, or YouTube link
2. The watcher detects it within seconds (or run `compile.py` manually)
3. The orchestrator runs sub-agents: clean → classify → write → standardize → link → cross-link → log → index → sort
4. Open Obsidian and browse the growing, interlinked knowledge graph

**Procurement loop:**
1. `procure.py` analyses wiki gaps, crawls configured sources, and scores leads with Gemini
2. Leads are written to `inbox/leads.md` for human review
3. Mark `[x]` next to approved leads, run `--approve` — free articles are auto-ingested

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Set your Gemini API key

```bash
# Windows (PowerShell)
$env:GEMINI_API_KEY = "your_key_here"

# macOS / Linux
export GEMINI_API_KEY="your_key_here"
```

Optional — add extra keys to multiply your rate limits:

```bash
$env:GEMINI_API_KEY_2 = "second_key"
$env:GEMINI_API_KEY_3 = "third_key"
```

### 3. Start the file watcher

```bash
python agent/watch.py
```

Leave it running. Drop files into `inbox/` whenever you want.

---

## CLI Reference

### Ingest

```bash
# Process entire inbox (recommended)
python agent/compile.py

# Preview without writing anything
python agent/compile.py --dry-run

# Process a single file
python agent/compile.py --file "inbox/my-recipe.txt"

# Process a URL directly
python agent/compile.py --url "https://example.com/recipe"

# Rebuild the master index only
python agent/compile.py --reindex
```

### Query

Ask natural-language questions answered from your wiki content:

```bash
python agent/query.py "How do I make a proper beurre blanc?"

# Ask and save the answer back into the wiki
python agent/query.py "What is the Maillard reaction?" --file
```

### Procurement

Discover new content leads from configured sources:

```bash
# Full run — gap analysis + crawl + score + write leads.md
python agent/procure.py

# Estimate token cost before running
python agent/procure.py --estimate

# Run lint first, feed real gaps into the procurer
python agent/procure.py --lint

# Approve leads marked [x] in leads.md — auto-ingest free sources
python agent/procure.py --approve

# Run a single source only
python agent/procure.py --source gastronomica
```

### Maintenance

```bash
# Health check — find orphans, stubs, gaps, contradictions
python agent/lint.py
python agent/lint.py --json

# Sort misplaced wiki root files into correct subfolders (dry-run)
python agent/sort.py

# Apply the sort
python agent/sort.py --apply --delete-garbled
```

---

## Project Structure

```
gastronomy-wiki/
├── inbox/
│   ├── examples/               # Demo files (carbonara, emulsification)
│   └── processed/              # Files archived here after ingest
├── wiki/                       # Generated knowledge base (local only, not in git)
│   ├── index.md                # Master index — AI-generated
│   ├── log.md                  # Timestamped changelog — auto-maintained
│   ├── recipes/
│   ├── ingredients/
│   ├── techniques/
│   ├── cuisines/
│   ├── tools/
│   ├── people/
│   ├── science/
│   ├── safety/
│   ├── management/
│   └── other/
├── _templates/                 # Obsidian manual entry templates
├── frontend/                   # Web UI (in progress)
└── agent/
    ├── orchestrator.py         # Coordinates all agents
    ├── gemini.py               # GeminiPool — multi-key round-robin client
    ├── utils.py                # Shared utilities (slugify, load_config, etc.)
    ├── compile.py              # CLI: ingest inbox
    ├── watch.py                # CLI: file watcher
    ├── query.py                # CLI: query the wiki
    ├── lint.py                 # CLI: health check
    ├── sort.py                 # CLI: sort misplaced wiki files
    ├── procure.py              # CLI: procurement pipeline
    ├── config.yaml             # Model, paths, feature flags
    ├── sources.yaml            # Configured crawl sources
    ├── taxonomy.yaml           # Culinary topic taxonomy for gap analysis
    ├── prompts/                # Gemini system prompts (one per agent)
    └── agents/
        ├── processing/         # cleaner, classifier, writer, standardizer, wiki_linker
        ├── filing/             # cross_linker, logger, indexer
        ├── retrieval/          # query_agent
        ├── maintenance/        # lint_agent
        └── procurer/           # gap_analyzer, crawlers, scorer, leads_writer
```

---

## Agent Pipeline

Each ingest costs approximately **$0.012 per page** (Gemini 2.5 Flash, non-thinking).

| Stage | Agent | Gemini calls |
|---|---|---|
| Clean | CleanerAgent | 1 |
| Classify | ClassifierAgent | 1 |
| Write | WriterAgent | 1 |
| Standardize | StandardizerAgent | 0 or 1 |
| Link | WikiLinkerAgent | 1 |
| Cross-link | CrossLinkerAgent | 0–5 (parallel) |
| Log | LoggerAgent | 0 |
| Index | IndexerAgent | 0 |
| Sort | orchestrator.sort() | 0 |

Writer and lint agents use **Gemini 2.5 Pro** (higher quality for writing and analysis). All other agents use **Gemini 2.5 Flash**. Model routing is configurable per-agent in `config.yaml`.

---

## Procurement Sources

Configured in `agent/sources.yaml`. Three crawler types available:

| Crawler | Use for |
|---|---|
| `WebCrawler` | Free web articles — 2-level crawl with noise filtering |
| `JournalScraper` | Paywalled academic journals (Tandfonline, ScienceDirect) — metadata + abstract only |
| `MultiLinkCrawler` | Container pages (journal issues, article indexes) — drills into individual articles, confirms with OpenGraph/JSON-LD |

Default sources: The Culinary Pro, Gastronomica (open access), AACC Culinary Arts Library Guide, International Journal of Gastronomy and Food Science, International Journal of Food Science & Technology, Food Research International.

---

## Wiki Page Format

Every page is Obsidian-compatible markdown with YAML frontmatter:

```markdown
---
title: "Emulsification"
tags: [technique, science, emulsion]
source: https://example.com/emulsification
date_added: 2026-04-22
---

## Overview
...

## See Also
- [[Hollandaise Sauce]] — classic emulsion sauce
- [[Vinaigrette]] — temporary emulsion

## Attribution
Author, A. A. (2024). Title of article. Site Name. https://...
```

- All culinary entities wrapped in `[[WikiLinks]]` for graph connectivity
- APA 7th edition citation in every `## Attribution` section
- Source URL surfaced in query responses under `## Sources`

---

## What Goes in `inbox/`

| Content type | How to add |
|---|---|
| Recipe or technique | Paste as `.txt` or `.md` |
| Web article | Add URL to `inbox/urls.txt`, one per line |
| PDF | Drop the file directly |
| YouTube video | Add URL to `inbox/urls.txt` |
| Your own notes | Any `.txt` or `.md` — rough drafts are fine |

---

## Configuration

Edit `agent/config.yaml` to adjust:
- Gemini model per agent (`agents:` section)
- Additional API keys (`extra_api_key_envs`)
- Standardizer thresholds (min word count, min WikiLinks)
- Watcher debounce delay
- Index rebuild on every run vs. manual only

---

## Obsidian Tips

Open the `wiki/` folder as your vault in Obsidian.

- **Graph View** — visualise how recipes, techniques, and ingredients interconnect
- **Backlinks pane** — see every page that links to `[[Emulsification]]`
- **Search** — full-text across your entire culinary knowledge base
- **Dataview plugin** — `TABLE cuisine, techniques FROM "recipes"`
