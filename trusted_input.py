"""Input primitives for scraper-bot: trusted typing, id-less targeting, overlay guard.

Why this exists (evidence-based, per the PropStream/PropertyRadar failure breakdown)
------------------------------------------------------------------------------------
scraper-bot's existing ``/click`` uses Selenium ``el.click()`` which is already a
NATIVE, trusted (isTrusted=true) event — clicks were never the problem. The real,
confirmed gaps are:

1. TYPING (the headline). Synthetic value-setting (``el.value = x`` + a dispatched
   ``input`` event) does NOT drive the listeners JS frameworks rely on: React's
   value-tracker ignores programmatic sets (react-select filter never fires), and
   ExtJS combos will not render their suggestion dropdown on programmatic typing.
   Real key events via CDP ``Input.dispatchKeyEvent`` / ``Input.insertText`` reach
   the framework. This unblocks PropertyRadar's state combo (→ cycle 2).

2. TARGETING id-less elements. PropertyRadar picker items (``.x-list-plain``) have
   no ids, so a locator can only reach the list container. ``click_text`` resolves a
   specific element by visible text within a scope; ``click_point`` clicks a raw
   coordinate/bounding box. Both then click NATIVELY (trusted) — this is a targeting
   gap, not a trust gap.

3. OVERLAY / interception DETECTION before a mutating step. PropStream's "server is
   in process of restart" overlays are real backend instability. The value here is
   detecting the interception BEFORE acting (and naming the covering element) rather
   than discovering it from a mid-sequence exception. This primitive DETECTS and
   ATTRIBUTES interception; it does not and cannot fix the backend.

4. VIRTUALIZED-list timing. Option lookups aborted because a query ran before items
   rendered. ``wait_for`` / ``select_option`` poll for the item to be present first.

Honest failure attribution: every primitive records WHICH primitive was attempted
(``primitive`` field) on both success and failure, and distinguishes our-tooling
failures (element_not_found, effect_not_observed) from site/backend conditions
(covered_by_overlay) so we stop over-blaming — or wrongly crediting — either side.

Backend: CDP paths (typing, raw-coordinate click) require the chromedriver/CDP
driver (undetected_chromedriver, which exposes ``execute_cdp_cmd``); a non-CDP
backend raises ``backend_unsupported`` rather than degrading to untrusted input.
Native-click paths (click_text, element clicks) work on any Selenium driver.

CDP coordinate space: ``Input.dispatchMouseEvent`` x/y are CSS pixels relative to
the layout viewport — the same space as ``getBoundingClientRect()`` — independent
of devicePixelRatio.
"""

from __future__ import annotations

import time
from functools import wraps
import fill_diagnostics as fd
import uuid
from typing import Any, Optional

# Stable reason codes clients may branch on.
REASONS = {
    "bad_request",           # invalid/empty target or params
    "backend_unsupported",   # backend has no CDP Input support
    "element_not_found",     # locator/text matched nothing
    "element_not_visible",   # matched but zero-size / hidden
    "out_of_viewport",       # visible but not scrollable into viewport
    "covered_by_overlay",    # an overlay sits on the interaction point (backend/site condition)
    "effect_not_observed",   # action dispatched but expected post-condition never met
    "not_editable",          # typing target is not an editable field
    "list_empty",            # dropdown/list never rendered items in time
    "item_not_found",        # requested item (text/index) not present
}


class TrustedInputError(Exception):
    """Explicit, attributable failure of an input primitive."""

    def __init__(self, reason: str, message: str, primitive: Optional[str] = None, **extra: Any):
        self.reason = reason
        self.message = message
        self.primitive = primitive
        self.extra = extra
        super().__init__(f"{primitive or '?'}/{reason}: {message}")

    def to_dict(self) -> dict:
        d = {"ok": False, "reason": self.reason, "message": self.message, "primitive": self.primitive}
        d.update(self.extra)
        return d


def run_confidential_fill(fn, *args, diagnostic_primitive="trusted.type", **kwargs):
    """The common fill boundary: safe success/error metadata and no raw chain."""
    with fd.confidential_logs():
        try:
            return fd.success_detail(fn(*args, **kwargs), diagnostic_primitive)
        except TrustedInputError as error:
            detail = fd.error_detail(error.reason, error.primitive or diagnostic_primitive)
        except Exception:
            detail = fd.error_detail(primitive=diagnostic_primitive)
    # Raise outside the handler: do not retain the raw exception as __context__.
    raise TrustedInputError(detail["reason"], detail["message"], primitive=detail["primitive"]) from None


def confidential_fill(primitive):
    def decorate(fn):
        @wraps(fn)
        def invoke(*args, **kwargs):
            chosen = fd.error_detail(primitive=kwargs.get("primitive", primitive))["primitive"]
            kwargs["primitive"] = chosen
            return run_confidential_fill(fn, *args, diagnostic_primitive=chosen, **kwargs)
        return invoke
    return decorate


def _disable_fill_capture(driver):
    # Selenium adapters enable performance/Network logging at session creation.
    # Stop future automatic URL/body events BEFORE editing; never read the log.
    cdp = getattr(driver, "execute_cdp_cmd", None)
    if callable(cdp):
        cdp("Network.disable", {})


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------

def _require_cdp(driver, primitive: str):
    fn = getattr(driver, "execute_cdp_cmd", None)
    if not callable(fn):
        raise TrustedInputError(
            "backend_unsupported",
            "this primitive requires the chromedriver/CDP backend (execute_cdp_cmd); "
            f"session driver is {type(driver).__name__}",
            primitive=primitive)
    return fn


def _cdp(driver, method: str, params: dict):
    return driver.execute_cdp_cmd(method, params)


def _dispatch_mouse_click(driver, x: float, y: float, button: str = "left", click_count: int = 1):
    mask = {"left": 1, "middle": 4, "right": 2}.get(button, 1)
    _cdp(driver, "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y, "buttons": 0})
    _cdp(driver, "Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y,
                                              "button": button, "buttons": mask, "clickCount": click_count})
    _cdp(driver, "Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y,
                                              "button": button, "buttons": 0, "clickCount": click_count})


def _key_params(ch: str) -> dict:
    """Best-effort key/code/virtual-key for a single character. The `text` field is
    what makes Chrome insert the char and fire input/keypress; key/code let framework
    keydown handlers behave normally."""
    key, code, vk = ch, "", 0
    if ch.isalpha():
        code, vk = "Key" + ch.upper(), ord(ch.upper())
    elif ch.isdigit():
        code, vk = "Digit" + ch, ord(ch)
    elif ch == " ":
        code, vk = "Space", 32
    else:
        _SP = {"-": ("Minus", 189), "_": ("Minus", 189), ".": ("Period", 190),
               ",": ("Comma", 188), "/": ("Slash", 191), ":": ("Semicolon", 186),
               ";": ("Semicolon", 186), "@": ("Digit2", 50)}
        if ch in _SP:
            code, vk = _SP[ch]
    return {"key": key, "code": code, "windowsVirtualKeyCode": vk}


def _type_char(driver, ch: str):
    p = _key_params(ch)
    _cdp(driver, "Input.dispatchKeyEvent", {"type": "keyDown", "text": ch, **p})
    _cdp(driver, "Input.dispatchKeyEvent", {"type": "keyUp", **p})


def _press_key(driver, key: str, code: str, vk: int):
    _cdp(driver, "Input.dispatchKeyEvent", {"type": "keyDown", "key": key, "code": code, "windowsVirtualKeyCode": vk})
    _cdp(driver, "Input.dispatchKeyEvent", {"type": "keyUp", "key": key, "code": code, "windowsVirtualKeyCode": vk})


# ---------------------------------------------------------------------------
# In-page resolver + hit-test (one atomic JS evaluation)
# ---------------------------------------------------------------------------

_RESOLVE_JS = r"""
var spec = arguments[0];
function vis(el){
  if(!el || el.nodeType!==1) return false;
  var cs = getComputedStyle(el);
  if(cs.display==='none' || cs.visibility==='hidden' || parseFloat(cs.opacity)===0) return false;
  var r = el.getBoundingClientRect();
  if(r.width<=0 || r.height<=0) return false;
  if(el.offsetParent===null && cs.position!=='fixed' && cs.position!=='sticky') return false;
  return true;
}
function desc(el){
  if(!el) return null;
  var t=(el.textContent||'').replace(/\s+/g,' ').trim();
  return {tag:el.tagName, id:el.id||null,
          cls:(el.className && el.className.toString ? el.className.toString().slice(0,120):null),
          text:t.slice(0,80)};
}
function scopeRoot(){
  if(spec.scopeCss){ return document.querySelector(spec.scopeCss); }
  if(spec.scopeXpath){
    var r=document.evaluate(spec.scopeXpath,document,null,XPathResult.FIRST_ORDERED_NODE_TYPE,null);
    return r.singleNodeValue;
  }
  return document;
}
function collect(){
  if(spec.by==='css'){ return Array.prototype.slice.call(document.querySelectorAll(spec.css)); }
  if(spec.by==='xpath'){
    var out=[], res=document.evaluate(spec.xpath,document,null,XPathResult.ORDERED_NODE_SNAPSHOT_TYPE,null);
    for(var i=0;i<res.snapshotLength;i++) out.push(res.snapshotItem(i));
    return out;
  }
  if(spec.by==='text'){
    var root=scopeRoot(); if(!root) return null;
    var els=Array.prototype.slice.call(root.querySelectorAll(spec.tag||'*'));
    var needle=(spec.text||'').replace(/\s+/g,' ').trim().toLowerCase();
    var m=els.filter(function(el){
      var t=(el.textContent||'').replace(/\s+/g,' ').trim().toLowerCase();
      return spec.exact ? t===needle : t.indexOf(needle)!==-1;
    });
    return m.filter(function(el){ return !m.some(function(o){ return o!==el && el.contains(o); }); });
  }
  return [];
}
if(spec.by==='point'){
  var px=spec.x, py=spec.y, top=document.elementFromPoint(px,py);
  return {found:true, byPoint:true, point:{x:px,y:py},
          inViewport:(px>=0&&py>=0&&px<=innerWidth&&py<=innerHeight), hit:desc(top)};
}
if(spec.by==='text' && scopeRoot()===null){ return {found:false, reason:'scope_missing'}; }
var cands=collect() || [];
var visible=cands.filter(vis);
if(cands.length===0){ return {found:false, reason:'not_found', matched:0}; }
if(visible.length===0){ return {found:false, reason:'not_visible', matched:cands.length}; }
var idx=spec.index||0; if(idx<0) idx=0;
if(idx>=visible.length){ return {found:false, reason:'index_out_of_range', matched:visible.length}; }
var el=visible[idx];
if(spec.scroll){ try{ el.scrollIntoView({block:'center', inline:'center'}); }catch(e){} }
var r=el.getBoundingClientRect();
var cx=Math.min(Math.max(r.left+r.width/2,1), innerWidth-1);
var cy=Math.min(Math.max(r.top+r.height/2,1), innerHeight-1);
var inVp=(r.bottom>0 && r.right>0 && r.top<innerHeight && r.left<innerWidth);
if(spec.token){ el.setAttribute('data-ti-token', spec.token); }
var top=document.elementFromPoint(cx,cy);
var covered = !(top===el || el.contains(top) || (top && top.contains(el)));
return {found:true, matched:visible.length, index:idx,
        point:{x:cx,y:cy}, rect:{x:r.left,y:r.top,w:r.width,h:r.height},
        inViewport:inVp, covered:covered, cover: covered?desc(top):null,
        target:desc(el), token:spec.token};
"""

_CLEANUP_JS = r"""
var t=arguments[0];
var els=document.querySelectorAll('[data-ti-token'+(t?'="'+t+'"':'')+']');
for(var i=0;i<els.length;i++){ els[i].removeAttribute('data-ti-token'); }
return els.length;
"""


def _build_spec(by, css, xpath, text, scope_css, scope_xpath, exact, tag, x, y, index, scroll, token, primitive):
    if by == "point":
        if x is None or y is None:
            raise TrustedInputError("bad_request", "point target requires x and y", primitive=primitive)
        spec = {"by": "point", "x": x, "y": y}
    elif by == "css":
        if not css:
            raise TrustedInputError("bad_request", "css target requires a css selector", primitive=primitive)
        spec = {"by": "css", "css": css}
    elif by == "xpath":
        if not xpath:
            raise TrustedInputError("bad_request", "xpath target requires an xpath", primitive=primitive)
        spec = {"by": "xpath", "xpath": xpath}
    elif by == "text":
        if not text:
            raise TrustedInputError("bad_request", "text target requires text", primitive=primitive)
        spec = {"by": "text", "text": text, "exact": bool(exact), "tag": tag,
                "scopeCss": scope_css, "scopeXpath": scope_xpath}
    else:
        raise TrustedInputError("bad_request", f"unknown target kind {by!r}", primitive=primitive)
    spec["index"] = index
    spec["scroll"] = bool(scroll) and by != "point"
    if token:
        spec["token"] = token
    return spec


def _resolve(driver, spec: dict, primitive: str) -> dict:
    res = driver.execute_script(_RESOLVE_JS, spec)
    if not isinstance(res, dict):
        raise TrustedInputError("element_not_found", "resolver returned no result", primitive=primitive)
    if not res.get("found"):
        reason = res.get("reason")
        if reason == "not_visible":
            raise TrustedInputError("element_not_visible",
                                    f"matched {res.get('matched')} element(s) but none visible",
                                    primitive=primitive, matched=res.get("matched"))
        if reason == "index_out_of_range":
            raise TrustedInputError("item_not_found",
                                    f"index out of range; {res.get('matched')} visible match(es)",
                                    primitive=primitive, matched=res.get("matched"))
        if reason == "scope_missing":
            raise TrustedInputError("element_not_found", "text-search scope not found", primitive=primitive)
        raise TrustedInputError("element_not_found", "no element matched the target",
                                primitive=primitive, matched=res.get("matched", 0))
    return res


def _find_by_token(driver, token: str):
    from selenium.webdriver.common.by import By
    els = driver.find_elements(By.CSS_SELECTOR, f'[data-ti-token="{token}"]')
    return els[0] if els else None


def _cleanup_tokens(driver, token: Optional[str] = None):
    try:
        driver.execute_script(_CLEANUP_JS, token or "")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Post-condition verification
# ---------------------------------------------------------------------------

def _eval_expect(driver, expect: dict) -> bool:
    kind = expect.get("kind")
    if kind == "appear":
        return bool(driver.execute_script(
            "var e=document.querySelector(arguments[0]);if(!e)return false;"
            "var r=e.getBoundingClientRect();return r.width>0&&r.height>0;", expect["css"]))
    if kind == "disappear":
        return bool(driver.execute_script(
            "var e=document.querySelector(arguments[0]);if(!e)return true;"
            "var r=e.getBoundingClientRect();return !(r.width>0&&r.height>0);", expect["css"]))
    if kind == "text":
        return bool(driver.execute_script(
            "var root=arguments[0]?document.querySelector(arguments[0]):document.body;if(!root)return false;"
            "var t=(root.textContent||'').replace(/\\s+/g,' ').toLowerCase();"
            "return t.indexOf((arguments[1]||'').toLowerCase())!==-1;", expect.get("scope"), expect["text"]))
    if kind == "value":
        return bool(driver.execute_script(
            "var e=document.querySelector(arguments[0]);if(!e)return false;"
            "return (e.value||'').indexOf(arguments[1])!==-1;", expect["css"], expect["text"]))
    if kind == "js":
        return bool(driver.execute_script(expect["js"]))
    return False


def _verify(driver, expect: Optional[dict], timeout_ms: int, primitive: str):
    if not expect:
        return None
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        try:
            if _eval_expect(driver, expect):
                return True
        except Exception:
            pass
        time.sleep(0.1)
    raise TrustedInputError("effect_not_observed",
                            "action dispatched but post-condition was not observed",
                            primitive=primitive)


# ---------------------------------------------------------------------------
# click_text / click_element — native, trusted click on a RESOLVED target
# ---------------------------------------------------------------------------

def click(driver, *, by: str = "css", css: str = None, xpath: str = None, text: str = None,
          scope_css: str = None, scope_xpath: str = None, exact: bool = False, tag: str = None,
          x: float = None, y: float = None, index: int = 0, scroll: bool = True,
          allow_covered: bool = False, button: str = "left", click_count: int = 1,
          expect: dict = None, verify_timeout_ms: int = 4000, primitive: str = "trusted.click") -> dict:
    """Resolve a target (css/xpath/visible-text/point) and click it.

    Element/text targets are clicked NATIVELY via Selenium (already trusted) — this
    adds *targeting* (esp. id-less by-text) and an overlay pre-check, not a trust
    layer. ``by='point'`` clicks a raw coordinate via CDP. Refuses on missing /
    invisible / off-screen / overlay-covered targets unless allow_covered=True.
    """
    from selenium.common.exceptions import ElementClickInterceptedException, WebDriverException
    token = uuid.uuid4().hex[:12]
    spec = _build_spec(by, css, xpath, text, scope_css, scope_xpath, exact, tag, x, y, index, scroll, token, primitive)
    try:
        res = _resolve(driver, spec, primitive)

        if res.get("byPoint"):
            _require_cdp(driver, primitive)
            point = res["point"]
            if not res.get("inViewport"):
                raise TrustedInputError("out_of_viewport",
                                        f"point ({point['x']},{point['y']}) outside viewport",
                                        primitive=primitive, point=point)
            _dispatch_mouse_click(driver, point["x"], point["y"], button, click_count)
            return {"ok": True, "primitive": primitive, "method": "cdp_point", "point": point,
                    "hit": res.get("hit"), "verified": _verify(driver, expect, verify_timeout_ms, primitive)}

        if not res.get("inViewport"):
            raise TrustedInputError("out_of_viewport", "target could not be scrolled into the viewport",
                                    primitive=primitive, rect=res.get("rect"))
        if res.get("covered") and not allow_covered:
            raise TrustedInputError("covered_by_overlay",
                                    "an overlay covers the interaction point — refusing to click through it",
                                    primitive=primitive, cover=res.get("cover"),
                                    target=res.get("target"), point=res.get("point"))

        el = _find_by_token(driver, token)
        if el is None:
            raise TrustedInputError("element_not_found", "resolved element vanished before click",
                                    primitive=primitive)
        try:
            el.click()  # native Selenium click == trusted event
        except ElementClickInterceptedException as e:
            raise TrustedInputError("covered_by_overlay",
                                    "native click intercepted by another element",
                                    primitive=primitive, detail=str(e).splitlines()[0] if str(e) else None)
        except WebDriverException as e:
            raise TrustedInputError("element_not_visible",
                                    f"native click failed: {str(e).splitlines()[0] if str(e) else e}",
                                    primitive=primitive)
        return {"ok": True, "primitive": primitive, "method": "native", "point": res.get("point"),
                "matched": res.get("matched"), "target": res.get("target"),
                "verified": _verify(driver, expect, verify_timeout_ms, primitive)}
    finally:
        _cleanup_tokens(driver, token)


# ---------------------------------------------------------------------------
# type — TRUSTED keystroke / insertText typing (the headline primitive)
# ---------------------------------------------------------------------------

@confidential_fill("trusted.type")
def type_text(driver, *, text: str, css: str = None, xpath: str = None, locate_text: str = None,
              scope_css: str = None, scope_xpath: str = None, exact: bool = False, index: int = 0,
              mode: str = "keystroke", focus: bool = True, require_focus: bool = False,
              clear_first: bool = False, press_enter: bool = False, expect: dict = None,
              verify: bool = True, verify_timeout_ms: int = 4000, per_char_delay_ms: int = 0,
              primitive: str = "trusted.type") -> dict:
    """Type ``text`` into a field as REAL key events (isTrusted=true).

    mode='keystroke' (default): per-character CDP ``Input.dispatchKeyEvent`` — fires
    keydown/keypress/input/keyup, so framework filters (react-select, ExtJS combos)
    react exactly as to a human. mode='send_keys': native Selenium ``el.send_keys``
    (also a trusted event) — matches the live PropertyRadar recipe. mode='insert':
    one ``Input.insertText`` (fires input but no key events) — faster when the app
    only reads ``value``.

    Focuses the field first (native click). ``require_focus=True`` turns an
    unconfirmed focus into a loud error instead of a soft ``focus_warning`` — use it
    when a mis-focus would send keystrokes to the wrong element (e.g. driven from
    ``select_option`` where the next step depends on the field having the text).

    Value-reflect verify (verify=True, no ``expect``) checks the field's own value
    contains the text. Fields that TRANSFORM input (phone/date masks) can fail this
    even when typing worked — pass an explicit ``expect`` or ``verify=False`` there.
    """
    _require_cdp(driver, primitive)
    _disable_fill_capture(driver)
    if not text and not clear_first and not press_enter:
        raise TrustedInputError("bad_request", "nothing to type", primitive=primitive)

    # Resolve + focus the target field.
    by = "css" if css else ("xpath" if xpath else "text")
    token = uuid.uuid4().hex[:12]
    spec = _build_spec(by, css, xpath, locate_text, scope_css, scope_xpath, exact, None, None, None,
                       index, True, token, primitive)
    try:
        res = _resolve(driver, spec, primitive)
        if res.get("covered"):
            raise TrustedInputError("covered_by_overlay", "an overlay covers the input field",
                                    primitive=primitive, cover=res.get("cover"))
        el = _find_by_token(driver, token)
        if el is None:
            raise TrustedInputError("element_not_found", "resolved field vanished", primitive=primitive)

        editable = driver.execute_script(
            "var e=arguments[0];var t=(e.tagName||'').toUpperCase();"
            "return t==='INPUT'||t==='TEXTAREA'||e.isContentEditable===true;", el)
        if not editable:
            raise TrustedInputError("not_editable",
                                    "target is not an editable field (input/textarea/contenteditable)",
                                    primitive=primitive, target=res.get("target"))
        if focus:
            try:
                el.click()
            except Exception:
                pass
            driver.execute_script("arguments[0].focus();", el)
            active = driver.execute_script(
                "return document.activeElement===arguments[0] || arguments[0].contains(document.activeElement);", el)
            if not active:
                if require_focus:
                    raise TrustedInputError(
                        "effect_not_observed",
                        "could not confirm focus on the target field before typing",
                        primitive=primitive, target=res.get("target"))
                # else keep going — some frameworks proxy focus to an inner input — but note it
                res["focus_warning"] = "activeElement is not the resolved field"

        if clear_first:
            if mode == "send_keys":
                try:
                    el.clear()
                except Exception:
                    _clear_field(driver, el, primitive)
            else:
                _clear_field(driver, el, primitive)

        if text:
            if mode == "insert":
                _cdp(driver, "Input.insertText", {"text": text})
            elif mode == "keystroke":
                for ch in text:
                    _type_char(driver, ch)
                    if per_char_delay_ms:
                        time.sleep(per_char_delay_ms / 1000.0)
            elif mode == "send_keys":
                el.send_keys(text)
            else:
                raise TrustedInputError("bad_request", f"unknown mode {mode!r}", primitive=primitive)

        if press_enter:
            if mode == "send_keys":
                from selenium.webdriver.common.keys import Keys
                el.send_keys(Keys.RETURN)
            else:
                _press_key(driver, "Enter", "Enter", 13)

        # Verification: explicit expect wins; else default value-reflects-text check.
        verified = None
        if expect:
            verified = _verify(driver, expect, verify_timeout_ms, primitive)
        elif verify and text:
            got = driver.execute_script(
                "var e=arguments[0];return e.isContentEditable?(e.textContent||''):(e.value||'');", el)
            if text not in (got or ""):
                raise TrustedInputError("effect_not_observed",
                                        "typed input did not match field readback",
                                        primitive=primitive)
            verified = True
        return {"ok": True, "primitive": primitive, "mode": mode, "target": res.get("target"),
                "verified": verified, "focus_warning": res.get("focus_warning")}
    finally:
        _cleanup_tokens(driver, token)


def _clear_field(driver, el, primitive: str):
    """Clear an editable field with real edit events (select-all then Backspace)."""
    try:
        driver.execute_script(
            "var e=arguments[0];"
            "if(e.select){e.select();}"
            "else{var r=document.createRange();r.selectNodeContents(e);"
            "var sel=window.getSelection();sel.removeAllRanges();sel.addRange(r);}", el)
    except Exception:
        pass
    # Backspace deletes the selection in one keystroke; loop as a safety net.
    for _ in range(3):
        cur = driver.execute_script(
            "var e=arguments[0];return e.isContentEditable?(e.textContent||''):(e.value||'');", el)
        if not cur:
            break
        _press_key(driver, "Backspace", "Backspace", 8)
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# wait_for — poll for an element (virtualized-list timing)
# ---------------------------------------------------------------------------

def wait_for(driver, *, by: str = "css", css: str = None, xpath: str = None, text: str = None,
             scope_css: str = None, scope_xpath: str = None, exact: bool = False, tag: str = None,
             timeout_ms: int = 5000, min_count: int = 1, primitive: str = "trusted.wait_for") -> dict:
    """Poll until at least ``min_count`` visible element(s) match, or time out.

    Returns {ok, count, ...}. Raises ``element_not_found`` on timeout — never a
    silent empty result. Use before reading a virtualized/async-rendered list.
    """
    deadline = time.time() + timeout_ms / 1000.0
    count_js = _COUNT_JS
    last = 0
    while time.time() < deadline:
        try:
            last = driver.execute_script(count_js, {
                "by": by, "css": css, "xpath": xpath, "text": text, "exact": bool(exact),
                "tag": tag, "scopeCss": scope_css, "scopeXpath": scope_xpath})
        except Exception:
            last = 0
        if last and last >= min_count:
            return {"ok": True, "primitive": primitive, "count": last}
        time.sleep(0.1)
    raise TrustedInputError("element_not_found",
                            f"fewer than {min_count} visible match(es) after {timeout_ms}ms (saw {last})",
                            primitive=primitive, count=last)


_COUNT_JS = r"""
var spec=arguments[0];
function vis(el){if(!el||el.nodeType!==1)return false;var cs=getComputedStyle(el);
 if(cs.display==='none'||cs.visibility==='hidden'||parseFloat(cs.opacity)===0)return false;
 var r=el.getBoundingClientRect();if(r.width<=0||r.height<=0)return false;
 if(el.offsetParent===null&&cs.position!=='fixed'&&cs.position!=='sticky')return false;return true;}
function root(){if(spec.scopeCss)return document.querySelector(spec.scopeCss);
 if(spec.scopeXpath){var r=document.evaluate(spec.scopeXpath,document,null,9,null);return r.singleNodeValue;}
 return document;}
var cands=[];
if(spec.by==='css'){cands=Array.prototype.slice.call(document.querySelectorAll(spec.css));}
else if(spec.by==='xpath'){var res=document.evaluate(spec.xpath,document,null,7,null);
 for(var i=0;i<res.snapshotLength;i++)cands.push(res.snapshotItem(i));}
else if(spec.by==='text'){var rt=root();if(!rt)return 0;
 var els=Array.prototype.slice.call(rt.querySelectorAll(spec.tag||'*'));
 var n=(spec.text||'').replace(/\s+/g,' ').trim().toLowerCase();
 var m=els.filter(function(el){var t=(el.textContent||'').replace(/\s+/g,' ').trim().toLowerCase();
   return spec.exact?t===n:t.indexOf(n)!==-1;});
 cands=m.filter(function(el){return !m.some(function(o){return o!==el&&el.contains(o);});});}
return cands.filter(vis).length;
"""


# ---------------------------------------------------------------------------
# select_option — id-less combo end-to-end, per the LIVE PropertyRadar recipe
# ---------------------------------------------------------------------------

def _scope_text(driver, css: str):
    return driver.execute_script(
        "var e=document.querySelector(arguments[0]);"
        "return e?(e.textContent||'').replace(/\\s+/g,' ').trim():null;", css)


def _scope_committed_values(driver, scope_css: str, value_css: str, primitive: str):
    """Return only value-bearing descendants of the committed-selection scope.

    ExtJS ``vCsvItem`` chips can expose their label only through a child input's
    ``value``; their textContent is merely the delete glyph.  ``value_css`` is
    deliberately evaluated *relative to* ``scope_css`` so a typed combo input
    or a floating dropdown elsewhere in the document cannot satisfy read-back.
    """
    result = driver.execute_script(
        "var scopes=document.querySelectorAll(arguments[0]);"
        "if(scopes.length!==1)return {scopeCount:scopes.length,values:null};"
        "return {scopeCount:1,values:Array.prototype.map.call("
        "scopes[0].querySelectorAll(arguments[1]),"
        "function(e){return e&&e.value!=null?String(e.value):'';})};",
        scope_css, value_css,
    )
    if not isinstance(result, dict) or result.get("scopeCount") != 1:
        count = result.get("scopeCount") if isinstance(result, dict) else None
        raise TrustedInputError(
            "bad_request",
            "verify_value_css requires verify_scope_css to resolve to exactly one committed scope",
            primitive=primitive, scope=scope_css, scope_count=count,
        )
    return result.get("values")


def _readback_contains(readback, needle: str, primitive: str) -> bool:
    if isinstance(readback, list):
        matching = [value for value in readback if needle in str(value).lower()]
        if len(matching) > 1:
            raise TrustedInputError(
                "effect_not_observed",
                "selection read-back is ambiguous: more than one committed value matches",
                primitive=primitive,
            )
        return len(matching) == 1
    return needle in str(readback or "").lower()


def _wait_gone(driver, css: str, timeout_ms: int, primitive: str):
    """Poll until the element at ``css`` is absent or not visible (e.g. an ExtJS
    x-mask clearing). Raises if it is still covering after the timeout."""
    deadline = time.time() + timeout_ms / 1000.0
    while time.time() < deadline:
        present = driver.execute_script(
            "var e=document.querySelector(arguments[0]); if(!e) return false;"
            "var cs=getComputedStyle(e); var r=e.getBoundingClientRect();"
            "return cs.display!=='none' && cs.visibility!=='hidden' && r.width>0 && r.height>0;", css)
        if not present:
            return True
        time.sleep(0.1)
    raise TrustedInputError("covered_by_overlay",
                            f"loading overlay {css!r} did not clear within {timeout_ms}ms",
                            primitive=primitive, mask=css)


def _verify_readback(driver, scope_css: str, text: str, before, timeout_ms: int, primitive: str,
                     verify_value_css: str = None):
    """Trustworthy selection check: the committed scope must now CONTAIN ``text`` AND
    have CHANGED from ``before``. When ``verify_value_css`` is supplied, only the
    matching value-bearing *descendants* of the committed scope participate; this
    supports ExtJS vCsvItem labels rendered in child input values while excluding
    the typed combo input and floating dropdown from the proof."""
    needle = (text or "").lower()
    deadline = time.time() + timeout_ms / 1000.0
    cur = before
    while time.time() < deadline:
        cur = (_scope_committed_values(driver, scope_css, verify_value_css, primitive)
               if verify_value_css else _scope_text(driver, scope_css))
        if cur is not None and _readback_contains(cur, needle, primitive) and cur != before:
            return True
        time.sleep(0.1)
    raise TrustedInputError(
        "effect_not_observed",
        "selection was not confirmed on committed readback",
        primitive=primitive)


@confidential_fill("trusted.select_option")
def select_option(driver, *, input_css: str = None, input_xpath: str = None,
                  item_text: str = None, item_index: int = None,
                  item_scope_css: str = None, item_scope_xpath: str = None,
                  item_tag: str = None, item_exact: bool = True,
                  type_value: str = None, clear_first: bool = True, type_mode: str = "send_keys",
                  settle_ms: int = 0, mask_css: str = None, mask_timeout_ms: int = 8000,
                  open_timeout_ms: int = 5000,
                  verify_scope_css: str = None, verify_text: str = None, verify_timeout_ms: int = 4000,
                  verify_value_css: str = None,
                  trigger_css: str = None, trigger_xpath: str = None, open_via_trigger: bool = False,
                  primitive: str = "trusted.select_option") -> dict:
    """Select an item from an id-less combo, encoding the LIVE PropertyRadar recipe
    that beat the ExtJS State picker after ~15 other approaches failed:

      1. FOCUS the combo input with a native (trusted) click — dispatched focus is
         swallowed. (``input_css``/``input_xpath``; re-fetch by a STABLE selector,
         since the combo's id changes after every selection.)
      2. TYPE the option text into the input (``type_value``, default ``item_text``),
         clear_first — this, not the trigger, runs the async store-query. Clicking
         the trigger only opens the id-less container and selects nothing, so the
         trigger is skipped by default.
      3. SETTLE ``settle_ms`` (the live widget needs ~3000ms; its render is async and
         inconsistent and a <2s wait misses it), then wait for the x-mask
         (``mask_css``) to clear.
      4. WAIT for the option to render, then resolve it by EXACT visible text (or
         index), JS-tag it, and NATIVE-click it (all handled by ``click``).
      5. VERIFY by reading the committed selection back from ``verify_scope_css`` —
         it must now contain the text AND have CHANGED. A verify scoped to the whole
         document would false-pass because the option text is already in the
         dropdown/input; scope it to the committed chips/tagfield. For ExtJS vCsvItem
         labels rendered in child input values rather than textContent, pass
         ``verify_value_css``; it is evaluated only beneath that committed scope.
         This scope is required: there is no successful unverified select path.

    Any step that cannot complete raises an explicit, attributable error — never a
    silent no-op. Set ``open_via_trigger=True`` (with ``trigger_css``/``trigger_xpath``)
    for combos that genuinely open on a trigger click rather than on typing.
    """
    if item_text is None and item_index is None:
        raise TrustedInputError("bad_request", "select_option requires item_text or item_index",
                                primitive=primitive)
    if not (verify_scope_css and verify_scope_css.strip()):
        raise TrustedInputError(
            "bad_request",
            "select_option requires verify_scope_css for committed-selection read-back",
            primitive=primitive,
        )
    if verify_value_css is not None and not verify_value_css.strip():
        raise TrustedInputError(
            "bad_request",
            "verify_value_css must be a non-empty descendant selector when supplied",
            primitive=primitive,
        )
    if item_index is not None and not (verify_text and verify_text.strip()):
        raise TrustedInputError(
            "bad_request",
            "select_option with item_index requires explicit verify_text for read-back",
            primitive=primitive,
        )
    has_input = bool(input_css or input_xpath)
    if not has_input and not (open_via_trigger and (trigger_css or trigger_xpath)):
        raise TrustedInputError("bad_request",
                                "select_option needs input_css/input_xpath to type into "
                                "(or open_via_trigger with a trigger)", primitive=primitive)
    _require_cdp(driver, primitive)

    opened = False
    # (0) optional trigger open — only for combos that truly open on a trigger click.
    if open_via_trigger and (trigger_css or trigger_xpath):
        click(driver, by=("css" if trigger_css else "xpath"), css=trigger_css, xpath=trigger_xpath,
              primitive="trusted.select_option.open")
        opened = True

    # (1)+(2) focus the input and TYPE the option text — the proven store-query path.
    typed = None
    if has_input:
        tv = type_value if type_value is not None else item_text
        if tv is None:
            raise TrustedInputError("bad_request", "type_value or item_text required to type into the combo",
                                    primitive=primitive)
        type_text(driver, text=tv, css=input_css, xpath=input_xpath, mode=type_mode,
                  clear_first=clear_first, focus=True, require_focus=True, verify=False,
                  primitive="trusted.select_option.type")
        typed = tv

    # (3) fixed settle for the inconsistent async render, then wait for the x-mask.
    if settle_ms:
        time.sleep(settle_ms / 1000.0)
    if mask_css:
        _wait_gone(driver, mask_css, mask_timeout_ms, primitive="trusted.select_option.mask")

    # Baseline for the required read-back change check (before the pick commits
    # anything). A select without a committed-selection verify is not a success.
    before = (_scope_committed_values(driver, verify_scope_css, verify_value_css, primitive)
              if verify_value_css else _scope_text(driver, verify_scope_css))

    # (4) wait for the option to render, then tag + native-click it.
    if item_text is not None:
        wait_for(driver, by="text", text=item_text, scope_css=item_scope_css, scope_xpath=item_scope_xpath,
                 tag=item_tag, exact=item_exact, timeout_ms=open_timeout_ms,
                 primitive="trusted.select_option.wait")
        click(driver, by="text", text=item_text, scope_css=item_scope_css, scope_xpath=item_scope_xpath,
              tag=item_tag, exact=item_exact, primitive="trusted.select_option.pick")
        picked = item_text
    else:
        by = "css" if item_scope_css else "xpath"
        css = (item_scope_css + " " + (item_tag or "*")) if item_scope_css else None
        wait_for(driver, by=by, css=css, xpath=item_scope_xpath, min_count=item_index + 1,
                 timeout_ms=open_timeout_ms, primitive="trusted.select_option.wait")
        click(driver, by=by, css=css, xpath=item_scope_xpath, index=item_index,
              primitive="trusted.select_option.pick")
        picked = f"index {item_index}"

    # (5) required, trustworthy read-back verify scoped to the committed selection.
    # Text-mode may use the picked text by default; index-mode validated an explicit
    # verification text above because an index is not a committed selection identity.
    vtext = verify_text if verify_text is not None else item_text
    verified = _verify_readback(driver, verify_scope_css, vtext, before, verify_timeout_ms, primitive,
                                verify_value_css=verify_value_css)
    return {"ok": True, "primitive": primitive, "opened": opened, "verified": verified}


# ---------------------------------------------------------------------------
# overlay_check — DETECT interception before acting (attribution, not a fix)
# ---------------------------------------------------------------------------

def overlay_check(driver, *, css: str = None, xpath: str = None, text: str = None,
                  scope_css: str = None, scope_xpath: str = None, exact: bool = False,
                  x: float = None, y: float = None, primitive: str = "trusted.overlay_check") -> dict:
    """Report whether a blocking overlay covers a target (or point). No click.

    Returns {covered, cover:{tag,id,cls,text}, point, target}. Lets a caller poll
    "is the restart overlay still intercepting?" and ATTRIBUTE a block to the site's
    backend condition rather than to our tooling. It does not dismiss or fix anything.
    """
    if x is not None and y is not None:
        spec = {"by": "point", "x": x, "y": y, "scroll": False}
    elif css:
        spec = _build_spec("css", css, None, None, None, None, False, None, None, None, 0, True, None, primitive)
    elif xpath:
        spec = _build_spec("xpath", None, xpath, None, None, None, False, None, None, None, 0, True, None, primitive)
    elif text:
        spec = _build_spec("text", None, None, text, scope_css, scope_xpath, exact, None, None, None, 0, True, None, primitive)
    else:
        raise TrustedInputError("bad_request", "overlay_check needs a target (css/xpath/text/point)",
                                primitive=primitive)
    res = _resolve(driver, spec, primitive)
    if res.get("byPoint"):
        return {"ok": True, "primitive": primitive, "covered": None, "point": res.get("point"),
                "hit": res.get("hit"), "inViewport": res.get("inViewport")}
    return {"ok": True, "primitive": primitive, "covered": bool(res.get("covered")),
            "cover": res.get("cover"), "point": res.get("point"), "target": res.get("target")}


# ---------------------------------------------------------------------------
# prove_trusted — assert isTrusted from inside the page (acceptance helper)
# ---------------------------------------------------------------------------

def prove_trusted(driver, *, css: str, text: str = None, primitive: str = "trusted.prove") -> dict:
    """Type into ``css`` and read back, from inside the page, whether the resulting
    keydown + input events were isTrusted=true. This is the acceptance proof that our
    synthesized typing is a real browser event, not a swallowed synthetic one."""
    _require_cdp(driver, primitive)
    driver.execute_script(
        "var e=document.querySelector(arguments[0]);window.__ti_kd=null;window.__ti_inp=null;"
        "e.addEventListener('keydown',function(ev){if(!window.__ti_kd)window.__ti_kd={isTrusted:ev.isTrusted,key:ev.key};});"
        "e.addEventListener('input',function(ev){if(!window.__ti_inp)window.__ti_inp={isTrusted:ev.isTrusted};});", css)
    type_text(driver, text=(text or "x"), css=css, verify=False, primitive=primitive)
    kd = driver.execute_script("return window.__ti_kd;")
    inp = driver.execute_script("return window.__ti_inp;")
    return {"ok": True, "primitive": primitive,
            "keydown_trusted": bool(kd and kd.get("isTrusted")),
            "input_trusted": bool(inp and inp.get("isTrusted")),
            "keydown": kd, "input": inp}
