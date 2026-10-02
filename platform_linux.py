"""Linux and WSL process/display adapter."""
from __future__ import annotations
import os
from pathlib import Path
import re
import shutil
import subprocess
from platform_macos import MacOSAdapter


class LinuxAdapter(MacOSAdapter):
    @property
    def default_data_dir(self) -> str:
        return str(Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "scraper-bot")

    def hold_power_assertion(self) -> None:
        pass

    def release_power_assertion(self) -> None:
        pass

    def headful_display_check(self) -> tuple[bool, str]:
        available = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
        return available, "desktop display configured" if available else "no display; use headless=True or enable WSLg"

    def chrome_major_version(self) -> int | None:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
            executable = shutil.which(name)
            if executable:
                try:
                    result = subprocess.check_output([executable, "--version"], timeout=5, text=True)
                    match = re.search(r"\b(\d+)\.\d+", result)
                    if match:
                        return int(match.group(1))
                except (OSError, subprocess.SubprocessError):
                    continue
        return None
