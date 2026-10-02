import signal
import sys
from unittest import mock

import pytest

from platform_macos import MacOSAdapter

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only platform adapter tests")


def test_kill_pid_selects_sigkill_for_hard():
    with mock.patch("platform_macos.os.kill") as kill:
        assert MacOSAdapter().kill_pid(123, hard=True) is True
    kill.assert_called_once_with(123, signal.SIGKILL)


def test_kill_pid_selects_sigterm_for_graceful():
    with mock.patch("platform_macos.os.kill") as kill:
        assert MacOSAdapter().kill_pid(123, hard=False) is True
    kill.assert_called_once_with(123, signal.SIGTERM)


def test_pid_command_uses_ps_command():
    output = b"/Applications/Google Chrome.app --headless\n"
    with mock.patch("platform_macos.subprocess.check_output", return_value=output) as check_output:
        assert MacOSAdapter().pid_command(123) == "/Applications/Google Chrome.app --headless"
    check_output.assert_called_once_with(
        ["ps", "-p", "123", "-o", "command="],
        stderr=mock.ANY,
        timeout=2,
    )
