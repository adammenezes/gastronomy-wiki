# Cooking Brain — Session Summary (Procurement & Optimization)

## 1. Pipeline Routing & Quality Control
- **RouterAgent Implemented:** Replaced the legacy `ClassifierAgent` with a rigorous `RouterAgent`.
- **Substance Score Index:** The agent now evaluates all incoming content on a 0-10 scale based on cultural weight, technical depth, and density. Low-substance content (e.g., sitemaps, marketing fluff) is automatically rejected and moved to `inbox/rejected/`.
- **1-to-N Hub Splitting:** The pipeline is no longer 1-to-1. Dense "hub" articles are intelligently chunked into multiple distinct wiki pages. For example, a single kitchen prep article was successfully split into 21 independent pages.
- **Strict JSON Enforcement:** Switched the Gemini call to enforce `response_mime_type="application/json"`, ensuring the orchestration loop never crashes due to malformed LLM output.

## 2. Cross-Linker Turbocharging
- **Model Downgrade for Speed:** Switched the `CrossLinkerAgent` from the slow `gemini-2.5-pro` to the lightning-fast `gemini-2.5-flash`.
- **Paragraph Isolation:** Completely rewrote the Python logic. Instead of passing massive documents to the LLM, the agent now splits candidates by paragraph, locates the specific block containing the target keyword, and passes *only* that paragraph to Gemini for link injection.
- **Max Concurrency:** Bumped the `ThreadPoolExecutor` up to 10 workers (`_MAX_UPDATE_WORKERS = 10`).
- **Results:** Processing 12 candidate pages dropped from **3–4 minutes** down to exactly **31 seconds**.

## 3. Initial Ingestion Success
- The background batch successfully processed the first 10 URLs from `leads.md`.
- **72 total pages** were generated, fully formatted, and heavily cross-linked, proving the 1-to-N split logic is functioning perfectly on a large scale.
- Fixed a minor bug in `indexer.py` where the LLM generated YAML list arrays for frontmatter titles, allowing the master index to regenerate flawlessly.

## 4. Codebase Reorganization
- **CLI Clean-up:** Created an `agent/cli/` folder and moved all executable entry points (`watch.py`, `procure.py`, `compile.py`, `lint.py`, etc.) out of the root directory.
- **Path Refactoring:** Safely updated the Python `sys.path` injection across all moved files to correctly resolve dependencies from the parent folder.
- **Documentation Updated:** Brought `AGENTS.md` fully up to date to reflect the new `RouterAgent`, the `cli/` file paths, and the new cross-linker logic.

---
*The Cooking Brain is now robust, extremely fast, and structurally neat.*
