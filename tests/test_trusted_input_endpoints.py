"""Endpoint-layer integration tests for the /input/* trusted-input primitives.

Drives the real FastAPI request path (request model -> _session_action ->
trusted_input -> dm.driver) against a real headless browser, with the session
lookup patched to yield a private driver so no production session is touched.
Skips cleanly when Chrome cannot be launched.
"""
import contextlib
import pathlib
import types

import pytest

import server

FIXTURE_URL = (pathlib.Path(__file__).resolve().parent / "fixtures" / "trusted_input_fixture.html").as_uri()


@pytest.fixture(scope="module")
def driver():
    # Match the supported Selenium/CDP backend without UC's unpinned driver
    # download. The real HTTP journey separately covers the configured adapter.
    import tempfile
    from selenium import webdriver
    with tempfile.TemporaryDirectory(prefix="ti-isolated-") as profile:
        options = webdriver.ChromeOptions()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--window-size=1280,1024")
        options.add_argument(f"--user-data-dir={profile}")
        try:
            browser = webdriver.Chrome(options=options)
        except Exception as error:  # pragma: no cover - external prerequisite
            pytest.skip(f"Chrome/Selenium driver unavailable: {type(error).__name__}")
        try:
            yield browser
        finally:
            browser.quit()


@pytest.fixture
def client(driver, monkeypatch):
    try:
        from fastapi.testclient import TestClient
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"TestClient unavailable: {exc}")
    driver.get(FIXTURE_URL)
    driver.execute_script("return window.__fixture.reset();")
    sess = types.SimpleNamespace(id="test", last_error=None, error_count=0,
                                 last_request_at=None, last_action_at=None, action_count=0)
    dm = types.SimpleNamespace(driver=driver)

    @contextlib.contextmanager
    def fake_action(session_id):
        yield sess, dm

    monkeypatch.setattr(server, "_session_action", fake_action)
    return TestClient(server.app), sess, driver


def test_type_endpoint_filters_react_select(client):
    tc, _sess, driver = client
    r = tc.post("/sessions/test/input/type", json={
        "text": "Aus", "css": "#rsInput",
        "expect": {"kind": "js", "js": "return window.__rs.optionCount()>0;"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["primitive"] == "trusted.type"
    assert driver.execute_script("return window.__rs.optionCount();") == 1


def test_click_text_endpoint_selects_idless_option(client):
    tc, _sess, driver = client
    tc.post("/sessions/test/input/type", json={"text": "Aus", "css": "#rsInput"})
    r = tc.post("/sessions/test/input/click_text", json={
        "by": "text", "text": "Austin", "scope_css": "#rsMenu", "exact": True,
        "expect": {"kind": "js", "js": "return window.__rs.chosen()==='geo: Austin';"}})
    assert r.status_code == 200, r.text
    assert r.json()["method"] == "native"
    assert driver.execute_script("return window.__rs.chosen();") == "geo: Austin"


def test_select_option_endpoint_end_to_end(client):
    tc, _sess, driver = client
    r = tc.post("/sessions/test/input/select_option", json={
        "input_css": ".state-input", "item_text": "Florida", "item_scope_css": "#stateList",
        "item_tag": "li", "settle_ms": 500, "mask_css": "#stateMask",
        "verify_scope_css": "div.fr-criteria-editor:not(.x-hide-display)",
        "verify_value_css": "input[readonly]"})
    assert r.status_code == 200, r.text
    assert r.json()["verified"] is True
    assert driver.execute_script("return window.__fixture.selectedStates();") == ["Florida"]
    assert driver.execute_script("return window.__fixture.chipsValues();") == ["Florida"]


def test_select_option_endpoint_requires_committed_selection_verify_scope(client):
    tc, _sess, _driver = client
    r = tc.post("/sessions/test/input/select_option", json={
        "input_css": ".state-input", "item_text": "Florida", "item_scope_css": "#stateList",
        "item_tag": "li", "settle_ms": 500, "mask_css": "#stateMask"})
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert detail["reason"] == "bad_request"
    assert "verify_scope_css" in detail["message"]


def test_type_endpoint_not_editable_maps_to_400(client):
    tc, sess, _driver = client
    r = tc.post("/sessions/test/input/type", json={"text": "x", "css": "#applyBtn"})
    assert r.status_code == 400
    assert r.json()["detail"]["reason"] == "not_editable"
    assert sess.error_count == 1


def test_element_not_found_maps_to_422_not_404(client):
    # 404 is reserved for "session not found"; a missing ELEMENT must not look
    # like a lost session to the client.
    tc, _sess, _driver = client
    r = tc.post("/sessions/test/input/click_text", json={"by": "css", "css": "#nope"})
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "element_not_found"


def test_click_endpoint_covered_maps_to_409(client):
    tc, _sess, driver = client
    driver.execute_script("return window.__fixture.showOverlay();")
    r = tc.post("/sessions/test/input/click_text", json={"by": "css", "css": "#applyBtn"})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["reason"] == "covered_by_overlay"
    assert detail["cover"]["id"] == "restartOverlay"


def test_overlay_check_endpoint(client):
    tc, _sess, driver = client
    driver.execute_script("return window.__fixture.showOverlay();")
    r = tc.post("/sessions/test/input/overlay_check", json={"css": "#applyBtn"})
    assert r.status_code == 200 and r.json()["covered"] is True


def test_wait_for_endpoint_times_out_422(client):
    tc, _sess, _driver = client
    r = tc.post("/sessions/test/input/wait_for", json={
        "by": "text", "text": "Nope", "scope_css": "#stateList", "tag": "li", "timeout_ms": 300})
    assert r.status_code == 422
    assert r.json()["detail"]["reason"] == "element_not_found"
