import json
from types import SimpleNamespace
from unittest import mock

import pytest
from fastapi import HTTPException

import client
import server


@pytest.fixture(autouse=True)
def isolated_sessions():
    sessions_backup = dict(server._sessions)
    server._sessions.clear()
    try:
        yield
    finally:
        server._sessions.clear()
        server._sessions.update(sessions_backup)


class FakeDriver:
    def __init__(self, pid=4242):
        self.service = SimpleNamespace(process=SimpleNamespace(pid=pid))

    def execute_cdp_cmd(self, command, params):
        assert command == "Browser.setDownloadBehavior"
        assert params["behavior"] == "allow"


class FakeManager:
    def __init__(self, *, pid=4242, trace_path=None):
        self.driver = FakeDriver(pid)
        self.trace_path = trace_path
        self.closed = False

    def close(self):
        self.closed = True


@pytest.mark.parametrize("requested,expected", [(None, True), (False, False), (True, True)])
def test_create_session_defaults_to_chromedriver(requested, expected):
    captured = {}

    def fake_driver_manager(**kwargs):
        captured.update(kwargs)
        return FakeManager()

    with mock.patch.object(server, "_db_upsert_record"), \
         mock.patch.object(server, "_current_fd_count", return_value=0), \
         mock.patch.object(server, "_get_our_chrome_descendants", return_value={}), \
         mock.patch.object(server, "resolve_headless", side_effect=lambda value: True if value is None else value), \
         mock.patch.object(server, "DriverManager", side_effect=fake_driver_manager), \
         mock.patch.object(server, "PlaywrightDriverManager") as playwright_manager:
        response = server.create_session(server.CreateSessionRequest(headless=requested))

    assert response["driver"] == "chromedriver"
    assert response["trace_path"] is None
    assert captured["headless"] is expected
    assert captured["undetected"] is True
    assert captured["user_data_dir"] is None
    playwright_manager.assert_not_called()


def test_health_exposes_driver_capabilities_without_losing_existing_fields():
    with mock.patch.object(server, "_windowserver_access", return_value=(True, "ok")), \
         mock.patch.object(server, "_current_fd_count", return_value=12), \
         mock.patch.object(server, "_playwright_available", return_value=True):
        payload = server.health()

    assert payload["status"] == "ok"
    assert payload["version"] == server.VERSION
    assert payload["active_sessions"] == 0
    assert payload["max_sessions"] == server.MAX_SESSIONS
    assert payload["fd_count"] == 12
    assert payload["drivers"]["chromedriver"] == {"default": True, "available": True}
    assert payload["drivers"]["playwright"] == {"default": False, "available": True}


def test_create_session_uses_playwright_when_requested(tmp_path):
    profile_dir = tmp_path / "runtime-profile"
    trace_dir = tmp_path / "traces"
    fake_manager = FakeManager(pid=0, trace_path=str(trace_dir / "sid.zip"))

    with mock.patch.object(server, "_db_upsert_record"), \
         mock.patch.object(server, "_current_fd_count", return_value=0), \
         mock.patch.object(server, "_get_our_chrome_descendants", return_value={}), \
         mock.patch.object(server, "_playwright_available", return_value=True), \
         mock.patch.object(server, "_prepare_playwright_profile", return_value=str(profile_dir)) as prepare, \
         mock.patch.object(server, "PlaywrightDriverManager", return_value=fake_manager) as playwright_manager, \
         mock.patch.object(server, "DriverManager") as driver_manager:
        response = server.create_session(
            server.CreateSessionRequest(
                driver="playwright",
                user_data_dir=str(tmp_path / "source-profile"),
                trace=True,
                trace_dir=str(trace_dir),
            )
        )

    assert response["driver"] == "playwright"
    assert response["trace_path"] == str(trace_dir / "sid.zip")
    prepare.assert_called_once()
    playwright_manager.assert_called_once()
    driver_manager.assert_not_called()
    [record] = list(server._sessions.values())
    assert record.driver_backend == "playwright"
    assert record.user_data_dir == str(profile_dir)


def test_reaper_keeps_active_playwright_session_with_current_url_facade():
    class FakePlaywrightManager:
        def __init__(self):
            self.driver = server.PlaywrightDriverFacade(self)

        def get_current_url(self):
            return "https://example.com/"

    now = server.datetime.now()
    record = server.SessionRecord(
        id="sid-playwright",
        name="test",
        owner="tests",
        labels={},
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
        heartbeat_ttl_seconds=300,
        user_data_dir=None,
        pid=None,
        hostname=server.HOSTNAME,
        driver_backend="playwright",
        dm=FakePlaywrightManager(),
    )
    server._sessions[record.id] = record

    server._run_reaper_pass()

    assert record.state == server.SessionState.ACTIVE
    assert record.last_error is None


def test_unsupported_driver_fails_before_session_create():
    with mock.patch.object(server, "_current_fd_count") as fd_count:
        with pytest.raises(HTTPException) as exc:
            server.create_session(server.CreateSessionRequest(driver="firefox"))

    assert exc.value.status_code == 400
    assert server._sessions == {}
    fd_count.assert_not_called()


def test_client_sends_driver_trace_fields():
    bot = client.ScraperBot(base_url="http://scraper-bot.test")
    response = mock.Mock(ok=True)
    response.json.return_value = {"session_id": "sid-1"}
    captured = {}

    def fake_request(method, url, *, json, params, headers, timeout):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = json
        captured["timeout"] = timeout
        return response

    with mock.patch.object(bot._session, "request", side_effect=fake_request):
        session_id = bot.create_session(driver="playwright", trace=True, trace_dir="/tmp/traces")

    assert session_id == "sid-1"
    assert captured["method"] == "POST"
    assert captured["url"] == "http://scraper-bot.test/sessions"
    assert captured["json"]["driver"] == "playwright"
    assert captured["json"]["trace"] is True
    assert captured["json"]["trace_dir"] == "/tmp/traces"


def test_close_during_playwright_create_closes_launched_manager(tmp_path):
    fake_manager = FakeManager(pid=0)

    def delayed_manager(**kwargs):
        for record in server._sessions.values():
            with record.lock:
                record.state = server.SessionState.CLOSED
                record.close_reason = server.CloseReason.CLIENT_CLOSE
                record.closed_at = server.datetime.now()
        return fake_manager

    with mock.patch.object(server, "_db_upsert_record"), \
         mock.patch.object(server, "_current_fd_count", return_value=0), \
         mock.patch.object(server, "_get_our_chrome_descendants", return_value={}), \
         mock.patch.object(server, "_playwright_available", return_value=True), \
         mock.patch.object(server, "_prepare_playwright_profile", return_value=str(tmp_path / "runtime-profile")), \
         mock.patch.object(server, "PlaywrightDriverManager", side_effect=delayed_manager):
        response = server.create_session(server.CreateSessionRequest(driver="playwright"))

    assert response.status_code == 409
    assert json.loads(response.body)["error"] == "session_closed_during_create"
    assert fake_manager.closed is True
