"""
Scraper Bot client — lightweight wrapper for AI agents to control remote Chrome sessions.

Usage (simple):
    from client import ScraperBot

    bot = ScraperBot()
    s = bot.create_session(name="My Job", owner="land-bot")
    bot.navigate(s, "https://example.com")
    html = bot.html(s)
    bot.click(s, css="button.submit")
    bot.close(s)

Usage (ScrapeJob — with heartbeat + auto-recovery):
    from client import ScrapeJob

    def my_resume(session_id):
        \"\"\"Called after session recovery; replay login/navigation state.\"\"\"
        bot.navigate(session_id, "https://example.com")
        bot.click(session_id, css="#login")
        # ... restore state up to the point of failure

    with ScrapeJob(name="Denver Foreclosures", owner="land-bot",
                   resume_from=my_resume) as job:
        job.navigate("https://example.com/list")
        for item in items:
            job.click(css=f"#row-{item.id}")
            job.wait(css=".detail-panel")
            html = job.html()
            # process html ...

Error taxonomy:
    ScraperBotConnectionError  — transport / connect timeout / 502-504
    ScraperBotSessionLost      — 404 on session route (server restart or reaper)
    ScraperBotActionError      — 400/408 from Selenium (element not found, timeout)
    ScraperBotServerError      — 500 unexpected server error
"""

from __future__ import annotations

import logging
import os
import random
import threading
import time
import uuid
from typing import Any, Callable, Optional

import requests
import fill_diagnostics as fd
from platform_base import get_adapter
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("scraper-bot.client")

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ScraperBotError(Exception):
    """Base class for all scraper-bot client errors."""


class ScraperBotConnectionError(ScraperBotError):
    """Transport failure, connect timeout, or 502/503/504.

    Safe to retry with backoff + session recreation via ScrapeJob.
    """


class ScraperBotSessionLost(ScraperBotError):
    """Session not found on server (404). Server restarted or reaper killed it.

    ScrapeJob will recreate session and call resume_from().
    """


class ScraperBotActionError(ScraperBotError):
    """Selenium/browser error: 400 (element not found) or 408 (wait timeout).

    Do NOT retry — propagate to caller.
    """


class ScraperBotServerError(ScraperBotError):
    """Unexpected server error (500). Do NOT retry — log and raise."""


class ScraperBotInputError(ScraperBotActionError):
    """A trusted-input primitive (/input/*) refused rather than silently no-op'ing.

    Carries the structured, attributable outcome so callers can branch honestly:
    ``reason`` (e.g. 'covered_by_overlay', 'element_not_found', 'effect_not_observed'),
    ``primitive`` (which primitive ran), ``status_code``/``status`` (the HTTP
    refusal status), and ``detail`` (full body incl. covering element). A
    'covered_by_overlay' is a site/backend condition; 'element_not_found' /
    'effect_not_observed' point at our call or the page, not backend instability.
    """

    def __init__(self, reason: str, message: str, primitive: str = None,
                 detail: dict = None, status_code: int = None):
        self.reason = reason
        self.primitive = primitive
        self.status_code = status_code
        self.status = status_code  # Stable shorthand for callers that branch on HTTP status.
        self.detail = detail or {}
        super().__init__(f"{primitive or 'input'}/{reason}: {message}")


# ---------------------------------------------------------------------------
# CSRF token loader
# ---------------------------------------------------------------------------

DASHBOARD_TOKEN_PATH = os.environ.get(
    "SCRAPERBOT_DASHBOARD_TOKEN_PATH", os.path.join(
        os.path.dirname(os.environ.get("SCRAPERBOT_DB_PATH", os.path.join(
            os.environ.get("SCRAPERBOT_DATA_DIR", get_adapter().default_data_dir), "sessions.db"))),
        "dashboard_token")
)
_cached_token: Optional[str] = None


def _load_dashboard_token() -> str:
    """Lazily load the dashboard token from disk (cached after first read)."""
    global _cached_token
    if _cached_token:
        return _cached_token
    try:
        with open(DASHBOARD_TOKEN_PATH) as f:
            _cached_token = f.read().strip()
    except FileNotFoundError:
        logger.warning(f"Dashboard token file not found at {DASHBOARD_TOKEN_PATH}; destructive calls will fail 403")
        _cached_token = ""
    return _cached_token


# ---------------------------------------------------------------------------
# ScraperBot client
# ---------------------------------------------------------------------------


class ScraperBot:
    """HTTP client for the scraper-bot server.

    Creates a shared requests.Session with retry logic and consistent timeouts.
    Raises typed exceptions for all error categories.
    """

    def __init__(self, base_url: str = "http://localhost:9020"):
        self.base = base_url.rstrip("/")
        self._session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[502, 503, 504],
            allowed_methods=["GET"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _headers(self, destructive: bool = False) -> dict:
        headers = {}
        if destructive:
            headers["X-Scraper-Token"] = _load_dashboard_token()
        return headers

    def _raise(self, resp: requests.Response) -> None:
        """Convert HTTP error responses to typed exceptions."""
        status = resp.status_code
        try:
            body = resp.json()
            detail = body.get("detail") or body.get("error") or str(body)
        except Exception:
            detail = resp.text[:200]

        if status == 404:
            raise ScraperBotSessionLost(f"Session not found (404): {detail}")
        if status in (400, 408):
            raise ScraperBotActionError(f"Action error ({status}): {detail}")
        if status == 500:
            raise ScraperBotServerError(f"Server error (500): {detail}")
        if status >= 400:
            raise ScraperBotError(f"HTTP {status}: {detail}")

    def _request(self, method: str, path: str, json_data: dict = None,
                 params: dict = None, destructive: bool = False) -> dict:
        url = self._url(path)
        headers = self._headers(destructive=destructive)
        try:
            resp = self._session.request(
                method, url,
                json=json_data, params=params, headers=headers,
                timeout=(5, 60),
            )
        except requests.exceptions.ConnectionError as e:
            raise ScraperBotConnectionError(f"Connection failed to {url}: {e}") from e
        except requests.exceptions.Timeout as e:
            raise ScraperBotConnectionError(f"Request timed out to {url}: {e}") from e

        if not resp.ok:
            self._raise(resp)
        return resp.json()

    def _post(self, path: str, json_data: dict = None, destructive: bool = False) -> dict:
        return self._request("POST", path, json_data=json_data, destructive=destructive)

    def _get(self, path: str, params: dict = None) -> dict:
        return self._request("GET", path, params=params)

    def _delete(self, path: str, params: dict = None) -> dict:
        return self._request("DELETE", path, params=params, destructive=True)

    def _multipart_post(self, path: str, data: dict, files: dict) -> dict:
        url = self._url(path)
        try:
            resp = self._session.post(
                url,
                data=data,
                files=files,
                timeout=(5, 60),
            )
        except requests.exceptions.ConnectionError as e:
            raise ScraperBotConnectionError(f"Connection failed to {url}: {e}") from e
        except requests.exceptions.Timeout as e:
            raise ScraperBotConnectionError(f"Request timed out to {url}: {e}") from e

        if not resp.ok:
            self._raise(resp)
        return resp.json()

    def _input_post(self, path: str, body: dict) -> dict:
        """POST to an /input/* primitive, raising ScraperBotInputError (with the
        structured reason) on a refusal so callers can attribute failures."""
        if path.endswith("/input/type"):
            return self._fill_post(path, body, "trusted.type")
        if path.endswith("/input/select_option"):
            return self._fill_post(path, body, "trusted.select_option")
        url = self._url(path)
        try:
            resp = self._session.post(url, json=body, headers=self._headers(),
                                      timeout=(5, 120))
        except requests.exceptions.ConnectionError as e:
            raise ScraperBotConnectionError(f"Connection failed to {url}: {e}") from e
        except requests.exceptions.Timeout as e:
            raise ScraperBotConnectionError(f"Request timed out to {url}: {e}") from e
        if resp.ok:
            return resp.json()
        try:
            detail = resp.json().get("detail")
        except Exception:
            detail = None
        if isinstance(detail, dict) and detail.get("reason"):
            raise ScraperBotInputError(detail.get("reason"), detail.get("message", ""),
                                       detail.get("primitive"), detail, resp.status_code)
        self._raise(resp)

    def _fill_post(self, path: str, body: dict, primitive: str) -> dict:
        with fd.confidential_logs():
            try:
                resp = self._session.post(self._url(path), json=body, headers=self._headers(), timeout=(5, 120))
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
                error = ScraperBotConnectionError("confidential fill transport failed")
            except Exception:
                error = ScraperBotError("confidential fill request failed")
            else:
                try:
                    data = resp.json()
                except Exception:
                    data = None
                if resp.ok and isinstance(data, dict) and (data.get("ok") is True or
                        (primitive == "legacy.type" and data.get("status") == "typed")):
                    return fd.success_detail(data, primitive)
                status = resp.status_code
                detail = data.get("detail") if isinstance(data, dict) else None
                detail = fd.error_detail(detail.get("reason") if isinstance(detail, dict) else "error",
                                         fd.safe_primitive(detail.get("primitive"), primitive) if isinstance(detail, dict) else primitive)
                if status == 404:
                    error = ScraperBotSessionLost("confidential fill session not found (404)")
                elif status in (502, 503, 504):
                    error = ScraperBotConnectionError("confidential fill service unavailable")
                elif status >= 500:
                    error = ScraperBotServerError("confidential fill server failed")
                else:
                    error = ScraperBotInputError(detail["reason"], detail["message"],
                                                 detail["primitive"], detail, status)
        # Outside all exception handlers: no raw transport/JSON exception chain.
        raise error from None

    # -- Session management --

    def health(self) -> dict:
        """Return server health info (uptime, sessions, cap, etc.)."""
        return self._get("/health")

    def list_sessions(self) -> dict:
        """Return all non-closed sessions as {session_id: metadata}."""
        return self._get("/sessions")

    def create_session(
        self,
        undetected: bool = True,
        headless: Optional[bool] = None,
        chrome_version_main: Optional[int] = None,
        view: str = "desktop",
        user_data_dir: Optional[str] = None,
        driver: str = "chromedriver",
        trace: bool = False,
        trace_dir: Optional[str] = None,
        # New optional fields (back-compat: callers not passing these still work)
        name: Optional[str] = None,
        owner: Optional[str] = None,
        labels: Optional[dict] = None,
        job_id: Optional[str] = None,
        lease_mode: bool = False,
        heartbeat_ttl_seconds: int = 300,
        max_chrome_retries: int = 1,
    ) -> str:
        """Create a new Chrome session, return session_id.

        All new fields are optional — existing callers don't need to pass them.
        Pass name + owner for observability in the dashboard and DB.
        Use lease_mode=True for ScrapeJob-managed sessions (auto-heartbeat).
        """
        body: dict = {
            "undetected": undetected,
            "headless": headless,
            "chrome_version_main": chrome_version_main,
            "view": view,
            "driver": driver,
            "trace": trace,
            "lease_mode": lease_mode,
            "heartbeat_ttl_seconds": heartbeat_ttl_seconds,
        }
        if trace_dir:
            body["trace_dir"] = trace_dir
        if user_data_dir:
            body["user_data_dir"] = user_data_dir
        if name:
            body["name"] = name
        if owner:
            body["owner"] = owner
        if labels:
            body["labels"] = labels
        if job_id:
            body["job_id"] = job_id

        # Retry transient Chrome startup failures only when the caller opts in.
        import time as _time
        last_err: Optional[Exception] = None
        attempts = max(1, max_chrome_retries)
        retry_deadline = _time.monotonic() + 10.0
        for attempt in range(attempts):
            try:
                resp = self._post("/sessions", body)
                return resp["session_id"]
            except ScraperBotServerError as e:
                msg = str(e)
                if "Failed to start Chrome" not in msg and "Unable to obtain driver" not in msg:
                    raise
                last_err = e
                if attempt < attempts - 1:
                    base_delay = 2 ** attempt + 1  # 2s, 3s, 5s...
                    delay = base_delay * (1 + random.uniform(0.0, 0.3))
                    remaining = retry_deadline - _time.monotonic()
                    if remaining <= 0:
                        break
                    delay = min(delay, remaining)
                    logger.warning(f"Chrome start retry {attempt + 1}/{max_chrome_retries} after {delay:.2f}s")
                    _time.sleep(delay)
        if last_err:
            raise last_err
        raise ScraperBotError("create_session failed with no response")

    def close(self, session_id: str, close_reason: str = "client_close") -> dict:
        """Close a session. Idempotent."""
        return self._delete(f"/sessions/{session_id}", params={"close_reason": close_reason})

    def heartbeat(self, session_id: str) -> dict:
        """Send heartbeat to keep lease-mode session alive."""
        return self._post(f"/sessions/{session_id}/heartbeat")

    # -- Navigation --

    def navigate(self, session_id: str, url: str) -> dict:
        return self._post(f"/sessions/{session_id}/navigate", {"url": url})

    def url(self, session_id: str) -> str:
        return self._get(f"/sessions/{session_id}/url")["url"]

    def title(self, session_id: str) -> str:
        return self._get(f"/sessions/{session_id}/title")["title"]

    def back(self, session_id: str) -> dict:
        return self._post(f"/sessions/{session_id}/back")

    def forward(self, session_id: str) -> dict:
        return self._post(f"/sessions/{session_id}/forward")

    def refresh(self, session_id: str) -> dict:
        return self._post(f"/sessions/{session_id}/refresh")

    # -- Page content --

    def html(self, session_id: str) -> str:
        return self._get(f"/sessions/{session_id}/html")["html"]

    def screenshot_base64(self, session_id: str) -> str:
        return self._get(f"/sessions/{session_id}/screenshot")["screenshot_base64"]

    def cookies(self, session_id: str) -> dict:
        return self._get(f"/sessions/{session_id}/cookies")["cookies"]

    # -- Interaction --

    def click(self, session_id: str, xpath: str = None, css: str = None,
              id: str = None, link_text: str = None, scroll_first: bool = True,
              action_instance_id: str = None) -> dict:
        return self._post(f"/sessions/{session_id}/click", {
            "xpath": xpath, "css": css, "id": id,
            "link_text": link_text, "scroll_first": scroll_first,
            "action_instance_id": action_instance_id,
        })

    def type(self, session_id: str, text: str, xpath: str = None, css: str = None,
             id: str = None, clear_first: bool = False, press_enter: bool = False) -> dict:
        return self._fill_post(f"/sessions/{session_id}/type", {
            "xpath": xpath, "css": css, "id": id,
            "text": text, "clear_first": clear_first, "press_enter": press_enter,
        }, "legacy.type")

    def upload(self, session_id: str, file_path: str, *, css: str = None, xpath: str = None) -> dict:
        if bool(css) == bool(xpath):
            raise ScraperBotError("Provide exactly one selector: css or xpath")

        selector_type = "xpath" if xpath else "css"
        selector = xpath or css
        with open(file_path, "rb") as f:
            return self._multipart_post(
                f"/sessions/{session_id}/upload",
                data={"selector": selector, "selector_type": selector_type},
                files={"file": (os.path.basename(file_path), f)},
            )

    def execute(self, session_id: str, script: str, args: list = None) -> Any:
        resp = self._post(f"/sessions/{session_id}/execute", {
            "script": script, "args": args or [],
        })
        return resp["result"]

    def begin_download(self, session_id: str, action_instance_id: str) -> dict:
        return self._post(f"/sessions/{session_id}/downloads/begin", {"action_instance_id": action_instance_id})

    def download_events(self, session_id: str) -> list:
        return self._get(f"/sessions/{session_id}/downloads")["events"]

    def find(self, session_id: str, xpath: str = None, css: str = None,
             id: str = None, tag: str = None, class_name: str = None,
             multiple: bool = False) -> dict:
        return self._post(f"/sessions/{session_id}/find", {
            "xpath": xpath, "css": css, "id": id,
            "tag": tag, "class_name": class_name, "multiple": multiple,
        })

    def wait(self, session_id: str, xpath: str = None, css: str = None,
             id: str = None, timeout: int = 10) -> dict:
        return self._post(f"/sessions/{session_id}/wait", {
            "xpath": xpath, "css": css, "id": id, "timeout": timeout,
        })

    def scroll(self, session_id: str, amount: int = None, direction: str = "down",
               xpath: str = None) -> dict:
        return self._post(f"/sessions/{session_id}/scroll", {
            "amount": amount, "direction": direction, "xpath": xpath,
        })

    def select(self, session_id: str, id: str, value: str) -> dict:
        return self._post(f"/sessions/{session_id}/select", {"id": id, "value": value})

    def iframe(self, session_id: str, xpath: str = None, index: int = None,
               main: bool = False) -> dict:
        return self._post(f"/sessions/{session_id}/iframe", {
            "xpath": xpath, "index": index, "main": main,
        })

    # -- Trusted-input primitives (/input/*) --
    # Trusted typing (real key events), id-less targeting, overlay detection, and
    # virtualized-list waiting. All raise ScraperBotInputError with a structured
    # .reason on refusal — they never silently succeed. See trusted_input.py.

    def trusted_type(self, session_id: str, text: str, *, css: str = None, xpath: str = None,
                     locate_text: str = None, scope_css: str = None, scope_xpath: str = None,
                     exact: bool = False, index: int = 0, mode: str = "keystroke",
                     focus: bool = True, require_focus: bool = False, clear_first: bool = False,
                     press_enter: bool = False, expect: dict = None, verify: bool = True,
                     verify_timeout_ms: int = 4000, per_char_delay_ms: int = 0) -> dict:
        """Type as REAL key events so framework filters (react-select, ExtJS combos)
        react. mode='keystroke' (per-char CDP), 'send_keys' (native Selenium), or
        'insert' (insertText). require_focus=True errors if focus can't be confirmed."""
        return self._input_post(f"/sessions/{session_id}/input/type", {
            "text": text, "css": css, "xpath": xpath, "locate_text": locate_text,
            "scope_css": scope_css, "scope_xpath": scope_xpath, "exact": exact, "index": index,
            "mode": mode, "focus": focus, "require_focus": require_focus, "clear_first": clear_first,
            "press_enter": press_enter, "expect": expect, "verify": verify,
            "verify_timeout_ms": verify_timeout_ms, "per_char_delay_ms": per_char_delay_ms,
        })

    def click_text(self, session_id: str, text: str = None, *, css: str = None, xpath: str = None,
                   scope_css: str = None, scope_xpath: str = None, exact: bool = False,
                   tag: str = None, index: int = 0, scroll: bool = True, allow_covered: bool = False,
                   expect: dict = None, verify_timeout_ms: int = 4000) -> dict:
        """Click a specific element by VISIBLE TEXT within a scope (for id-less items),
        or by css/xpath. Native trusted click, with an overlay pre-check."""
        by = "css" if css else ("xpath" if xpath else "text")
        return self._input_post(f"/sessions/{session_id}/input/click_text", {
            "by": by, "text": text, "css": css, "xpath": xpath, "scope_css": scope_css,
            "scope_xpath": scope_xpath, "exact": exact, "tag": tag, "index": index,
            "scroll": scroll, "allow_covered": allow_covered, "expect": expect,
            "verify_timeout_ms": verify_timeout_ms,
        })

    def click_point(self, session_id: str, x: float, y: float, *, expect: dict = None,
                    verify_timeout_ms: int = 4000) -> dict:
        """Click a raw viewport coordinate (CSS px) via CDP."""
        return self._input_post(f"/sessions/{session_id}/input/click_point", {
            "x": x, "y": y, "expect": expect, "verify_timeout_ms": verify_timeout_ms,
        })

    def select_option(self, session_id: str, *, input_css: str = None, input_xpath: str = None,
                      item_text: str = None, item_index: int = None, item_scope_css: str = None,
                      item_scope_xpath: str = None, item_tag: str = None, item_exact: bool = True,
                      type_value: str = None, clear_first: bool = True, type_mode: str = "send_keys",
                      settle_ms: int = 0, mask_css: str = None, mask_timeout_ms: int = 8000,
                      open_timeout_ms: int = 5000, verify_scope_css: str = None, verify_text: str = None,
                      verify_timeout_ms: int = 4000, verify_value_css: str = None,
                      trigger_css: str = None, trigger_xpath: str = None,
                      open_via_trigger: bool = False) -> dict:
        """Id-less combo end-to-end, per the live PropertyRadar recipe: focus the
        input, TYPE the option text (this, not the trigger, runs the store-query),
        SETTLE, wait the x-mask clear, WAIT for the option, tag+native-click it, then
        VERIFY by reading the committed selection back from ``verify_scope_css`` (must
        contain the text AND have changed — a document-scoped verify would false-pass).
        ``verify_scope_css`` is required and must scope the committed chips/tagfield,
        not the whole page. ``verify_value_css`` optionally selects committed child
        input values relative to that scope (ExtJS vCsvItem). In index mode, provide
        ``verify_text`` explicitly."""
        return self._input_post(f"/sessions/{session_id}/input/select_option", {
            "input_css": input_css, "input_xpath": input_xpath, "item_text": item_text,
            "item_index": item_index, "item_scope_css": item_scope_css,
            "item_scope_xpath": item_scope_xpath, "item_tag": item_tag, "item_exact": item_exact,
            "type_value": type_value, "clear_first": clear_first, "type_mode": type_mode,
            "settle_ms": settle_ms, "mask_css": mask_css, "mask_timeout_ms": mask_timeout_ms,
            "open_timeout_ms": open_timeout_ms, "verify_scope_css": verify_scope_css,
            "verify_text": verify_text, "verify_timeout_ms": verify_timeout_ms,
            "verify_value_css": verify_value_css,
            "trigger_css": trigger_css, "trigger_xpath": trigger_xpath,
            "open_via_trigger": open_via_trigger,
        })

    def wait_for_item(self, session_id: str, *, css: str = None, xpath: str = None,
                      text: str = None, scope_css: str = None, scope_xpath: str = None,
                      exact: bool = False, tag: str = None, timeout_ms: int = 5000,
                      min_count: int = 1) -> dict:
        """Poll until >= min_count visible matches render (virtualized lists), or raise."""
        by = "css" if css else ("xpath" if xpath else "text")
        return self._input_post(f"/sessions/{session_id}/input/wait_for", {
            "by": by, "css": css, "xpath": xpath, "text": text, "scope_css": scope_css,
            "scope_xpath": scope_xpath, "exact": exact, "tag": tag, "timeout_ms": timeout_ms,
            "min_count": min_count,
        })

    def overlay_check(self, session_id: str, *, css: str = None, xpath: str = None,
                      text: str = None, scope_css: str = None, scope_xpath: str = None,
                      exact: bool = False, x: float = None, y: float = None) -> dict:
        """Detect (without clicking) whether a blocking overlay covers a target/point.
        Returns {covered, cover:{...}}. Use to attribute a block to a site/backend
        overlay before acting."""
        return self._input_post(f"/sessions/{session_id}/input/overlay_check", {
            "css": css, "xpath": xpath, "text": text, "scope_css": scope_css,
            "scope_xpath": scope_xpath, "exact": exact, "x": x, "y": y,
        })

    # -- Network --

    def enable_network(self, session_id: str) -> dict:
        return self._post(f"/sessions/{session_id}/network/enable")

    def network_requests(self, session_id: str, only_xhr: bool = False) -> list:
        return self._get(f"/sessions/{session_id}/network/requests",
                         {"only_xhr": only_xhr})["requests"]

    def network_traffic(self, session_id: str) -> list:
        return self._get(f"/sessions/{session_id}/network/traffic")["traffic"]

    def clear_network(self, session_id: str) -> dict:
        """Clear captured network logs. Subsequent requests/traffic calls
        return only entries logged after this point."""
        return self._post(f"/sessions/{session_id}/network/clear")

    def network_filter(self, session_id: str, url_contains: str = None,
                       only_xhr: bool = False) -> list:
        """Return network requests matching server-side filters.
        More efficient than pulling all requests and filtering client-side."""
        body = {"only_xhr": only_xhr}
        if url_contains:
            body["url_contains"] = url_contains
        resp = self._post(f"/sessions/{session_id}/network/filter", body)
        return resp["requests"]

    # -- reCAPTCHA --

    def solve_recaptcha(self, session_id: str, max_attempts: int = 3,
                        parent_iframe_xpath: str = None) -> bool:
        """Attempt to solve a reCAPTCHA v2 challenge on the current page.
        Uses audio challenge + Whisper transcription server-side.
        parent_iframe_xpath: xpath of the iframe the reCAPTCHA is nested inside
        (e.g. a Fillout embed), resolved from the top frame. Required when the
        reCAPTCHA is not at the top level.
        Returns True if solved."""
        body = {"max_attempts": max_attempts}
        if parent_iframe_xpath:
            body["parent_iframe_xpath"] = parent_iframe_xpath
        resp = self._post(f"/sessions/{session_id}/solve_recaptcha", body)
        return resp.get("solved", False)


# ---------------------------------------------------------------------------
# ScrapeJob context manager
# ---------------------------------------------------------------------------


class ScrapeJob:
    """Context manager for resilient batch scraping jobs.

    Features:
    - Auto-creates a lease-mode session with heartbeat thread.
    - On ScraperBotConnectionError or ScraperBotSessionLost: exponential backoff,
      session recreation, calls resume_from(new_session_id), retries the action.
    - ScrapeJob.__exit__ is crash-safe and never raises.
    - Caller owns checkpointing; ScrapeJob provides recovery hooks only.

    Threading:
    - Lifecycle methods (__enter__, __exit__, _recover) are guarded by an internal
      RLock and are safe to call concurrently.
    - Action methods (navigate, click, etc.) are NOT designed for concurrent use
      from multiple threads on the same job. Chrome itself is single-threaded per
      session, so two parallel calls would serialize on the server-side session
      lock anyway. Use one ScrapeJob per scraper thread.

    Example (land-bot style):
        def resume(session_id):
            bot.navigate(session_id, BASE_URL)
            bot.click(session_id, css="#login-btn")
            bot.type(session_id, text=EMAIL, css="#email")
            bot.type(session_id, text=PASSWORD, css="#password", press_enter=True)
            bot.wait(session_id, css=".dashboard", timeout=15)

        with ScrapeJob(name="Denver Foreclosures", owner="land-bot",
                       resume_from=resume) as job:
            for row in pending_rows:
                job.navigate(f"https://site.com/parcel/{row.id}")
                job.wait(css=".parcel-detail")
                html = job.html()
                process(html)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:9020",
        name: Optional[str] = None,
        owner: Optional[str] = None,
        labels: Optional[dict] = None,
        resume_from: Optional[Callable[[str], None]] = None,
        max_recreates_per_op: int = 1,
        max_recreates_per_job: int = 2,
        heartbeat_ttl_seconds: int = 300,
        # Chrome session options
        undetected: bool = True,
        headless: Optional[bool] = None,
        chrome_version_main: Optional[int] = None,
        view: str = "desktop",
        user_data_dir: Optional[str] = None,
    ):
        self._bot = ScraperBot(base_url)
        self._name = name
        self._owner = owner
        self._labels = labels
        self._resume_from = resume_from
        self._max_recreates_per_op = max_recreates_per_op
        self._max_recreates_per_job = max_recreates_per_job
        self._heartbeat_ttl_seconds = heartbeat_ttl_seconds
        self._chrome_kwargs = {
            "undetected": undetected,
            "headless": headless,
            "chrome_version_main": chrome_version_main,
            "view": view,
            "user_data_dir": user_data_dir,
        }
        self._job_id = uuid.uuid4().hex[:12]

        self._session_id: Optional[str] = None
        self._recreate_count = 0
        self.lease_unhealthy = False

        self._hb_stop = threading.Event()
        self._hb_thread: Optional[threading.Thread] = None
        # Serializes _create_session, _recover, _start/_stop_heartbeat, __exit__.
        # Action calls don't take this lock — they only read _session_id atomically.
        # Concurrent action calls from multiple threads on the same ScrapeJob are
        # NOT supported (Chrome itself is single-threaded per session anyway).
        self._job_lock = threading.RLock()

    @property
    def session_id(self) -> Optional[str]:
        """Current session id."""
        return self._session_id

    @property
    def bot(self) -> ScraperBot:
        """Underlying ScraperBot client."""
        return self._bot

    def __enter__(self) -> "ScrapeJob":
        with self._job_lock:
            self._create_session()
            self._start_heartbeat()
        return self

    def _create_session(self) -> None:
        """Create a new lease-mode session and store session_id."""
        self._session_id = self._bot.create_session(
            name=self._name,
            owner=self._owner,
            labels=self._labels,
            job_id=self._job_id,
            lease_mode=True,
            heartbeat_ttl_seconds=self._heartbeat_ttl_seconds,
            **self._chrome_kwargs,
        )
        logger.info(f"ScrapeJob {self._job_id}: created session {self._session_id}")

    def _start_heartbeat(self) -> None:
        """Start background heartbeat thread."""
        self._hb_stop.clear()
        self.lease_unhealthy = False
        interval = max(10, self._heartbeat_ttl_seconds // 2)
        t = threading.Thread(
            target=self._heartbeat_loop,
            args=(interval,),
            daemon=True,
            name=f"hb-{self._session_id}",
        )
        self._hb_thread = t
        t.start()

    def _heartbeat_loop(self, interval: int) -> None:
        """Send heartbeats every `interval` seconds until stopped."""
        while not self._hb_stop.wait(interval):
            if self._session_id is None:
                continue
            try:
                self._bot.heartbeat(self._session_id)
            except (ScraperBotConnectionError, ScraperBotSessionLost) as e:
                logger.warning(f"ScrapeJob {self._job_id}: heartbeat failed: {e}")
                self.lease_unhealthy = True
                return  # Exit thread; foreground action will drive recovery
            except Exception as e:
                logger.warning(f"ScrapeJob {self._job_id}: heartbeat unexpected error: {e}")
                self.lease_unhealthy = True
                return

    def _stop_heartbeat(self) -> None:
        """Signal heartbeat thread to stop and wait for it to join."""
        self._hb_stop.set()
        if self._hb_thread and self._hb_thread.is_alive():
            self._hb_thread.join(timeout=5)
        self._hb_thread = None

    def _recover(self) -> None:
        """Recreate session, call resume_from, then start heartbeat.
        Heartbeat is started LAST so a failed resume_from leaves a closed session,
        not a heartbeating half-restored one."""
        with self._job_lock:
            if self._recreate_count >= self._max_recreates_per_job:
                raise ScraperBotError(
                    f"ScrapeJob {self._job_id}: exceeded max_recreates_per_job={self._max_recreates_per_job}"
                )
            self._stop_heartbeat()
            self._recreate_count += 1
            logger.info(
                f"ScrapeJob {self._job_id}: recreating session "
                f"(attempt {self._recreate_count}/{self._max_recreates_per_job})"
            )
            self._create_session()
            new_sid = self._session_id
            if self._resume_from:
                logger.info(f"ScrapeJob {self._job_id}: calling resume_from({new_sid})")
                try:
                    self._resume_from(new_sid)
                except Exception as e:
                    logger.error(
                        f"ScrapeJob {self._job_id}: resume_from raised {type(e).__name__}: {e}. "
                        f"Closing partial session {new_sid} before re-raising."
                    )
                    try:
                        self._bot.close(new_sid, close_reason="client_close")
                    except Exception:
                        pass
                    self._session_id = None
                    raise
            # Only start heartbeat after resume_from succeeds.
            self._start_heartbeat()

    def _with_recovery(self, action: Callable[[], Any], max_recreates: int = None) -> Any:
        """Execute action, catching retryable errors and recovering up to max_recreates times."""
        if max_recreates is None:
            max_recreates = self._max_recreates_per_op
        recreates = 0
        backoff = 0.5
        while True:
            # Check if heartbeat thread flagged unhealthy
            if self.lease_unhealthy and self._session_id:
                logger.warning(f"ScrapeJob {self._job_id}: lease unhealthy, recovering before action")
                self._recover()
                recreates += 1

            try:
                return action()
            except (ScraperBotConnectionError, ScraperBotSessionLost) as e:
                if recreates >= max_recreates:
                    raise
                # Exponential backoff with jitter
                sleep_time = backoff + random.uniform(0, 0.3)
                logger.warning(
                    f"ScrapeJob {self._job_id}: {type(e).__name__}: {e}. "
                    f"Backing off {sleep_time:.1f}s then recovering..."
                )
                time.sleep(sleep_time)
                backoff = min(backoff * 2, 8.0)
                self._recover()
                recreates += 1

    def __exit__(self, exc_type, exc_val, exc_tb) -> bool:
        """Crash-safe exit: stop heartbeat, best-effort close session. Never raises."""
        with self._job_lock:
            try:
                self._stop_heartbeat()
            except Exception as e:
                logger.warning(f"ScrapeJob {self._job_id}: stop_heartbeat error: {e}")

            sid = self._session_id
            self._session_id = None

        if sid:
            try:
                self._bot.close(sid, close_reason="client_close")
            except (ScraperBotConnectionError, ScraperBotSessionLost):
                pass  # Server gone or session already closed — fine
            except Exception as e:
                logger.warning(f"ScrapeJob {self._job_id}: close session error (swallowed): {e}")

        return False  # Don't suppress exceptions from the with-block body

    # -- Action wrappers (with recovery) --

    def navigate(self, url: str) -> dict:
        """Navigate to URL, with auto-recovery on connection/session failure."""
        return self._with_recovery(lambda: self._bot.navigate(self._session_id, url))

    def click(self, xpath: str = None, css: str = None, id: str = None,
              link_text: str = None, scroll_first: bool = True) -> dict:
        return self._with_recovery(
            lambda: self._bot.click(self._session_id, xpath=xpath, css=css, id=id,
                                     link_text=link_text, scroll_first=scroll_first)
        )

    def type(self, text: str, xpath: str = None, css: str = None, id: str = None,
             clear_first: bool = False, press_enter: bool = False) -> dict:
        return self._with_recovery(
            lambda: self._bot.type(self._session_id, text=text, xpath=xpath, css=css,
                                    id=id, clear_first=clear_first, press_enter=press_enter)
        )

    def upload(self, file_path: str, *, css: str = None, xpath: str = None) -> dict:
        return self._with_recovery(
            lambda: self._bot.upload(self._session_id, file_path, css=css, xpath=xpath)
        )

    def execute(self, script: str, args: list = None) -> Any:
        return self._with_recovery(lambda: self._bot.execute(self._session_id, script, args))

    def find(self, xpath: str = None, css: str = None, id: str = None,
             tag: str = None, class_name: str = None, multiple: bool = False) -> dict:
        return self._with_recovery(
            lambda: self._bot.find(self._session_id, xpath=xpath, css=css, id=id,
                                    tag=tag, class_name=class_name, multiple=multiple)
        )

    def wait(self, xpath: str = None, css: str = None, id: str = None, timeout: int = 10) -> dict:
        return self._with_recovery(
            lambda: self._bot.wait(self._session_id, xpath=xpath, css=css, id=id, timeout=timeout)
        )

    def scroll(self, amount: int = None, direction: str = "down", xpath: str = None) -> dict:
        return self._with_recovery(
            lambda: self._bot.scroll(self._session_id, amount=amount, direction=direction, xpath=xpath)
        )

    # -- Trusted-input primitives (session-bound; see ScraperBot for docs) --

    def trusted_type(self, text: str, **kw) -> dict:
        return self._with_recovery(lambda: self._bot.trusted_type(self._session_id, text, **kw))

    def click_text(self, text: str = None, **kw) -> dict:
        return self._with_recovery(lambda: self._bot.click_text(self._session_id, text, **kw))

    def click_point(self, x: float, y: float, **kw) -> dict:
        return self._with_recovery(lambda: self._bot.click_point(self._session_id, x, y, **kw))

    def select_option(self, **kw) -> dict:
        return self._with_recovery(lambda: self._bot.select_option(self._session_id, **kw))

    def wait_for_item(self, **kw) -> dict:
        return self._with_recovery(lambda: self._bot.wait_for_item(self._session_id, **kw))

    def overlay_check(self, **kw) -> dict:
        return self._with_recovery(lambda: self._bot.overlay_check(self._session_id, **kw))

    def html(self) -> str:
        return self._with_recovery(lambda: self._bot.html(self._session_id))

    def url(self) -> str:
        return self._with_recovery(lambda: self._bot.url(self._session_id))

    def title(self) -> str:
        return self._with_recovery(lambda: self._bot.title(self._session_id))

    def back(self) -> dict:
        return self._with_recovery(lambda: self._bot.back(self._session_id))

    def forward(self) -> dict:
        return self._with_recovery(lambda: self._bot.forward(self._session_id))

    def refresh(self) -> dict:
        return self._with_recovery(lambda: self._bot.refresh(self._session_id))

    def screenshot_base64(self) -> str:
        return self._with_recovery(lambda: self._bot.screenshot_base64(self._session_id))

    def cookies(self) -> dict:
        return self._with_recovery(lambda: self._bot.cookies(self._session_id))

    def select(self, id: str, value: str) -> dict:
        return self._with_recovery(lambda: self._bot.select(self._session_id, id, value))

    def iframe(self, xpath: str = None, index: int = None, main: bool = False) -> dict:
        return self._with_recovery(
            lambda: self._bot.iframe(self._session_id, xpath=xpath, index=index, main=main)
        )

    # -- Network --

    def enable_network(self) -> dict:
        return self._with_recovery(lambda: self._bot.enable_network(self._session_id))

    def clear_network(self) -> dict:
        return self._with_recovery(lambda: self._bot.clear_network(self._session_id))

    def network_filter(self, url_contains: str = None, only_xhr: bool = False) -> list:
        return self._with_recovery(
            lambda: self._bot.network_filter(self._session_id, url_contains=url_contains, only_xhr=only_xhr)
        )

    def network_requests(self, only_xhr: bool = False) -> list:
        return self._with_recovery(lambda: self._bot.network_requests(self._session_id, only_xhr=only_xhr))

    # -- reCAPTCHA --

    def solve_recaptcha(self, max_attempts: int = 3, parent_iframe_xpath: str = None) -> bool:
        return self._with_recovery(lambda: self._bot.solve_recaptcha(
            self._session_id, max_attempts=max_attempts, parent_iframe_xpath=parent_iframe_xpath))
