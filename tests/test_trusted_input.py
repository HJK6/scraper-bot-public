"""Browser-backed acceptance tests for the input primitives.

Launches an isolated headless undetected_chromedriver against a local file://
fixture that reproduces the two confirmed production defects AND the live
PropertyRadar combo failure modes, so a primitive that takes a shortcut cannot
pass here:

  * a react-select-style filter whose value-tracker ignores programmatic value
    sets but reacts to real keystrokes (PropStream);
  * an ExtJS-style State combo whose suggestion list renders ONLY after a trusted
    TYPE + async delay (never on a trigger click), whose items are id-less, whose
    committed selection lives in a separate chips container (so a document-scoped
    verify would false-pass), and whose input id changes after each selection
    (PropertyRadar); plus
  * a "server is in process of restart" overlay that intercepts clicks (PropStream).

The fixture + driver are private to this test — they never touch the running
production server or any live vendor session. The module skips cleanly when Chrome
cannot be launched, so it never breaks the unit suite.
"""
import pathlib

import pytest

import trusted_input as ti
from trusted_input import TrustedInputError

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "trusted_input_fixture.html"
FIXTURE_URL = FIXTURE.as_uri()

# Comfortably exceeds the fixture's RENDER_DELAY_MS so the async list has rendered.
SETTLE_MS = 500


@pytest.fixture(scope="module")
def driver():
    try:
        import undetected_chromedriver as uc
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"undetected_chromedriver unavailable: {exc}")
    import tempfile
    opts = uc.ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=1280,1024")
    prof = tempfile.mkdtemp(prefix="ti-test-")
    try:
        d = uc.Chrome(options=opts, user_data_dir=prof, headless=True, use_subprocess=True)
    except Exception as exc:  # pragma: no cover
        pytest.skip(f"could not launch Chrome: {exc}")
    try:
        yield d
    finally:
        try:
            d.quit()
        except Exception:
            pass


@pytest.fixture(autouse=True)
def _load_fixture(driver):
    driver.get(FIXTURE_URL)
    driver.execute_script("return window.__fixture ? window.__fixture.reset() : null;")


# ==========================================================================
# 1. HEADLINE: trusted typing is real (isTrusted=true) and drives frameworks.
# ==========================================================================

def test_typing_events_are_trusted_from_inside_the_page(driver):
    res = ti.prove_trusted(driver, css="#rsInput", text="a")
    assert res["keydown_trusted"] is True, res
    assert res["input_trusted"] is True, res


def test_react_select_ignores_js_set_but_reacts_to_trusted_keystrokes(driver):
    driver.execute_script("window.__rs.jsSet('Aus');")
    assert driver.execute_script("return window.__rs.optionCount();") == 0
    driver.execute_script("window.__rs.reset();")
    ti.type_text(driver, text="Aus", css="#rsInput",
                 expect={"kind": "js", "js": "return window.__rs.optionCount()>0;"})
    assert driver.execute_script("return window.__rs.optionCount();") == 1
    ti.click(driver, by="text", text="Austin", scope_css="#rsMenu", exact=True,
             expect={"kind": "js", "js": "return window.__rs.chosen()==='geo: Austin';"})
    assert driver.execute_script("return window.__rs.chosen();") == "geo: Austin"


def test_type_reports_not_editable(driver):
    with pytest.raises(TrustedInputError) as ei:
        ti.type_text(driver, text="x", css="#applyBtn")
    assert ei.value.reason == "not_editable" and ei.value.primitive == "trusted.type"


def test_send_keys_mode_also_filters(driver):
    # The proven live typing method (Selenium send_keys) is also trusted.
    ti.type_text(driver, text="Chi", css="#rsInput", mode="send_keys",
                 expect={"kind": "js", "js": "return window.__rs.optionCount()>0;"})
    assert driver.execute_script("return window.__rs.optionCount();") == 1  # Chicago


def test_disappear_expect_is_false_while_target_is_visible(driver):
    # Regression: the JavaScript predicate already returns False for a visible
    # element. Wrapping it in ``not bool(...)`` inverted the contract and made
    # a successfully closed live modal report ``effect_not_observed``.
    assert ti._eval_expect(driver, {"kind": "disappear", "css": "#applyBtn"}) is False


@pytest.mark.parametrize(
    "make_gone",
    [
        "document.querySelector('#applyBtn').remove();",
        "document.querySelector('#applyBtn').style.display='none';",
    ],
    ids=["absent", "hidden"],
)
def test_disappear_expect_is_true_when_target_is_absent_or_hidden(driver, make_gone):
    driver.execute_script(make_gone)
    assert ti._eval_expect(driver, {"kind": "disappear", "css": "#applyBtn"}) is True


# ==========================================================================
# 2. The id-less ExtJS combo, driven by the LIVE recipe via select_option.
# ==========================================================================

def _select_state(driver, name):
    return ti.select_option(
        driver, input_css=".state-input", item_text=name, item_scope_css="#stateList",
        item_tag="li", settle_ms=SETTLE_MS, mask_css="#stateMask",
        verify_scope_css="div.fr-criteria-editor:not(.x-hide-display)",
        verify_value_css="input[readonly]")


def test_select_option_selects_idless_state_and_verifies_readback(driver):
    out = _select_state(driver, "Florida")
    assert out["ok"] and out["selected"] == "Florida" and out["verified"] is True
    assert driver.execute_script("return window.__fixture.selectedStates();") == ["Florida"]
    assert driver.execute_script("return window.__fixture.chipsText();") == "×"
    assert driver.execute_script("return window.__fixture.chipsValues();") == ["Florida"]
    assert driver.execute_script("return document.querySelector('.state-input').readOnly;") is False


def test_committed_value_readback_excludes_editable_combo_and_external_picker(driver):
    # The outer scope contains the editable combo and picker, but only the matching
    # readonly vCsvItem descendants supply the baseline/proof: [] -> [Florida].
    driver.execute_script(
        "var picker=document.createElement('div');picker.className='x-boundlist floating-picker';"
        "var stale=document.createElement('input');stale.readOnly=true;stale.value='Florida';"
        "picker.appendChild(stale);document.body.appendChild(picker);")
    ti.type_text(driver, text="Florida", css=".state-input", mode="send_keys", verify=False)
    before = ti._scope_committed_values(
        driver, "div.fr-criteria-editor:not(.x-hide-display)", "input[readonly]", "test.readback")
    assert before == []
    assert driver.execute_script("return document.querySelector('.state-input').value;") == "Florida"
    assert driver.execute_script("return document.querySelector('.state-input').readOnly;") is False
    assert driver.execute_script("return document.querySelector('.state-input').getAttribute('role');") == "combobox"
    driver.execute_script("return window.__fixture.reset();")
    assert _select_state(driver, "Florida")["verified"] is True
    assert driver.execute_script("return window.__fixture.chipsValues();") == ["Florida"]


def test_committed_value_scope_must_be_unique(driver):
    driver.execute_script(
        "var d=document.createElement('div');d.className='fr-criteria-editor';document.body.appendChild(d);")
    with pytest.raises(TrustedInputError) as ei:
        ti._scope_committed_values(
            driver, "div.fr-criteria-editor:not(.x-hide-display)", "input[readonly]", "test.readback")
    assert ei.value.reason == "bad_request"
    assert ei.value.extra["scope_count"] == 2


def test_committed_value_readback_rejects_ambiguous_matching_values(driver):
    driver.execute_script(
        "var s=document.createElement('span');s.className='vCsvItem';"
        "var a=document.createElement('input');a.readOnly=true;a.value='Florida';"
        "var b=document.createElement('input');b.readOnly=true;b.value='Florida';"
        "s.appendChild(a);s.appendChild(b);document.querySelector('#stateChips').appendChild(s);")
    with pytest.raises(TrustedInputError) as ei:
        ti._verify_readback(
            driver, "div.fr-criteria-editor:not(.x-hide-display)", "Florida", [], 100,
            "test.readback", verify_value_css="input[readonly]")
    assert ei.value.reason == "effect_not_observed"
    assert ei.value.extra["matching_values"] == ["Florida", "Florida"]


def test_select_option_requires_committed_selection_verify_scope(driver):
    with pytest.raises(TrustedInputError) as ei:
        ti.select_option(driver, input_css=".state-input", item_text="Florida",
                         item_scope_css="#stateList", item_tag="li")
    assert ei.value.reason == "bad_request"
    assert "verify_scope_css" in str(ei.value)


def test_select_option_index_requires_explicit_verify_text(driver):
    with pytest.raises(TrustedInputError) as ei:
        ti.select_option(driver, input_css=".state-input", item_index=0, type_value="Flor",
                         item_scope_css="#stateList", item_tag="li",
                         verify_scope_css="#stateChips")
    assert ei.value.reason == "bad_request"
    assert "verify_text" in str(ei.value)


def test_select_option_multi_select_survives_input_id_change(driver):
    wanted = ["Alaska", "California", "Florida", "Iowa"]
    first_id = driver.execute_script("return window.__fixture.inputId();")
    for name in wanted:
        _select_state(driver, name)
    assert driver.execute_script("return window.__fixture.selectedStates();") == wanted
    # the combo input's id changed after selections; the stable-class locator still worked
    assert driver.execute_script("return window.__fixture.inputId();") != first_id


def test_trigger_alone_renders_nothing_so_trigger_path_fails_loudly(driver):
    # The live finding: the trigger opens only the id-less container; the store-query
    # runs on TYPE. A trigger-only select therefore finds no items and fails LOUD.
    with pytest.raises(TrustedInputError) as ei:
        ti.select_option(driver, trigger_css="#stateTrigger", open_via_trigger=True,
                         item_text="Florida", item_scope_css="#stateList", item_tag="li",
                         open_timeout_ms=1200, verify_scope_css="#stateChips")
    assert ei.value.reason == "element_not_found"


def test_readback_scope_matters_document_would_false_pass(driver):
    # Prove the false-pass a document-scoped verify would suffer: after typing, the
    # option text is present in the page (dropdown) but NOT in the committed chips.
    ti.type_text(driver, text="Florida", css=".state-input", mode="send_keys", verify=False)
    ti.wait_for(driver, by="text", text="Florida", scope_css="#stateList", tag="li", timeout_ms=3000)
    body = driver.execute_script("return document.body.textContent;")
    assert "Florida" in body                                    # a document-scoped verify would PASS
    assert "Florida" not in driver.execute_script("return window.__fixture.chipsText();")  # nothing committed


def test_select_option_loud_when_item_never_renders(driver):
    with pytest.raises(TrustedInputError) as ei:
        ti.select_option(driver, input_css=".state-input", type_value="Zzz", item_text="Zzzland",
                         item_scope_css="#stateList", item_tag="li", settle_ms=SETTLE_MS,
                         open_timeout_ms=1500, verify_scope_css="#stateChips")
    assert ei.value.reason == "element_not_found"


def test_select_option_via_react_select_widget(driver):
    # select_option also drives a react-select: type -> option renders -> pick -> read back.
    out = ti.select_option(driver, input_css="#rsInput", type_value="Aus", item_text="Austin",
                           item_scope_css="#rsMenu", item_tag="li", verify_scope_css="#rsChosen")
    assert out["ok"] and out["verified"] is True
    assert driver.execute_script("return window.__rs.chosen();") == "geo: Austin"


# ==========================================================================
# 3. OVERLAY / interception DETECTION (attribute to backend, don't click blind).
# ==========================================================================

def test_guard_refuses_to_click_through_restart_overlay(driver):
    driver.execute_script("return window.__fixture.showOverlay();")
    with pytest.raises(TrustedInputError) as ei:
        ti.click(driver, by="css", css="#applyBtn")
    err = ei.value
    assert err.reason == "covered_by_overlay"
    assert err.extra.get("cover") and err.extra["cover"].get("id") == "restartOverlay"


def test_overlay_check_detects_then_clears_then_click_succeeds(driver):
    driver.execute_script("return window.__fixture.showOverlay();")
    covered = ti.overlay_check(driver, css="#applyBtn")
    assert covered["covered"] is True and covered["cover"]["id"] == "restartOverlay"
    driver.execute_script("return window.__fixture.hideOverlay();")
    assert ti.overlay_check(driver, css="#applyBtn")["covered"] is False
    res = ti.click(driver, by="css", css="#applyBtn",
                   expect={"kind": "text", "scope": "#applyStatus", "text": "applied"})
    assert res["verified"] is True
    assert driver.execute_script("return window.__applyClick.isTrusted;") is True


def test_typing_refuses_when_field_is_covered(driver):
    driver.execute_script("return window.__fixture.showOverlay();")
    with pytest.raises(TrustedInputError) as ei:
        ti.type_text(driver, text="x", css="#rsInput")
    assert ei.value.reason == "covered_by_overlay"


def test_click_missing_element_errors_not_silent(driver):
    with pytest.raises(TrustedInputError) as ei:
        ti.click(driver, by="css", css="#does-not-exist")
    assert ei.value.reason == "element_not_found"


# ==========================================================================
# 4. VIRTUALIZED-list timing: wait-for-item is loud on timeout.
# ==========================================================================

def test_wait_for_times_out_loudly(driver):
    with pytest.raises(TrustedInputError) as ei:
        ti.wait_for(driver, by="text", text="Nonexistentville", scope_css="#stateList",
                    tag="li", timeout_ms=400)
    assert ei.value.reason == "element_not_found" and ei.value.primitive == "trusted.wait_for"


# ==========================================================================
# 5. Raw-coordinate click (CDP) reports what it actually hit.
# ==========================================================================

def test_point_click_reports_hit_target(driver):
    rect = driver.execute_script(
        "var r=document.getElementById('applyBtn').getBoundingClientRect();"
        "return {x:r.left+r.width/2, y:r.top+r.height/2};")
    res = ti.click(driver, by="point", x=rect["x"], y=rect["y"])
    assert res["ok"] and res["method"] == "cdp_point" and res["hit"]["tag"] == "BUTTON"
    assert driver.execute_script("return window.__applyClick.isTrusted;") is True
