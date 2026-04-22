"""
Cooking Brain — Orchestrator
=============================
Coordinates all sub-agents. Processes inbox files in parallel using a
thread pool, then runs cross-linking and indexing after each batch.

Parallelism model:
  - Outer pool: up to MAX_FILE_WORKERS inbox files processed simultaneously.
  - Inner pool (inside CrossLinkerAgent): up to 5 page-update calls in parallel
    per ingested file.
"""

import shutil
import logging
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from utils import load_config                          # noqa: E402
from gemini import init_gemini                         # noqa: E402
from agents.processing.cleaner      import CleanerAgent      # noqa: E402
from agents.processing.classifier   import ClassifierAgent   # noqa: E402
from agents.processing.writer       import WriterAgent       # noqa: E402
from agents.processing.standardizer import StandardizerAgent # noqa: E402
from agents.processing.wiki_linker  import WikiLinkerAgent   # noqa: E402
from agents.filing.cross_linker     import CrossLinkerAgent  # noqa: E402
from agents.filing.logger           import LoggerAgent       # noqa: E402
from agents.filing.indexer          import IndexerAgent      # noqa: E402
from agents.retrieval.query_agent   import QueryAgent        # noqa: E402
from agents.maintenance.lint_agent  import LintAgent         # noqa: E402

log = logging.getLogger("cooking-brain.orchestrator")

MAX_FILE_WORKERS = 3   # parallel inbox files


class Orchestrator:
    def __init__(self, cfg: dict, dry_run: bool = False):
        self.cfg     = cfg
        self.dry_run = dry_run

        client      = init_gemini(cfg)
        prompts_dir = cfg["paths"]["prompts"]
        wiki_root   = cfg["paths"]["wiki"]

        # Build a per-agent gemini config, merging defaults with optional per-agent overrides.
        # Strips the 'agents' sub-dict so it never leaks into call_gemini().
        def _acfg(name: str) -> dict:
            base      = {k: v for k, v in cfg["gemini"].items() if k != "agents"}
            overrides = cfg["gemini"].get("agents", {}).get(name, {})
            return {**base, **overrides}

        std_cfg = cfg.get("standardizer", {})
        self.cleaner      = CleanerAgent(client, _acfg("cleaner"),      prompts_dir)
        self.classifier   = ClassifierAgent(client, _acfg("classifier"), prompts_dir)
        self.writer       = WriterAgent(client, _acfg("writer"),         prompts_dir, wiki_root)
        self.standardizer = StandardizerAgent(
            client, _acfg("standardizer"), prompts_dir,
            min_body_words = std_cfg.get("min_body_words", 80),
            min_wiki_links = std_cfg.get("min_wiki_links", 8),
        ) if std_cfg.get("enabled", True) else None
        self.wiki_linker  = WikiLinkerAgent(client, _acfg("wiki_linker"),  prompts_dir)
        self.cross_linker = CrossLinkerAgent(client, _acfg("cross_linker"), prompts_dir, wiki_root)
        self.logger       = LoggerAgent(wiki_root)
        self.indexer      = IndexerAgent(client, _acfg("indexer"),         prompts_dir, wiki_root)
        self.query_agent  = QueryAgent(client, _acfg("query_agent"),       prompts_dir, wiki_root)
        self.lint_agent   = LintAgent(client, _acfg("lint"),               prompts_dir, wiki_root,
                                      lint_cfg=cfg.get("lint"))

        self._wiki_root  = wiki_root

    # ══════════════════════════════════════════════════════════════════════════
    #  Ingest
    # ══════════════════════════════════════════════════════════════════════════

    def process_file(self, file_path: Path) -> bool:
        """
        Full pipeline for one inbox file:
          classify → generate → write → cross-link → log → archive
        Returns True on success.
        """
        log.info(f"▶  Processing: {file_path.name}")

        # 0. Clean — strip boilerplate from raw input (original file never modified)
        raw_text = self.cleaner.run(file_path)

        if not raw_text.strip():
            log.warning(f"Empty or unreadable file skipped: {file_path.name}")
            return False

        # 1. Classify
        classification = self.classifier.run(raw_text)
        category   = classification.get("category", "general_note")
        title      = classification.get("title_suggestion", file_path.stem)
        confidence = classification.get("confidence", 0.0)

        # 2. Generate wiki page
        content = self.writer.generate(category, raw_text, source_url=file_path.name)

        # 3. Standardize — check completeness, enrich if needed
        issues_fixed: list[str] = []
        if self.standardizer:
            content, issues_fixed = self.standardizer.run(category, content)
            if issues_fixed:
                log.info(f"  → Standardizer fixed {len(issues_fixed)} issue(s).")

        # 4. WikiLink — dedicated aggressive link-annotation pass
        content, links_added = self.wiki_linker.run(content)

        # 5. Write wiki page
        wiki_page_path = self.writer.write(category, title, content, self.dry_run)

        # 5. Cross-link existing pages (parallel internally)
        pages_updated = 0
        if not self.dry_run:
            try:
                pages_updated = self.cross_linker.run(wiki_page_path, content, self.dry_run)
            except Exception as e:
                log.error(f"Cross-linking failed for {file_path.name}: {e}", exc_info=True)

        # 6. Archive source file
        if not self.dry_run:
            dest = self.cfg["paths"]["processed"] / file_path.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), str(dest))
            log.info(f"  → Archived to processed/")

        # 7. Log
        self.logger.log_ingest(
            source_file   = file_path.name,
            category      = category,
            title         = title,
            wiki_page     = str(wiki_page_path.relative_to(self._wiki_root)),
            pages_updated = pages_updated,
            issues_fixed  = issues_fixed,
        )

        log.info(
            f"  ✓ Done: '{title}' | "
            f"standardized {len(issues_fixed)} issue(s) | "
            f"+{links_added} links | "
            f"cross-linked {pages_updated} page(s)."
        )
        return True

    def process_url(self, url: str) -> bool:
        """
        Full pipeline for a URL (web page or YouTube video).
        The URL is fetched and cleaned in memory — nothing is written to inbox.
        Returns True on success.
        """
        log.info(f"▶  Processing URL: {url}")

        # 0. Clean — fetch + extract + strip boilerplate (nothing written to disk)
        raw_text = self.cleaner.run_url(url)

        if not raw_text.strip():
            log.warning(f"No content extracted from URL: {url}")
            return False

        # 1. Classify
        classification = self.classifier.run(raw_text)
        category   = classification.get("category", "general_note")
        title      = classification.get("title_suggestion", "untitled")
        confidence = classification.get("confidence", 0.0)

        # 2. Generate wiki page
        content = self.writer.generate(category, raw_text, source_url=url)

        # 3. Standardize
        issues_fixed: list[str] = []
        if self.standardizer:
            content, issues_fixed = self.standardizer.run(category, content)
            if issues_fixed:
                log.info(f"  → Standardizer fixed {len(issues_fixed)} issue(s).")

        # 4. WikiLink
        content, links_added = self.wiki_linker.run(content)

        # 5. Write wiki page
        wiki_page_path = self.writer.write(category, title, content, self.dry_run)

        # 6. Cross-link existing pages
        pages_updated = 0
        if not self.dry_run:
            try:
                pages_updated = self.cross_linker.run(wiki_page_path, content, self.dry_run)
            except Exception as e:
                log.error(f"Cross-linking failed for URL: {e}", exc_info=True)

        # 7. Log (source_file records the URL for provenance)
        self.logger.log_ingest(
            source_file   = url,
            category      = category,
            title         = title,
            wiki_page     = str(wiki_page_path.relative_to(self._wiki_root)),
            pages_updated = pages_updated,
            issues_fixed  = issues_fixed,
        )

        log.info(
            f"  ✓ Done: '{title}' | "
            f"standardized {len(issues_fixed)} issue(s) | "
            f"+{links_added} links | "
            f"cross-linked {pages_updated} page(s)."
        )

        if self.cfg["agent"]["update_index_on_every_run"]:
            self.indexer.run(self.dry_run)

        return True

    def _process_urls_file(self, urls_file: Path):
        """
        Parse inbox/urls.txt, process each URL, then archive and clear the file.
        Lines starting with # or blank lines are ignored.
        """
        if not urls_file.exists():
            return

        raw_lines = urls_file.read_text(encoding="utf-8").splitlines()
        urls = [
            line.strip() for line in raw_lines
            if line.strip() and not line.strip().startswith("#")
        ]

        if not urls:
            return

        log.info(f"Found {len(urls)} URL(s) in urls.txt — processing…")
        success = 0

        for url in urls:
            if not re.match(r"https?://", url):
                log.warning(f"  [urls] Skipping invalid URL: {url}")
                continue
            if self.process_url(url):
                success += 1

        log.info(f"  [urls] {success}/{len(urls)} URL(s) processed.")

        # Archive processed urls.txt and reset the file
        if not self.dry_run:
            processed_dir = self.cfg["paths"]["processed"]
            processed_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            archive_path = processed_dir / f"urls-{timestamp}.txt"
            archive_path.write_text(
                "\n".join(f"# processed {timestamp}\n{u}" for u in urls),
                encoding="utf-8",
            )
            # Reset urls.txt to template (keep comments, remove URLs)
            template_lines = [l for l in raw_lines if not l.strip() or l.strip().startswith("#")]
            urls_file.write_text("\n".join(template_lines) + "\n", encoding="utf-8")
            log.info(f"  [urls] Archived to processed/urls-{timestamp}.txt — urls.txt cleared.")

    def process_inbox(self):
        """Process all eligible inbox files and urls.txt in parallel."""
        inbox      = self.cfg["paths"]["inbox"]
        extensions = self.cfg["watcher"]["extensions"]

        # Process urls.txt first if it has any URLs
        self._process_urls_file(inbox / "urls.txt")

        candidates = [
            f for f in inbox.iterdir()
            if f.is_file()
            and f.suffix.lower() in extensions
            and f.name != "urls.txt"
        ]

        if not candidates:
            log.info("Inbox is empty — nothing to process.")
            return

        log.info(
            f"Found {len(candidates)} file(s). "
            f"Processing in parallel (max {MAX_FILE_WORKERS} workers)…"
        )
        success = 0

        with ThreadPoolExecutor(max_workers=MAX_FILE_WORKERS) as pool:
            futures = {pool.submit(self.process_file, f): f for f in candidates}
            for fut in as_completed(futures):
                try:
                    if fut.result():
                        success += 1
                except Exception as e:
                    log.error(f"Failed: {futures[fut].name}: {e}", exc_info=True)

        log.info(f"Processed {success}/{len(candidates)} file(s).")

        if success > 0 and self.cfg["agent"]["update_index_on_every_run"]:
            self.indexer.run(self.dry_run)

    # ══════════════════════════════════════════════════════════════════════════
    #  Query
    # ══════════════════════════════════════════════════════════════════════════

    def query(self, question: str, file_answer: bool = False) -> dict:
        """
        Answer a question from the wiki.
        If file_answer=True, saves the answer as a new wiki page.
        """
        result = self.query_agent.run(question)

        filed_page = ""
        if file_answer and not self.dry_run:
            page_path  = self.query_agent.file_answer(
                question, result["answer"], self._wiki_root, self.dry_run
            )
            filed_page = str(page_path.relative_to(self._wiki_root))
            # Re-index so the new page appears in the index
            self.indexer.run(self.dry_run)

        self.logger.log_query(question, filed_page=filed_page)
        return result

    # ══════════════════════════════════════════════════════════════════════════
    #  Lint
    # ══════════════════════════════════════════════════════════════════════════

    def lint(self) -> dict:
        """Run a health check on the wiki vault."""
        report = self.lint_agent.run()
        self.logger.log_lint(report)
        return report

    # ══════════════════════════════════════════════════════════════════════════
    #  Sort
    # ══════════════════════════════════════════════════════════════════════════

    def sort(self, dry_run: bool = False, delete_garbled: bool = False) -> tuple[int, int]:
        """
        Move misplaced wiki root files to their correct subfolders based on
        frontmatter tags. Detects and optionally deletes garbled files (Gemini
        error messages written as page content).

        Returns (moved_count, flagged_count).
        """
        import yaml
        from utils import slugify, CATEGORY_DIR

        _TAG_TO_CATEGORY: dict[str, str] = {
            "recipe":            "recipe",
            "ingredient":        "ingredient",
            "technique":         "technique",
            "cuisine":           "cuisine",
            "tool":              "tool",
            "person":            "person",
            "chef":              "person",
            "safety":            "safety",
            "food-safety":       "safety",
            "management":        "management",
            "science":           "science",
            "chemistry":         "science",
            "food-science":      "science",
            "other":             "other",
            "culinary_concept":  "other",
            "culinary_category": "other",
            "reference":         "other",
        }

        _GARBLED_PREFIXES = (
            "The provided content",
            "To generate a",
            "I need",
            "Please provide",
            "The content provided",
        )

        moved   = 0
        flagged = 0

        for md_file in sorted(self._wiki_root.glob("*.md")):
            if md_file.name in ("index.md", "log.md"):
                continue

            text     = md_file.read_text(encoding="utf-8")
            stripped = text.strip()

            # ── Garbled file detection ────────────────────────────────────────
            if not re.match(r"^---", stripped):
                if any(stripped.startswith(p) for p in _GARBLED_PREFIXES):
                    log.warning(f"  [sort] GARBLED: {md_file.name}")
                    flagged += 1
                    if delete_garbled and not dry_run:
                        md_file.unlink()
                        log.info(f"  [sort] Deleted: {md_file.name}")
                else:
                    log.warning(f"  [sort] REVIEW (no frontmatter): {md_file.name}")
                    flagged += 1
                continue

            # ── Parse frontmatter ─────────────────────────────────────────────
            fm_match = re.match(r"^---\s*\n(.*?)\n---", stripped, re.DOTALL)
            if not fm_match:
                log.warning(f"  [sort] REVIEW (malformed frontmatter): {md_file.name}")
                flagged += 1
                continue

            try:
                fm = yaml.safe_load(fm_match.group(1)) or {}
            except Exception:
                log.warning(f"  [sort] REVIEW (YAML error): {md_file.name}")
                flagged += 1
                continue

            tags  = fm.get("tags", [])
            if isinstance(tags, str):
                tags = [tags]
            title = fm.get("title", md_file.stem)

            # ── Infer category from tags ──────────────────────────────────────
            category = None
            for tag in tags:
                key = str(tag).lower().replace(" ", "-")
                if key in _TAG_TO_CATEGORY:
                    category = _TAG_TO_CATEGORY[key]
                    break

            if not category:
                log.warning(f"  [sort] REVIEW (unrecognised tags {tags}): {md_file.name}")
                flagged += 1
                continue

            # ── Determine destination ─────────────────────────────────────────
            subdir = CATEGORY_DIR.get(category, "other")
            if subdir == ".":
                subdir = "other"

            dest = self._wiki_root / subdir / f"{slugify(title)}.md"

            if dest.exists():
                log.warning(f"  [sort] CONFLICT (dest exists): {md_file.name} → {subdir}/{dest.name}")
                flagged += 1
                continue

            log.info(f"  [sort] {'WOULD MOVE' if dry_run else 'MOVE'}: {md_file.name} → {subdir}/{dest.name}")
            if not dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                md_file.rename(dest)
            moved += 1

        log.info(f"  [sort] {moved} moved, {flagged} flagged{'  (dry-run)' if dry_run else ''}.")
        return moved, flagged

    # ══════════════════════════════════════════════════════════════════════════
    #  Reindex
    # ══════════════════════════════════════════════════════════════════════════

    def reindex(self):
        self.indexer.run(self.dry_run)
