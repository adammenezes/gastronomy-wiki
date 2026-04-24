"""
Cooking Brain — File Watcher
============================
Watches inbox/ and automatically triggers the Orchestrator whenever a
new file is added or modified.

Usage:
    python agent/watch.py
    python agent/watch.py --dry-run
"""

import sys
import time
import logging
import argparse
from pathlib import Path

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
except ImportError:
    print("ERROR: watchdog not installed.\nRun: pip install watchdog")
    sys.exit(1)

_HERE = Path(__file__).resolve().parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from utils import load_config           # noqa: E402
from orchestrator import Orchestrator   # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cooking-brain.watcher")


# ── Debounce helper ───────────────────────────────────────────────────────────

class _PendingFiles:
    """Tracks files added/modified, with debouncing before triggering the agent."""

    def __init__(self, debounce_seconds: float, orch: Orchestrator, extensions: list[str]):
        self.debounce_seconds = debounce_seconds
        self.orch             = orch
        self.extensions       = extensions
        self._pending: dict[str, float] = {}

    def add(self, path: str):
        self._pending[path] = time.monotonic()

    def flush_ready(self):
        """Process files that have been stable for debounce_seconds."""
        now   = time.monotonic()
        ready = [p for p, ts in list(self._pending.items())
                 if (now - ts) >= self.debounce_seconds]

        for path_str in ready:
            del self._pending[path_str]
            file_path = Path(path_str)

            if file_path.suffix.lower() not in self.extensions:
                continue
            if not file_path.exists():
                log.info(f"File disappeared before processing: {file_path.name}")
                continue

            log.info(f"▶  Inbox change detected — {file_path.name}")
            try:
                ok = self.orch.process_file(file_path)
                if ok and self.orch.cfg["agent"]["update_index_on_every_run"]:
                    self.orch.reindex()
            except Exception as e:
                log.error(f"Error processing {file_path.name}: {e}", exc_info=True)


# ── Watchdog handler ──────────────────────────────────────────────────────────

class InboxHandler(FileSystemEventHandler):
    def __init__(self, pending: _PendingFiles):
        self._pending = pending

    def on_created(self, event):
        if not event.is_directory:
            self._pending.add(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self._pending.add(event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self._pending.add(event.dest_path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cooking Brain — File Watcher")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    cfg = load_config()
    if args.dry_run:
        cfg["agent"]["dry_run"] = True

    orch       = Orchestrator(cfg, dry_run=args.dry_run)
    inbox_path = cfg["paths"]["inbox"]
    debounce   = cfg["watcher"]["debounce_seconds"]
    extensions = cfg["watcher"]["extensions"]

    pending = _PendingFiles(debounce, orch, extensions)
    handler = InboxHandler(pending)

    observer = Observer()
    observer.schedule(handler, str(inbox_path), recursive=False)
    observer.start()

    log.info("=" * 60)
    log.info("  Cooking Brain — File Watcher RUNNING")
    log.info(f"  Watching: {inbox_path}")
    log.info(f"  Debounce: {debounce}s")
    log.info("  Drop files into inbox/ to trigger the agent.")
    log.info("  Press Ctrl+C to stop.")
    log.info("=" * 60)

    try:
        while True:
            time.sleep(0.5)
            pending.flush_ready()
    except KeyboardInterrupt:
        log.info("Stopping watcher…")
        observer.stop()

    observer.join()
    log.info("Watcher stopped.")


if __name__ == "__main__":
    main()
