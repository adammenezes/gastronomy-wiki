"""
Cross-Linker Sub-Agent
=======================
After a new wiki page is written, identifies existing pages that should
reference it and updates them in parallel.

Pipeline per ingest:
  1. Python keyword filter (free): find existing pages that mention the new
     page's title terms but don't already link to it. O(disk reads), not O(N*tokens).
  2. N parallel Gemini calls (update): insert the [[WikiLink]] into each candidate.

The old Gemini scan call (which sent ALL page metadata on every ingest) is gone.
Cost is now flat regardless of wiki size.
"""

import re
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from gemini import call_gemini        # noqa: E402
from utils import load_prompt, collect_wiki_pages  # noqa: E402

log = logging.getLogger("cooking-brain.cross_linker")

_MAX_UPDATE_WORKERS = 10   # parallel page-update calls
_MAX_CANDIDATES     = 12  # cap to avoid runaway updates on very generic titles

# Common words that are too generic to use as search terms
_STOP_WORDS = {
    "with", "and", "the", "for", "how", "make", "making", "using", "use",
    "from", "into", "over", "about", "also", "when", "than", "that", "this",
    "each", "some", "very", "well", "just", "more", "most", "then", "they",
    "will", "note", "tips", "dish", "food", "cook", "cooking",
}


class CrossLinkerAgent:
    def __init__(self, client, gemini_cfg: dict, prompts_dir: Path, wiki_root: Path):
        self.client         = client
        self.gemini_cfg     = gemini_cfg
        self.wiki_root      = wiki_root
        self._update_prompt = load_prompt(prompts_dir, "cross_link_update")

    def run(self, new_page_path: Path, new_page_content: str, dry_run: bool) -> int:
        """
        Find existing pages that mention the new page's title and update them.
        Returns the number of pages actually updated.
        """
        existing = collect_wiki_pages(self.wiki_root, include_content=True)

        # Exclude the new page itself
        new_rel = str(new_page_path.relative_to(self.wiki_root))
        existing = [p for p in existing if p["file"] != new_rel]

        if not existing:
            return 0

        # ── Step 1: keyword pre-filter (Python, zero API cost) ─────────────────
        title, link_text, search_terms = _extract_title_info(new_page_content, new_page_path)
        candidates = _keyword_candidates(existing, title, link_text, search_terms)

        if not candidates:
            log.info("  [cross_linker] No candidate pages found via keyword filter.")
            return 0

        log.info(
            f"  [cross_linker] {len(candidates)} candidate(s) from keyword filter "
            f"(terms: {', '.join(search_terms)}) — updating in parallel…"
        )

        # ── Step 2: update each candidate page in parallel ─────────────────────
        updated = 0

        def _update(page: dict) -> bool:
            file_key         = page["file"]
            existing_content = page["content"]
            page_path: Path  = page["path"]

            # Skip if link already present (belt-and-suspenders)
            if link_text in existing_content:
                log.debug(f"  [cross_linker] {link_text} already in {file_key}")
                return False

            # Isolate paragraph
            # We split by \n\n but keep the delimiters so we can stitch it back exactly.
            blocks = re.split(r'(\n{2,})', existing_content)
            target_block_idx = -1
            target_block_text = ""
            
            for idx, block in enumerate(blocks):
                if idx % 2 == 0:  # Text blocks (delimiters are at odd indices)
                    if any(term in block.lower() for term in search_terms):
                        target_block_idx = idx
                        target_block_text = block
                        break
            
            if target_block_idx == -1:
                log.warning(f"  [cross_linker] Could not locate keyword block in {file_key}.")
                return False

            reason = (
                f"This paragraph mentions '{title}' — add {link_text} where it first appears."
            )
            update_input = (
                f"PARAGRAPH TO UPDATE:\n{target_block_text}\n\n"
                f"NEW LINK TO ADD:\n{link_text}\n\n"
                f"REASON:\n{reason}"
            )
            updated_block = call_gemini(
                self.client, self.gemini_cfg, self._update_prompt, update_input
            ).strip()

            # Safety: only [[brackets]] may be added — no prose changes allowed
            if _delinked(updated_block) != _delinked(target_block_text.strip()):
                log.warning(
                    f"  [cross_linker] Prose altered in {file_key} — skipping."
                )
                return False

            if dry_run:
                log.info(f"  [cross_linker] [DRY RUN] Would update: {file_key}")
                return True

            # Stitch back
            blocks[target_block_idx] = updated_block
            updated_content = "".join(blocks)

            page_path.write_text(updated_content, encoding="utf-8")
            log.info(f"  [cross_linker] Updated: {file_key}")
            return True

        with ThreadPoolExecutor(max_workers=_MAX_UPDATE_WORKERS) as pool:
            futures = {pool.submit(_update, p): p for p in candidates}
            for fut in as_completed(futures):
                try:
                    if fut.result():
                        updated += 1
                except Exception as e:
                    log.error(f"  [cross_linker] Update failed: {e}", exc_info=True)

        return updated


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_title_info(content: str, path: Path) -> tuple[str, str, list[str]]:
    """
    Return (title, link_text, search_terms) from the new page.
    search_terms: significant words from the title (len > 3, not stop words).
    """
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if match:
        try:
            fm = yaml.safe_load(match.group(1)) or {}
            title = fm.get("title", path.stem)
        except yaml.YAMLError:
            title = path.stem
    else:
        title = path.stem

    link_text = f"[[{title}]]"

    # All words from title that are significant
    words = re.findall(r"[a-zA-Z']+", title.lower())
    search_terms = [
        w for w in words
        if len(w) > 3 and w not in _STOP_WORDS
    ]
    # Always include the full title as a term (catches "Pasta Carbonara" as a phrase)
    if len(title) > 4:
        search_terms = [title.lower()] + search_terms

    return title, link_text, search_terms


def _delinked(text: str) -> str:
    """Strip [[WikiLink]] brackets, keep inner text, normalise whitespace."""
    plain = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", r"\1", text)
    return " ".join(plain.split())


def _keyword_candidates(
    pages: list[dict],
    title: str,
    link_text: str,
    search_terms: list[str],
) -> list[dict]:
    """
    Return pages that:
    1. Contain at least one search term in their title or body
    2. Do NOT already contain the link_text
    Capped at _MAX_CANDIDATES.
    """
    candidates = []
    link_lower = link_text.lower()

    for page in pages:
        content = page.get("content", "")
        # Skip if already linked
        if link_lower in content.lower():
            continue

        haystack = (page["title"] + " " + content).lower()
        if any(term in haystack for term in search_terms):
            candidates.append(page)
            if len(candidates) >= _MAX_CANDIDATES:
                break

    return candidates
