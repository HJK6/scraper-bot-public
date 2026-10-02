from types import SimpleNamespace

import server


def test_auto_chrome_major_returns_adapter_value_and_logs_once(caplog, monkeypatch):
    monkeypatch.setattr(server, "_auto_chrome_major_logged", False)
    monkeypatch.setattr(server, "_platform", SimpleNamespace(chrome_major_version=lambda: 149))

    with caplog.at_level("INFO", logger="scraper-bot"):
        assert server._auto_chrome_major() == 149
        assert server._auto_chrome_major() == 149

    records = [
        r for r in caplog.records
        if "Auto-detected installed Chrome major 149" in r.getMessage()
    ]
    assert len(records) == 1


def test_auto_chrome_major_returns_none_without_log(caplog, monkeypatch):
    monkeypatch.setattr(server, "_auto_chrome_major_logged", False)
    monkeypatch.setattr(server, "_platform", SimpleNamespace(chrome_major_version=lambda: None))

    with caplog.at_level("INFO", logger="scraper-bot"):
        assert server._auto_chrome_major() is None

    assert not [
        r for r in caplog.records
        if "Auto-detected installed Chrome major" in r.getMessage()
    ]
