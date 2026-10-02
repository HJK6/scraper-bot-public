from __future__ import annotations

import sys


class PlatformAdapter:
    @property
    def default_data_dir(self) -> str:
        raise NotImplementedError

    def kill_pid(self, pid: int, *, hard: bool = True) -> bool:
        raise NotImplementedError

    def terminate_then_kill(self, pid: int, *, wait_seconds: float) -> bool:
        raise NotImplementedError

    def process_is_alive(self, pid: int) -> bool:
        raise NotImplementedError

    def pid_command(self, pid: int) -> str:
        raise NotImplementedError

    def hold_power_assertion(self) -> None:
        raise NotImplementedError

    def release_power_assertion(self) -> None:
        raise NotImplementedError

    def headful_display_check(self) -> tuple[bool, str]:
        raise NotImplementedError

    def chrome_major_version(self) -> int | None:
        return None


def get_adapter() -> PlatformAdapter:
    if sys.platform == "win32":
        from platform_windows import WindowsAdapter

        return WindowsAdapter()

    if sys.platform == "darwin":
        from platform_macos import MacOSAdapter
        return MacOSAdapter()
    if sys.platform.startswith("linux"):
        from platform_linux import LinuxAdapter
        return LinuxAdapter()
    raise RuntimeError(f"Unsupported platform: {sys.platform}")
