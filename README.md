# Cooking Brain

> An AI-powered, self-organizing cooking knowledge base — built with the **Karpathy LLM Wiki pattern**.

Your knowledge compounds. Drop in a recipe. The AI reads it, writes a structured wiki page, cross-links it across your entire vault, and updates the master index — automatically.

---

## How It Works

```
inbox/ ──► Orchestrator ──────────────────────────► wiki/ ──► Obsidian
              │                                        │
              ├── Classifier (what kind of content?)   │
              ├── Writer (generate structured page)    │
              ├── Cross-Linker (update related pages)◄─┘
              ├── Logger (append to wiki/log.md)
              └── Indexer (rebuild wiki/index.md)
```

1. **Drop** any cooking content into `inbox/` (.txt, .md, .pdf, .url)
2. **Watcher** detects the new file within seconds
3. **Orchestrator** runs sub-agents in parallel — classify, write, cross-link, log, index
4. **Open Obsidian** — browse your growing, interconnected cooking graph

---

## Setup

### 1. Install dependencies

```powershell
pip install -r requirements.txt
```

### 2. Set your Gemini API key

```powershell
$env:GEMINI_API_KEY = "your_key_here"
```

### 3. Start the file watcher

```powershell
python agent/watch.py
```

Leave it running. Drop files into `inbox/` whenever you want.

---

## Commands

### Ingest

```powershell
# Process inbox once (no watcher needed)
python agent/compile.py

# Preview changes without writing anything
python agent/compile.py --dry-run

# Process a single specific file
python agent/compile.py --file "inbox/my-recipe.txt"

# Regenerate the master index only
python agent/compile.py --reindex
```

### Query

Ask natural-language questions answered from your wiki:

```powershell
# Ask a question
python agent/query.py "How do I make a proper beurre blanc?"

# Ask and save the answer back into the wiki
python agent/query.py "What is the Maillard reaction?" --file
```

### Lint

Health check your vault — find orphan pages, stubs, gaps, and contradictions:

```powershell
python agent/lint.py

# Output as JSON
python agent/lint.py --json
```

---

## Project Structure

```
cooking-brain/
├── inbox/                  # Drop zone — put raw content here
│   ├── processed/          # Processed files moved here automatically
│   └── examples/           # Demo files to try out
├── wiki/
│   ├── index.md            # Master index (AI-generated)
│   ├── log.md              # Timestamped changelog (AI-maintained)
│   ├── recipes/
│   ├── ingredients/
│   ├── techniques/
│   ├── cuisines/
│   ├── tools/
│   └── people/
├── _templates/             # Obsidian manual entry templates
└── agent/
    ├── orchestrator.py     # Coordinates all sub-agents
    ├── compile.py          # CLI: ingest inbox
    ├── watch.py            # CLI: file watcher
    ├── query.py            # CLI: query the wiki
    ├── lint.py             # CLI: health check
    ├── gemini.py           # Gemini client wrapper
    ├── utils.py            # Shared utilities
    ├── agents/
    │   ├── classifier.py   # Determines content type
    │   ├── writer.py       # Generates + writes wiki pages
    │   ├── cross_linker.py # Updates existing pages (runs in parallel)
    │   ├── logger.py       # Writes wiki/log.md
    │   ├── indexer.py      # Regenerates wiki/index.md
    │   ├── query_agent.py  # Answers questions from wiki
    │   └── lint_agent.py   # Vault health check
    ├── config.yaml
    └── prompts/
        ├── classify.txt
        ├── extract_recipe.txt
        ├── extract_ingredient.txt
        ├── extract_technique.txt
        ├── update_index.txt
        ├── cross_link_scan.txt
        ├── cross_link_update.txt
        ├── query.txt
        └── lint.txt
```

---

## What Goes in `inbox/`?

Anything cooking-related as plain text or markdown:

| What to drop | Example |
|---|---|
| A recipe you found online | Copy-paste it as `.txt` |
| A technique you want to learn | Your own notes in `.md` |
| Notes from a cooking class | Rough bullet points are fine |
| A chef you want to remember | Bio snippet from Wikipedia |
| A cuisine overview | Saved article as `.txt` |

---

## Configuration

Edit `agent/config.yaml` to change the Gemini model, debounce delay, max backlinks, etc.

---

## Obsidian Tips

Open `cooking-brain/` as your vault in Obsidian.

- **Graph View** — see how all recipes, techniques, and ingredients interconnect
- **Backlinks pane** — see every recipe that uses `[[Emulsification]]`
- **Dataview plugin** — query: `TABLE cuisine, difficulty FROM "wiki/recipes"`
- **Search** — full-text across your entire cooking knowledge
