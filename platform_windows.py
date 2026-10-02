from __future__ import annotations

import ctypes
import logging
import os
import re
import subprocess

from platform_base import PlatformAdapter

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is expected in production
    psutil = None

logger = logging.getLogger("scraper-bot")

_CHROME_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")


class WindowsAdapter(PlatformAdapter):
    def __init__(self) -> None:
        self._power_assertion_held = False

    @property
    def default_data_dir(self) -> str:
        return r"C:\scraper-bot"

    def kill_pid(self, pid: int, *, hard: bool = True) -> bool:
        try:
            p = psutil.Process(pid)
            if hard:
                p.kill()
            else:
                p.terminate()
            return True
        except (psutil.NoSuchProcess, ProcessLookupError):
            return False
        except psutil.AccessDenied:
            subprocess.check_call(
                ["taskkill", "/F", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True

    def process_is_alive(self, pid: int) -> bool:
        return psutil.pid_exists(pid)

    def terminate_then_kill(self, pid: int, *, wait_seconds: float) -> bool:
        try:
            p = psutil.Process(pid)
            p.terminate()
            try:
                p.wait(timeout=wait_seconds)
            except psutil.TimeoutExpired:
                p.kill()
            return True
        except psutil.NoSuchProcess:
            return False

    def pid_command(self, pid: int) -> str:
        try:
            p = psutil.Process(pid)
            cmd = " ".join(p.cmdline()).strip()
            return cmd or p.name()
        except Exception:
            return ""

    def hold_power_assertion(self) -> None:
        if self._power_assertion_held:
            return
        try:
            # SetThreadExecutionState signals failure by returning 0 (NULL), not by
            # raising — so a bare call would mark the assertion held even on failure.
            result = ctypes.windll.kernel32.SetThreadExecutionState(
                0x80000000 | 0x00000001 | 0x00000002
            )
            if not result:
                logger.warning(
                    "Could not start power assertion (SetThreadExecutionState returned 0). "
                    "Headful sessions may die if the machine idle-sleeps."
                )
                return
            self._power_assertion_held = True
            logger.info(
                "Power assertion held (SetThreadExecutionState); idle sleep prevented while the server runs."
            )
        except Exception as e:
            logger.warning(
                f"Could not start power assertion (SetThreadExecutionState): {e}. "
                "Headful sessions may die if the machine idle-sleeps."
            )

    def release_power_assertion(self) -> None:
        if not self._power_assertion_held:
            return
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        except Exception:
            pass
        self._power_assertion_held = False

    def headful_display_check(self) -> tuple[bool, str]:
        return True, "windows desktop session"

    def chrome_major_version(self) -> int | None:
        try:
            majors: list[int] = []
            bases = [
                r"C:\Program Files\Google\Chrome\Application",
                r"C:\Program Files (x86)\Google\Chrome\Application",
            ]
            local_appdata = os.environ.get("LOCALAPPDATA")
            if local_appdata:
                bases.append(os.path.join(local_appdata, r"Google\Chrome\Application"))

            for base in bases:
                try:
                    if not os.path.isdir(base):
                        continue
                    for name in os.listdir(base):
                        if _CHROME_VERSION_RE.match(name):
                            majors.append(int(name.split(".")[0]))
                except Exception:
                    continue

            if majors:
                return max(majors)

            try:
                import winreg
            except Exception:
                return None

            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, r"Software\Google\Chrome\BLBeacon") as key:
                        version, _ = winreg.QueryValueEx(key, "version")
                    if isinstance(version, str) and _CHROME_VERSION_RE.match(version):
                        return int(version.split(".")[0])
                except Exception:
                    continue
            return None
        except Exception:
            return None
