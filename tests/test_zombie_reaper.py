"""Regression tests for the zombie-child reaper.

Guards the 2026-07 fork-ceiling incident: server.py SIGKILLed its own
Chrome/chromedriver children (orphan reaper, create-failure cleanup,
abandoned-driver close) without ever os.waitpid()'ing them, so 2,150 <defunct>
children accumulated and crossed the server process `ulimit -u`, causing machine-wide fork
failures. `_reap_zombie_children` is the durable backstop that reaps any exited
child regardless of which path abandoned it.
"""
import os
import subprocess
import sys
import time
import unittest

import server


class ZombieReaperTests(unittest.TestCase):
    def _wait_until_reapable(self, pid: int, timeout: float = 5.0) -> None:
        """Block until `pid` has exited into <defunct> (without reaping it)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if server.psutil is not None:
                try:
                    if server.psutil.Process(pid).status() == server.psutil.STATUS_ZOMBIE:
                        return
                except Exception:
                    return  # gone/unreadable — treat as exited
            time.sleep(0.02)

    def test_reaps_exited_child(self) -> None:
        # Spawn a child that exits immediately; never wait on it -> <defunct>.
        proc = subprocess.Popen([sys.executable, "-c", ""])
        pid = proc.pid

        self._wait_until_reapable(pid)
        reaped = server._reap_zombie_children()
        self.assertGreaterEqual(reaped, 1, "expected the exited child to be reaped")

        # Already reaped: the kernel no longer has it as our child.
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)

        # We reaped the child out from under Popen; mark it handled so
        # Popen.__del__ doesn't emit a ResourceWarning re-waiting on it.
        proc.returncode = 0

    def test_reap_is_safe_with_no_dead_children(self) -> None:
        # Must never raise and must report an int, even with nothing to reap.
        result = server._reap_zombie_children()
        self.assertIsInstance(result, int)
        self.assertGreaterEqual(result, 0)

    def test_count_zombie_children_returns_nonnegative_int(self) -> None:
        count = server._count_zombie_children()
        self.assertIsInstance(count, int)
        self.assertGreaterEqual(count, 0)

    def test_count_zombie_children_handles_missing_psutil(self) -> None:
        from unittest import mock

        with mock.patch.object(server, "psutil", None):
            self.assertEqual(server._count_zombie_children(), 0)


if __name__ == "__main__":
    unittest.main()
