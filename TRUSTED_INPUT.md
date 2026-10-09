# Trusted-input primitives

`trusted_input.py` + the `/sessions/{id}/input/*` endpoints add the interaction
capabilities the existing `/click`, `/type` and `/execute` cannot provide, driven by
evidence from the PropStream and PropertyRadar automation failures.

They are **added alongside** the existing endpoints — `execute_js`, `click`, `type`
retain their interaction behavior. Fill diagnostics follow the confidentiality rules below.

## What was actually broken (and what wasn't)

- **Clicks were never the problem.** `/click` uses Selenium `el.click()`, already a
  native, `isTrusted=true` event. Do not reach for a "trusted click" to fix a click.
- **Typing was the problem.** Setting `el.value = x` + dispatching an `input` event
  does **not** drive framework filters: React's value-tracker ignores programmatic
  sets (react-select never filters), and ExtJS combos won't render their suggestion
  list on programmatic typing. **`/input/type` sends real key events** (CDP
  `Input.dispatchKeyEvent` / `insertText`) so the framework reacts as to a human.
- **Id-less targeting.** PropertyRadar picker items (`.x-list-plain`) have no ids, so
  a locator only reaches the container. **`/input/click_text`** resolves a specific
  element by visible text within a scope; **`/input/click_point`** clicks a raw
  coordinate.
- **Overlay interception is real backend instability, not our tooling.**
  **`/input/overlay_check`** detects a "server is in process of restart" overlay
  covering a target *before* acting, and names the covering element — so a block is
  attributed honestly instead of surfacing as a mid-sequence click-intercepted
  exception. It detects; it does not fix the backend.
- **Virtualized lists render late.** **`/input/wait_for`** polls for the item to be
  present before you read/click it.

## Honest failure attribution

Every response — success or failure — records **which primitive ran** (`primitive`).
On refusal the client raises `ScraperBotInputError` with a structured `reason`,
the structured `detail` (content-free for fills), and the originating HTTP status (`status_code`, with stable
`status` alias):

| reason | meaning | attribute to |
| --- | --- | --- |
| `covered_by_overlay` | an overlay sits on the point | **site/backend** (e.g. restart dialog) |
| `element_not_found` / `item_not_found` | locator/text matched nothing | our call or the page |
| `element_not_visible` / `out_of_viewport` | matched but not interactable | our call or the page |
| `effect_not_observed` | action dispatched, expected post-condition never met | our call or the page |
| `not_editable` | typing target isn't an input/textarea/contenteditable | our call |
| `backend_unsupported` | session backend has no CDP Input | config (use chromedriver) |

A primitive that cannot tell success from silent failure is exactly the defect being
fixed — so these never silently no-op.

## Client usage (`client.py`)

```python
from client import ScraperBot, ScraperBotInputError
bot = ScraperBot("http://localhost:9020")
sid = ...  # existing session

# Trusted typing filters a react-select / ExtJS combo:
bot.trusted_type(sid, "Florida", css="input.react-select__input",
                 expect={"kind": "js", "js": "return document.querySelectorAll('.option').length>0;"})

# Id-less ExtJS combo end-to-end, encoding the LIVE PropertyRadar recipe:
#   focus input -> TYPE the option (this runs the store-query, NOT the trigger)
#   -> settle ~3s (async, inconsistent render) -> wait x-mask clear -> wait item
#   -> tag + native click -> read-back verify scoped to the committed chips.
bot.select_option(sid, input_css=".x-form-field",   # stable class: the input id changes per selection
                  item_text="Florida",
                  item_scope_css=".x-list-plain", item_tag="li",
                  settle_ms=3000, mask_css=".x-mask",
                  verify_scope_css="div.fr-criteria-editor:not(.x-hide-display)",
                  verify_value_css="input[readonly]") # child values only, relative to one scope

# Detect a restart overlay before applying a saved search:
if bot.overlay_check(sid, css="#applyBtn")["covered"]:
    ...  # backend is restarting — back off, don't click blind
bot.click_text(sid, "Apply Saved Search",
               expect={"kind": "text", "scope": "#status", "text": "applied"})

try:
    bot.click_text(sid, "Georgia", scope_css=".x-list-plain", exact=True)
except ScraperBotInputError as e:
    if e.reason == "covered_by_overlay":
        ...  # site condition
    elif e.reason == "element_not_found":
        ...  # our locator / timing
```

`expect` post-conditions: `{"kind":"appear","css":...}`, `{"kind":"disappear","css":...}`,
`{"kind":"text","scope":css,"text":...}`, `{"kind":"value","css":...,"text":...}`,
`{"kind":"js","js":"return <bool>;"}`.

## The id-less combo recipe (why `select_option` is shaped this way)

`select_option` encodes the recipe the PropertyRadar lane proved on the live ExtJS
State picker after ~15 other approaches failed:

1. **Focus the input with a native (trusted) click** — a dispatched focus is swallowed.
2. **TYPE the option text into the input** (`type_value`, default `item_text`),
   `clear_first`. Typing — *not* the trigger — runs the async store-query; clicking
   the trigger only opens the id-less container and selects nothing. So the trigger
   is skipped by default (`open_via_trigger=False`).
3. **Settle** (`settle_ms` ~3000): the render is async and inconsistent, and a <2s
   wait misses it. Then wait for the ExtJS **x-mask** (`mask_css`) to clear.
4. **Wait** for the option to render, resolve it by **exact visible text**, **JS-tag**
   it, and **native-click** it.
5. **Verify by reading the committed selection back** from `verify_scope_css`, which
   must now contain the text **and have changed**. Scope this to the committed
   chips/tagfield — a verify scoped to the whole document would **false-pass**,
   because the option text is already present in the dropdown and the input. The
   input id also changes after each selection, so locate the input by a stable class.
   Some ExtJS `vCsvItem` chips render their selected label only in a child textfield
   `value`; their `textContent` can be just a delete glyph. For that form, pass a
   unique visible outer scope (for the live PR form,
   `div.fr-criteria-editor:not(.x-hide-display)`) plus `verify_value_css` as a
   selector **relative to** that scope (the live committed-only selector is
   `input[readonly]`). The baseline and post-pick read-back then use only those child
   values, so neither the editable combo input nor external floating dropdown can
   false-pass. This mode rejects a non-unique scope and more than one committed value
   matching the expected label.
   `verify_scope_css` is mandatory; text selection defaults `verify_text` to
   `item_text`, while index selection must supply `verify_text` explicitly. There
   is no successful unverified `select_option` path.

## Limitations

- **Current/top frame only.** The resolver uses `document.querySelector` /
  `elementFromPoint` on the current frame; it does not switch into iframes. Elements
  inside an iframe are unreachable (they fail loudly as `element_not_found`), and a
  `click_point` `hit` inside an iframe reports the IFRAME element, not the inner
  content. If a target is in an iframe, `switch_to.frame` (via the existing session)
  before calling these primitives.
- **Value-transforming fields.** The default value-reflect verify checks the field's
  own value contains the typed text; fields that mask/format input (phone, date) can
  fail this even when typing worked. Pass an explicit `expect` (or `verify=False`)
  for those. `require_focus=True` turns an unconfirmed focus into a loud error rather
  than typing into the wrong element.

## Backend

Requires the **chromedriver** backend (undetected_chromedriver, which exposes
`execute_cdp_cmd`). Element/text clicks work on any Selenium driver; typing and raw
coordinate clicks need CDP. A non-CDP backend fails loudly with `backend_unsupported`.

## Tests

`tests/test_trusted_input.py` (module, real browser), `tests/test_trusted_input_endpoints.py`
(endpoint path, real browser), `tests/test_client_input.py` (client error mapping, no
browser). The browser tests skip cleanly where Chrome is unavailable. The fixture
`tests/fixtures/trusted_input_fixture.html` reproduces both defects faithfully: a
react-select value-tracker, an id-less ExtJS combo, and a click-intercepting overlay.


## Confidential fill diagnostics

Every value passed to `type`, `trusted_type`, or `select_option` is confidential by
default. No flag is required. The shared fill boundary returns only stable reason,
primitive, HTTP status, fixed messages and non-content success flags. It omits
submitted/readback/expectation values, target/cover descriptions, and selected or
typed strings. Verification still detects mismatches, unconfirmed focus and
missing or ambiguous committed selections; a safe error never means success.
Legacy `type` retains its existing send-keys behavior without adding verification.

Raw browser/transport exceptions and validation bodies are replaced with safe
errors. Client errors also protect against older or malformed server responses;
unknown reason/primitive strings and nested detail are discarded. Fill-scoped
library DEBUG records omit request/readback/exception content. When distributing
`client.py` separately, include `fill_diagnostics.py` beside it.

Fills refuse before dispatch on a session with automatic tracing or enabled
network recording, including URL/query recording. Create an untraced session for
confidential fills. CDP fills disable default Network/performance event recording
before editing; they never collect buffered logs. A later explicit network-enable
request may resume capture, so use it only with suitable data. The fill contract
covers `/type`, `/input/type` and the entire `/input/select_option` operation.
Explicit screenshots, HTML, JavaScript/debugger reads, the `prove_trusted` key
probe and native `/select` are separate APIs and retain their existing behavior.
Browser state may hold input values; these rules protect fill diagnostics.

Focused validation: `python -m pytest tests/test_fill_confidential.py -q`.
Actual isolated HTTP/client/Chrome validation:
`python tests/run_fill_http_journey.py`. It uses a synthetic truncating form,
temporary data/profile and its own server with global maintenance disabled;
its receipt must show four safe mismatch/success cases, absent automatic query
capture and complete owned-session/server cleanup. Use the supported Python and
provisioned browser dependencies. No real credentials, cards or profiles belong
in these checks.
