"""
Cooking Brain — Web Server
===========================
FastAPI server: wraps the Orchestrator over HTTP and serves the frontend.

Run:
    python agent/server.py

Then open: http://localhost:8000
"""

import sys
import shutil
import logging
from pathlib import Path
from datetime import datetime

import uvicorn
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from utils import load_config, collect_wiki_pages  # noqa: E402
from orchestrator import Orchestrator               # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cooking-brain.server")

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Cooking Brain", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_cfg  = None
_orch = None


def _get_orch() -> Orchestrator:
    global _cfg, _orch
    if _orch is None:
        _cfg  = load_config()
        _orch = Orchestrator(_cfg)
    return _orch


# ── Models ────────────────────────────────────────────────────────────────────

class QueryRequest(BaseModel):
    question: str
    filters: list[str] = []

class IngestUrlRequest(BaseModel):
    url: str

class IngestUrlsRequest(BaseModel):
    urls: list[str]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index():
    html = (_ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/status")
def status():
    _get_orch()
    pages = collect_wiki_pages(_cfg["paths"]["wiki"])
    by_cat: dict[str, int] = {}
    for p in pages:
        by_cat[p["category"]] = by_cat.get(p["category"], 0) + 1

    log_path = _cfg["paths"]["wiki"] / "log.md"
    last_updated = None
    if log_path.exists():
        last_updated = datetime.fromtimestamp(log_path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")

    return {"page_count": len(pages), "categories": by_cat, "last_updated": last_updated}


@app.get("/api/wiki/pages")
def list_pages():
    _get_orch()
    pages = collect_wiki_pages(_cfg["paths"]["wiki"])
    return [
        {
            "title":      p["title"],
            "category":   p["category"],
            "file":       p["file"],
            "date_added": str(p.get("date_added", "")),
            "tags":       p.get("tags", []),
        }
        for p in pages
    ]


@app.post("/api/query")
def query(req: QueryRequest):
    o = _get_orch()
    question = req.question
    if req.filters:
        question = f"{question} (focus: {', '.join(req.filters)})"
    return o.query(question)


@app.post("/api/ingest/url")
def ingest_url(req: IngestUrlRequest):
    o = _get_orch()
    ok = o.process_url(req.url)
    if not ok:
        raise HTTPException(422, detail=f"Could not extract content from: {req.url}")
    return {"success": True}


@app.post("/api/ingest/urls")
def ingest_urls(req: IngestUrlsRequest):
    o = _get_orch()
    processed, failed = [], []
    for url in req.urls:
        try:
            if o.process_url(url):
                processed.append(url)
            else:
                failed.append(url)
        except Exception as e:
            log.error(f"Failed {url}: {e}")
            failed.append(url)
    return {"processed": len(processed), "failed": len(failed), "failed_urls": failed}


@app.post("/api/ingest/file")
def ingest_file(file: UploadFile = File(...)):
    o    = _get_orch()
    dest = _cfg["paths"]["inbox"] / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    try:
        ok = o.process_file(dest)
        if ok:
            o.reindex()
        return {"success": ok, "filename": file.filename}
    except Exception as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(500, detail=str(e))


@app.get("/api/log")
def get_log():
    _get_orch()
    log_path = _cfg["paths"]["wiki"] / "log.md"
    if not log_path.exists():
        return {"entries": []}
    lines = log_path.read_text(encoding="utf-8").splitlines()
    # Return last ~30 lines (enough for recent activity)
    return {"content": "\n".join(lines[-60:])}


if __name__ == "__main__":
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True, app_dir=str(_HERE))
