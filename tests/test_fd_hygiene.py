import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import HTTPException

import server


class ScraperBotFdHygieneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sessions_backup = dict(server._sessions)
        server._sessions.clear()
        self.orphan_candidates_backup = dict(server._orphan_proc_candidates)
        server._orphan_proc_candidates.clear()
        self.db_conn_backup = server._db_conn
        server._db_conn = object()
        self.deep_health_warmed_backup = server._deep_health_warmed
        server._deep_health_warmed = False

    def tearDown(self) -> None:
        server._sessions.clear()
        server._sessions.update(self.sessions_backup)
        server._orphan_proc_candidates.clear()
        server._orphan_proc_candidates.update(self.orphan_candidates_backup)
        server._db_conn = self.db_conn_backup
        server._deep_health_warmed = self.deep_health_warmed_backup

    def _active_record(self, *, pid=None, user_data_dir=None, dm=None):
        now = server.datetime.now()
        record = server.SessionRecord(
            id="sid-live",
            name=None,
            owner=None,
            labels=None,
            job_id=None,
            state=server.SessionState.ACTIVE,
            close_reason=None,
            created_at=now,
            last_request_at=now,
            last_action_at=now,
            last_heartbeat_at=None,
            last_url="",
            last_error=None,
            action_count=0,
            error_count=0,
            closed_at=None,
            lease_mode=False,
            heartbeat_ttl_seconds=0,
            user_data_dir=user_data_dir,
            pid=pid,
            hostname=server.HOSTNAME,
            dm=dm,
        )
        server._sessions[record.id] = record
        return record

    def test_create_failure_cleans_up_orphan_pid(self) -> None:
        proc = None
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = str(Path(tmpdir) / "profile")

            def failing_driver_manager(*args, **kwargs):
                nonlocal proc
                Path(kwargs["user_data_dir"]).mkdir(parents=True, exist_ok=True)
                proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                err = RuntimeError("chrome bootstrap failed")
                err.pid = proc.pid
                raise err

            chrome_pid_snapshots = {"calls": 0}

            def chrome_child_pids():
                chrome_pid_snapshots["calls"] += 1
                if chrome_pid_snapshots["calls"] == 1:
                    return {}
                return {proc.pid: "chromedriver"} if proc is not None else {}

            def kill_pid(pid, sig=None):
                if proc is not None and pid == proc.pid:
                    proc.kill()
                    return True
                return False

            with mock.patch.object(server, "_db_upsert_record"), \
                 mock.patch.object(server, "_current_fd_count", return_value=0), \
                 mock.patch.object(server, "_get_our_chrome_descendants", side_effect=chrome_child_pids), \
                 mock.patch.object(server, "_managed_session_pids", return_value=set()), \
                 mock.patch.object(server, "_extract_pid_candidates", return_value=set()), \
                 mock.patch.object(server, "_kill_pid", side_effect=kill_pid), \
                 mock.patch.object(server, "DriverManager", side_effect=failing_driver_manager):
                with self.assertRaises(HTTPException) as ctx:
                    server.create_session(server.CreateSessionRequest(user_data_dir=profile_dir))

            self.assertEqual(ctx.exception.status_code, 500)
            self.assertIsNotNone(proc)
            deadline = time.time() + 2.0
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                proc.terminate()
                proc.wait(timeout=5)
                self.fail("expected orphan chrome subprocess to be killed")
            self.assertFalse(Path(profile_dir).exists())

    def test_create_session_does_not_reactivate_closed_record(self) -> None:
        class FakeDriverManager:
            def __init__(self):
                self.closed = False
                self.driver = SimpleNamespace(
                    service=SimpleNamespace(process=SimpleNamespace(pid=424242)),
                    execute_cdp_cmd=lambda command, params: None,
                )

            def close(self):
                self.closed = True

        fake_dm = FakeDriverManager()

        def delayed_driver_manager(*args, **kwargs):
            for record in server._sessions.values():
                with record.lock:
                    record.state = server.SessionState.CLOSED
                    record.close_reason = server.CloseReason.CLIENT_CLOSE
                    record.closed_at = server.datetime.now()
            return fake_dm

        with mock.patch.object(server, "_db_upsert_record"), \
             mock.patch.object(server, "_current_fd_count", return_value=0), \
             mock.patch.object(server, "_get_our_chrome_descendants", return_value={}), \
             mock.patch.object(server, "DriverManager", side_effect=delayed_driver_manager):
            response = server.create_session(server.CreateSessionRequest())

        self.assertEqual(response.status_code, 409)
        payload = json.loads(response.body)
        self.assertEqual(payload["error"], "session_closed_during_create")
        self.assertTrue(fake_dm.closed)
        self.assertEqual(server._count_active(), 0)
        [record] = list(server._sessions.values())
        self.assertEqual(record.state, server.SessionState.CLOSED)
        self.assertIsNone(record.dm)

    def test_current_fd_count_returns_sane_integer(self) -> None:
        server._fd_cache_value = None
        server._fd_cache_at = 0.0
        fd_count = server._current_fd_count()
        self.assertIsInstance(fd_count, int)
        self.assertGreater(fd_count, 0)

    def test_create_session_refuses_when_fd_pressure_high(self) -> None:
        with mock.patch.object(server, "_current_fd_count", return_value=server.FD_MAX + 1):
            response = server.create_session(server.CreateSessionRequest())

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload["detail"], "scraper-bot FD pressure — refuse new sessions")
        self.assertEqual(payload["fd_count"], server.FD_MAX + 1)

    def test_close_releases_command_executor_and_process_streams(self) -> None:
        executor = mock.Mock()
        stdout = mock.Mock()
        process = SimpleNamespace(
            pid=424242,
            stdin=None,
            stdout=stdout,
            stderr=None,
            wait=mock.Mock(),
        )
        dm = SimpleNamespace(
            driver=SimpleNamespace(
                command_executor=executor,
                service=SimpleNamespace(process=process),
            ),
            close=mock.Mock(),
        )

        self.assertTrue(server._close_dm_with_timeout(dm, 1.0))

        dm.close.assert_called_once_with()
        executor.close.assert_called_once_with()
        stdout.close.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=1)

    def test_deep_health_probe_creates_and_closes_real_session_path(self) -> None:
        record = self._active_record(dm=SimpleNamespace())

        with mock.patch.object(
            server, "create_session", return_value={"session_id": record.id}
        ) as create_session, mock.patch.object(
            server, "_close_session_sync"
        ) as close_session, mock.patch.object(
            server, "_current_fd_count_uncached", side_effect=[30, 30]
        ):
            payload = server._deep_health_probe()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["session_path"], "ok")
        self.assertEqual(payload["fd_delta"], 0)
        create_session.assert_called_once()
        request = create_session.call_args.args[0]
        self.assertTrue(request.headless)
        self.assertEqual(request.labels, {"probe": "deep-health"})
        close_session.assert_called_once_with(record, server.CloseReason.CLIENT_CLOSE)

    def test_deep_health_probe_reports_session_path_failure(self) -> None:
        with mock.patch.object(
            server, "create_session", side_effect=RuntimeError("wedged")
        ), mock.patch.object(
            server, "_current_fd_count_uncached", side_effect=[30, 31]
        ):
            response = server._deep_health_probe()

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["phase"], "session_path")
        self.assertEqual(payload["fd_count"], 31)

    def test_deep_health_probe_rejects_fd_growth(self) -> None:
        record = self._active_record(dm=SimpleNamespace())
        server._deep_health_warmed = True

        with mock.patch.object(
            server, "create_session", return_value={"session_id": record.id}
        ), mock.patch.object(
            server, "_close_session_sync"
        ), mock.patch.object(
            server, "_current_fd_count_uncached", side_effect=[30, 32]
        ):
            response = server._deep_health_probe()

        self.assertEqual(response.status_code, 503)
        payload = json.loads(response.body)
        self.assertEqual(payload["phase"], "fd_leak")
        self.assertEqual(payload["fd_delta"], 2)

    def test_orphan_reaper_skips_live_browser_pid_from_driver_capabilities(self) -> None:
        chrome_cmd = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            "--disable-blink-features=AutomationControlled "
            "--remote-debugging-port=9222 --user-data-dir=/tmp/tmp-live-profile"
        )
        helper_cmd = "Google Chrome Helper --type=renderer"
        fake_dm = SimpleNamespace(
            driver=SimpleNamespace(
                capabilities={
                    "goog:processID": 200,
                    "goog:chromeOptions": {"debuggerAddress": "127.0.0.1:9222"},
                },
                service=SimpleNamespace(process=SimpleNamespace(pid=100)),
            )
        )
        self._active_record(pid=100, dm=fake_dm)
        server._orphan_proc_candidates[200] = server.datetime.now() - server.timedelta(seconds=120)
        server._orphan_proc_candidates[201] = server.datetime.now() - server.timedelta(seconds=120)

        def descendants(pid):
            return {201} if pid == 200 else set()

        with mock.patch.object(server, "psutil", None), \
             mock.patch.object(server, "_db_load_active_user_data_dirs", return_value=set()), \
             mock.patch.object(server, "_iter_machine_process_commands", return_value=[
                 (100, 1, "undetected_chromedriver"),
                 (200, 1, chrome_cmd),
                 (201, 200, helper_cmd),
             ]), \
             mock.patch.object(server, "_get_our_chrome_descendants", return_value={
                 100: "undetected_chromedriver",
                 200: chrome_cmd,
                 201: helper_cmd,
             }), \
             mock.patch.object(server, "_descendant_pids_via_pgrep", side_effect=descendants), \
             mock.patch.object(server, "_kill_pid") as kill_pid:
            server._run_orphan_process_reaper_pass()

        kill_pid.assert_not_called()

    def test_orphan_reaper_skips_live_browser_by_session_user_data_dir(self) -> None:
        profile_dir = "/tmp/tmp-live-profile"
        chrome_cmd = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            f"--disable-blink-features=AutomationControlled --user-data-dir={profile_dir}"
        )
        self._active_record(pid=100, user_data_dir=profile_dir)
        server._orphan_proc_candidates[200] = server.datetime.now() - server.timedelta(seconds=120)

        with mock.patch.object(server, "psutil", None), \
             mock.patch.object(server, "_db_load_active_user_data_dirs", return_value=set()), \
             mock.patch.object(server, "_iter_machine_process_commands", return_value=[]), \
             mock.patch.object(server, "_get_our_chrome_descendants", return_value={200: chrome_cmd}), \
             mock.patch.object(server, "_descendant_pids_via_pgrep", return_value=set()), \
             mock.patch.object(server, "_kill_pid") as kill_pid:
            server._run_orphan_process_reaper_pass()

        kill_pid.assert_not_called()

    def test_orphan_reaper_still_kills_unmanaged_chrome_after_grace(self) -> None:
        chrome_cmd = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome "
            "--disable-blink-features=AutomationControlled --user-data-dir=/tmp/tmp-dead-profile"
        )
        server._orphan_proc_candidates[300] = server.datetime.now() - server.timedelta(seconds=120)

        with mock.patch.object(server, "psutil", None), \
             mock.patch.object(server, "_db_load_active_user_data_dirs", return_value=set()), \
             mock.patch.object(server, "_iter_machine_process_commands", return_value=[]), \
             mock.patch.object(server, "_get_our_chrome_descendants", return_value={300: chrome_cmd}), \
             mock.patch.object(server, "_descendant_pids_via_pgrep", return_value=set()), \
             mock.patch.object(server, "_kill_pid", return_value=True) as kill_pid:
            server._run_orphan_process_reaper_pass()

        kill_pid.assert_called_once_with(300)


if __name__ == "__main__":
    unittest.main()
