"""JSON persistence for one paper book under the configured root."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from src.paper_book.models import BookState, DailyReport

_BOOK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class PaperBookError(ValueError):
    """A day-step the book will not apply."""


def paper_book_root(explicit: Path | str | None = None) -> Path:
    """Directory that holds ``<book_id>/state.json`` and daily reports."""
    if explicit:
        path = Path(explicit).expanduser()
    else:
        from src.config.accessor import get_env_config

        configured = (get_env_config().paths.vibe_trading_paper_book_root or "").strip()
        if configured:
            path = Path(configured).expanduser()
        else:
            path = Path.home() / ".vibe-trading" / "paper_book"
    path.mkdir(parents=True, exist_ok=True)
    return path


def require_book_id(book_id: str) -> str:
    text = (book_id or "").strip()
    if not _BOOK_ID.match(text):
        raise PaperBookError(f"invalid book id: {book_id!r}")
    return text


def require_date(as_of: str) -> str:
    text = (as_of or "").strip()
    if not _DATE.match(text):
        raise PaperBookError(f"invalid session date: {as_of!r}")
    return text


def book_dir(root: Path, book_id: str) -> Path:
    path = root / require_book_id(book_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_state(root: Path, book_id: str) -> BookState | None:
    path = book_dir(root, book_id) / "state.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PaperBookError(f"paper book state unreadable: {exc}") from exc
    state = BookState.from_dict(raw if isinstance(raw, dict) else None)
    state.book_id = book_id
    return state


def save_state(root: Path, state: BookState) -> Path:
    path = book_dir(root, state.book_id) / "state.json"
    _atomic_json(path, state.to_dict())
    return path


def save_report(root: Path, report: DailyReport) -> Path:
    day = require_date(report.as_of)
    path = book_dir(root, report.book_id) / "reports" / f"{day}.json"
    _atomic_json(path, report.to_dict())
    return path


def load_report(root: Path, book_id: str, as_of: str) -> dict | None:
    path = book_dir(root, book_id) / "reports" / f"{require_date(as_of)}.json"
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PaperBookError(f"paper book report unreadable: {exc}") from exc
    return raw if isinstance(raw, dict) else None


def list_report_dates(root: Path, book_id: str) -> list[str]:
    folder = book_dir(root, book_id) / "reports"
    if not folder.is_dir():
        return []
    dates = [path.stem for path in folder.glob("*.json") if _DATE.match(path.stem)]
    dates.sort()
    return dates


def previous_nav(root: Path, book_id: str, as_of: str, initial_cash: float) -> float:
    earlier = [day for day in list_report_dates(root, book_id) if day < as_of]
    if not earlier:
        return initial_cash
    report = load_report(root, book_id, earlier[-1]) or {}
    try:
        nav = float(report.get("nav"))
    except (TypeError, ValueError):
        return initial_cash
    return nav if nav > 0 else initial_cash


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
