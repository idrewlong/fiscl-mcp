"""Inbox watcher: import files dropped into ~/fiscl/inbox, then file them as processed or failed."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import Config
from .db import open_db
from .guard import clean_text
from .importers.core import SUPPORTED, import_file

log = logging.getLogger("fiscl.watch")


def _wait_until_stable(path: Path, timeout: float = 30.0) -> bool:
    """Wait until a file stops growing (browsers write downloads in chunks)."""
    last, deadline = -1, time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last and size > 0:
            return True
        last = size
        time.sleep(0.5)
    return False


def _move(path: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / path.name
    n = 1
    while target.exists():
        target = dest_dir / f"{path.stem}-{n}{path.suffix}"
        n += 1
    shutil.move(str(path), target)
    return target


def process_inbox_file(cfg: Config, path: Path, on_imported: Callable[[], None] | None = None) -> str:
    """Import one inbox file. The watcher never guesses an account: CSVs need a profile with
    `account` set, otherwise they go to failed/ with a note. `on_imported` runs after a file that added rows."""
    if path.suffix.lower() not in SUPPORTED or path.parent != cfg.inbox or path.name.startswith("."):
        return "ignored"
    if path.is_symlink() or not path.is_file():  # a symlink could point anywhere; a FIFO would block the read
        return "ignored"
    if not _wait_until_stable(path):
        return "ignored"
    try:
        with open_db(cfg) as conn:
            result = import_file(conn, cfg, path)
    except Exception as exc:  # unexpected; keep the watcher alive
        log.exception("import crashed for %s", path.name)
        result = None
        error = f"unexpected error: {type(exc).__name__}"
    else:
        error = clean_text(result.error, 200)
    if result is not None and result.status in ("ok", "duplicate_file"):
        _move(path, cfg.processed)
        log.info("imported %s: %s added, %s skipped", path.name, result.rows_added, result.rows_skipped)
        if on_imported is not None and result.status == "ok":
            try:
                on_imported()
            except Exception:  # a notification problem must never stop the watcher
                log.exception("on_imported callback failed")
        return result.status
    moved = _move(path, cfg.failed)
    moved.with_suffix(moved.suffix + ".error.txt").write_text(f"{error}\n", encoding="utf-8")
    log.warning("import failed for %s: %s", path.name, error)
    return "failed"


class _Handler(FileSystemEventHandler):
    def __init__(self, cfg: Config, on_imported: Callable[[], None] | None = None):
        self.cfg = cfg
        self.on_imported = on_imported
        self.lock = threading.Lock()

    def _handle(self, src: str) -> None:
        with self.lock:
            process_inbox_file(self.cfg, Path(src), self.on_imported)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(str(event.src_path))

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._handle(str(event.dest_path))


def start_watcher(cfg: Config, on_imported: Callable[[], None] | None = None):
    """Process anything already waiting in the inbox, then watch for new files."""
    for path in sorted(cfg.inbox.iterdir()):
        if path.is_file() and not path.is_symlink():
            process_inbox_file(cfg, path, on_imported)
    observer = Observer()
    observer.schedule(_Handler(cfg, on_imported), str(cfg.inbox), recursive=False)
    observer.daemon = True
    observer.start()
    return observer
