"""
Cleaner Sub-Agent
==================
First step in the pipeline. Extracts clean content from raw input files
(PDFs saved from browser, copy-pasted web pages, URLs, YouTube videos,
plain text with boilerplate).

Contract:
  - NEVER modifies or writes the original file.
  - Returns clean text as a string — the rest of the pipeline works in memory.
  - For PDFs: extracts raw text via pypdf, then strips boilerplate via Gemini.
  - For URLs: fetches and extracts article text via trafilatura, then Gemini clean.
  - For YouTube: transcript via youtube-transcript-api (free), falls back to
    Gemini native video processing if no transcript is available.
  - For .txt/.md: strips boilerplate via Gemini.
  - Falls back to raw text if extraction fails, so the pipeline never hard-stops.

Cost:
  - PDF/txt/html/url: 1 Gemini call (boilerplate removal)
  - YouTube w/ transcript: 1 Gemini call (cleaning transcript text)
  - YouTube w/o transcript: 1 Gemini call (native video processing, ~300 tok/sec)
  - PDF text extraction: pure Python (zero API cost)
"""

import re
import logging
import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent.parent
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

from gemini import call_gemini, call_gemini_video   # noqa: E402
from utils import load_prompt                        # noqa: E402

log = logging.getLogger("cooking-brain.cleaner")

# File types that go through Gemini boilerplate removal
_CLEAN_EXTENSIONS = {".txt", ".md", ".html", ".htm", ".pdf"}

# Patterns that identify a YouTube URL
_YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{11})"
)


class CleanerAgent:
    def __init__(self, client, gemini_cfg: dict, prompts_dir: Path):
        self.client     = client
        self.gemini_cfg = gemini_cfg
        self._prompt    = load_prompt(prompts_dir, "clean")

    # ── Public entry points ──────────────────────────────────────────────────

    def run(self, file_path: Path) -> str:
        """
        Extract and clean text from a raw input file.
        Returns clean text. Original file is never touched.
        """
        suffix = file_path.suffix.lower()

        if suffix not in _CLEAN_EXTENSIONS:
            log.info(f"  [cleaner] Unknown type '{suffix}' — passing through raw.")
            return _read_text(file_path)

        # Step 1: extract raw text
        if suffix == ".pdf":
            raw = _extract_pdf(file_path)
        else:
            raw = _read_text(file_path)

        if not raw.strip():
            log.warning(f"  [cleaner] No text extracted from {file_path.name}.")
            return ""

        return self._clean(raw)

    def run_url(self, url: str) -> str:
        """
        Fetch and clean a web URL.
        Returns clean text. Nothing is written to disk.
        """
        # Check for YouTube first
        yt_match = _YOUTUBE_RE.search(url)
        if yt_match:
            video_id = yt_match.group(1)
            return self._clean_youtube(video_id, url)

        # Regular web page
        raw = _fetch_url(url)
        if not raw.strip():
            log.warning(f"  [cleaner] No text extracted from URL.")
            return ""

        return self._clean(raw)

    # ── Internal ─────────────────────────────────────────────────────────────

    def _clean(self, raw: str) -> str:
        """Gemini boilerplate removal on raw text."""
        log.info(f"  [cleaner] Extracted {len(raw.split())} words — stripping boilerplate…")

        clean = call_gemini(self.client, self.gemini_cfg, self._prompt, raw)

        # Safety: if Gemini returns something far shorter, the prompt may have
        # over-stripped. Fall back to raw text so the pipeline still runs.
        if len(clean.split()) < len(raw.split()) * 0.15:
            log.warning(
                f"  [cleaner] Clean output suspiciously short "
                f"({len(clean.split())} vs {len(raw.split())} words) — using raw text."
            )
            return raw

        log.info(
            f"  [cleaner] {len(raw.split())} → {len(clean.split())} words "
            f"({100 * len(clean.split()) // len(raw.split())}% kept)."
        )
        return clean

    def _clean_youtube(self, video_id: str, url: str) -> str:
        """
        Extract content from a YouTube video.
        Strategy: transcript API (free) → Gemini native video (fallback).
        """
        log.info(f"  [cleaner] YouTube detected: {video_id}")

        # Try transcript first (free, fast)
        transcript = _fetch_youtube_transcript(video_id)

        if transcript:
            log.info(
                f"  [cleaner] Transcript fetched: {len(transcript.split())} words "
                f"— cleaning via Gemini…"
            )
            return self._clean(transcript)

        # No transcript available — fall back to Gemini native video processing
        log.info(
            "  [cleaner] No transcript available — "
            "using Gemini native video processing…"
        )
        clean = call_gemini_video(
            self.client, self.gemini_cfg, self._prompt, url
        )

        if not clean or not clean.strip():
            log.warning("  [cleaner] Gemini video processing returned empty.")
            return ""

        log.info(f"  [cleaner] Gemini video extraction: {len(clean.split())} words.")
        return clean


# ── Helpers ───────────────────────────────────────────────────────────────────

def _read_text(file_path: Path) -> str:
    """Read a text file, trying UTF-8 then latin-1."""
    for enc in ("utf-8", "latin-1"):
        try:
            return file_path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return file_path.read_bytes().decode("utf-8", errors="replace")


def _extract_pdf(file_path: Path) -> str:
    """Extract plain text from a PDF using pypdf."""
    try:
        import pypdf
    except ImportError:
        log.error(
            "  [cleaner] PDF detected but 'pypdf' is not installed. "
            "Run: pip install pypdf"
        )
        return ""

    try:
        reader = pypdf.PdfReader(str(file_path))
        pages  = [page.extract_text() or "" for page in reader.pages]
        text   = "\n\n".join(pages)
        log.info(f"  [cleaner] PDF: extracted {len(reader.pages)} page(s).")
        return text
    except Exception as e:
        log.error(f"  [cleaner] PDF extraction failed: {e}")
        return ""


def _fetch_url(url: str) -> str:
    """Fetch and extract article text from a web URL using trafilatura."""
    try:
        import trafilatura
    except ImportError:
        log.error(
            "  [cleaner] URL ingestion requires 'trafilatura'. "
            "Run: pip install trafilatura"
        )
        return ""

    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            log.warning(f"  [cleaner] Could not fetch URL: {url}")
            return ""

        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
        )
        if not text:
            log.warning(f"  [cleaner] trafilatura extracted no text from: {url}")
            return ""

        log.info(f"  [cleaner] URL: extracted {len(text.split())} words.")
        return text

    except Exception as e:
        log.error(f"  [cleaner] URL fetch failed: {e}")
        return ""


def _fetch_youtube_transcript(video_id: str) -> str:
    """
    Fetch YouTube transcript via youtube-transcript-api (free, no API key).
    Returns joined transcript text, or empty string on failure.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
    except ImportError:
        log.error(
            "  [cleaner] YouTube transcript requires 'youtube-transcript-api'. "
            "Run: pip install youtube-transcript-api"
        )
        return ""

    try:
        api = YouTubeTranscriptApi()
        transcript = api.fetch(video_id)
        # Join all snippet texts into a single block
        text = " ".join(snippet.text for snippet in transcript)
        return text

    except Exception as e:
        log.info(f"  [cleaner] Transcript not available: {e}")
        return ""
