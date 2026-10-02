import os
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

import client
import server


def test_client_upload_sends_multipart_file_and_selector(tmp_path):
    csv_path = tmp_path / "lead-upload.csv"
    csv_path.write_text("name\nAda\n", newline="\n")

    bot = client.ScraperBot(base_url="http://scraper-bot.test")
    response = mock.Mock(ok=True)
    response.json.return_value = {
        "ok": True,
        "filename": "lead-upload.csv",
        "selector": "input[type=file]",
    }

    captured = {}

    def fake_post(url, *, data, files, timeout):
        captured["url"] = url
        captured["data"] = data
        captured["timeout"] = timeout
        captured["filename"], file_obj = files["file"]
        captured["file_body"] = file_obj.read()
        captured["file_closed_during_request"] = file_obj.closed
        return response

    with mock.patch.object(bot._session, "post", side_effect=fake_post):
        result = bot.upload("sid-1", str(csv_path), css="input[type=file]")

    assert result["ok"] is True
    assert captured["url"] == "http://scraper-bot.test/sessions/sid-1/upload"
    assert captured["data"] == {
        "selector": "input[type=file]",
        "selector_type": "css",
    }
    assert captured["filename"] == "lead-upload.csv"
    assert captured["file_body"] == b"name\nAda\n"
    assert captured["file_closed_during_request"] is False
    assert captured["timeout"] == (5, 60)


def test_upload_endpoint_sends_temp_path_and_persists_for_async_read(tmp_path):
    send_keys_paths = []

    class FakeElement:
        def send_keys(self, path):
            assert Path(path).exists()
            send_keys_paths.append(path)

    now = datetime.now()
    record = server.SessionRecord(
        id="sid-1",
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
        dm=SimpleNamespace(driver=SimpleNamespace()),
    )

    sessions_backup = dict(server._sessions)
    server._sessions.clear()
    server._sessions["sid-1"] = record
    try:
        with mock.patch.object(server, "_find_element", return_value=FakeElement()) as find:
            test_client = TestClient(server.app)
            response = test_client.post(
                "/sessions/sid-1/upload",
                data={"selector": "input[type=file]", "selector_type": "css"},
                files={"file": ("leads.csv", b"name\nAda\n", "text/csv")},
            )
    finally:
        server._sessions.clear()
        server._sessions.update(sessions_backup)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "filename": "leads.csv",
        "selector": "input[type=file]",
    }
    find.assert_called_once_with(record.dm, css="input[type=file]")
    assert len(send_keys_paths) == 1
    # The upload file must PERSIST past send_keys: the page's async uploader
    # (e.g. FilePond) reads/POSTs it after the call returns, so deleting it inline
    # races that read ("Error during upload"). It is kept under a dedicated uploads
    # dir (stale-reaped on later calls) and keeps its original filename.
    assert os.path.exists(send_keys_paths[0])
    assert os.path.basename(send_keys_paths[0]) == "leads.csv"
    assert "scraper-bot-uploads" in send_keys_paths[0]
    assert record.action_count == 1
    # don't litter: remove the persisted upload this test created
    import shutil
    shutil.rmtree(os.path.dirname(send_keys_paths[0]), ignore_errors=True)


def test_client_upload_requires_exactly_one_selector(tmp_path):
    file_path = tmp_path / "upload.csv"
    file_path.write_text("x\n")
    bot = client.ScraperBot()

    with mock.patch.object(bot._session, "post") as post:
        try:
            bot.upload("sid-1", str(file_path))
        except client.ScraperBotError as exc:
            assert "exactly one selector" in str(exc)
        else:
            raise AssertionError("expected selector validation failure")

    post.assert_not_called()
