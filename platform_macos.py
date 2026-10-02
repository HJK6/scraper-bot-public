from __future__ import annotations

import logging
import os
from pathlib import Path
import signal
import subprocess
import time

from platform_base import PlatformAdapter

logger = logging.getLogger("scraper-bot")


class MacOSAdapter(PlatformAdapter):
    def __init__(self) -> None:
        self._power_assertion_proc: subprocess.Popen | None = None

    @property
    def default_data_dir(self) -> str:
        return str(Path.home() / "Library" / "Application Support" / "scraper-bot")

    def kill_pid(self, pid: int, *, hard: bool = True) -> bool:
        try:
            os.kill(pid, signal.SIGKILL if hard else signal.SIGTERM)
            return True
        except ProcessLookupError:
            return False
        except Exception:
            raise

    def process_is_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def terminate_then_kill(self, pid: int, *, wait_seconds: float) -> bool:
        if not self.kill_pid(pid, hard=False):
            return False

        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            if not self.process_is_alive(pid):
                return True
            time.sleep(0.05)

        if self.process_is_alive(pid):
            self.kill_pid(pid, hard=True)
        return True

    def pid_command(self, pid: int) -> str:
        try:
            out = subprocess.check_output(
                ["ps", "-p", str(pid), "-o", "command="],
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).decode("utf-8", errors="ignore")
        except Exception:
            return ""
        return out.strip()

    def hold_power_assertion(self) -> None:
        """Hold a system/display sleep assertion for the server's lifetime.

        macOS idle-sleep (`pmset sleep`) suspends the whole machine, which destroys a
        headful Chrome's WindowServer surface and severs every chromedriver session
        (headless mostly survives a brief sleep; headful does not). On this host
        `pmset` reports an aggressive idle timer held off only by transient
        third-party assertions, so a lapse can let the machine sleep mid-session. We
        assert our own `caffeinate` for as long as the server runs (`-w <pid>` makes
        it self-clean if we die unexpectedly); `-d` also keeps the display awake for
        headful rendering even if a future config re-enables display idle sleep."""
        if (
            self._power_assertion_proc is not None
            and self._power_assertion_proc.poll() is None
        ):
            return
        try:
            self._power_assertion_proc = subprocess.Popen(
                ["/usr/bin/caffeinate", "-dis", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info(
                f"Power assertion held (caffeinate pid={self._power_assertion_proc.pid}); "
                "idle sleep prevented while the server runs."
            )
        except Exception as e:
            logger.warning(
                f"Could not start power assertion (caffeinate): {e}. "
                "Headful sessions may die if the machine idle-sleeps."
            )

    def release_power_assertion(self) -> None:
        if self._power_assertion_proc is not None:
            try:
                self._power_assertion_proc.terminate()
            except Exception:
                pass
            self._power_assertion_proc = None

    def headful_display_check(self) -> tuple[bool, str]:
        """Probe whether this process can put a window on the active GUI display.

        This is the single most important precondition for headful (headless=False)
        sessions to be *visible on screen*. A scraper-bot launchd agent only has a
        live WindowServer connection when its process was spawned inside the active
        console GUI (Aqua) session. If it was respawned at boot before login, or
        bootstrapped over SSH (`launchctl load`/`bootstrap` from an ssh session),
        it lands in a security session with NO WindowServer — headful Chrome then
        renders OFF-SCREEN (CDP screenshots still work, so the breakage is silent).

        We detect this via CoreGraphics' CGSessionCopyCurrentDictionary(), which
        returns the session dict with kCGSSessionOnConsoleKey=True only when the
        caller is attached to the on-console GUI session, and None otherwise.

        Returns (has_access, detail). detail is a short human-readable reason.
        Never raises — degrades to (True, "probe-unavailable") if Quartz is missing,
        so a missing dependency can't block session creation.
        """
        try:
            import Quartz  # PyObjC; present in the global venv
        except Exception as e:  # pragma: no cover - environment dependent
            return True, f"probe-unavailable ({type(e).__name__}); cannot verify WindowServer"
        try:
            d = Quartz.CGSessionCopyCurrentDictionary()
        except Exception as e:  # pragma: no cover
            return True, f"probe-error ({type(e).__name__}); cannot verify WindowServer"
        if not d:
            return False, "no GUI session (CGSessionCopyCurrentDictionary returned None)"
        on_console = bool(d.get("kCGSSessionOnConsoleKey"))
        if not on_console:
            return False, "GUI session present but not on-console (kCGSSessionOnConsoleKey=False)"
        return True, "on-console GUI session"

    def chrome_major_version(self) -> int | None:
        # macOS Chrome already matches the latest driver, so no pin is needed.
        return None
