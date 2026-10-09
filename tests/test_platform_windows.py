import sys
import types
from unittest import mock

import pytest

import platform_windows
from platform_windows import WindowsAdapter

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only platform adapter tests")


def test_headful_display_check():
    assert WindowsAdapter().headful_display_check() == (True, "windows desktop session")


def test_chrome_major_version_reads_version_folder(monkeypatch):
    version_dir = r"C:\Program Files\Google\Chrome\Application"

    def isdir(path):
        return path == version_dir

    def listdir(path):
        assert path == version_dir
        return ["149.0.7827.201", "chrome.exe", "SetupMetrics"]

    monkeypatch.setattr(platform_windows.os.path, "isdir", isdir)
    monkeypatch.setattr(platform_windows.os, "listdir", listdir)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert WindowsAdapter().chrome_major_version() == 149


def test_chrome_major_version_uses_max_version_folder(monkeypatch):
    versions = {
        r"C:\Program Files\Google\Chrome\Application": ["148.0.1.1"],
        r"C:\Program Files (x86)\Google\Chrome\Application": ["149.0.7827.201"],
        r"C:\synthetic-profile\AppData\Local\Google\Chrome\Application": ["147.0.1.1", "150.0.2.2"],
    }

    monkeypatch.setenv("LOCALAPPDATA", r"C:\synthetic-profile\AppData\Local")
    monkeypatch.setattr(platform_windows.os.path, "isdir", lambda path: path in versions)
    monkeypatch.setattr(platform_windows.os, "listdir", lambda path: versions[path])

    assert WindowsAdapter().chrome_major_version() == 150


def test_chrome_major_version_uses_registry_fallback(monkeypatch):
    class FakeKey:
        def __init__(self, hive):
            self.hive = hive

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    fake_winreg = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        HKEY_LOCAL_MACHINE=object(),
    )

    def open_key(hive, path):
        assert path == r"Software\Google\Chrome\BLBeacon"
        if hive is fake_winreg.HKEY_CURRENT_USER:
            return FakeKey(hive)
        raise FileNotFoundError

    def query_value_ex(key, name):
        assert name == "version"
        return "149.0.7827.201", None

    fake_winreg.OpenKey = open_key
    fake_winreg.QueryValueEx = query_value_ex

    monkeypatch.setattr(platform_windows.os.path, "isdir", lambda path: False)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert WindowsAdapter().chrome_major_version() == 149


def test_chrome_major_version_returns_none_when_absent(monkeypatch):
    fake_winreg = types.SimpleNamespace(
        HKEY_CURRENT_USER=object(),
        HKEY_LOCAL_MACHINE=object(),
        OpenKey=mock.Mock(side_effect=FileNotFoundError),
    )

    monkeypatch.setattr(platform_windows.os.path, "isdir", lambda path: False)
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert WindowsAdapter().chrome_major_version() is None


def test_kill_pid_uses_psutil_kill_for_hard():
    proc = mock.Mock()
    with mock.patch.object(platform_windows.psutil, "Process", return_value=proc):
        assert WindowsAdapter().kill_pid(123, hard=True) is True
    proc.kill.assert_called_once_with()
    proc.terminate.assert_not_called()


def test_kill_pid_uses_psutil_terminate_for_graceful():
    proc = mock.Mock()
    with mock.patch.object(platform_windows.psutil, "Process", return_value=proc):
        assert WindowsAdapter().kill_pid(123, hard=False) is True
    proc.terminate.assert_called_once_with()
    proc.kill.assert_not_called()


def test_kill_pid_returns_false_for_missing_process():
    with mock.patch.object(
        platform_windows.psutil,
        "Process",
        side_effect=platform_windows.psutil.NoSuchProcess(pid=123),
    ):
        assert WindowsAdapter().kill_pid(123) is False


def test_process_is_alive_uses_pid_exists():
    with mock.patch.object(platform_windows.psutil, "pid_exists", return_value=True) as pid_exists:
        assert WindowsAdapter().process_is_alive(123) is True
    pid_exists.assert_called_once_with(123)


def test_pid_command_joins_cmdline_or_name():
    proc = mock.Mock()
    proc.cmdline.return_value = ["chrome.exe", "--headless"]
    proc.name.return_value = "chrome.exe"
    with mock.patch.object(platform_windows.psutil, "Process", return_value=proc):
        assert WindowsAdapter().pid_command(123) == "chrome.exe --headless"

    proc.cmdline.return_value = []
    with mock.patch.object(platform_windows.psutil, "Process", return_value=proc):
        assert WindowsAdapter().pid_command(123) == "chrome.exe"


def test_pid_command_returns_empty_on_failure():
    with mock.patch.object(
        platform_windows.psutil,
        "Process",
        side_effect=platform_windows.psutil.NoSuchProcess(pid=123),
    ):
        assert WindowsAdapter().pid_command(123) == ""


def test_terminate_then_kill_returns_false_for_missing_process():
    with mock.patch.object(
        platform_windows.psutil,
        "Process",
        side_effect=platform_windows.psutil.NoSuchProcess(pid=123),
    ):
        assert WindowsAdapter().terminate_then_kill(123, wait_seconds=0.1) is False


def test_terminate_then_kill_kills_after_timeout():
    proc = mock.Mock()
    proc.wait.side_effect = platform_windows.psutil.TimeoutExpired(seconds=0.1, pid=123)
    with mock.patch.object(platform_windows.psutil, "Process", return_value=proc):
        assert WindowsAdapter().terminate_then_kill(123, wait_seconds=0.1) is True
    proc.terminate.assert_called_once_with()
    proc.kill.assert_called_once_with()
