"""Where the web UI keeps your files while you use it.

Each upload gets a "session": a private folder (readable by your user account
only) inside one Retrack folder in the computer's temp directory, plus the
check's progress and results in memory.

The files never leave this computer, and they're deleted:
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
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ROOT = Path(tempfile.gettempdir()) / "retrack-sessions"
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

    @property
    def total(self):
        return len(self.todo)

    def fail(self, title, message):
        self.error = (title, message)
        self.state = "failed"


class SessionStore:
    def __init__(self, root=None):
        self.root = Path(root or DEFAULT_ROOT)  # looked up now, so tests can point it elsewhere
        self._sessions = {}
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
        session = Session(session_id, folder, Path(filename).name or "catalog", upload_path)
        with self._lock:
            self._sessions[session_id] = session
        return session

    def get(self, session_id):
        if not ID_RE.match(session_id or ""):
            return None
        with self._lock:
            return self._sessions.get(session_id)

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
