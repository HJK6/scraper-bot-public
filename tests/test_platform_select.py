import sys
from pathlib import Path
import pytest
import platform_base
from platform_macos import MacOSAdapter
from platform_windows import WindowsAdapter
from platform_linux import LinuxAdapter

@pytest.mark.parametrize('platform,expected', [('win32',WindowsAdapter),('darwin',MacOSAdapter),('linux',LinuxAdapter)])
def test_select(monkeypatch, platform, expected):
    monkeypatch.setattr(sys,'platform',platform)
    assert isinstance(platform_base.get_adapter(),expected)


def test_storage_defaults(monkeypatch):
    monkeypatch.setenv('XDG_DATA_HOME','/tmp/user-data')
    assert LinuxAdapter().default_data_dir == '/tmp/user-data/scraper-bot'
    assert MacOSAdapter().default_data_dir == str(Path.home() / 'Library/Application Support/scraper-bot')
    assert WindowsAdapter().default_data_dir == r'C:\scraper-bot'


def test_linux_no_display(monkeypatch):
    monkeypatch.delenv('DISPLAY',raising=False)
    monkeypatch.delenv('WAYLAND_DISPLAY',raising=False)
    assert LinuxAdapter().headful_display_check()[0] is False
    monkeypatch.setenv('DISPLAY',':0')
    assert LinuxAdapter().headful_display_check()[0] is True


def test_unsupported_platform(monkeypatch):
    monkeypatch.setattr(sys,'platform','unknown')
    with pytest.raises(RuntimeError):
        platform_base.get_adapter()
