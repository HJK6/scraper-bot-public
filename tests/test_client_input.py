"""Fast, no-browser unit tests for the client's /input/* wrappers.

Fakes the HTTP layer to prove ScraperBotInputError carries the structured,
attributable reason on refusal, and that a success body is returned verbatim.
"""
import types

import pytest

from client import ScraperBot, ScraperBotInputError


class _FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def _bot_with_response(monkeypatch, resp):
    bot = ScraperBot("http://localhost:0")
    monkeypatch.setattr(bot._session, "post", lambda *a, **k: resp)
    return bot


def test_input_success_returns_body(monkeypatch):
    resp = _FakeResp(200, {"ok": True, "primitive": "trusted.type", "verified": True})
    bot = _bot_with_response(monkeypatch, resp)
    out = bot.trusted_type("s1", "hello", css="#q")
    assert out["ok"] is True and out["primitive"] == "trusted.type"


def test_select_option_forwards_scoped_committed_value_selector(monkeypatch):
    body = {}

    def post(_url, **kwargs):
        body.update(kwargs["json"])
        return _FakeResp(200, {"ok": True, "primitive": "trusted.select_option", "verified": True})

    bot = ScraperBot("http://localhost:0")
    monkeypatch.setattr(bot._session, "post", post)
    out = bot.select_option(
        "s1", input_css=".combo", item_text="Florida", item_scope_css=".picker",
        verify_scope_css="div.fr-criteria-editor:not(.x-hide-display)",
        verify_value_css="input[readonly]",
    )
    assert out["verified"] is True
    assert body["verify_value_css"] == "input[readonly]"


def test_covered_overlay_raises_structured_error(monkeypatch):
    resp = _FakeResp(409, {"detail": {"ok": False, "reason": "covered_by_overlay",
                                      "message": "overlay covers point", "primitive": "trusted.click_text",
                                      "cover": {"id": "restartOverlay"}}})
    bot = _bot_with_response(monkeypatch, resp)
    with pytest.raises(ScraperBotInputError) as ei:
        bot.click_text("s1", "Apply", scope_css="#main")
    err = ei.value
    assert err.reason == "covered_by_overlay"
    assert err.primitive == "trusted.click_text"
    assert err.status_code == 409
    assert err.status == 409
    assert err.detail["cover"]["id"] == "restartOverlay"


def test_element_not_found_raises_structured_error(monkeypatch):
    resp = _FakeResp(422, {"detail": {"ok": False, "reason": "element_not_found",
                                      "message": "no match", "primitive": "trusted.wait_for"}})
    bot = _bot_with_response(monkeypatch, resp)
    with pytest.raises(ScraperBotInputError) as ei:
        bot.wait_for_item("s1", css="#list li", min_count=1)
    err = ei.value
    assert err.reason == "element_not_found"
    assert err.primitive == "trusted.wait_for"
    assert err.status_code == 422
    assert err.status == 422
    assert err.detail["message"] == "no match"
