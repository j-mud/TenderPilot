"""Tender packs uploaded in the app: checked, saved to a temporary folder per browser session.

    output/uploads/<session>/tenders/<tender_id>/  booklet.pdf, appendix.pdf, meta.json
    output/uploads/<session>/results/              results.json, proposal .docx, tracker.db

Never written inside data/. A new upload from the same session replaces the old folder,
and folders of abandoned sessions are removed after a day.
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from .documents import DOC_EXTS, normalize_deadline

MAX_FILE_MB = 25
MAX_PAGES = 150
CHARS_PER_PAGE = 3000  # page estimate for DOCX/XLSX/MD/TXT


class UploadError(ValueError):
    """A problem with the uploaded files, worded for the user."""


def tender_id_for(title: str, first_file: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", title or Path(first_file).stem).strip("_")[:40]
    return f"UPLOAD_{slug or 'tender'}"


def build_meta(title: str = "", deadline: str = "", value: str = "") -> dict:
    """meta.json values from the optional form fields; these override what the model extracts."""
    meta: dict = {}
    if title.strip():
        meta["title"] = title.strip()
    if deadline.strip():
        if not normalize_deadline(deadline.strip())[0]:
            raise UploadError(f"Could not read the deadline '{deadline}'. Use e.g. 2026-12-10 or 1448-06-24 هـ.")
        meta["deadline"] = deadline.strip()
    if value.strip():
        try:
            meta["estimated_value_sar"] = float(re.sub(r"[,\s]|SAR|ر\.?س", "", value, flags=re.I))
        except ValueError:
            raise UploadError(f"Could not read the estimated value '{value}'. Use a number, e.g. 3800000.") from None
    return meta


def count_pages(pack: Path) -> int:
    pages = 0
    for f in pack.iterdir():
        if f.suffix.lower() == ".pdf":
            import pymupdf

            with pymupdf.open(f) as doc:
                pages += doc.page_count
        elif f.suffix.lower() in DOC_EXTS:
            from .documents import read_document

            pages += -(-len(read_document(f)) // CHARS_PER_PAGE)  # round up
    return pages


def save_pack(session_dir: Path, files: list[tuple[str, bytes]], meta: dict,
              max_file_mb: float = MAX_FILE_MB, max_pages: int = MAX_PAGES) -> tuple[Path, str]:
    """Save one tender pack (all files together). Returns (data dir for TenderPilot, tender_id)."""
    if not files:
        raise UploadError("Upload at least one file.")
    for name, data in files:
        if Path(name).suffix.lower() not in DOC_EXTS:
            raise UploadError(f"{name}: unsupported file type. Use PDF, DOCX, XLSX, MD or TXT.")
        if len(data) > max_file_mb * 1024 * 1024:
            raise UploadError(f"{name} is {len(data) / 1024 / 1024:.0f} MB; the limit is {max_file_mb:.0f} MB per file.")
    shutil.rmtree(session_dir, ignore_errors=True)  # a new upload replaces the previous one
    tender_id = tender_id_for(meta.get("title", ""), files[0][0])
    pack = session_dir / "tenders" / tender_id
    pack.mkdir(parents=True)
    for name, data in files:
        (pack / Path(name).name).write_bytes(data)  # Path(...).name drops any folder part of the name
    if meta:
        (pack / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    try:
        pages = count_pages(pack)
    except Exception as err:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise UploadError(f"Could not read the files: {err}") from None
    if pages > max_pages:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise UploadError(f"This pack is about {pages} pages; the limit is {max_pages}. Upload the booklet and key appendices only.")
    return session_dir, tender_id


def prune(uploads_root: Path, max_age_s: int = 86_400) -> None:
    """Remove upload folders of sessions that have been inactive for a day."""
    if not uploads_root.exists():
        return
    for d in uploads_root.iterdir():
        if d.is_dir() and time.time() - d.stat().st_mtime > max_age_s:
            shutil.rmtree(d, ignore_errors=True)
