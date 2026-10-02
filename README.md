# scraper-bot

A local HTTP server with persistent Chrome sessions. Agents can navigate, read
HTML, extract data with JavaScript, click, type, take screenshots, and close
sessions through the Python client or HTTP API. Sessions are tracked in SQLite.

## macOS setup

Install Python 3.11 or newer and Google Chrome, then from this checkout:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python server.py --host 127.0.0.1 --port 9020
```

Run headful sessions from a logged-in desktop terminal. Headless sessions do not
need a visible desktop. The server prevents idle sleep on macOS while it runs.

## WSL / Linux setup

Use WSL Ubuntu with Python 3.11 or newer and its venv support, and install Google
Chrome or Chromium **inside Linux**. Follow the same venv commands above. For
headful sessions enable WSLg or configure a desktop display. Headless mode is
automatic when Linux/WSL has neither `DISPLAY` nor `WAYLAND_DISPLAY`. Pass
`headless=False` to explicitly request headful, or `headless=True` to force headless. When Chromium is outside its standard location, set
`SCRAPERBOT_CHROME_BINARY` to its executable path for Selenium sessions.

Selenium Manager downloads the matching driver on first use (network access
required). The default backend uses undetected-chromedriver. Set
`undetected=False` for standard Selenium.

Platform notes: UC is best-effort for stealth and automatically falls back to
standard Selenium when it cannot start a usable session, including bleeding-edge
Chrome versions such as Chrome 149 headless on Linux. The scrape journey continues
through Selenium with the same requested options. Both Selenium option branches add
`--no-sandbox` and `--disable-dev-shm-usage` on Linux for WSL, containers and CI;
Chrome sandbox isolation is disabled there. macOS retains its headful default
and does not receive these Linux flags. ARM macOS
automatically falls back to standard Selenium with a log message because upstream
UC downloads an x86 driver (Errno 86). Both use the local public
`browser_manager.py` adapter. Optional Playwright sessions use installed Chrome;
for its bundled Chromium instead, run `python -m playwright install chromium`
and set `SCRAPERBOT_PLAYWRIGHT_CHANNEL` to an empty string. Run Playwright browser
installation inside this checkout's activated venv.

## Storage and API

Storage defaults to `~/Library/Application Support/scraper-bot` on macOS,
`${XDG_DATA_HOME:-$HOME/.local/share}/scraper-bot` on Linux/WSL, and
`C:\scraper-bot` on native Windows. Set `SCRAPERBOT_DATA_DIR` before starting the
server **and client** to override it. The directory is created automatically.
Other overrides: `SCRAPERBOT_DB_PATH`, `SCRAPERBOT_LOG_PATH`,
`SCRAPERBOT_PROFILES_ROOT`, and `SCRAPERBOT_DASHBOARD_TOKEN_PATH`.

On Linux/WSL, keep the data/profile path (and `TMPDIR`) reasonably short. Chrome
creates a per-profile `SingletonSocket` as a Unix domain socket under the profile
directory, and a path beyond the ~108-character socket limit makes Chrome abort at
startup with `session not created: Chrome instance exited`. The default locations
are short; avoid pointing `SCRAPERBOT_DATA_DIR` / `SCRAPERBOT_PROFILES_ROOT` at a
deeply nested directory.
Keep browser profiles, databases and dashboard tokens out of Git.

Bind to loopback. The local API controls a browser; exposing it to a network
requires your own authentication and access controls. Destructive requests use
the generated `X-Scraper-Token`; the Python client loads it from local storage.
See `/docs` for the endpoint schemas and [TRUSTED_INPUT.md](TRUSTED_INPUT.md) for
CDP input primitives. Browser process cleanup is scoped to this server's children
and configured profile root so separate instances can coexist.

```python
from client import ScraperBot
bot = ScraperBot("http://127.0.0.1:9020")
session = bot.create_session(headless=True, undetected=False)
try:
    bot.navigate(session, "https://example.com")
    print(bot.execute(session, "return document.title"))
finally:
    bot.close(session)
```

The optional `/solve_recaptcha` integration is not included. It returns an
explicit error unless an external compatible solver is supplied through
`LAND_BOT_PATH`; normal scraping has no dependency on that extension.

## Validation

```sh
python -m pytest tests -q
python scripts/scrape_journey.py --base-url http://127.0.0.1:9020 --output _artifacts/journey.json
```

The journey serves a local catalog, starts a real Chrome session, navigates,
extracts rows, verifies values, and closes it. Use a separate port and data
directory when another server is running. The proof includes session identity,
extracted rows and terminal state. Runtime evidence in `_artifacts/` is excluded
from public source archives.

MIT licensed; see [LICENSE](LICENSE) and [THIRD_PARTY_NOTICES](THIRD_PARTY_NOTICES).
