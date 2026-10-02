import hashlib
import json
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

import client
import server


class DummyWebSocket:
    def close(self):
        pass


class ConnectWebSocket(DummyWebSocket):
    def __init__(self):
        self.sent = []
        self.timeout = "unset"

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self):
        return json.dumps({"id": 1, "result": {}})

    def settimeout(self, value):
        self.timeout = value


def _record(tmp_path):
    now = datetime.now()
    record = server.SessionRecord(
        id="sid-1", name="test", owner="tests", labels={}, job_id=None,
        state=server.SessionState.ACTIVE, close_reason=None, created_at=now,
        last_request_at=now, last_action_at=now, last_heartbeat_at=None,
        last_url="", last_error=None, action_count=0, error_count=0,
        closed_at=None, lease_mode=False, heartbeat_ttl_seconds=300,
        user_data_dir=None, pid=None, download_dir=str(tmp_path),
        dm=SimpleNamespace(driver=SimpleNamespace(current_url="https://app.propstream.com/export")),
    )
    record.download_tracker = server.BrowserDownloadTracker(record.id, str(tmp_path), DummyWebSocket())
    return record


def _browser_complete(record, filename="export.csv", guid="guid-1"):
    record.download_tracker._handle_message(json.dumps({"method": "Browser.downloadWillBegin", "params": {"guid": guid, "url": "https://app.propstream.com/export", "suggestedFilename": filename}}))
    record.download_tracker._handle_message(json.dumps({"method": "Browser.downloadProgress", "params": {"guid": guid, "state": "completed"}}))


def test_client_download_contract_uses_exact_session_routes():
    bot = client.ScraperBot(base_url="http://scraper-bot.test")
    with mock.patch.object(bot, "_post", return_value={"status": "armed", "action_instance_id": "export-1"}) as post:
        assert bot.begin_download("sid-1", "export-1")["status"] == "armed"
        post.assert_called_once_with("/sessions/sid-1/downloads/begin", {"action_instance_id": "export-1"})
    with mock.patch.object(bot, "_get", return_value={"events": [{"event_id": "one"}]}) as get:
        assert bot.download_events("sid-1") == [{"event_id": "one"}]
        get.assert_called_once_with("/sessions/sid-1/downloads")
    with mock.patch.object(bot, "_post", return_value={"status": "clicked"}) as post:
        bot.click("sid-1", css="#paid", action_instance_id="save-1")
        assert post.call_args.args[1]["action_instance_id"] == "save-1"


def test_browser_tracker_subscribes_to_completion_events_without_idle_timeout(tmp_path):
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = json.dumps({"webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/one"}).encode()
    ws = ConnectWebSocket()
    driver = SimpleNamespace(capabilities={"goog:chromeOptions": {"debuggerAddress": "127.0.0.1:9222"}})
    with mock.patch.object(server.urllib.request, "urlopen", return_value=response), mock.patch.object(server.websocket, "create_connection", return_value=ws):
        tracker = server.BrowserDownloadTracker.connect("sid-1", str(tmp_path), driver)
    assert ws.sent[0]["method"] == "Browser.setDownloadBehavior"
    assert ws.sent[0]["params"]["eventsEnabled"] is True
    assert ws.timeout is None
    tracker.close()


def test_server_rejects_files_without_browser_completion_event(tmp_path):
    record = _record(tmp_path)
    backup = dict(server._sessions)
    server._sessions.clear()
    server._sessions[record.id] = record
    try:
        assert server.begin_download(record.id, server.DownloadIntentRequest(action_instance_id="export-1"))["status"] == "armed"
        artifact = tmp_path / "export.csv"
        artifact.write_text("id\n1\n")
        assert server.get_download_events(record.id)["events"] == []
        _browser_complete(record)
        result = server.get_download_events(record.id)
        assert len(result["events"]) == 1
        event = result["events"][0]
        stat = artifact.stat()
        assert event["session_id"] == record.id
        assert event["action_instance_id"] == "export-1"
        assert event["path"] == str(artifact.absolute())
        assert event["source_url"] == "https://app.propstream.com/export"
        assert (event["device"], event["inode"], event["size"]) == (stat.st_dev, stat.st_ino, stat.st_size)
        assert event["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    finally:
        server._sessions.clear()
        server._sessions.update(backup)


def test_server_download_event_rejects_partial_and_symlink(tmp_path):
    record = _record(tmp_path)
    backup = dict(server._sessions)
    server._sessions.clear()
    server._sessions[record.id] = record
    try:
        server.begin_download(record.id, server.DownloadIntentRequest(action_instance_id="export-1"))
        (tmp_path / "partial.crdownload").write_text("partial")
        source = tmp_path / "source.csv"
        source.write_text("id\n1\n")
        (tmp_path / "linked.csv").symlink_to(source)
        _browser_complete(record, "partial.crdownload", "partial")
        _browser_complete(record, "linked.csv", "linked")
        assert server.get_download_events(record.id)["events"] == []
    finally:
        server._sessions.clear()
        server._sessions.update(backup)


def test_server_rejects_artifact_changed_after_browser_completion(tmp_path):
    record = _record(tmp_path)
    backup = dict(server._sessions)
    server._sessions.clear()
    server._sessions[record.id] = record
    try:
        server.begin_download(record.id, server.DownloadIntentRequest(action_instance_id="export-1"))
        artifact = tmp_path / "export.csv"
        artifact.write_text("id\n1\n")
        _browser_complete(record)
        artifact.write_text("id\n2\n")
        assert server.get_download_events(record.id)["events"] == []
    finally:
        server._sessions.clear()
        server._sessions.update(backup)


def test_server_refuses_to_arm_dead_browser_event_listener(tmp_path):
    record = _record(tmp_path)
    record.download_tracker.running = False
    backup = dict(server._sessions)
    server._sessions.clear()
    server._sessions[record.id] = record
    try:
        with __import__("pytest").raises(server.HTTPException, match="not healthy"):
            server.begin_download(record.id, server.DownloadIntentRequest(action_instance_id="export-1"))
    finally:
        server._sessions.clear()
        server._sessions.update(backup)


def test_mutating_http_and_idempotent_click_cannot_replay(tmp_path):
    bot = client.ScraperBot(base_url="http://scraper-bot.test")
    assert set(bot._session.adapters["http://"].max_retries.allowed_methods) == {"GET"}
    record = _record(tmp_path)
    backup = dict(server._sessions)
    server._sessions.clear()
    server._sessions[record.id] = record
    element = mock.Mock()
    request = server.ClickRequest(css="#paid", scroll_first=False, action_instance_id="save-1")
    try:
        with mock.patch.object(server, "_find_element", return_value=element):
            assert server.click(record.id, request)["status"] == "clicked"
            assert server.click(record.id, request)["status"] == "clicked"
            with __import__("pytest").raises(server.HTTPException, match="different click"):
                server.click(record.id, server.ClickRequest(css="#other", scroll_first=False, action_instance_id="save-1"))
        element.click.assert_called_once_with()
    finally:
        server._sessions.clear()
        server._sessions.update(backup)
