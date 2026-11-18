"""Where the web UI keeps your files while you use it.

Each upload gets a "session": a private folder (readable by your user account
only) inside one Retrack folder in the computer's temp directory, plus the
check's progress and results in memory.

The files never leave this computer, and they're deleted:
  - 60 minutes after the upload (SESSION_SECONDS): the fix page says so
    ("This page disappears ... after 60 minutes"),
  - when you press "Delete my files now" (SessionStore.delete),
  - when the server stops (SessionStore.delete_all, called by web.main),
  - when the server starts, in case it crashed last time and couldn't clean
    up (SessionStore.__init__ clears anything left over).
"""

import re
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = Path(tempfile.gettempdir()) / "retrack-sessions"
SESSION_SECONDS = 60 * 60  # the whole visit, payment included: then everything is deleted
ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")  # secrets.token_urlsafe(16) makes 22 characters


@dataclass
class Session:
    id: str
    folder: Path
    filename: str          # the name the user's file had, for display only
    upload_path: Path
    table: object = None   # importer.Table
    mapping: dict = None   # importer column mapping (proposed, then confirmed)
    proposed: dict = None  # what the rules proposed, to keep their confidence
    settings: dict = field(default_factory=lambda: {"smart_names": False, "ai_search": False})
    import_result: object = None  # importer.ImportResult
    rows: list = field(default_factory=list)     # CatalogRows, in catalog order
    results: list = field(default_factory=list)  # checker Results, one per row
    todo: list = field(default_factory=list)     # row numbers this run checks (all, or a re-check)
    state: str = "columns"  # columns -> ready -> running -> done | failed
    done: int = 0
    error: tuple = None     # (title, message) when state == "failed"
    claude_failures: int = 0
    cancelled: bool = False
    created_at: float = field(default_factory=time.monotonic)
    # Payment (see payments.py): what's been paid for, and the open order if any
    attestation: object = None   # fixes.Attestation, recorded when the user ticks the box
    order: dict = None           # {"songs": [...], "checkout_id": "...", "url": "...", "n": order number}
    paid_songs: list = field(default_factory=list)  # result indexes the user has paid for
    orders_placed: int = 0
    template_note: str = ""  # e.g. songs left out of a filled-in MLC template, and why

    @property
    def total(self):
        return len(self.todo)

    def fail(self, title, message):
        self.error = (title, message)
        self.state = "failed"


class SessionStore:
    def __init__(self, root=None, lifetime=SESSION_SECONDS, clock=time.monotonic):
        self.root = Path(root or DEFAULT_ROOT)  # looked up now, so tests can point it elsewhere
        self.lifetime = lifetime
        self.clock = clock  # passed in so tests can move time forward
        self._sessions = {}
        self._expired = set()  # ids deleted because their time ran out, to say so plainly
        self._lock = threading.Lock()
        self.clear_leftovers()

    def clear_leftovers(self):
        """Delete every session folder a previous run left behind (e.g. after a crash)."""
        if self.root.exists():
            for child in self.root.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)

    def create(self, filename, data):
        """Save an upload in a new private folder and return its Session."""
        session_id = secrets.token_urlsafe(16)
        folder = self.root / session_id
        folder.mkdir(mode=0o700)
        suffix = Path(filename).suffix.lower()
        upload_path = folder / f"upload{suffix if suffix in ('.csv', '.xlsx') else ''}"
        upload_path.write_bytes(data)
        session = Session(session_id, folder, Path(filename).name or "catalog", upload_path,
                          created_at=self.clock())
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id):
        if not ID_RE.match(session_id or ""):
            return None
        self.sweep()
        with self._lock:
            return self._sessions.get(session_id)

    def seconds_left(self, session):
        """How long until this session's files are deleted."""
        return max(0, int(self.lifetime - (self.clock() - session.created_at)))

    def expired(self, session_id):
        """True if this session was deleted because its 60 minutes ran out."""
        return session_id in self._expired

    def sweep(self):
        """Delete every session older than its lifetime. Called on each request and by a timer."""
        now = self.clock()
        with self._lock:
            old = [sid for sid, s in self._sessions.items() if now - s.created_at >= self.lifetime]
        for sid in old:
            self.delete(sid)
            self._expired.add(sid)
        return old

    def delete(self, session_id):
        """Forget a session and delete its folder. A running check stops after its current song."""
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is not None:
            session.cancelled = True
            shutil.rmtree(session.folder, ignore_errors=True)
        return session is not None

    def delete_all(self):
        """Called when the server stops."""
        with self._lock:
            ids = list(self._sessions)
        for session_id in ids:
            self.delete(session_id)
        shutil.rmtree(self.root, ignore_errors=True)
