"""
Scraper Bot — Persistent Chrome session server for AI agents.

Agents create browser sessions via HTTP, then send commands (navigate, click,
type, run JS, get HTML, screenshot, etc.) without opening/closing drivers.

Lifecycle: creating → active → closing → closed (terminal).
Sessions are persisted to SQLite on SSD and reconciled on restart.

Usage:
    python server.py [--port 9020] [--host 127.0.0.1]
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
from email.parser import BytesParser
from email.policy import default as email_policy_default
import json
import logging
import logging.handlers
import os
import hashlib
import queue
import re
import shlex
import signal
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

import websocket

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler, http_exception_handler
from functools import wraps
from contextvars import copy_context
import fill_diagnostics as fd
from pydantic import BaseModel, Field
import uvicorn

from platform_base import get_adapter

try:
    import psutil
except ImportError:  # pragma: no cover - optional dependency
    psutil = None

_platform = get_adapter()

# ---------------------------------------------------------------------------
# Public browser adapter
# ---------------------------------------------------------------------------
from browser_manager import DriverManager, resolve_headless

# Trusted-input primitives (CDP Input.*). Local module, same dir as server.py.
import trusted_input as ti

# ---------------------------------------------------------------------------
# Logging — RotatingFileHandler 10 MB x 5 on SSD
# ---------------------------------------------------------------------------
SCRAPERBOT_DATA_DIR = os.environ.get("SCRAPERBOT_DATA_DIR", _platform.default_data_dir)
LOG_PATH = os.environ.get(
    "SCRAPERBOT_LOG_PATH",
    os.path.join(SCRAPERBOT_DATA_DIR, "logs", "scraper_bot.log"),
)

_console_handler = logging.StreamHandler()
_console_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
)

_handlers: list[logging.Handler] = [_console_handler]
try:
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    _log_handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5
    )
except OSError as e:
    _console_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    )
    _console_handler.handle(
        logging.makeLogRecord(
            {
                "levelno": logging.WARNING,
                "levelname": "WARNING",
                "msg": f"Could not open log file {LOG_PATH}: {e}",
            }
        )
    )
else:
    _log_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] [%(name)s] %(message)s")
    )
    _handlers.insert(0, _log_handler)

logging.basicConfig(level=logging.INFO, handlers=_handlers)
logger = logging.getLogger("scraper-bot")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get(
    "SCRAPERBOT_DB_PATH",
    os.path.join(SCRAPERBOT_DATA_DIR, "sessions.db"),
)
MAX_SESSIONS = int(os.environ.get("SCRAPERBOT_MAX_SESSIONS", "8"))
INACTIVITY_TTL_MINUTES = 30
REAPER_INTERVAL_SECONDS = 60
DASHBOARD_TOKEN_PATH = os.environ.get(
    "SCRAPERBOT_DASHBOARD_TOKEN_PATH",
    os.path.join(os.path.dirname(DB_PATH), "dashboard_token"),
)
VERSION = "2.0.0"
DEFAULT_DRIVER = "chromedriver"
PLAYWRIGHT_DRIVER = "playwright"
SUPPORTED_DRIVERS = {DEFAULT_DRIVER, PLAYWRIGHT_DRIVER}
PLAYWRIGHT_CHANNEL = os.environ.get("SCRAPERBOT_PLAYWRIGHT_CHANNEL", "chrome") or None
FD_MAX = int(os.environ.get("SCRAPER_BOT_FD_MAX", "8000"))
DEEP_HEALTH_MAX_FD_DELTA = int(os.environ.get("SCRAPERBOT_DEEP_HEALTH_MAX_FD_DELTA", "1"))
DEEP_HEALTH_WARMUP_MAX_FD_DELTA = int(
    os.environ.get("SCRAPERBOT_DEEP_HEALTH_WARMUP_MAX_FD_DELTA", "8")
)
ORPHAN_REAPER_INTERVAL_SECONDS = int(os.environ.get("SCRAPERBOT_ORPHAN_REAPER_INTERVAL_SECONDS", str(5 * 60)))
ORPHAN_PROCESS_GRACE_SECONDS = int(os.environ.get("SCRAPERBOT_ORPHAN_PROCESS_GRACE_SECONDS", "30"))
ORPHAN_SIGTERM_WAIT_SECONDS = float(os.environ.get("SCRAPERBOT_ORPHAN_SIGTERM_WAIT_SECONDS", "2.0"))
ORPHAN_PROFILE_REAPER_INTERVAL_SECONDS = 10 * 60
ORPHAN_PROFILE_MAX_AGE_SECONDS = 60 * 60
# Cadence for the zombie-child gauge log (visibility for the 2026-07 fork-ceiling
# incident, where 2,150 unreaped <defunct> children crossed `ulimit -u`).
ZOMBIE_CHILD_LOG_INTERVAL_SECONDS = int(
    os.environ.get("SCRAPERBOT_ZOMBIE_CHILD_LOG_INTERVAL_SECONDS", "60")
)
PROFILES_ROOT = os.environ.get(
    "SCRAPERBOT_PROFILES_ROOT",
    os.path.join(os.path.dirname(DB_PATH), "profiles"),
)

# ---------------------------------------------------------------------------
# State & reason enums
# ---------------------------------------------------------------------------

class SessionState(str, Enum):
    CREATING = "creating"
    ACTIVE = "active"
    CLOSING = "closing"
    CLOSED = "closed"


class CloseReason(str, Enum):
    EXPIRED = "expired"
    HEARTBEAT_TIMEOUT = "heartbeat_timeout"
    MANUAL_KILL = "manual_kill"
    SERVER_SHUTDOWN = "server_shutdown"
    CREATE_FAILED = "create_failed"
    CLIENT_CLOSE = "client_close"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Session dataclass (in-memory record)
# ---------------------------------------------------------------------------

@dataclass
class SessionRecord:
    """In-memory representation of a single Chrome session."""
    id: str
    name: Optional[str]
    owner: Optional[str]
    labels: Optional[dict]
    job_id: Optional[str]
    state: SessionState
    close_reason: Optional[CloseReason]
    created_at: datetime
    last_request_at: datetime
    last_action_at: datetime
    last_heartbeat_at: Optional[datetime]
    last_url: str
    last_error: Optional[str]
    action_count: int
    error_count: int
    closed_at: Optional[datetime]
    lease_mode: bool
    heartbeat_ttl_seconds: int
    user_data_dir: Optional[str]
    pid: Optional[int]
    hostname: Optional[str] = None
    driver_backend: str = DEFAULT_DRIVER
    trace_path: Optional[str] = None
    download_dir: Optional[str] = None
    download_intent: Optional[dict] = None
    download_events: list[dict] = field(default_factory=list)
    download_tracker: Any = field(default=None, repr=False)
    idempotent_actions: dict[str, dict] = field(default_factory=dict, repr=False)
    # Not persisted to SQLite
    dm: Optional[Any] = field(default=None, repr=False)
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


@dataclass(frozen=True)
class MachineWideChromeCandidate:
    pid: int
    ppid: int
    command: str
    user_data_dir: str
    signature: str = "uc_automation_temp_user_data_dir_ppid1"


# ---------------------------------------------------------------------------
# SQLite persistence
# ---------------------------------------------------------------------------

_db_conn: Optional[sqlite3.Connection] = None
_db_lock = threading.Lock()  # Serialize ALL sqlite writes/reads across threads

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT,
    owner TEXT,
    labels_json TEXT,
    job_id TEXT,
    state TEXT NOT NULL,
    close_reason TEXT,
    created_at TEXT NOT NULL,
    last_request_at TEXT,
    last_action_at TEXT,
    last_heartbeat_at TEXT,
    last_url TEXT,
    last_error TEXT,
    action_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    closed_at TEXT,
    lease_mode INTEGER,
    heartbeat_ttl_seconds INTEGER,
    user_data_dir TEXT,
    pid INTEGER,
    hostname TEXT
);
CREATE INDEX IF NOT EXISTS idx_state ON sessions(state);
CREATE INDEX IF NOT EXISTS idx_owner ON sessions(owner);
CREATE INDEX IF NOT EXISTS idx_created ON sessions(created_at);
"""

HOSTNAME = socket.gethostname()


def _windowserver_access() -> tuple[bool, str]:
    return _platform.headful_display_check()


_auto_chrome_major_logged = False


def _auto_chrome_major() -> int | None:
    global _auto_chrome_major_logged
    v = _platform.chrome_major_version()
    if v is not None and not _auto_chrome_major_logged:
        logger.info(
            f"Auto-detected installed Chrome major {v}; defaulting version_main "
            f"(callers may override with chrome_version_main)."
        )
        _auto_chrome_major_logged = True
    return v


# Remediation printed wherever a headful-visibility problem is detected.
HEADFUL_REMEDIATION = (
    "Headful browser windows require an active desktop session. "
    "Start the server from a logged-in desktop terminal, or use headless=True."
)


def _dt(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt else None


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(s) if s else None


def _open_db() -> sqlite3.Connection:
    """Create the configured storage directory and open SQLite."""
    db_path = DB_PATH
    parent = os.path.dirname(db_path)
    os.makedirs(parent or ".", exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # Lazy migration: add `hostname` column if missing (for DBs created before v2.0.1)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    if "hostname" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN hostname TEXT")
    conn.commit()
    logger.info(f"SQLite DB opened at {db_path}")
    return conn


def _db_upsert_record(conn: sqlite3.Connection, r: SessionRecord) -> None:
    """Write or update all fields of a session record. Thread-safe via _db_lock."""
    with _db_lock:
        conn.execute(
            """
            INSERT OR REPLACE INTO sessions
              (id, name, owner, labels_json, job_id, state, close_reason,
               created_at, last_request_at, last_action_at, last_heartbeat_at,
               last_url, last_error, action_count, error_count, closed_at,
               lease_mode, heartbeat_ttl_seconds, user_data_dir, pid, hostname)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                r.id, r.name, r.owner,
                json.dumps(r.labels) if r.labels else None,
                r.job_id, r.state.value,
                r.close_reason.value if r.close_reason else None,
                _dt(r.created_at), _dt(r.last_request_at), _dt(r.last_action_at),
                _dt(r.last_heartbeat_at), r.last_url, r.last_error,
                r.action_count, r.error_count, _dt(r.closed_at),
                1 if r.lease_mode else 0, r.heartbeat_ttl_seconds,
                r.user_data_dir, r.pid, r.hostname,
            ),
        )
        conn.commit()


def _db_update_metadata(conn: sqlite3.Connection, r: SessionRecord) -> None:
    """Batch metadata update (non-transition fields). Thread-safe."""
    with _db_lock:
        conn.execute(
            """
            UPDATE sessions SET
              last_request_at=?, last_action_at=?, last_heartbeat_at=?,
              last_url=?, last_error=?, action_count=?, error_count=?
            WHERE id=?
            """,
            (
                _dt(r.last_request_at), _dt(r.last_action_at),
                _dt(r.last_heartbeat_at), r.last_url, r.last_error,
                r.action_count, r.error_count, r.id,
            ),
        )
        conn.commit()


def _db_load_incomplete(conn: sqlite3.Connection) -> list[dict]:
    """Load sessions with state in {creating, active, closing} for startup reconcile."""
    with _db_lock:
        cur = conn.execute(
            "SELECT * FROM sessions WHERE state IN ('creating','active','closing')"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _db_load_recent_closed(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    with _db_lock:
        cur = conn.execute(
            "SELECT * FROM sessions WHERE state='closed' ORDER BY closed_at DESC LIMIT ?",
            (limit,),
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def _db_trim_old_closed(conn: sqlite3.Connection, keep: int = 1000) -> None:
    """Keep last `keep` closed rows, delete older ones."""
    with _db_lock:
        conn.execute(
            """
            DELETE FROM sessions WHERE state='closed' AND id NOT IN (
                SELECT id FROM sessions WHERE state='closed'
                ORDER BY closed_at DESC LIMIT ?
            )
            """,
            (keep,),
        )
        conn.commit()


def _db_mark_closed_bulk(conn: sqlite3.Connection, ids: list[str], reason: str, closed_at: str) -> None:
    """Used by startup reconcile to bulk-mark sessions closed."""
    if not ids:
        return
    with _db_lock:
        conn.executemany(
            "UPDATE sessions SET state='closed', close_reason=?, closed_at=? WHERE id=?",
            [(reason, closed_at, sid) for sid in ids],
        )
        conn.commit()


# ---------------------------------------------------------------------------
# CSRF / dashboard token
# ---------------------------------------------------------------------------

_dashboard_token: str = ""


def _init_dashboard_token() -> str:
    """Generate a random token, write to file with 0600, return it."""
    token = uuid.uuid4().hex + uuid.uuid4().hex  # 64 hex chars
    with open(DASHBOARD_TOKEN_PATH, "w") as f:
        f.write(token)
    os.chmod(DASHBOARD_TOKEN_PATH, 0o600)
    logger.info(f"Dashboard token written to {DASHBOARD_TOKEN_PATH}")
    return token


def _check_token(request: Request) -> None:
    """Validate CSRF token from header or query param. Raise 403 on fail."""
    token = request.headers.get("X-Scraper-Token") or request.query_params.get("token")
    if not token or token != _dashboard_token:
        raise HTTPException(status_code=403, detail="Missing or invalid X-Scraper-Token")


# ---------------------------------------------------------------------------
# In-memory session store + helpers
# ---------------------------------------------------------------------------

_sessions: Dict[str, SessionRecord] = {}
_sessions_lock = threading.Lock()  # guards _sessions dict mutations


def _count_active() -> int:
    return sum(
        1 for s in _sessions.values()
        if s.state in (SessionState.CREATING, SessionState.ACTIVE, SessionState.CLOSING)
    )


def _get_or_404(session_id: str) -> SessionRecord:
    """Return session record or raise 404 if not found (never existed)."""
    sess = _sessions.get(session_id)
    if sess is None:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")
    return sess


def _touch(sess: SessionRecord) -> None:
    """Update last_request_at in-memory (flushed by background writer)."""
    sess.last_request_at = datetime.now()


def _touch_action(sess: SessionRecord) -> None:
    """Update last_action_at + last_request_at in-memory."""
    now = datetime.now()
    sess.last_request_at = now
    sess.last_action_at = now
    sess.action_count += 1


def _pid_is_chrome(pid: int) -> bool:
    """Return True if the process at `pid` looks like a Chrome/chromedriver.
    Used to avoid SIGKILL'ing an unrelated PID on startup reconcile
    (e.g. if the SSD was mounted from another machine or PID was reused)."""
    return _is_chrome_command(_platform.pid_command(pid))


def _cleanup_chrome_profile(user_data_dir: str) -> None:
    """Remove Chrome singleton lock files from a profile directory.

    SingletonLock is a *symlink* whose content is `<host>-<pid>`; os.path.exists()
    follows it and returns False (the target string is not a real path), so a
    guarded exists()-check would silently skip the single most important file.
    Remove directly and ignore absence instead, which is correct for both the
    symlink (SingletonLock) and the regular files (SingletonCookie/Socket)."""
    for fname in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        path = os.path.join(user_data_dir, fname)
        try:
            os.remove(path)
            logger.info(f"Removed stale Chrome lock {path}")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Could not remove {path}: {e}")


def _normalize_driver_name(driver: Optional[str]) -> str:
    value = (driver or DEFAULT_DRIVER).strip().lower()
    if value in {"chrome", "selenium"}:
        return DEFAULT_DRIVER
    return value


def _playwright_profile_dir(session_id: str) -> str:
    return os.path.realpath(os.path.join(PROFILES_ROOT, f"playwright-{session_id}"))


def _prepare_playwright_profile(session_id: str, source_user_data_dir: Optional[str]) -> str:
    target = _playwright_profile_dir(session_id)
    if os.path.exists(target):
        shutil.rmtree(target, ignore_errors=True)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    if source_user_data_dir and os.path.isdir(source_user_data_dir):
        shutil.copytree(
            source_user_data_dir,
            target,
            ignore=shutil.ignore_patterns(
                "SingletonLock",
                "SingletonCookie",
                "SingletonSocket",
                "lockfile",
            ),
            symlinks=True,
        )
    else:
        os.makedirs(target, exist_ok=True)
    _cleanup_chrome_profile(target)
    return target


def _playwright_available() -> bool:
    try:
        return importlib.util.find_spec("playwright.sync_api") is not None
    except ModuleNotFoundError:
        return False


def _artifact_dir(path: Optional[str]) -> str:
    root = path or os.environ.get("SCRAPERBOT_ARTIFACT_ROOT")
    if root:
        os.makedirs(root, exist_ok=True)
        return os.path.realpath(root)
    default_root = os.path.join(tempfile.gettempdir(), "scraper-bot-artifacts")
    os.makedirs(default_root, exist_ok=True)
    return default_root


def _start_power_assertion() -> None:
    _platform.hold_power_assertion()


def _stop_power_assertion() -> None:
    _platform.release_power_assertion()

_orphan_proc_candidates: dict[int, datetime] = {}
_maintenance_stop = threading.Event()
_fd_cache_lock = threading.Lock()
_fd_cache_value: Optional[int] = None
_fd_cache_at: float = 0.0


def _pid_command(pid: int) -> str:
    return _platform.pid_command(pid)


def _kill_pid(pid: int, *, hard: bool = True) -> bool:
    return _platform.kill_pid(pid, hard=hard)


def _is_chrome_command(command: str) -> bool:
    command = (command or "").lower()
    return ("chrome" in command) or ("chromedriver" in command)


def _process_is_alive(pid: int) -> bool:
    return _platform.process_is_alive(pid)


def _terminate_pid_then_kill(pid: int, *, wait_seconds: float = ORPHAN_SIGTERM_WAIT_SECONDS) -> bool:
    return _platform.terminate_then_kill(pid, wait_seconds=wait_seconds)


def _split_command_args(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _extract_user_data_dir_from_args(args: list[str]) -> Optional[str]:
    for idx, arg in enumerate(args):
        if arg.startswith("--user-data-dir="):
            return arg.split("=", 1)[1]
        if arg == "--user-data-dir" and idx + 1 < len(args):
            return args[idx + 1]
    return None


def _extract_remote_debugging_port_from_args(args: list[str]) -> Optional[int]:
    for idx, arg in enumerate(args):
        value = None
        if arg.startswith("--remote-debugging-port="):
            value = arg.split("=", 1)[1]
        elif arg == "--remote-debugging-port" and idx + 1 < len(args):
            value = args[idx + 1]
        if value and value.isdigit():
            return int(value)
    return None


def _real_user_data_dir_from_command(command: str) -> Optional[str]:
    user_data_dir = _extract_user_data_dir_from_args(_split_command_args(command))
    return os.path.realpath(user_data_dir) if user_data_dir else None


def _is_temp_user_data_dir(user_data_dir: str, *, temp_root: Optional[str] = None) -> bool:
    if not user_data_dir:
        return False
    temp_real = os.path.realpath(temp_root or tempfile.gettempdir())
    dir_real = os.path.realpath(user_data_dir)
    try:
        if os.path.commonpath([temp_real, dir_real]) != temp_real:
            return False
    except ValueError:
        return False

    rel = os.path.relpath(dir_real, temp_real)
    first_part = rel.split(os.sep, 1)[0]
    return first_part.startswith("tmp")


def _is_main_google_chrome_command(command: str, args: list[str]) -> bool:
    if "Google Chrome Helper" in command:
        return False
    if any(arg.startswith("--type=") for arg in args):
        return False
    return "/Google Chrome.app/Contents/MacOS/Google Chrome" in command


def _has_uc_automation_flag(args: list[str]) -> bool:
    for arg in args:
        if not arg.startswith("--disable-blink-features="):
            continue
        _, value = arg.split("=", 1)
        if "AutomationControlled" in value.split(","):
            return True
    return False


def _match_machine_wide_uc_orphan(
    pid: int,
    ppid: int,
    command: str,
    *,
    managed_pids: set[int],
    active_user_data_dirs: set[str],
    temp_root: Optional[str] = None,
) -> Optional[MachineWideChromeCandidate]:
    if ppid != 1 or pid in managed_pids:
        return None

    args = _split_command_args(command)
    if not _is_main_google_chrome_command(command, args):
        return None
    if not _has_uc_automation_flag(args):
        return None

    user_data_dir = _extract_user_data_dir_from_args(args)
    if not user_data_dir:
        return None

    real_user_data_dir = os.path.realpath(user_data_dir)
    if real_user_data_dir in active_user_data_dirs:
        return None
    if not _is_temp_user_data_dir(real_user_data_dir, temp_root=temp_root):
        return None

    return MachineWideChromeCandidate(
        pid=pid,
        ppid=ppid,
        command=command,
        user_data_dir=real_user_data_dir,
    )


def _command_executable_basename(command: str) -> str:
    """Basename of the launched binary, robust to spaces in the path.

    The executable is everything before the first " -" flag token. This avoids
    shlex.split, which mis-splits an unquoted path like ".../Application
    Support/.../undetected_chromedriver" (the `ps` fallback emits these
    unquoted; psutil's shlex.join quotes them). Surrounding quotes that
    shlex.join may add are stripped.
    """
    head = command.split(" -", 1)[0].strip()
    if len(head) >= 2 and head[0] in "'\"" and head[-1] == head[0]:
        head = head[1:-1]
    return os.path.basename(head)


def _is_chromedriver_executable(command: str) -> bool:
    """True when the launched binary is (undetected_)chromedriver itself.

    Matches on the executable basename rather than a substring of the whole
    command line, so a python wrapper that merely mentions "chromedriver" in
    its args does not qualify.
    """
    return _command_executable_basename(command) in (
        "chromedriver",
        "undetected_chromedriver",
    )


def _match_machine_wide_chromedriver_orphan(
    pid: int,
    ppid: int,
    command: str,
    *,
    managed_pids: set[int],
) -> Optional[MachineWideChromeCandidate]:
    """Match a chromedriver whose controlling process has died.

    When a direct DriverManager caller (ad-hoc scrape, Codex login automation)
    is SIGKILLed, both its Chrome and its chromedriver reparent to launchd. The
    Chrome is caught by `_match_machine_wide_uc_orphan`; this catches the
    sibling chromedriver, which has no --user-data-dir of its own. A *live*
    chromedriver is the child of its launching process (ppid != 1) and a
    scraper-bot-managed one is excluded by pid, so neither can match.
    """
    if ppid != 1 or pid in managed_pids:
        return None

    if not _is_chromedriver_executable(command):
        return None

    return MachineWideChromeCandidate(
        pid=pid,
        ppid=ppid,
        command=command,
        user_data_dir="",
        signature="orphan_chromedriver_ppid1",
    )


def _iter_child_pids_ppid(ppid: int) -> list[int]:
    try:
        out = subprocess.check_output(
            ["pgrep", "-P", str(ppid)], stderr=subprocess.DEVNULL, timeout=2
        ).decode("utf-8", errors="ignore")
    except Exception:
        return []
    return [int(line.strip()) for line in out.splitlines() if line.strip().isdigit()]


def _descendant_pids_via_pgrep(root_pid: int) -> set[int]:
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        parent = stack.pop()
        for child_pid in _iter_child_pids_ppid(parent):
            if child_pid in seen:
                continue
            seen.add(child_pid)
            stack.append(child_pid)
    return seen


def _get_our_process_descendants() -> dict[int, str]:
    descendants: dict[int, str] = {}
    if psutil is not None:
        try:
            proc = psutil.Process(os.getpid())
            for child in proc.children(recursive=True):
                try:
                    command = " ".join(child.cmdline()).strip() or child.name()
                except Exception:
                    command = ""
                descendants[child.pid] = command
            return descendants
        except Exception as e:
            logger.warning(f"psutil child enumeration failed, falling back to pgrep: {e}")

    for pid in _descendant_pids_via_pgrep(os.getpid()):
        descendants[pid] = _pid_command(pid)
    return descendants


def _get_our_chrome_descendants() -> dict[int, str]:
    return {
        pid: command
        for pid, command in _get_our_process_descendants().items()
        if _is_chrome_command(command)
    }


def _iter_machine_process_commands() -> list[tuple[int, int, str]]:
    if psutil is not None:
        processes: list[tuple[int, int, str]] = []
        for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
            try:
                info = proc.info
                cmdline = info.get("cmdline") or []
                command = shlex.join(cmdline) if cmdline else (info.get("name") or "")
                processes.append((int(info["pid"]), int(info["ppid"]), command))
            except Exception:
                continue
        return processes

    try:
        out = subprocess.check_output(
            ["ps", "axww", "-o", "pid=", "-o", "ppid=", "-o", "command="],
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).decode("utf-8", errors="ignore")
    except Exception:
        return []

    processes = []
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        processes.append((int(parts[0]), int(parts[1]), parts[2]))
    return processes


def _is_under_profiles_root(path: str) -> bool:
    try:
        root = os.path.realpath(PROFILES_ROOT)
        resolved = os.path.realpath(path)
        return resolved != root and os.path.commonpath([root, resolved]) == root
    except ValueError:
        return False


def _get_machine_wide_uc_orphan_candidates(
    *,
    managed_pids: set[int],
    active_user_data_dirs: set[str],
) -> dict[int, MachineWideChromeCandidate]:
    candidates: dict[int, MachineWideChromeCandidate] = {}
    for pid, ppid, command in _iter_machine_process_commands():
        candidate: Optional[MachineWideChromeCandidate] = None
        if "AutomationControlled" in command and "--user-data-dir" in command:
            candidate = _match_machine_wide_uc_orphan(
                pid,
                ppid,
                command,
                managed_pids=managed_pids,
                active_user_data_dirs=active_user_data_dirs,
            )
        elif "chromedriver" in command:
            candidate = _match_machine_wide_chromedriver_orphan(
                pid,
                ppid,
                command,
                managed_pids=managed_pids,
            )
        if candidate is not None and candidate.user_data_dir and _is_under_profiles_root(candidate.user_data_dir):
            candidates[pid] = candidate
    return candidates


def _extract_pid_candidates(value: Any, seen: set[int]) -> set[int]:
    candidates: set[int] = set()
    if value is None:
        return candidates
    obj_id = id(value)
    if obj_id in seen:
        return candidates
    seen.add(obj_id)

    if isinstance(value, int) and value > 0:
        candidates.add(value)
        return candidates
    if isinstance(value, dict):
        for item in value.values():
            candidates.update(_extract_pid_candidates(item, seen))
        return candidates
    if isinstance(value, (list, tuple, set)):
        for item in value:
            candidates.update(_extract_pid_candidates(item, seen))
        return candidates

    for attr in ("pid", "process", "service", "driver", "_process"):
        try:
            nested = getattr(value, attr)
        except Exception:
            continue
        candidates.update(_extract_pid_candidates(nested, seen))
    return candidates


def _live_session_records_snapshot() -> list[SessionRecord]:
    with _sessions_lock:
        return [
            sess
            for sess in _sessions.values()
            if sess.state in (SessionState.CREATING, SessionState.ACTIVE, SessionState.CLOSING)
        ]


def _driver_capabilities(dm: Any) -> dict:
    if dm is None:
        return {}
    try:
        caps = getattr(dm.driver, "capabilities", None)
    except Exception:
        return {}
    return caps if isinstance(caps, dict) else {}


def _debugger_port_from_capabilities(caps: dict) -> Optional[int]:
    chrome_options = caps.get("goog:chromeOptions")
    if not isinstance(chrome_options, dict):
        return None
    debugger_address = chrome_options.get("debuggerAddress")
    if not isinstance(debugger_address, str) or ":" not in debugger_address:
        return None
    port = debugger_address.rsplit(":", 1)[1]
    return int(port) if port.isdigit() else None


def _process_id_from_capabilities(caps: dict) -> Optional[int]:
    value = caps.get("goog:processID")
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _active_session_root_pids(sessions: Optional[list[SessionRecord]] = None) -> set[int]:
    pids: set[int] = set()
    for sess in sessions if sessions is not None else _live_session_records_snapshot():
        pid = sess.pid
        if pid is None and sess.dm is not None:
            try:
                pid = sess.dm.driver.service.process.pid
            except Exception:
                pid = None
        if pid:
            pids.add(pid)
        caps = _driver_capabilities(sess.dm)
        browser_pid = _process_id_from_capabilities(caps)
        if browser_pid:
            pids.add(browser_pid)
    return pids


def _session_debugger_ports(sessions: list[SessionRecord]) -> set[int]:
    ports: set[int] = set()
    for sess in sessions:
        port = _debugger_port_from_capabilities(_driver_capabilities(sess.dm))
        if port:
            ports.add(port)
    return ports


def _add_process_descendants(pids: set[int]) -> set[int]:
    managed = set(pids)
    if psutil is not None:
        for root_pid in list(managed):
            try:
                proc = psutil.Process(root_pid)
                for child in proc.children(recursive=True):
                    managed.add(child.pid)
            except Exception:
                continue
        return managed

    for root_pid in list(managed):
        managed.update(_descendant_pids_via_pgrep(root_pid))
    return managed


def _managed_session_pids(
    process_commands: Optional[list[tuple[int, int, str]]] = None,
) -> set[int]:
    sessions = _live_session_records_snapshot()
    managed = set(_active_session_root_pids(sessions))
    debugger_ports = _session_debugger_ports(sessions)
    if debugger_ports:
        processes = process_commands if process_commands is not None else _iter_machine_process_commands()
        for pid, _ppid, command in processes:
            port = _extract_remote_debugging_port_from_args(_split_command_args(command))
            if port in debugger_ports:
                managed.add(pid)
    return _add_process_descendants(managed)


def _live_session_user_data_dirs_snapshot(
    *,
    managed_pids: Optional[set[int]] = None,
    process_commands: Optional[list[tuple[int, int, str]]] = None,
) -> set[str]:
    dirs = {
        os.path.realpath(sess.user_data_dir)
        for sess in _live_session_records_snapshot()
        if sess.user_data_dir
    }
    if managed_pids:
        processes = process_commands if process_commands is not None else _iter_machine_process_commands()
        for pid, _ppid, command in processes:
            if pid not in managed_pids:
                continue
            user_data_dir = _real_user_data_dir_from_command(command)
            if user_data_dir:
                dirs.add(user_data_dir)
    return dirs


def _is_active_session_user_data_dir(command: str, active_user_data_dirs: set[str]) -> bool:
    user_data_dir = _real_user_data_dir_from_command(command)
    return bool(user_data_dir and user_data_dir in active_user_data_dirs)


def _cleanup_user_data_dir_if_unclaimed(
    user_data_dir: Optional[str],
    created_fresh: bool,
    exclude_session_id: Optional[str] = None,
) -> bool:
    if not user_data_dir or not created_fresh or not os.path.isdir(user_data_dir):
        return False

    with _sessions_lock:
        claimed = any(
            sess.id != exclude_session_id
            and sess.user_data_dir == user_data_dir
            and sess.state in (SessionState.CREATING, SessionState.ACTIVE, SessionState.CLOSING)
            for sess in _sessions.values()
        )
    if claimed:
        return False

    try:
        shutil.rmtree(user_data_dir)
        logger.info(f"Create failure cleanup: removed user_data_dir={user_data_dir}")
        return True
    except Exception as e:
        logger.warning(f"Create failure cleanup: failed to remove user_data_dir={user_data_dir}: {e}")
        return False


def _cleanup_failed_driver_start(
    session_id: str,
    exc: Exception,
    before_children: dict[int, str],
    user_data_dir: Optional[str],
    created_fresh_profile: bool,
) -> None:
    killed_from_exception: set[int] = set()
    tb = sys.exc_info()[2]
    while tb is not None:
        frame = tb.tb_frame
        for value in frame.f_locals.values():
            for pid in _extract_pid_candidates(value, set()):
                command = _pid_command(pid)
                if _is_chrome_command(command):
                    killed_from_exception.add(pid)
        tb = tb.tb_next

    managed = _managed_session_pids()
    killed: list[int] = []
    for pid in sorted(killed_from_exception - managed):
        try:
            if _kill_pid(pid):
                killed.append(pid)
                logger.info(f"Session {session_id} create failure cleanup: killed pid={pid}")
        except Exception as e:
            logger.warning(f"Session {session_id} create failure cleanup: failed to kill pid={pid}: {e}")

    after_children = _get_our_chrome_descendants()
    new_children = (set(after_children) - set(before_children)) - managed - set(killed)
    for pid in sorted(new_children):
        try:
            if _kill_pid(pid):
                killed.append(pid)
                logger.info(f"Session {session_id} create failure cleanup: killed new child pid={pid}")
        except Exception as e:
            logger.warning(f"Session {session_id} create failure cleanup: failed to kill pid={pid}: {e}")

    removed_dir = _cleanup_user_data_dir_if_unclaimed(
        user_data_dir,
        created_fresh_profile,
        exclude_session_id=session_id,
    )
    logger.info(
        f"Session {session_id} create failure cleanup result: "
        f"killed_pids={sorted(set(killed))}, removed_user_data_dir={removed_dir}, "
        f"user_data_dir={user_data_dir!r}, error={exc}"
    )


def _db_load_active_user_data_dirs(conn: sqlite3.Connection) -> set[str]:
    with _db_lock:
        cur = conn.execute(
            """
            SELECT DISTINCT user_data_dir
            FROM sessions
            WHERE state IN ('creating','active','closing') AND user_data_dir IS NOT NULL
            """
        )
        return {os.path.realpath(row[0]) for row in cur.fetchall() if row[0]}


def _active_user_data_dirs_snapshot(
    *,
    managed_pids: Optional[set[int]] = None,
    process_commands: Optional[list[tuple[int, int, str]]] = None,
) -> set[str]:
    active_dirs = _live_session_user_data_dirs_snapshot(
        managed_pids=managed_pids,
        process_commands=process_commands,
    )
    if not _db_conn:
        return active_dirs
    try:
        active_dirs.update(_db_load_active_user_data_dirs(_db_conn))
    except Exception as e:
        logger.warning(f"Orphan reaper: failed to load active user-data dirs: {e}")
    return active_dirs


def _pw_selector(kind: str, value: str) -> str:
    if kind == "xpath":
        return f"xpath={value}"
    if kind == "css":
        return value
    if kind == "id":
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'[id="{escaped}"]'
    if kind == "tag":
        return value
    if kind == "class_name":
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'[class~="{escaped}"]'
    raise ValueError(f"Unsupported locator type: {kind}")


def _selenium_by_to_kind(by: str) -> str:
    mapping = {
        "xpath": "xpath",
        "css selector": "css",
        "id": "id",
        "link text": "link_text",
        "tag name": "tag",
        "class name": "class_name",
    }
    return mapping.get(str(by).lower(), str(by).lower())


class SimpleProcessFacade:
    process = None


class PlaywrightElement:
    def __init__(self, manager: "PlaywrightDriverManager", kind: str, value: str, index: int = 0):
        self._manager = manager
        self._kind = kind
        self._value = value
        self._index = index

    def _with_locator(self, callback):
        return self._manager._call(
            lambda: callback(self._manager._locator(self._kind, self._value).nth(self._index))
        )

    @property
    def tag_name(self) -> str:
        return self._with_locator(lambda loc: loc.evaluate("el => el.tagName.toLowerCase()"))

    @property
    def text(self) -> str:
        return self._with_locator(lambda loc: loc.inner_text(timeout=5000))

    def get_attribute(self, name: str) -> str:
        return self._with_locator(lambda loc: loc.get_attribute(name) or "")

    def is_displayed(self) -> bool:
        return self._with_locator(lambda loc: loc.is_visible())

    def is_enabled(self) -> bool:
        return self._with_locator(lambda loc: loc.is_enabled())

    def click(self) -> None:
        self._with_locator(lambda loc: loc.click())

    def clear(self) -> None:
        self._with_locator(lambda loc: loc.fill(""))

    def send_keys(self, text: str) -> None:
        def _send(loc):
            if text == "\ue006":
                loc.press("Enter")
            elif os.path.exists(str(text)):
                loc.set_input_files(str(text))
            else:
                loc.type(str(text))
        self._with_locator(_send)


class PlaywrightSwitchFacade:
    def __init__(self, manager: "PlaywrightDriverManager"):
        self._manager = manager

    def frame(self, index: int) -> None:
        def _switch():
            frames = self._manager.page.frames
            if index < 0 or index >= len(frames):
                raise IndexError(f"Frame index out of range: {index}")
            self._manager.current_frame = frames[index]
        self._manager._call(_switch)


class PlaywrightDriverFacade:
    capabilities: dict = {}

    def __init__(self, manager: "PlaywrightDriverManager"):
        self._manager = manager
        self.service = SimpleProcessFacade()
        self.switch_to = PlaywrightSwitchFacade(manager)

    @property
    def title(self) -> str:
        return self._manager._call(lambda: self._manager.page.title())

    @property
    def current_url(self) -> str:
        return self._manager.get_current_url()

    def find_element(self, by: str, value: str) -> PlaywrightElement:
        return self._manager.find_element(by, value)

    def find_elements(self, by: str, value: str) -> list[PlaywrightElement]:
        return self._manager.find_elements(by, value)

    def get_screenshot_as_base64(self) -> str:
        data = self._manager._call(lambda: self._manager.page.screenshot(full_page=True))
        return base64.b64encode(data).decode("ascii")


class PlaywrightDriverManager:
    def __init__(
        self,
        *,
        headless: bool,
        view: str,
        user_data_dir: str,
        trace: bool = False,
        trace_dir: Optional[str] = None,
        session_id: Optional[str] = None,
    ):
        self.user_data_dir = user_data_dir
        self.trace_path: Optional[str] = None
        self._network_enabled = False
        self._network_requests: list[dict[str, Any]] = []
        self._tasks: "queue.Queue[tuple[Optional[Any], queue.Queue]]" = queue.Queue()
        self._closed = False
        self._ready: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)
        self.driver = PlaywrightDriverFacade(self)
        self._thread = threading.Thread(
            target=self._owner_thread,
            kwargs={
                "headless": headless,
                "view": view,
                "user_data_dir": user_data_dir,
                "trace": trace,
                "trace_dir": trace_dir,
                "session_id": session_id,
            },
            daemon=True,
            name=f"playwright-{session_id or 'session'}",
        )
        self._thread.start()
        ok, payload = self._ready.get(timeout=30)
        if not ok:
            raise payload

    def _owner_thread(
        self,
        *,
        headless: bool,
        view: str,
        user_data_dir: str,
        trace: bool,
        trace_dir: Optional[str],
        session_id: Optional[str],
    ) -> None:
        try:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            viewport = {"width": 390, "height": 844} if view == "mobile" else {"width": 1440, "height": 1000}
            self.context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                headless=headless,
                viewport=viewport,
                channel=PLAYWRIGHT_CHANNEL,
            )
            self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
            self.current_frame = self.page
            if trace:
                artifact_root = _artifact_dir(trace_dir)
                name = session_id or uuid.uuid4().hex[:12]
                self.trace_path = os.path.join(artifact_root, f"{name}.zip")
                self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
            self._ready.put((True, None))
        except BaseException as exc:
            self._ready.put((False, exc))
            return

        while True:
            func, result = self._tasks.get()
            if func is None:
                result.put((True, None))
                return
            try:
                result.put((True, func()))
            except BaseException as exc:
                result.put((False, exc))

    def _call(self, func):
        if self._closed:
            raise RuntimeError("Playwright session is closed")
        result: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)
        context = copy_context()
        self._tasks.put((lambda: context.run(func), result))
        ok, payload = result.get(timeout=120)
        if ok:
            return payload
        raise payload

    def close(self) -> None:
        if self._closed:
            return

        def _close_owned():
            try:
                if self.trace_path:
                    self.context.tracing.stop(path=self.trace_path)
            finally:
                try:
                    self.context.close()
                finally:
                    self._playwright.stop()

        try:
            self._call(_close_owned)
        finally:
            self._closed = True
            result: "queue.Queue[tuple[bool, Any]]" = queue.Queue(maxsize=1)
            self._tasks.put((None, result))
            try:
                result.get(timeout=5)
            except queue.Empty:
                pass
            self._thread.join(timeout=5)

    def get(self, url: str) -> None:
        self._call(lambda: (self.page.goto(url, wait_until="domcontentloaded"), setattr(self, "current_frame", self.page)))

    def get_current_url(self) -> str:
        return self._call(lambda: self.page.url)

    def get_page_source(self) -> str:
        return self._call(lambda: self.page.content())

    def execute_script(self, script: str, *args: Any) -> Any:
        def _execute():
            if not args:
                return self.page.evaluate(script)
            if len(args) == 1:
                return self.page.evaluate(script, args[0])
            return self.page.evaluate(script, list(args))
        return self._call(_execute)

    def _locator(self, kind: str, value: str):
        if kind == "link_text":
            return self.current_frame.get_by_text(value, exact=True)
        return self.current_frame.locator(_pw_selector(kind, value))

    def find_element(self, by: str, value: str) -> PlaywrightElement:
        kind = _selenium_by_to_kind(by)
        self._call(lambda: self._locator(kind, value).first.wait_for(state="attached", timeout=5000))
        return PlaywrightElement(self, kind, value)

    def find_elements(self, by: str, value: str) -> list[PlaywrightElement]:
        kind = _selenium_by_to_kind(by)
        count = self._call(lambda: self._locator(kind, value).count())
        return [PlaywrightElement(self, kind, value, i) for i in range(min(count, 100))]

    def find_element_by_xpath(self, xpath: str) -> PlaywrightElement:
        return self.find_element("xpath", xpath)

    def wait_for_selector(self, *, xpath: Optional[str] = None, css: Optional[str] = None, id_: Optional[str] = None, timeout: int = 10) -> None:
        if xpath:
            selector = _pw_selector("xpath", xpath)
        elif css:
            selector = _pw_selector("css", css)
        elif id_:
            selector = _pw_selector("id", id_)
        else:
            raise HTTPException(status_code=400, detail="Provide xpath, css, or id to wait for")
        self._call(lambda: self.current_frame.locator(selector).first.wait_for(state="attached", timeout=timeout * 1000))

    def scroll_to_view(self, el: PlaywrightElement) -> None:
        el._with_locator(lambda loc: loc.scroll_into_view_if_needed())

    def scroll_by(self, amount: int) -> None:
        self._call(lambda: self.page.mouse.wheel(0, amount))

    def select_by_value(self, id_: str, value: str) -> None:
        self._call(lambda: self.page.locator(_pw_selector("id", id_)).select_option(value))

    def switch_to_main(self) -> None:
        self._call(lambda: setattr(self, "current_frame", self.page))

    def switch_to_iframe(self, iframe: PlaywrightElement) -> None:
        def _switch():
            locator = self._locator(iframe._kind, iframe._value).nth(iframe._index)
            handle = locator.element_handle()
            frame = handle.content_frame() if handle else None
            if frame is None:
                raise ValueError("Element is not an iframe")
            self.current_frame = frame
        self._call(_switch)

    def get_browser_cookies(self) -> list[dict[str, Any]]:
        return self._call(lambda: self.context.cookies())

    def enable_network_logging(self) -> None:
        if self._network_enabled:
            return
        self._network_enabled = True

        def _enable():
            def _capture(request):
                self._network_requests.append(
                    {
                        "url": request.url,
                        "method": request.method,
                        "resource_type": request.resource_type,
                    }
                )
            self.page.on("request", _capture)
        self._call(_enable)

    def get_network_requests(self, only_xhr: bool = False) -> list[dict[str, Any]]:
        requests = self._call(lambda: list(self._network_requests))
        if not only_xhr:
            return requests
        return [r for r in requests if r.get("resource_type") in {"xhr", "fetch"}]

    def get_network_traffic(self) -> dict[str, Any]:
        requests = self.get_network_requests()
        return {"requests": requests, "count": len(requests)}

    def clear_network(self) -> None:
        self._call(lambda: self._network_requests.clear())


def _run_orphan_process_reaper_pass() -> None:
    now = datetime.now()
    process_commands = _iter_machine_process_commands()
    managed = _managed_session_pids(process_commands=process_commands)
    active_user_data_dirs = _active_user_data_dirs_snapshot(
        managed_pids=managed,
        process_commands=process_commands,
    )
    orphan_candidates = {
        pid: command
        for pid, command in _get_our_chrome_descendants().items()
        if pid not in managed and not _is_active_session_user_data_dir(command, active_user_data_dirs)
    }
    machine_wide_candidates = _get_machine_wide_uc_orphan_candidates(
        managed_pids=managed,
        active_user_data_dirs=active_user_data_dirs,
    )
    for pid, candidate in machine_wide_candidates.items():
        orphan_candidates.setdefault(pid, candidate.command)

    for pid in list(_orphan_proc_candidates):
        if pid not in orphan_candidates:
            _orphan_proc_candidates.pop(pid, None)

    killed = 0
    machine_wide_killed = 0
    for pid in sorted(orphan_candidates):
        first_seen = _orphan_proc_candidates.setdefault(pid, now)
        age_seconds = (now - first_seen).total_seconds()
        if age_seconds < ORPHAN_PROCESS_GRACE_SECONDS:
            continue
        try:
            machine_wide = machine_wide_candidates.get(pid)
            if machine_wide is not None:
                did_kill = _terminate_pid_then_kill(pid)
                if did_kill:
                    killed += 1
                    machine_wide_killed += 1
                    logger.info(
                        "Machine-wide orphan reaper: killed pid=%s age=%.1fs "
                        "user_data_dir=%s signature=%s",
                        pid,
                        age_seconds,
                        machine_wide.user_data_dir,
                        machine_wide.signature,
                    )
                    if machine_wide.user_data_dir:
                        try:
                            shutil.rmtree(machine_wide.user_data_dir)
                            logger.info(
                                "Machine-wide orphan reaper: removed user_data_dir=%s pid=%s",
                                machine_wide.user_data_dir,
                                pid,
                            )
                        except FileNotFoundError:
                            pass
                        except Exception as e:
                            logger.warning(
                                "Machine-wide orphan reaper: failed to remove user_data_dir=%s pid=%s: %s",
                                machine_wide.user_data_dir,
                                pid,
                                e,
                            )
            elif _kill_pid(pid):
                killed += 1
        except Exception as e:
            logger.warning(f"Orphan reaper: failed to kill pid={pid}: {e}")
        finally:
            _orphan_proc_candidates.pop(pid, None)

    logger.info(
        "Orphan reaper pass: orphan_procs=%s, machine_wide=%s, killed=%s, machine_wide_killed=%s",
        len(orphan_candidates),
        len(machine_wide_candidates),
        killed,
        machine_wide_killed,
    )


def _run_orphan_profile_reaper_pass() -> None:
    if not os.path.isdir(PROFILES_ROOT):
        logger.info(
            f"Orphan profile reaper pass: orphan_profiles=0 (profiles root unavailable: {PROFILES_ROOT})"
        )
        return

    active_dirs = _db_load_active_user_data_dirs(_db_conn) if _db_conn else set()
    cutoff = time.time() - ORPHAN_PROFILE_MAX_AGE_SECONDS
    removed = 0

    for entry in os.scandir(PROFILES_ROOT):
        if not entry.is_dir(follow_symlinks=False):
            continue
        real_path = os.path.realpath(entry.path)
        if real_path in active_dirs:
            continue
        try:
            mtime = entry.stat(follow_symlinks=False).st_mtime
        except FileNotFoundError:
            continue
        if mtime > cutoff:
            continue
        try:
            shutil.rmtree(real_path)
            removed += 1
        except Exception as e:
            logger.warning(f"Orphan profile reaper: failed to remove {real_path}: {e}")

    logger.info(f"Orphan profile reaper pass: orphan_profiles={removed}")


# ---------------------------------------------------------------------------
# Zombie-child reaper
# ---------------------------------------------------------------------------
# server.py spawns Chrome/chromedriver children (via DriverManager) and SIGKILLs
# some of them out-of-band: the orphan-process reaper (_run_orphan_process_reaper_pass
# → _kill_pid), create-failure cleanup (_cleanup_failed_driver_start), and the
# abandoned-driver close-timeout path (_close_session_sync) all os.kill() a child
# without ever os.waitpid()'ing it. A killed/exited child that is never waited on
# stays a <defunct> zombie occupying a per-user process-table slot until this
# process dies. On 2026-07-06..09 they accumulated to 2,150 defunct children and
# crossed the process `ulimit -u` fork ceiling, causing machine-wide fork failures that
# killed batch 2026-07-C subprocesses. This non-blocking sweep reaps any exited
# child regardless of which path abandoned it. It costs one WNOHANG waitpid when
# nothing is dead and never blocks. It does not corrupt libraries that reap their
# own children: selenium's chromedriver Popen reaps synchronously inside quit()
# and CPython's Popen.wait/poll tolerate ECHILD (returncode 0) if this sweep ever
# wins the race for an already-exited child. Playwright's optional (non-default)
# async child-watcher may at worst emit a benign "Unknown child process" log line
# in that race — it is not corrupted.

_zombies_reaped_total = 0


def _reap_zombie_children() -> int:
    """Non-blocking reap of every exited (zombie) child. Returns the count reaped.

    Loops os.waitpid(-1, WNOHANG) until it reports no terminated child (pid 0) or
    we have no children at all (ChildProcessError / ECHILD)."""
    global _zombies_reaped_total
    reaped = 0
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            break  # no child processes remain
        except OSError as e:  # pragma: no cover - defensive
            logger.warning(f"Zombie reaper: waitpid failed: {e}")
            break
        if pid == 0:
            break  # children exist but none have exited
        reaped += 1
    if reaped:
        _zombies_reaped_total += reaped
        logger.info(
            f"Zombie reaper: reaped {reaped} exited child process(es) "
            f"(total_since_start={_zombies_reaped_total})"
        )
    return reaped


def _count_zombie_children() -> int:
    """Best-effort count of still-unreaped <defunct> direct children (visibility)."""
    if psutil is None:
        return 0
    try:
        me = psutil.Process(os.getpid())
        return sum(
            1 for child in me.children() if child.status() == psutil.STATUS_ZOMBIE
        )
    except Exception:  # pragma: no cover - defensive
        return 0


def _maintenance_loop() -> None:
    next_orphan_proc_at = time.monotonic()
    next_orphan_profile_at = time.monotonic()
    next_zombie_log_at = time.monotonic()
    while not _maintenance_stop.wait(5):
        now = time.monotonic()
        # Reap any exited children every pass so SIGKILLed Chrome/chromedriver
        # never lingers as <defunct> (see _reap_zombie_children for why).
        _reap_zombie_children()
        if now >= next_orphan_proc_at:
            _run_orphan_process_reaper_pass()
            # Reap the children the orphan pass just killed, same pass.
            _reap_zombie_children()
            next_orphan_proc_at = now + ORPHAN_REAPER_INTERVAL_SECONDS
        if now >= next_orphan_profile_at:
            _run_orphan_profile_reaper_pass()
            next_orphan_profile_at = now + ORPHAN_PROFILE_REAPER_INTERVAL_SECONDS
        if now >= next_zombie_log_at:
            logger.info(
                "Zombie-child gauge: defunct_remaining=%s reaped_total_since_start=%s",
                _count_zombie_children(),
                _zombies_reaped_total,
            )
            next_zombie_log_at = now + ZOMBIE_CHILD_LOG_INTERVAL_SECONDS


def _current_fd_count_uncached() -> int:
    proc_fd_dir = "/proc/self/fd"
    if os.path.isdir(proc_fd_dir):
        return len(os.listdir(proc_fd_dir))

    if psutil is not None:
        proc = psutil.Process(os.getpid())
        if hasattr(proc, "num_fds"):
            return int(proc.num_fds())
        try:
            return len(proc.open_files())
        except Exception:
            pass

    dev_fd_dir = "/dev/fd"
    if os.path.isdir(dev_fd_dir):
        return len(os.listdir(dev_fd_dir))

    try:
        out = subprocess.check_output(
            ["lsof", "-p", str(os.getpid())], stderr=subprocess.DEVNULL, timeout=2
        ).decode("utf-8", errors="ignore")
        return max(0, len(out.splitlines()) - 1)
    except Exception:
        return 0


def _current_fd_count() -> int:
    global _fd_cache_at, _fd_cache_value
    now = time.monotonic()
    with _fd_cache_lock:
        if _fd_cache_value is not None and (now - _fd_cache_at) < 2.0:
            return _fd_cache_value
        _fd_cache_value = _current_fd_count_uncached()
        _fd_cache_at = now
        return _fd_cache_value


def _finalize_driver_resources(dm) -> None:
    """Close resources that undetected_chromedriver.quit() leaves open."""
    driver = getattr(dm, "driver", None)
    executor = getattr(driver, "command_executor", None)
    if executor is not None:
        try:
            executor.close()
        except Exception as e:
            logger.warning("command executor close error: %s", e)

    service = getattr(driver, "service", None)
    process = getattr(service, "process", None)
    if process is None:
        return
    for stream in (
        getattr(process, "stdin", None),
        getattr(process, "stdout", None),
        getattr(process, "stderr", None),
    ):
        if stream is None:
            continue
        try:
            stream.close()
        except Exception as e:
            logger.warning("chromedriver stream close error: %s", e)
    try:
        wait = getattr(process, "wait", None)
        if wait is not None:
            wait(timeout=1)
    except (subprocess.TimeoutExpired, ChildProcessError):
        pass


def _close_dm_with_timeout(dm, timeout: float) -> bool:
    """Call dm.close() in a daemon thread, join with timeout.
    Returns True if clean close, False if timed out (dm abandoned)."""
    if getattr(dm, "close_on_current_thread", False):
        try:
            dm.close()
            return True
        except Exception as e:
            logger.warning(f"dm.close() error: {e}")
            return True
        finally:
            _finalize_driver_resources(dm)

    result = {"ok": False, "err": None}

    def _target():
        try:
            dm.close()
            result["ok"] = True
        except Exception as e:
            result["err"] = e
        finally:
            _finalize_driver_resources(dm)

    t = threading.Thread(target=_target, daemon=True, name="dm-close")
    t.start()
    t.join(timeout=max(0.1, timeout))
    if t.is_alive():
        return False
    if result["err"]:
        logger.warning(f"dm.close() error: {result['err']}")
    return True


def _close_session_sync(sess: SessionRecord, reason: CloseReason, deadline: float = None) -> None:
    """
    Transition session to closing → closed with given reason.
    Caller must hold sess.lock.
    deadline: absolute time.time() limit; if exceeded, force-close without waiting on dm.close().
    """
    if sess.state in (SessionState.CLOSING, SessionState.CLOSED):
        return

    sess.state = SessionState.CLOSING
    sess.close_reason = reason
    if _db_conn:
        _db_upsert_record(_db_conn, sess)

    if sess.download_tracker is not None:
        sess.download_tracker.close()
        sess.download_tracker = None

    if sess.dm is not None:
        per_session_timeout = (deadline - time.time()) if deadline else 5.0
        per_session_timeout = min(per_session_timeout, 5.0) if deadline else 5.0
        if per_session_timeout > 0:
            ok = _close_dm_with_timeout(sess.dm, per_session_timeout)
            if not ok:
                logger.warning(
                    f"Session {sess.id}: dm.close() timed out after {per_session_timeout:.1f}s; "
                    f"abandoning driver. Chrome PID {sess.pid} may leak — startup reconcile will clean it up."
                )
        sess.dm = None

    sess.state = SessionState.CLOSED
    sess.closed_at = datetime.now()
    if _db_conn:
        _db_upsert_record(_db_conn, sess)
    logger.info(f"Session {sess.id} closed (reason={reason.value})")


# ---------------------------------------------------------------------------
# Background metadata flush
# ---------------------------------------------------------------------------

_flush_stop = threading.Event()


def _metadata_flush_loop() -> None:
    """Flush in-memory metadata to DB every 10 seconds."""
    while not _flush_stop.wait(10):
        if _db_conn is None:
            continue
        active = list(_sessions.values())
        for sess in active:
            if sess.state in (SessionState.ACTIVE, SessionState.CREATING):
                try:
                    _db_update_metadata(_db_conn, sess)
                except Exception as e:
                    logger.warning(f"Metadata flush error for {sess.id}: {e}")


# ---------------------------------------------------------------------------
# Reaper
# ---------------------------------------------------------------------------

_reaper_stop = threading.Event()
_reaper_last_ran_at: Optional[datetime] = None


def _reaper_loop() -> None:
    """Background reaper: reaped expired / heartbeat-timeout / orphaned sessions."""
    global _reaper_last_ran_at
    while not _reaper_stop.wait(REAPER_INTERVAL_SECONDS):
        _run_reaper_pass()


CLOSED_EVICTION_MINUTES = 5


def _run_reaper_pass() -> None:
    global _reaper_last_ran_at
    now = datetime.now()
    inactivity_cutoff = now - timedelta(minutes=INACTIVITY_TTL_MINUTES)
    eviction_cutoff = now - timedelta(minutes=CLOSED_EVICTION_MINUTES)
    _reaper_last_ran_at = now

    candidates = list(_sessions.values())
    to_evict: list[str] = []
    for sess in candidates:
        # Evict long-closed sessions from memory (DB retains them)
        if sess.state == SessionState.CLOSED:
            if sess.closed_at and sess.closed_at < eviction_cutoff:
                to_evict.append(sess.id)
            continue
        if sess.state != SessionState.ACTIVE:
            continue

        acquired = sess.lock.acquire(timeout=0.5)
        if not acquired:
            continue

        try:
            # Re-check state under lock
            if sess.state != SessionState.ACTIVE:
                continue

            # 1) Inactivity TTL
            if sess.last_action_at < inactivity_cutoff:
                logger.info(f"Reaper: expiring inactive session {sess.id}")
                _close_session_sync(sess, CloseReason.EXPIRED)
                continue

            # 2) Heartbeat TTL (lease_mode only)
            if sess.lease_mode and sess.last_heartbeat_at is not None:
                hb_cutoff = now - timedelta(seconds=sess.heartbeat_ttl_seconds)
                if sess.last_heartbeat_at < hb_cutoff:
                    logger.info(f"Reaper: heartbeat timeout on session {sess.id}")
                    _close_session_sync(sess, CloseReason.HEARTBEAT_TIMEOUT)
                    continue

            # 3) Orphaned Chrome PID check
            if sess.dm is not None:
                try:
                    _ = sess.dm.driver.current_url
                except Exception as e:
                    # Capture WHY so a future transient is explainable: the
                    # exception and whether the chromedriver service pid is still
                    # alive (driver_alive=False → driver process gone;
                    # driver_alive=True → driver up but the browser session is
                    # invalid, e.g. Chrome quit / handed off a singleton profile).
                    driver_pid = sess.pid
                    driver_alive = _process_is_alive(driver_pid) if driver_pid else None
                    detail = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
                    logger.warning(
                        f"Reaper: session {sess.id} driver dead, closing "
                        f"(err={detail!r}, driver_pid={driver_pid}, driver_alive={driver_alive})"
                    )
                    sess.last_error = detail
                    _close_session_sync(sess, CloseReason.ERROR)
                    continue
        finally:
            sess.lock.release()

    if to_evict:
        with _sessions_lock:
            for sid in to_evict:
                _sessions.pop(sid, None)
        logger.debug(f"Reaper: evicted {len(to_evict)} closed session(s) from memory")


# ---------------------------------------------------------------------------
# Startup reconcile
# ---------------------------------------------------------------------------

def _reconcile_prior_run() -> None:
    """Mark any still-incomplete sessions from prior runs as server_shutdown.
    Only kills PIDs that (a) match our hostname (not a DB from another machine)
    and (b) are actually Chrome/chromedriver processes."""
    rows = _db_load_incomplete(_db_conn)
    if not rows:
        return
    logger.info(f"Reconciling {len(rows)} incomplete sessions from prior run (hostname={HOSTNAME})")
    now = datetime.now().isoformat()
    closed_ids = []
    for row in rows:
        row_host = row.get("hostname")
        pid = row.get("pid")

        # PID kill — only if hostname matches (or is null for legacy rows) AND PID looks like Chrome
        if pid and (row_host is None or row_host == HOSTNAME):
            if _pid_is_chrome(pid):
                try:
                    _kill_pid(pid)
                    logger.info(f"Killed orphaned Chrome PID {pid} for session {row['id']}")
                except ProcessLookupError:
                    pass
                except Exception as e:
                    logger.warning(f"Could not kill pid {pid}: {e}")
            else:
                logger.info(f"Skipping PID {pid} for session {row['id']} — not a Chrome process (likely reused/stale)")
        elif pid and row_host != HOSTNAME:
            logger.info(f"Skipping PID {pid} for session {row['id']} — hostname {row_host!r} != {HOSTNAME!r}")

        # Remove Chrome singleton lock files (safe: only removes files we wrote)
        udd = row.get("user_data_dir")
        if udd and (row_host is None or row_host == HOSTNAME):
            _cleanup_chrome_profile(udd)

        closed_ids.append(row["id"])

    _db_mark_closed_bulk(_db_conn, closed_ids, CloseReason.SERVER_SHUTDOWN.value, now)
    logger.info(f"Reconcile complete: marked {len(closed_ids)} rows closed")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db_conn, _dashboard_token

    # ── Startup ──────────────────────────────────────────────────────────
    _start_power_assertion()
    _db_conn = _open_db()
    _reconcile_prior_run()
    _db_trim_old_closed(_db_conn)

    _dashboard_token = _init_dashboard_token()

    # Start background flush thread
    flush_thread = threading.Thread(target=_metadata_flush_loop, daemon=True, name="metadata-flush")
    flush_thread.start()

    # Start reaper thread
    reaper_thread = threading.Thread(target=_reaper_loop, daemon=True, name="reaper")
    reaper_thread.start()

    _run_orphan_process_reaper_pass()
    _run_orphan_profile_reaper_pass()
    maintenance_thread = threading.Thread(target=_maintenance_loop, daemon=True, name="maintenance")
    maintenance_thread.start()

    logger.info(f"Scraper Bot v{VERSION} started. max_sessions={MAX_SESSIONS}")

    # Headful-visibility self-check: warn loudly at startup if this process can't
    # surface windows, so the silent off-screen failure mode is never a mystery.
    _hf_ok, _hf_detail = _windowserver_access()
    if _hf_ok:
        logger.info(f"WindowServer access OK ({_hf_detail}); headful sessions will be visible.")
    else:
        logger.warning(f"NO WindowServer access ({_hf_detail}). {HEADFUL_REMEDIATION}")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────
    logger.info("Scraper Bot shutting down...")
    _stop_power_assertion()
    _reaper_stop.set()
    _flush_stop.set()
    _maintenance_stop.set()

    # Close all live sessions in parallel with 10s overall deadline
    live = [s for s in _sessions.values() if s.state == SessionState.ACTIVE]
    if live:
        deadline = time.time() + 10.0
        logger.info(f"Closing {len(live)} active sessions (10s deadline)")

        def close_one(sess: SessionRecord):
            remaining = max(0.5, deadline - time.time())
            acquired = sess.lock.acquire(timeout=remaining)
            if not acquired:
                # Handler is stuck holding the lock. Force-mark closed in DB
                # without calling dm.close() (dm will die with the process).
                logger.warning(f"Shutdown: lock timeout on {sess.id}, force-marking closed")
                sess.state = SessionState.CLOSED
                sess.close_reason = CloseReason.SERVER_SHUTDOWN
                sess.closed_at = datetime.now()
                if _db_conn:
                    try:
                        _db_upsert_record(_db_conn, sess)
                    except Exception:
                        pass
                return
            try:
                _close_session_sync(sess, CloseReason.SERVER_SHUTDOWN, deadline=deadline)
            finally:
                sess.lock.release()

        exe = ThreadPoolExecutor(max_workers=len(live))
        futs = {exe.submit(close_one, s): s for s in live}
        try:
            for fut in as_completed(futs, timeout=11):
                try:
                    fut.result()
                except Exception as e:
                    s = futs[fut]
                    logger.warning(f"Shutdown close error for {s.id}: {e}")
        except TimeoutError:
            logger.warning("Shutdown deadline exceeded — force-marking remaining sessions closed")
        # Force-mark anything still not closed (handler hung / dm.close timed out)
        now_dt = datetime.now()
        for s in live:
            if s.state != SessionState.CLOSED:
                s.state = SessionState.CLOSED
                s.close_reason = CloseReason.SERVER_SHUTDOWN
                s.closed_at = now_dt
                if _db_conn:
                    try:
                        _db_upsert_record(_db_conn, s)
                    except Exception:
                        pass
        # Don't wait for stuck workers — daemon threads will die with the process
        exe.shutdown(wait=False, cancel_futures=True)

    # Final metadata flush
    if _db_conn:
        for sess in _sessions.values():
            try:
                _db_update_metadata(_db_conn, sess)
            except Exception:
                pass
        _db_conn.close()

    logger.info(f"Scraper Bot shutdown complete. closed={len(live)} session(s).")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

_server_start_time = datetime.now()
app = FastAPI(title="Scraper Bot", version=VERSION, lifespan=lifespan)


def _fill_primitive(path):
    if path.endswith("/input/type"):
        return "trusted.type"
    if path.endswith("/input/select_option"):
        return "trusted.select_option"
    if path.startswith("/sessions/") and path.endswith("/type"):
        return "legacy.type"
    return None


@app.exception_handler(RequestValidationError)
async def confidential_validation_error(request, error):
    primitive = _fill_primitive(request.url.path)
    if primitive:
        return JSONResponse(status_code=422, content={"detail": fd.error_detail("bad_request", primitive)})
    return await request_validation_exception_handler(request, error)


@app.exception_handler(HTTPException)
async def confidential_http_error(request, error):
    primitive = _fill_primitive(request.url.path)
    if primitive:
        detail = error.detail if isinstance(error.detail, dict) else {}
        return JSONResponse(status_code=error.status_code,
                            content={"detail": fd.error_detail(detail.get("reason", "error"), fd.safe_primitive(detail.get("primitive"), primitive))})
    return await http_exception_handler(request, error)


def fill_endpoint(primitive):
    """Include session lookup/lock and capture guards in the safe endpoint boundary."""
    def decorate(fn):
        @wraps(fn)
        def invoke(*args, **kwargs):
            with fd.confidential_logs():
                try:
                    return fn(*args, **kwargs)
                except HTTPException as error:
                    status = error.status_code
                    detail = error.detail if isinstance(error.detail, dict) else {}
                    detail = fd.error_detail(detail.get("reason", "error"), fd.safe_primitive(detail.get("primitive"), primitive))
                except Exception:
                    status, detail = 400, fd.error_detail(primitive=primitive)
            raise HTTPException(status_code=status, detail=detail) from None
        return invoke
    return decorate

# ---------------------------------------------------------------------------
# Request-ID middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    req_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:8]
    session_id = request.path_params.get("session_id", "-")
    logger.debug(f"REQ {req_id} [{request.method} {request.url.path}] session={session_id}")
    response = await call_next(request)
    response.headers["X-Request-ID"] = req_id
    return response


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    """Request body for POST /sessions.

    All new fields (name, owner, labels, job_id, lease_mode, heartbeat_ttl_seconds)
    are optional with sensible defaults for backwards-compatibility.
    """
    # Existing fields (back-compat)
    undetected: bool = True
    headless: Optional[bool] = None
    chrome_version_main: Optional[int] = None
    view: str = "desktop"
    user_data_dir: Optional[str] = None
    driver: str = DEFAULT_DRIVER
    trace: bool = False
    trace_dir: Optional[str] = None
    # New labeling + lease fields
    name: Optional[str] = None
    owner: Optional[str] = None
    labels: Optional[dict] = None
    job_id: Optional[str] = None
    lease_mode: bool = False
    heartbeat_ttl_seconds: int = 300


class NavigateRequest(BaseModel):
    url: str


class ClickRequest(BaseModel):
    xpath: Optional[str] = None
    css: Optional[str] = None
    id: Optional[str] = None
    link_text: Optional[str] = None
    scroll_first: bool = True
    action_instance_id: Optional[str] = None


class TypeRequest(BaseModel):
    xpath: Optional[str] = None
    css: Optional[str] = None
    id: Optional[str] = None
    text: str
    clear_first: bool = False
    press_enter: bool = False


class ExecuteJsRequest(BaseModel):
    script: str
    args: list[Any] = Field(default_factory=list)


# --- trusted-input primitives (see trusted_input.py) -----------------------

class TrustedTypeRequest(BaseModel):
    """Trusted keystroke/insertText typing into a field."""
    text: str = ""
    css: Optional[str] = None
    xpath: Optional[str] = None
    locate_text: Optional[str] = None
    scope_css: Optional[str] = None
    scope_xpath: Optional[str] = None
    exact: bool = False
    index: int = 0
    mode: str = "keystroke"          # keystroke | send_keys | insert
    focus: bool = True
    require_focus: bool = False
    clear_first: bool = False
    press_enter: bool = False
    expect: Optional[dict] = None
    verify: bool = True
    verify_timeout_ms: int = 4000
    per_char_delay_ms: int = 0


class ClickTextRequest(BaseModel):
    """Click by visible text (or css/xpath) — native, trusted, with overlay guard."""
    by: str = "text"                 # text | css | xpath
    text: Optional[str] = None
    css: Optional[str] = None
    xpath: Optional[str] = None
    scope_css: Optional[str] = None
    scope_xpath: Optional[str] = None
    exact: bool = False
    tag: Optional[str] = None
    index: int = 0
    scroll: bool = True
    allow_covered: bool = False
    button: str = "left"
    click_count: int = 1
    expect: Optional[dict] = None
    verify_timeout_ms: int = 4000


class ClickPointRequest(BaseModel):
    """Click a raw viewport coordinate (CSS px) via CDP."""
    x: float
    y: float
    button: str = "left"
    click_count: int = 1
    expect: Optional[dict] = None
    verify_timeout_ms: int = 4000


class SelectOptionRequest(BaseModel):
    """Id-less combo end-to-end (focus input -> type -> settle -> wait mask ->
    wait item -> tag+native click -> required committed-selection read-back verify).
    ``verify_scope_css`` is validated by trusted_input.select_option so invalid
    requests receive its structured ``bad_request`` response. ``verify_value_css``
    optionally selects committed child input values relative to that scope. See that
    function."""
    input_css: Optional[str] = None
    input_xpath: Optional[str] = None
    item_text: Optional[str] = None
    item_index: Optional[int] = None
    item_scope_css: Optional[str] = None
    item_scope_xpath: Optional[str] = None
    item_tag: Optional[str] = None
    item_exact: bool = True
    type_value: Optional[str] = None
    clear_first: bool = True
    type_mode: str = "send_keys"     # send_keys | keystroke | insert
    settle_ms: int = 0
    mask_css: Optional[str] = None
    mask_timeout_ms: int = 8000
    open_timeout_ms: int = 5000
    verify_scope_css: Optional[str] = None
    verify_text: Optional[str] = None
    verify_timeout_ms: int = 4000
    verify_value_css: Optional[str] = None
    trigger_css: Optional[str] = None
    trigger_xpath: Optional[str] = None
    open_via_trigger: bool = False


class WaitForRequest(BaseModel):
    """Poll until >= min_count visible matches, or time out loudly."""
    by: str = "css"                  # css | xpath | text
    css: Optional[str] = None
    xpath: Optional[str] = None
    text: Optional[str] = None
    scope_css: Optional[str] = None
    scope_xpath: Optional[str] = None
    exact: bool = False
    tag: Optional[str] = None
    timeout_ms: int = 5000
    min_count: int = 1


class OverlayCheckRequest(BaseModel):
    """Detect a blocking overlay over a target/point (no click)."""
    css: Optional[str] = None
    xpath: Optional[str] = None
    text: Optional[str] = None
    scope_css: Optional[str] = None
    scope_xpath: Optional[str] = None
    exact: bool = False
    x: Optional[float] = None
    y: Optional[float] = None


# HTTP status for each trusted-input failure reason. Caller/spec problems are 400;
# target-resolution problems are 422 (Unprocessable); runtime state/effect problems
# are 409. We deliberately never return 404 here — the client reserves 404 for
# "session not found", so a not-found ELEMENT must not masquerade as a lost session.
_TI_STATUS = {
    "bad_request": 400,
    "backend_unsupported": 400,
    "not_editable": 400,
    "element_not_found": 422,
    "element_not_visible": 422,
    "out_of_viewport": 422,
    "item_not_found": 422,
    "list_empty": 422,
    "covered_by_overlay": 409,
    "effect_not_observed": 409,
}


def _run_trusted(sess, fn, *args, **kwargs):
    """Invoke a trusted_input primitive, mapping its errors onto HTTP responses and
    recording failures on the session (never a silent no-op)."""
    try:
        result = fn(*args, **kwargs)
        _touch_action(sess)
        return result
    except ti.TrustedInputError as e:
        sess.last_error = str(e)
        sess.error_count += 1
        raise HTTPException(status_code=_TI_STATUS.get(e.reason, 409), detail=e.to_dict())
    except HTTPException:
        raise
    except Exception as e:
        sess.last_error = str(e)
        sess.error_count += 1
        raise HTTPException(status_code=400, detail={"ok": False, "reason": "error", "message": str(e)})


def _run_fill(sess, dm, fn, *args, primitive="trusted.type", **kwargs):
    def operation():
        if (getattr(sess, "trace_path", None) or getattr(dm, "trace_path", None)
                or getattr(dm, "_network_enabled", False)):
            raise ti.TrustedInputError("bad_request", fd.MESSAGES["bad_request"], primitive=primitive)
        return fn(*args, **kwargs)
    try:
        result = ti.run_confidential_fill(operation, diagnostic_primitive=primitive)
        _touch_action(sess)
        return result
    except ti.TrustedInputError as error:
        detail = fd.error_detail(error.reason, error.primitive)
        sess.last_error = f"{detail['primitive']}/{detail['reason']}: {detail['message']}"
        sess.error_count += 1
    raise HTTPException(status_code=_TI_STATUS.get(detail["reason"], 400), detail=detail) from None


class BrowserDownloadTracker:
    """Record only Chrome Browser-domain download lifecycle events."""

    def __init__(self, session_id: str, download_dir: str, ws):
        self.session_id = session_id
        self.download_dir = Path(download_dir).resolve()
        self.ws = ws
        self.lock = threading.RLock()
        self.intent: Optional[dict] = None
        self.pending: dict[str, dict] = {}
        self.completed: list[dict] = []
        self.running = True
        self.thread = threading.Thread(target=self._listen, daemon=True, name=f"download-{session_id}")

    @classmethod
    def connect(cls, session_id: str, download_dir: str, driver):
        caps = getattr(driver, "capabilities", {}) or {}
        address = (caps.get("goog:chromeOptions") or {}).get("debuggerAddress", "")
        host = address.rsplit(":", 1)[0].strip("[]")
        if host not in {"127.0.0.1", "localhost", "::1"}:
            return None
        with urllib.request.urlopen(f"http://{address}/json/version", timeout=2) as response:
            ws_url = json.loads(response.read())["webSocketDebuggerUrl"]
        ws = websocket.create_connection(ws_url, timeout=1, origin=f"http://{address}")
        ws.send(json.dumps({"id": 1, "method": "Browser.setDownloadBehavior", "params": {"behavior": "allow", "downloadPath": str(Path(download_dir).resolve()), "eventsEnabled": True}}))
        acknowledgement = json.loads(ws.recv())
        if acknowledgement.get("id") != 1 or acknowledgement.get("error"):
            ws.close()
            raise RuntimeError("Browser download event subscription was rejected")
        ws.settimeout(None)
        tracker = cls(session_id, download_dir, ws)
        tracker.thread.start()
        return tracker

    def arm(self, action_instance_id: str) -> None:
        with self.lock:
            if not self.running or (self.thread.ident is not None and not self.thread.is_alive()):
                raise RuntimeError("browser download event listener is not healthy")
            self.intent = {"action_instance_id": action_instance_id, "armed_at": time.time()}
            self.pending = {}
            self.completed = []

    def _listen(self) -> None:
        try:
            while self.running:
                self._handle_message(self.ws.recv())
        except Exception:
            if self.running:
                logger.exception("Browser download event listener stopped for %s", self.session_id)
        finally:
            self.running = False

    def _handle_message(self, raw: str) -> None:
        message = json.loads(raw)
        method = message.get("method")
        params = message.get("params") or {}
        with self.lock:
            if method == "Browser.downloadWillBegin" and self.intent:
                self.pending[params["guid"]] = {
                    "guid": params["guid"],
                    "session_id": self.session_id,
                    "action_instance_id": self.intent["action_instance_id"],
                    "source_url": params.get("url", ""),
                    "suggested_filename": params.get("suggestedFilename", ""),
                    "browser_started_at": time.time(),
                }
            elif method == "Browser.downloadProgress" and params.get("state") == "completed":
                event = self.pending.pop(params.get("guid"), None)
                if event:
                    path = Path(params.get("filePath") or (self.download_dir / event["suggested_filename"]))
                    descriptor = self._descriptor(path)
                    if descriptor:
                        event.update({"browser_state": "completed", "browser_completed_at": time.time(), "browser_file_path": str(path.absolute()), "browser_descriptor": descriptor})
                        self.completed.append(event)
            elif method == "Browser.downloadProgress" and params.get("state") == "canceled":
                self.pending.pop(params.get("guid"), None)

    def events(self) -> list[dict]:
        with self.lock:
            return [dict(event) for event in self.completed]

    def _descriptor(self, path: Path) -> Optional[dict]:
        try:
            if path.parent.resolve() != self.download_dir or path.is_symlink() or not path.is_file():
                return None
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                first = os.fstat(fd)
                digest = hashlib.sha256()
                while chunk := os.read(fd, 1024 * 1024):
                    digest.update(chunk)
                second = os.fstat(fd)
            finally:
                os.close(fd)
        except OSError:
            return None
        if (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns) != (second.st_dev, second.st_ino, second.st_size, second.st_mtime_ns) or second.st_size <= 0:
            return None
        return {"device": second.st_dev, "inode": second.st_ino, "size": second.st_size, "mtime_ns": second.st_mtime_ns, "sha256": digest.hexdigest()}

    def close(self) -> None:
        self.running = False
        try:
            self.ws.close()
        except Exception:
            pass


class DownloadIntentRequest(BaseModel):
    action_instance_id: str


class FindRequest(BaseModel):
    xpath: Optional[str] = None
    css: Optional[str] = None
    id: Optional[str] = None
    tag: Optional[str] = None
    class_name: Optional[str] = None
    multiple: bool = False


class WaitRequest(BaseModel):
    xpath: Optional[str] = None
    css: Optional[str] = None
    id: Optional[str] = None
    timeout: int = 10


class ScrollRequest(BaseModel):
    amount: Optional[int] = None
    direction: str = "down"
    xpath: Optional[str] = None


class SelectRequest(BaseModel):
    id: str
    value: str


class SwitchIframeRequest(BaseModel):
    xpath: Optional[str] = None
    index: Optional[int] = None
    main: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_element(dm, xpath=None, css=None, id_=None, link_text=None, tag=None, class_name=None):
    from selenium.webdriver.common.by import By
    if xpath:
        return dm.driver.find_element(By.XPATH, xpath)
    if css:
        return dm.driver.find_element(By.CSS_SELECTOR, css)
    if id_:
        return dm.driver.find_element(By.ID, id_)
    if link_text:
        return dm.driver.find_element(By.LINK_TEXT, link_text)
    if tag:
        return dm.driver.find_element(By.TAG_NAME, tag)
    if class_name:
        return dm.driver.find_element(By.CLASS_NAME, class_name)
    raise HTTPException(status_code=400, detail="Provide at least one locator (xpath, css, id, link_text, tag, class_name)")


def _find_elements(dm, xpath=None, css=None, id_=None, tag=None, class_name=None):
    from selenium.webdriver.common.by import By
    if xpath:
        return dm.driver.find_elements(By.XPATH, xpath)
    if css:
        return dm.driver.find_elements(By.CSS_SELECTOR, css)
    if id_:
        return dm.driver.find_elements(By.ID, id_)
    if tag:
        return dm.driver.find_elements(By.TAG_NAME, tag)
    if class_name:
        return dm.driver.find_elements(By.CLASS_NAME, class_name)
    raise HTTPException(status_code=400, detail="Provide at least one locator")


def _element_info(el) -> dict:
    try:
        return {
            "tag": el.tag_name,
            "text": el.text[:500] if el.text else "",
            "id": el.get_attribute("id") or "",
            "class": el.get_attribute("class") or "",
            "href": el.get_attribute("href") or "",
            "value": el.get_attribute("value") or "",
            "displayed": el.is_displayed(),
            "enabled": el.is_enabled(),
        }
    except Exception:
        return {"tag": "unknown", "text": "", "error": "element went stale"}


async def _parse_upload_request(request: Request) -> tuple[str, bytes, str, str]:
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" not in content_type:
        raise HTTPException(status_code=400, detail="multipart/form-data is required")

    body = await request.body()
    msg = BytesParser(policy=email_policy_default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        + body
    )
    if not msg.is_multipart():
        raise HTTPException(status_code=400, detail="invalid multipart body")

    filename = None
    file_bytes = None
    fields: dict[str, str] = {}
    for part in msg.iter_parts():
        name = part.get_param("name", header="content-disposition")
        if not name:
            continue
        payload = part.get_payload(decode=True) or b""
        part_filename = part.get_filename()
        if name == "file" and part_filename is not None:
            filename = part_filename
            file_bytes = payload
        else:
            charset = part.get_content_charset() or "utf-8"
            fields[name] = payload.decode(charset, errors="replace")

    if filename is None or file_bytes is None:
        raise HTTPException(status_code=400, detail="multipart file field 'file' is required")

    selector = fields.get("selector")
    selector_type = fields.get("selector_type") or "css"
    if not selector:
        raise HTTPException(status_code=400, detail="selector is required")
    if selector_type not in {"css", "xpath"}:
        raise HTTPException(status_code=400, detail="selector_type must be 'css' or 'xpath'")

    return filename, file_bytes, selector, selector_type


def _session_to_dict(sess: SessionRecord) -> dict:
    """Serialize a SessionRecord to a JSON-safe dict."""
    return {
        "id": sess.id,
        "name": sess.name,
        "owner": sess.owner,
        "labels": sess.labels,
        "job_id": sess.job_id,
        "state": sess.state.value,
        "close_reason": sess.close_reason.value if sess.close_reason else None,
        "created_at": _dt(sess.created_at),
        "last_request_at": _dt(sess.last_request_at),
        "last_action_at": _dt(sess.last_action_at),
        "last_heartbeat_at": _dt(sess.last_heartbeat_at),
        "last_url": sess.last_url,
        "last_error": sess.last_error,
        "action_count": sess.action_count,
        "error_count": sess.error_count,
        "closed_at": _dt(sess.closed_at),
        "lease_mode": sess.lease_mode,
        "heartbeat_ttl_seconds": sess.heartbeat_ttl_seconds,
        "user_data_dir": sess.user_data_dir,
        "driver": sess.driver_backend,
        "trace_path": sess.trace_path,
    }


def _require_active(sess: SessionRecord) -> None:
    """Raise 404 if session is closed (gone), 409 if not yet active."""
    if sess.state == SessionState.CLOSED:
        raise HTTPException(status_code=404, detail=f"Session '{sess.id}' is closed")
    if sess.state != SessionState.ACTIVE:
        raise HTTPException(status_code=409, detail=f"Session '{sess.id}' is {sess.state.value}")


from contextlib import contextmanager


@contextmanager
def _session_action(session_id: str):
    """Context manager: acquire session lock, validate state, yield (sess, dm).
    Guarantees the driver won't be closed by reaper/DELETE while the caller uses it.
    Raises 404/409 outside the lock if session is not usable."""
    sess = _get_or_404(session_id)
    _require_active(sess)  # fast-path check; re-checked after lock
    acquired = sess.lock.acquire(timeout=60.0)
    if not acquired:
        raise HTTPException(status_code=503, detail="Session busy — could not acquire lock")
    try:
        # Re-check after lock: reaper/DELETE may have closed the session while we waited
        if sess.state != SessionState.ACTIVE:
            raise HTTPException(status_code=404, detail=f"Session '{sess.id}' closed while waiting for lock")
        _touch(sess)
        yield sess, sess.dm
    finally:
        sess.lock.release()


# ---------------------------------------------------------------------------
# Endpoints — read-only (no token required)
# ---------------------------------------------------------------------------

@app.get("/health")
def health():
    """Enhanced health endpoint with uptime, cap, and reaper status."""
    uptime = (datetime.now() - _server_start_time).total_seconds()
    hf_ok, hf_detail = _windowserver_access()
    return {
        "status": "ok",
        "uptime_seconds": uptime,
        "version": VERSION,
        "drivers": {
            DEFAULT_DRIVER: {"default": True, "available": True},
            PLAYWRIGHT_DRIVER: {"default": False, "available": _playwright_available()},
        },
        "active_sessions": _count_active(),
        "max_sessions": MAX_SESSIONS,
        "fd_count": _current_fd_count(),
        "reaper_last_ran_at": _dt(_reaper_last_ran_at),
        "db_path": DB_PATH,
        "bind_host": "127.0.0.1",
        # Whether headful (headless=False) sessions will be VISIBLE on screen.
        "headful_visible": hf_ok,
        "headful_detail": hf_detail,
    }


_deep_health_lock = threading.Lock()
_deep_health_warmed = False


def _deep_health_probe():
    """Create and tear down a real headless session through the broker path."""
    global _deep_health_warmed
    if not _deep_health_lock.acquire(blocking=False):
        return JSONResponse(
            status_code=503,
            content={"status": "error", "phase": "busy", "fd_count": _current_fd_count_uncached()},
        )

    started_at = time.monotonic()
    fd_before = _current_fd_count_uncached()
    session_id = None
    try:
        created = create_session(
            CreateSessionRequest(
                headless=True,
                name="scraper-bot-deep-health",
                owner="health-monitor",
                labels={"probe": "deep-health"},
            )
        )
        if isinstance(created, JSONResponse):
            payload = json.loads(created.body)
            raise RuntimeError(payload.get("detail") or payload.get("error") or "session create failed")
        session_id = created["session_id"]
        sess = _sessions[session_id]
        with sess.lock:
            _close_session_sync(sess, CloseReason.CLIENT_CLOSE)
        fd_after = _current_fd_count_uncached()
        warmup = not _deep_health_warmed
        _deep_health_warmed = True
        payload = {
            "status": "ok",
            "session_path": "ok",
            "session_id": session_id,
            "fd_count": fd_after,
            "fd_before": fd_before,
            "fd_delta": fd_after - fd_before,
            "warmup": warmup,
            "elapsed_seconds": round(time.monotonic() - started_at, 3),
        }
        max_fd_delta = (
            DEEP_HEALTH_WARMUP_MAX_FD_DELTA if warmup else DEEP_HEALTH_MAX_FD_DELTA
        )
        if payload["fd_delta"] > max_fd_delta:
            payload.update(status="error", phase="fd_leak")
            return JSONResponse(status_code=503, content=payload)
        return payload
    except Exception as e:
        if session_id:
            sess = _sessions.get(session_id)
            if sess is not None and sess.state not in (SessionState.CLOSING, SessionState.CLOSED):
                with sess.lock:
                    _close_session_sync(sess, CloseReason.ERROR)
        fd_after = _current_fd_count_uncached()
        logger.error("Deep health probe failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "status": "error",
                "phase": "session_path",
                "detail": str(e),
                "fd_count": fd_after,
                "fd_before": fd_before,
                "elapsed_seconds": round(time.monotonic() - started_at, 3),
            },
        )
    finally:
        _deep_health_lock.release()


@app.post("/health/deep")
def deep_health(request: Request):
    """Authenticated liveness probe for session creation and teardown."""
    _check_token(request)
    return _deep_health_probe()


@app.get("/sessions")
def list_sessions():
    """List all live sessions (active/creating/closing) with metadata."""
    return {
        sid: _session_to_dict(sess)
        for sid, sess in _sessions.items()
        if sess.state != SessionState.CLOSED
    }


@app.get("/sessions/{session_id}")
def get_session(session_id: str):
    """Get a single session by id."""
    sess = _get_or_404(session_id)
    return _session_to_dict(sess)


# ---------------------------------------------------------------------------
# Endpoints — session management (destructive routes require token)
# ---------------------------------------------------------------------------

@app.post("/sessions")
def create_session(req: CreateSessionRequest = None):
    """Create a new Chrome session.

    Returns 409 if user_data_dir is already held by an active session.
    Returns 429 if max_sessions cap is reached.
    """
    if req is None:
        req = CreateSessionRequest()

    driver_name = _normalize_driver_name(req.driver)
    if driver_name not in SUPPORTED_DRIVERS:
        raise HTTPException(status_code=400, detail=f"Unsupported driver: {req.driver}")
    if driver_name == PLAYWRIGHT_DRIVER and not _playwright_available():
        raise HTTPException(status_code=400, detail="Playwright is not installed")

    fd_count = _current_fd_count()
    if fd_count > FD_MAX:
        logger.warning(
            f"Refusing new session under FD pressure: fd_count={fd_count}, threshold={FD_MAX}"
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": "scraper-bot FD pressure — refuse new sessions",
                "fd_count": fd_count,
            },
        )

    source_user_data_dir = os.path.realpath(req.user_data_dir) if req.user_data_dir else None
    session_id = uuid.uuid4().hex[:12]
    normalized_udd = source_user_data_dir
    if driver_name == PLAYWRIGHT_DRIVER:
        normalized_udd = _prepare_playwright_profile(session_id, source_user_data_dir)

    # Check cap
    with _sessions_lock:
        if _count_active() >= MAX_SESSIONS:
            if driver_name == PLAYWRIGHT_DRIVER:
                _cleanup_user_data_dir_if_unclaimed(normalized_udd, True)
            return JSONResponse(
                status_code=429,
                content={"error": "session_cap_exceeded", "current": _count_active(), "max": MAX_SESSIONS},
            )

        # Check user_data_dir exclusivity
        if normalized_udd:
            for sess in _sessions.values():
                if (
                    sess.state in (SessionState.CREATING, SessionState.ACTIVE, SessionState.CLOSING)
                    and sess.user_data_dir == normalized_udd
                ):
                    return JSONResponse(
                        status_code=409,
                        content={"error": "user_data_dir_in_use", "held_by": sess.id},
                    )

        now = datetime.now()
        record = SessionRecord(
            id=session_id,
            name=req.name,
            owner=req.owner,
            labels=req.labels,
            job_id=req.job_id,
            state=SessionState.CREATING,
            close_reason=None,
            created_at=now,
            last_request_at=now,
            last_action_at=now,
            last_heartbeat_at=now if req.lease_mode else None,
            last_url="",
            last_error=None,
            action_count=0,
            error_count=0,
            closed_at=None,
            lease_mode=req.lease_mode,
            heartbeat_ttl_seconds=req.heartbeat_ttl_seconds,
            user_data_dir=normalized_udd,
            pid=None,
            hostname=HOSTNAME,
            driver_backend=driver_name,
            dm=None,
        )
        _sessions[session_id] = record
        _db_upsert_record(_db_conn, record)

    req.headless = resolve_headless(req.headless)

    logger.info(
        f"Creating session {session_id} "
        f"(name={req.name!r}, owner={req.owner!r}, driver={driver_name}, undetected={req.undetected}, headless={req.headless})"
    )

    # A caller asking for headful almost always wants to SEE the window. If we
    # can't surface windows, warn now (with remediation) instead of letting the
    # window render off-screen silently.
    if not req.headless:
        hf_ok, hf_detail = _windowserver_access()
        if not hf_ok:
            logger.warning(
                f"Session {session_id}: headful requested but no WindowServer "
                f"access ({hf_detail}). {HEADFUL_REMEDIATION}"
            )

    # Launch Chrome outside sessions_lock (slow op)
    user_data_dir_existed_before = bool(normalized_udd and os.path.exists(normalized_udd))
    preexisting_children = _get_our_chrome_descendants()

    # Clear stale Chrome singleton locks before launching on a persistent profile.
    # The 409 user_data_dir_in_use guard above guarantees no other active
    # scraper-bot session holds this profile, so any SingletonLock here is stale
    # (unclean prior exit). Left in place it makes a fresh headful uc.Chrome hand
    # off to a phantom instance and die with "invalid session id" seconds after
    # going active — the headful flakiness this change targets.
    if normalized_udd:
        _cleanup_chrome_profile(normalized_udd)

    try:
        if driver_name == PLAYWRIGHT_DRIVER:
            dm = PlaywrightDriverManager(
                headless=req.headless,
                view=req.view,
                user_data_dir=normalized_udd,
                trace=req.trace,
                trace_dir=req.trace_dir,
                session_id=session_id,
            )
        else:
            version_main = (
                req.chrome_version_main
                if req.chrome_version_main is not None
                else _auto_chrome_major()
            )
            dm = DriverManager(
                undetected=req.undetected,
                headless=req.headless,
                chrome_version_main=version_main,
                view=req.view,
                user_data_dir=normalized_udd,
            )
        download_dir = f"/tmp/scraper-bot-downloads/{session_id}"
        download_tracker = None
        os.makedirs(download_dir, mode=0o700, exist_ok=True)
        os.chmod(download_dir, 0o700)
        if driver_name != PLAYWRIGHT_DRIVER:
            dm.driver.execute_cdp_cmd("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": download_dir, "eventsEnabled": True})
            try:
                download_tracker = BrowserDownloadTracker.connect(session_id, download_dir, dm.driver)
            except Exception as tracker_error:
                logger.warning("Session %s has no browser download event channel: %s", session_id, tracker_error)
    except Exception as e:
        logger.error(f"Session {session_id}: {driver_name} start failed: {e}")
        _cleanup_failed_driver_start(
            session_id=session_id,
            exc=e,
            before_children=preexisting_children,
            user_data_dir=normalized_udd,
            created_fresh_profile=not user_data_dir_existed_before,
        )
        with record.lock:
            record.state = SessionState.CLOSED
            record.close_reason = CloseReason.CREATE_FAILED
            record.closed_at = datetime.now()
            record.last_error = str(e)
            _db_upsert_record(_db_conn, record)
        raise HTTPException(status_code=500, detail=f"Failed to start {driver_name}: {e}")

    # Grab chrome pid
    pid = None
    try:
        pid = dm.driver.service.process.pid
    except Exception:
        pass

    close_launched_dm = False
    final_state = "active"
    with record.lock:
        if record.state in (SessionState.CLOSING, SessionState.CLOSED):
            close_launched_dm = True
            final_state = record.state.value
            record.dm = None
            record.pid = pid
            if record.state == SessionState.CLOSING:
                record.state = SessionState.CLOSED
                if record.close_reason is None:
                    record.close_reason = CloseReason.CLIENT_CLOSE
                if record.closed_at is None:
                    record.closed_at = datetime.now()
            _db_upsert_record(_db_conn, record)
        else:
            record.dm = dm
            record.pid = pid
            record.trace_path = getattr(dm, "trace_path", None)
            record.download_dir = download_dir
            record.download_tracker = download_tracker
            record.state = SessionState.ACTIVE
            _db_upsert_record(_db_conn, record)

    if close_launched_dm:
        logger.info(
            f"Session {session_id} was {final_state} while Chrome launched; "
            "closing launched driver instead of reactivating it"
        )
        _close_dm_with_timeout(dm, 5.0)
        return JSONResponse(
            status_code=409,
            content={"error": "session_closed_during_create", "session_id": session_id},
        )

    logger.info(f"Session {session_id} active (pid={pid})")
    return {
        "session_id": session_id,
        "state": "active",
        "driver": record.driver_backend,
        "trace_path": record.trace_path,
    }


@app.delete("/sessions/{session_id}")
def close_session(request: Request, session_id: str, close_reason: str = "client_close"):
    """Close a session. Idempotent — always returns 200 with final state.

    Requires X-Scraper-Token header or ?token= query param.
    """
    _check_token(request)
    sess = _sessions.get(session_id)
    if sess is None:
        # Session may have been evicted from memory. Check DB for a closed row
        # so idempotency is preserved even after eviction.
        with _db_lock:
            cur = _db_conn.execute(
                "SELECT id, state, close_reason, closed_at FROM sessions WHERE id=? AND state='closed'",
                (session_id,),
            )
            row = cur.fetchone()
        if row:
            return {
                "status": "already_closed",
                "id": row[0],
                "state": row[1],
                "close_reason": row[2],
                "closed_at": row[3],
            }
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    try:
        reason = CloseReason(close_reason)
    except ValueError:
        reason = CloseReason.CLIENT_CLOSE

    # Generous timeout so an in-flight action can finish. If it doesn't,
    # return the current state without raising — caller can retry.
    acquired = sess.lock.acquire(timeout=65.0)
    if not acquired:
        logger.warning(f"DELETE {session_id}: lock timeout, returning current state")
        return {"status": "lock_timeout", **_session_to_dict(sess)}
    try:
        if sess.state in (SessionState.CLOSING, SessionState.CLOSED):
            return {"status": "already_closed", **_session_to_dict(sess)}
        _close_session_sync(sess, reason)
    finally:
        sess.lock.release()

    return {"status": "closed", "close_reason": sess.close_reason.value}


@app.post("/sessions/{session_id}/heartbeat")
def heartbeat(session_id: str):
    """Update last_heartbeat_at for lease-mode sessions.

    Returns state and when the session will be closed if no more heartbeats arrive.
    """
    sess = _get_or_404(session_id)
    # Short lock timeout: heartbeat is cheap, don't block on a slow handler.
    # If contended, we return the current state without updating — the thread
    # holding the lock will keep the session active itself.
    acquired = sess.lock.acquire(timeout=1.0)
    if not acquired:
        return {"state": sess.state.value, "closes_at": None, "contended": True}
    try:
        if sess.state != SessionState.ACTIVE:
            raise HTTPException(status_code=404, detail=f"Session '{sess.id}' is {sess.state.value}")
        now = datetime.now()
        sess.last_heartbeat_at = now
        sess.last_request_at = now
        closes_at = None
        if sess.lease_mode:
            closes_at = (now + timedelta(seconds=sess.heartbeat_ttl_seconds)).isoformat()
        return {"state": sess.state.value, "closes_at": closes_at}
    finally:
        sess.lock.release()


# ---------------------------------------------------------------------------
# Session-action endpoints
# ---------------------------------------------------------------------------

@app.post("/sessions/{session_id}/navigate")
def navigate(session_id: str, req: NavigateRequest):
    with _session_action(session_id) as (sess, dm):
        try:
            dm.get(req.url)
            url = dm.get_current_url()
            _touch_action(sess)
            sess.last_url = url
            return {"url": url}
        except Exception as e:
            sess.last_error = str(e)
            sess.error_count += 1
            raise HTTPException(status_code=400, detail=str(e))


@app.get("/sessions/{session_id}/url")
def get_url(session_id: str):
    with _session_action(session_id) as (_, dm):
        return {"url": dm.get_current_url()}


@app.get("/sessions/{session_id}/html")
def get_html(session_id: str):
    with _session_action(session_id) as (_, dm):
        return {"html": dm.get_page_source()}


@app.get("/sessions/{session_id}/title")
def get_title(session_id: str):
    with _session_action(session_id) as (_, dm):
        return {"title": dm.driver.title}


@app.post("/sessions/{session_id}/click")
def click(session_id: str, req: ClickRequest):
    with _session_action(session_id) as (sess, dm):
        action_key = req.action_instance_id
        fingerprint = hashlib.sha256(json.dumps({"xpath": req.xpath, "css": req.css, "id": req.id, "link_text": req.link_text, "scroll_first": req.scroll_first}, sort_keys=True).encode()).hexdigest()
        if action_key:
            prior = sess.idempotent_actions.get(action_key)
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise HTTPException(status_code=409, detail="action_instance_id reused with different click")
                if prior["state"] == "complete":
                    return dict(prior["result"])
                raise HTTPException(status_code=409, detail="action_instance_id outcome is ambiguous")
            sess.idempotent_actions[action_key] = {"fingerprint": fingerprint, "state": "dispatched"}
        try:
            el = _find_element(dm, xpath=req.xpath, css=req.css, id_=req.id, link_text=req.link_text)
            if req.scroll_first:
                dm.scroll_to_view(el)
            el.click()
            _touch_action(sess)
            result = {"status": "clicked", "action_instance_id": action_key}
            if action_key:
                sess.idempotent_actions[action_key] = {"fingerprint": fingerprint, "state": "complete", "result": result}
            return result
        except HTTPException:
            raise
        except Exception as e:
            sess.last_error = str(e)
            sess.error_count += 1
            raise HTTPException(status_code=400, detail=str(e))


@app.post("/sessions/{session_id}/type")
@fill_endpoint("legacy.type")
def type_text(session_id: str, req: TypeRequest):
    with _session_action(session_id) as (sess, dm):
        def operation():
            ti._disable_fill_capture(dm.driver)
            el = _find_element(dm, xpath=req.xpath, css=req.css, id_=req.id)
            if req.clear_first:
                el.clear()
            el.send_keys(req.text)
            if req.press_enter:
                from selenium.webdriver.common.keys import Keys
                el.send_keys(Keys.RETURN)
            return {"status": "typed"}
        return _run_fill(sess, dm, operation, primitive="legacy.type")


@app.post("/sessions/{session_id}/upload")
@app.post("/session/{session_id}/upload")
async def upload_file(session_id: str, request: Request):
    filename, file_bytes, selector, selector_type = await _parse_upload_request(request)
    # Persist the upload long enough for the browser's ASYNCHRONOUS upload (e.g. the
    # FilePond uploader on Fillout forms) to actually read it. Deleting the file
    # immediately after send_keys() — before the page's async read/POST — races that
    # read and surfaces in the UI as "Error during upload / tap to retry". Keep it in a
    # dedicated dir under its ORIGINAL filename (some uploaders validate the name) and
    # reap stale uploads on subsequent calls instead of unlinking inline.
    upload_root = os.path.join(tempfile.gettempdir(), "scraper-bot-uploads")
    os.makedirs(upload_root, exist_ok=True)
    _now = time.time()
    for _stale in os.listdir(upload_root):
        _p = os.path.join(upload_root, _stale)
        try:
            if _now - os.path.getmtime(_p) > 1800:
                shutil.rmtree(_p, ignore_errors=True) if os.path.isdir(_p) else os.unlink(_p)
        except OSError:
            pass
    _holder = os.path.join(upload_root, uuid.uuid4().hex)
    os.makedirs(_holder, exist_ok=True)
    temp_path = os.path.join(_holder, os.path.basename(filename) or "upload.bin")
    with open(temp_path, "wb") as tmp:
        tmp.write(file_bytes)

    with _session_action(session_id) as (sess, dm):
        try:
            if selector_type == "xpath":
                el = _find_element(dm, xpath=str(selector))
            else:
                el = _find_element(dm, css=str(selector))
            el.send_keys(temp_path)
            _touch_action(sess)
            return {
                "ok": True,
                "filename": filename,
                "selector": selector,
            }
        except HTTPException:
            raise
        except Exception as e:
            sess.last_error = str(e)
            sess.error_count += 1
            raise HTTPException(status_code=400, detail=str(e))


@app.post("/sessions/{session_id}/execute")
def execute_js(session_id: str, req: ExecuteJsRequest):
    with _session_action(session_id) as (sess, dm):
        try:
            result = dm.execute_script(req.script, *req.args)
            _touch_action(sess)
            try:
                json.dumps(result)
                return {"result": result}
            except (TypeError, ValueError):
                return {"result": str(result)}
        except HTTPException:
            raise
        except Exception as e:
            sess.last_error = str(e)
            sess.error_count += 1
            raise HTTPException(status_code=400, detail=str(e))


# ---------------------------------------------------------------------------
# Trusted-input primitives — added ALONGSIDE (never changing) execute_js/click.
# Headline: trusted typing (CDP key events) for framework filters; plus id-less
# targeting, overlay/interception detection, and virtualized-list wait semantics.
# Every response records which primitive ran, for honest failure attribution.
# ---------------------------------------------------------------------------

@app.post("/sessions/{session_id}/input/type")
@fill_endpoint("trusted.type")
def input_type(session_id: str, req: TrustedTypeRequest):
    with _session_action(session_id) as (sess, dm):
        return _run_fill(
            sess, dm, ti.type_text, dm.driver, text=req.text, css=req.css, xpath=req.xpath,
            locate_text=req.locate_text, scope_css=req.scope_css, scope_xpath=req.scope_xpath,
            exact=req.exact, index=req.index, mode=req.mode, focus=req.focus,
            require_focus=req.require_focus, clear_first=req.clear_first, press_enter=req.press_enter,
            expect=req.expect, verify=req.verify, verify_timeout_ms=req.verify_timeout_ms,
            per_char_delay_ms=req.per_char_delay_ms)


@app.post("/sessions/{session_id}/input/click_text")
def input_click_text(session_id: str, req: ClickTextRequest):
    with _session_action(session_id) as (sess, dm):
        return _run_trusted(
            sess, ti.click, dm.driver, by=req.by, css=req.css, xpath=req.xpath, text=req.text,
            scope_css=req.scope_css, scope_xpath=req.scope_xpath, exact=req.exact, tag=req.tag,
            index=req.index, scroll=req.scroll, allow_covered=req.allow_covered, button=req.button,
            click_count=req.click_count, expect=req.expect, verify_timeout_ms=req.verify_timeout_ms,
            primitive="trusted.click_text")


@app.post("/sessions/{session_id}/input/click_point")
def input_click_point(session_id: str, req: ClickPointRequest):
    with _session_action(session_id) as (sess, dm):
        return _run_trusted(
            sess, ti.click, dm.driver, by="point", x=req.x, y=req.y, button=req.button,
            click_count=req.click_count, expect=req.expect, verify_timeout_ms=req.verify_timeout_ms,
            primitive="trusted.click_point")


@app.post("/sessions/{session_id}/input/select_option")
@fill_endpoint("trusted.select_option")
def input_select_option(session_id: str, req: SelectOptionRequest):
    with _session_action(session_id) as (sess, dm):
        return _run_fill(
            sess, dm, ti.select_option, dm.driver, primitive="trusted.select_option", input_css=req.input_css, input_xpath=req.input_xpath,
            item_text=req.item_text, item_index=req.item_index, item_scope_css=req.item_scope_css,
            item_scope_xpath=req.item_scope_xpath, item_tag=req.item_tag, item_exact=req.item_exact,
            type_value=req.type_value, clear_first=req.clear_first, type_mode=req.type_mode,
            settle_ms=req.settle_ms, mask_css=req.mask_css, mask_timeout_ms=req.mask_timeout_ms,
            open_timeout_ms=req.open_timeout_ms, verify_scope_css=req.verify_scope_css,
            verify_text=req.verify_text, verify_timeout_ms=req.verify_timeout_ms,
            verify_value_css=req.verify_value_css,
            trigger_css=req.trigger_css, trigger_xpath=req.trigger_xpath,
            open_via_trigger=req.open_via_trigger)


@app.post("/sessions/{session_id}/input/wait_for")
def input_wait_for(session_id: str, req: WaitForRequest):
    with _session_action(session_id) as (sess, dm):
        return _run_trusted(
            sess, ti.wait_for, dm.driver, by=req.by, css=req.css, xpath=req.xpath, text=req.text,
            scope_css=req.scope_css, scope_xpath=req.scope_xpath, exact=req.exact, tag=req.tag,
            timeout_ms=req.timeout_ms, min_count=req.min_count)


@app.post("/sessions/{session_id}/input/overlay_check")
def input_overlay_check(session_id: str, req: OverlayCheckRequest):
    with _session_action(session_id) as (sess, dm):
        return _run_trusted(
            sess, ti.overlay_check, dm.driver, css=req.css, xpath=req.xpath, text=req.text,
            scope_css=req.scope_css, scope_xpath=req.scope_xpath, exact=req.exact, x=req.x, y=req.y)


@app.post("/sessions/{session_id}/downloads/begin")
def begin_download(session_id: str, req: DownloadIntentRequest):
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,256}", req.action_instance_id):
        raise HTTPException(status_code=400, detail="invalid action_instance_id")
    with _session_action(session_id) as (sess, _dm):
        directory = Path(sess.download_dir or "")
        tracker = sess.download_tracker
        if not directory.is_dir() or tracker is None:
            raise HTTPException(status_code=409, detail="browser download event channel unavailable")
        sess.download_intent = {
            "action_instance_id": req.action_instance_id,
            "started_at": datetime.now().astimezone().isoformat(),
            "before": sorted(path.name for path in directory.iterdir()),
        }
        sess.download_events = []
        try:
            tracker.arm(req.action_instance_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "armed", "action_instance_id": req.action_instance_id}


@app.get("/sessions/{session_id}/downloads")
def get_download_events(session_id: str):
    with _session_action(session_id) as (sess, _dm):
        intent = sess.download_intent
        directory = Path(sess.download_dir or "")
        tracker = sess.download_tracker
        if not intent or not directory.is_dir() or tracker is None:
            return {"events": []}
        events = []
        for browser_event in tracker.events():
            if browser_event.get("action_instance_id") != intent["action_instance_id"] or browser_event.get("browser_state") != "completed":
                continue
            browser_path = browser_event.get("browser_file_path")
            path = Path(browser_path) if browser_path else directory / browser_event.get("suggested_filename", "")
            try:
                if path.parent.resolve() != directory.resolve() or path.name in intent["before"]:
                    continue
            except OSError:
                continue
            if path.name.endswith((".crdownload", ".part")) or path.is_symlink() or not path.is_file():
                continue
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(path, flags)
                first = os.fstat(fd)
                digest = hashlib.sha256()
                while chunk := os.read(fd, 1024 * 1024):
                    digest.update(chunk)
                second = os.fstat(fd)
            except OSError:
                continue
            finally:
                if "fd" in locals():
                    os.close(fd)
                    del fd
            if (first.st_dev, first.st_ino, first.st_size, first.st_mtime_ns) != (second.st_dev, second.st_ino, second.st_size, second.st_mtime_ns) or second.st_size <= 0:
                continue
            current_descriptor = {"device": second.st_dev, "inode": second.st_ino, "size": second.st_size, "mtime_ns": second.st_mtime_ns, "sha256": digest.hexdigest()}
            if current_descriptor != browser_event.get("browser_descriptor"):
                continue
            event = {
                "event_id": hashlib.sha256(f"{session_id}:{intent['action_instance_id']}:{browser_event['guid']}:{second.st_ino}".encode()).hexdigest(),
                "session_id": session_id,
                "action_instance_id": intent["action_instance_id"],
                "path": str(path.absolute()),
                "source_url": browser_event["source_url"],
                "browser_guid": browser_event["guid"],
                "browser_state": "completed",
                "completed_at": browser_event["browser_completed_at"],
                "device": second.st_dev,
                "inode": second.st_ino,
                "size": second.st_size,
                "sha256": current_descriptor["sha256"],
            }
            events.append(event)
        sess.download_events = events
        return {"events": events}


@app.post("/sessions/{session_id}/find")
def find_elements(session_id: str, req: FindRequest):
    with _session_action(session_id) as (_, dm):
        try:
            if req.multiple:
                els = _find_elements(dm, xpath=req.xpath, css=req.css, id_=req.id, tag=req.tag, class_name=req.class_name)
                return {"count": len(els), "elements": [_element_info(el) for el in els[:100]]}
            else:
                el = _find_element(dm, xpath=req.xpath, css=req.css, id_=req.id, tag=req.tag, class_name=req.class_name)
                return {"element": _element_info(el)}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))


@app.post("/sessions/{session_id}/wait")
def wait_for(session_id: str, req: WaitRequest):
    with _session_action(session_id) as (sess, dm):
        try:
            if hasattr(dm, "wait_for_selector"):
                dm.wait_for_selector(xpath=req.xpath, css=req.css, id_=req.id, timeout=req.timeout)
            else:
                from selenium.webdriver.common.by import By
                from selenium.webdriver.support.ui import WebDriverWait
                from selenium.webdriver.support import expected_conditions as EC
                if req.xpath:
                    WebDriverWait(dm.driver, req.timeout).until(
                        EC.presence_of_element_located((By.XPATH, req.xpath))
                    )
                elif req.css:
                    WebDriverWait(dm.driver, req.timeout).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, req.css))
                    )
                elif req.id:
                    WebDriverWait(dm.driver, req.timeout).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, f"#{req.id}"))
                    )
                else:
                    raise HTTPException(status_code=400, detail="Provide xpath, css, or id to wait for")
            _touch_action(sess)
            return {"status": "found"}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=408, detail=f"Timeout waiting: {e}")


@app.post("/sessions/{session_id}/scroll")
def scroll(session_id: str, req: ScrollRequest):
    with _session_action(session_id) as (sess, dm):
        if req.xpath:
            el = dm.find_element_by_xpath(req.xpath)
            dm.scroll_to_view(el)
            _touch_action(sess)
            return {"status": "scrolled_to_element"}
        amount = req.amount or 500
        if req.direction == "up":
            amount = -amount
        dm.scroll_by(amount)
        _touch_action(sess)
        return {"status": "scrolled", "amount": amount}


@app.post("/sessions/{session_id}/select")
def select(session_id: str, req: SelectRequest):
    with _session_action(session_id) as (sess, dm):
        try:
            dm.select_by_value(req.id, req.value)
            _touch_action(sess)
            return {"status": "selected"}
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))


@app.post("/sessions/{session_id}/iframe")
def switch_iframe(session_id: str, req: SwitchIframeRequest):
    with _session_action(session_id) as (sess, dm):
        if req.main:
            dm.switch_to_main()
            return {"status": "switched_to_main"}
        if req.xpath:
            iframe = dm.find_element_by_xpath(req.xpath)
            dm.switch_to_iframe(iframe)
        elif req.index is not None:
            dm.driver.switch_to.frame(req.index)
        _touch_action(sess)
        return {"status": "switched_to_iframe"}


@app.get("/sessions/{session_id}/screenshot")
def screenshot(session_id: str):
    with _session_action(session_id) as (_, dm):
        try:
            return {"screenshot_base64": dm.driver.get_screenshot_as_base64()}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


@app.get("/sessions/{session_id}/cookies")
def get_cookies(session_id: str):
    with _session_action(session_id) as (_, dm):
        return {"cookies": dm.get_browser_cookies()}


@app.post("/sessions/{session_id}/network/enable")
def enable_network(session_id: str):
    with _session_action(session_id) as (_, dm):
        cdp = getattr(dm.driver, "execute_cdp_cmd", None)
        if callable(cdp):
            cdp("Network.enable", {})
        dm.enable_network_logging()
        dm._network_enabled = True
        return {"status": "network_logging_enabled"}


@app.get("/sessions/{session_id}/network/requests")
def get_network_requests(session_id: str, only_xhr: bool = False):
    with _session_action(session_id) as (_, dm):
        return {"requests": dm.get_network_requests(only_xhr=only_xhr)}


@app.get("/sessions/{session_id}/network/traffic")
def get_network_traffic(session_id: str):
    with _session_action(session_id) as (_, dm):
        return {"traffic": dm.get_network_traffic()}


# ---------------------------------------------------------------------------
# Network: clear + filtered requests
# ---------------------------------------------------------------------------


@app.post("/sessions/{session_id}/network/clear")
def clear_network(session_id: str):
    """Clear captured network logs for this session.

    After clearing, network_requests/traffic return only entries logged
    after this call.  DriverManager stores logs in-memory, so this
    resets that list to empty.
    """
    with _session_action(session_id) as (sess, dm):
        if hasattr(dm, "clear_network"):
            dm.clear_network()
        else:
            dm.clear_network_logs()
        _touch_action(sess)
        return {"status": "network_logs_cleared"}


class FilteredNetworkRequest(BaseModel):
    url_contains: Optional[str] = None
    only_xhr: bool = False


@app.post("/sessions/{session_id}/network/filter")
def get_filtered_network_requests(session_id: str, req: FilteredNetworkRequest):
    """Return network requests matching server-side filters.

    Avoids pulling the full log to the client when only a handful of
    URLs are interesting (e.g. ArcGIS MapServer queries).
    """
    with _session_action(session_id) as (_, dm):
        all_reqs = dm.get_network_requests(only_xhr=req.only_xhr)
        if req.url_contains:
            all_reqs = [r for r in all_reqs if req.url_contains in (r.get("url") or "")]
        return {"requests": all_reqs, "count": len(all_reqs)}


# ---------------------------------------------------------------------------
# reCAPTCHA solver
# ---------------------------------------------------------------------------


class SolveRecaptchaRequest(BaseModel):
    max_attempts: int = 3
    audio_download_dir: str = "/tmp/recaptcha_hack"
    # When the reCAPTCHA is nested inside another iframe (e.g. a Fillout form
    # embed), pass an xpath matching that parent iframe (resolved from the top
    # frame). The solver returns to this iframe between frame switches; if
    # omitted it searches the top frame only — which fails for nested embeds.
    parent_iframe_xpath: Optional[str] = None


@app.post("/sessions/{session_id}/solve_recaptcha")
def solve_recaptcha(session_id: str, req: SolveRecaptchaRequest = SolveRecaptchaRequest()):
    """Attempt to solve a reCAPTCHA v2 challenge on the current page.

    Uses the audio challenge + Whisper transcription approach from
    modules/ReCaptchaSolver.  Runs server-side with direct Selenium
    access — no HTTP round-trips per iframe switch.

    Returns {"solved": true/false}.
    """
    with _session_action(session_id) as (sess, dm):
        if sess.driver_backend == PLAYWRIGHT_DRIVER:
            raise HTTPException(status_code=400, detail="solve_recaptcha is only supported for chromedriver sessions")
        try:
            # Optional external solver; never required for browser scraping.
            _lb = os.environ.get("LAND_BOT_PATH")
            if not _lb:
                raise RuntimeError("Optional solver not installed; set LAND_BOT_PATH explicitly")
            if _lb not in sys.path:
                sys.path.insert(0, _lb)
            if "modules.TranscribeAI" not in sys.modules:
                _ta_spec = importlib.util.spec_from_file_location(
                    "modules.TranscribeAI",
                    os.path.join(_lb, "modules", "TranscribeAI.py"),
                )
                _ta_mod = importlib.util.module_from_spec(_ta_spec)
                sys.modules["modules.TranscribeAI"] = _ta_mod
                _ta_spec.loader.exec_module(_ta_mod)
            _rc_spec = importlib.util.spec_from_file_location(
                "ReCaptchaSolver",
                os.path.join(_lb, "modules", "ReCaptchaSolver.py"),
            )
            _rc_mod = importlib.util.module_from_spec(_rc_spec)
            _rc_spec.loader.exec_module(_rc_mod)
            _solve_recaptcha = _rc_mod.solve
            # Resolve the nested-iframe parent (e.g. the Fillout embed) so the
            # solver searches inside it for the anchor/challenge frames instead
            # of the top frame. Without this, a reCAPTCHA embedded in an iframe
            # is invisible to the solver and it returns False immediately.
            parent_el = None
            if req.parent_iframe_xpath:
                dm.switch_to_main()
                _pf = dm.find_elements_by_xpath(req.parent_iframe_xpath)
                if _pf:
                    parent_el = _pf[0]
                else:
                    logger.warning(
                        f"Session {sess.id}: solve_recaptcha parent_iframe_xpath "
                        f"matched no iframe: {req.parent_iframe_xpath!r}"
                    )
            solved = _solve_recaptcha(
                dm,
                audio_download_dir=req.audio_download_dir,
                max_attempts=req.max_attempts,
                parent_iframe=parent_el,
            )
            _touch_action(sess)
            return {"solved": solved}
        except Exception as e:
            sess.last_error = str(e)
            sess.error_count += 1
            logger.warning(f"Session {sess.id}: solve_recaptcha error: {e}")
            return {"solved": False, "error": str(e)}


@app.post("/sessions/{session_id}/back")
def go_back(session_id: str):
    with _session_action(session_id) as (sess, dm):
        dm.driver.back()
        _touch_action(sess)
        return {"url": dm.get_current_url()}


@app.post("/sessions/{session_id}/forward")
def go_forward(session_id: str):
    with _session_action(session_id) as (sess, dm):
        dm.driver.forward()
        _touch_action(sess)
        return {"url": dm.get_current_url()}


@app.post("/sessions/{session_id}/refresh")
def refresh_page(session_id: str):
    with _session_action(session_id) as (sess, dm):
        dm.driver.refresh()
        _touch_action(sess)
        return {"url": dm.get_current_url()}


# ---------------------------------------------------------------------------
# Kill-stale endpoint (destructive — requires token)
# ---------------------------------------------------------------------------

@app.post("/sessions/kill_stale")
def kill_stale(request: Request):
    """Close all sessions whose last_action_at is older than 10 minutes.

    Requires X-Scraper-Token header or ?token= query param.
    """
    _check_token(request)
    cutoff = datetime.now() - timedelta(minutes=10)
    killed = []
    for sess in list(_sessions.values()):
        if sess.state == SessionState.ACTIVE and sess.last_action_at < cutoff:
            acquired = sess.lock.acquire(timeout=1.0)
            if acquired:
                try:
                    _close_session_sync(sess, CloseReason.MANUAL_KILL)
                    killed.append(sess.id)
                finally:
                    sess.lock.release()
    return {"killed": killed, "count": len(killed)}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _render_dashboard() -> str:
    """Render the HTML dashboard as an f-string."""
    now = datetime.now()
    uptime_s = int((now - _server_start_time).total_seconds())
    uptime_str = f"{uptime_s // 3600}h {(uptime_s % 3600) // 60}m {uptime_s % 60}s"
    active_count = _count_active()
    reaper_str = _dt(_reaper_last_ran_at) or "not yet"

    # Try to read dashboard token for embedding in kill buttons
    try:
        token = open(DASHBOARD_TOKEN_PATH).read().strip()
    except Exception:
        token = ""

    # Live sessions
    live_rows = ""
    for sess in _sessions.values():
        if sess.state == SessionState.CLOSED:
            continue
        hb = _dt(sess.last_heartbeat_at) or "-"
        kill_url = f"/sessions/{sess.id}?token={token}&close_reason=manual_kill"
        live_rows += f"""
        <tr>
          <td><code>{sess.id}</code></td>
          <td>{sess.name or '-'}</td>
          <td>{sess.owner or '-'}</td>
          <td>{sess.job_id or '-'}</td>
          <td><span class="badge badge-{sess.state.value}">{sess.state.value}</span></td>
          <td>{_dt(sess.created_at) or '-'}</td>
          <td>{_dt(sess.last_action_at) or '-'}</td>
          <td>{hb}</td>
          <td>{sess.action_count}</td>
          <td>{sess.error_count}</td>
          <td>{sess.last_url[:60] or '-'}</td>
          <td><button class="kill-btn" data-url="{kill_url}" data-method="DELETE">Kill</button></td>
        </tr>"""

    # Recent closed (from DB)
    closed_rows = ""
    if _db_conn:
        for row in _db_load_recent_closed(_db_conn, limit=50):
            closed_rows += f"""
        <tr>
          <td><code>{row['id']}</code></td>
          <td>{row.get('name') or '-'}</td>
          <td>{row.get('owner') or '-'}</td>
          <td>{row.get('job_id') or '-'}</td>
          <td>{row.get('state', '-')}</td>
          <td>{row.get('close_reason') or '-'}</td>
          <td>{row.get('created_at') or '-'}</td>
          <td>{row.get('closed_at') or '-'}</td>
          <td>{row.get('action_count', 0)}</td>
          <td>{row.get('error_count', 0)}</td>
          <td>{(row.get('last_url') or '')[:60]}</td>
        </tr>"""

    kill_stale_url = f"/sessions/kill_stale?token={token}"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="refresh" content="5">
  <title>Scraper Bot Dashboard</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', monospace; margin: 20px; background: #0d1117; color: #c9d1d9; }}
    h1 {{ color: #58a6ff; }} h2 {{ color: #79c0ff; border-bottom: 1px solid #30363d; padding-bottom: 4px; }}
    .info-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 20px; }}
    .info-card {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 12px; }}
    .info-card .label {{ color: #8b949e; font-size: 12px; }} .info-card .value {{ font-size: 18px; font-weight: bold; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th {{ background: #161b22; color: #8b949e; text-align: left; padding: 6px 8px; border-bottom: 1px solid #30363d; }}
    td {{ padding: 5px 8px; border-bottom: 1px solid #21262d; }}
    tr:hover td {{ background: #161b22; }}
    .badge {{ border-radius: 4px; padding: 2px 6px; font-size: 11px; font-weight: bold; }}
    .badge-active {{ background: #238636; }} .badge-creating {{ background: #9e6a03; }}
    .badge-closing {{ background: #da3633; }} .badge-closed {{ background: #30363d; }}
    .kill-btn {{ background: #da3633; color: white; border: none; border-radius: 4px; padding: 3px 8px; cursor: pointer; font-size: 12px; }}
    .kill-btn:hover {{ background: #b91c1c; }}
    .kill-stale-btn {{ background: #9e6a03; color: white; border: none; border-radius: 4px; padding: 6px 14px; cursor: pointer; margin-bottom: 12px; }}
    code {{ color: #79c0ff; }}
  </style>
</head>
<body>
  <h1>Scraper Bot Dashboard</h1>
  <div class="info-grid">
    <div class="info-card"><div class="label">Uptime</div><div class="value">{uptime_str}</div></div>
    <div class="info-card"><div class="label">Active Sessions</div><div class="value">{active_count} / {MAX_SESSIONS}</div></div>
    <div class="info-card"><div class="label">Version</div><div class="value">{VERSION}</div></div>
    <div class="info-card"><div class="label">Reaper Last Ran</div><div class="value" style="font-size:13px">{reaper_str}</div></div>
  </div>

  <h2>Live Sessions</h2>
  <button class="kill-stale-btn" onclick="fetch('{kill_stale_url}',{{method:'POST'}}).then(r=>r.json()).then(d=>alert('Killed: '+d.killed.join(', ') || 'none'))">
    Kill All Stale (&gt;10min idle)
  </button>
  <table>
    <thead><tr>
      <th>ID</th><th>Name</th><th>Owner</th><th>Job ID</th><th>State</th>
      <th>Created</th><th>Last Action</th><th>Last HB</th>
      <th>Actions</th><th>Errors</th><th>URL</th><th></th>
    </tr></thead>
    <tbody>{live_rows or '<tr><td colspan="12" style="color:#8b949e;text-align:center">No live sessions</td></tr>'}</tbody>
  </table>

  <h2 style="margin-top:28px">Recent Closed Sessions</h2>
  <table>
    <thead><tr>
      <th>ID</th><th>Name</th><th>Owner</th><th>Job ID</th><th>State</th>
      <th>Close Reason</th><th>Created</th><th>Closed</th>
      <th>Actions</th><th>Errors</th><th>Last URL</th>
    </tr></thead>
    <tbody>{closed_rows or '<tr><td colspan="11" style="color:#8b949e;text-align:center">No closed sessions</td></tr>'}</tbody>
  </table>

  <script>
    document.querySelectorAll('.kill-btn').forEach(btn => {{
      btn.addEventListener('click', function() {{
        if (!confirm('Kill session?')) return;
        fetch(this.dataset.url, {{method: this.dataset.method}})
          .then(r => r.json()).then(() => location.reload());
      }});
    }});
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """HTML dashboard — auto-refreshes every 5 seconds."""
    return HTMLResponse(_render_dashboard())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scraper Bot server")
    parser.add_argument("--port", type=int, default=9020)
    parser.add_argument("--host", type=str, default="127.0.0.1")
    args = parser.parse_args()

    logger.info(f"Starting Scraper Bot v{VERSION} on {args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
