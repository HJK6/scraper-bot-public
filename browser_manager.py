"""Small public Selenium adapter for the server's browser operations.

Uses public Selenium or undetected-chromedriver; no external source checkout.
"""
from __future__ import annotations
import json
import logging
import platform
import sys
import os
import tempfile
import threading
import psutil
from pathlib import Path
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait
from selenium.webdriver.support import expected_conditions as EC


def resolve_headless(headless: bool | None) -> bool:
    """Resolve an omitted mode on the browser host; False explicitly requests headful."""
    if headless is not None:
        return headless
    if sys.platform.startswith("linux"):
        from platform_linux import LinuxAdapter
        available, _ = LinuxAdapter().headful_display_check()
        return not available
    return False


_UC_START_LOCK = threading.Lock()


def _cleanup_failed_uc(driver, profile, previous_processes):
    """Stop only this profile's Chrome tree and newly spawned UC driver children."""
    owned = {}
    profile_in_use = False
    children = psutil.Process().children(recursive=True)
    child_pids = {p.pid for p in children} - previous_processes
    for process in psutil.process_iter(['pid', 'cmdline', 'name']):
        try:
            args = process.info['cmdline'] or []
            profile_arg = f"--user-data-dir={profile}"
            if profile_arg in args and process.pid in previous_processes:
                profile_in_use = True
            uc_child = process.pid in child_pids and args and 'undetected_chromedriver' in Path(args[0]).name
            if process.pid not in previous_processes and (profile_arg in args or uc_child):
                owned[process.pid] = process
                owned.update({p.pid: p for p in process.children(recursive=True)})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # Kill broken sessions directly: quit() can repeat the failing HTTP timeout.
    for process in owned.values():
        try:
            process.terminate()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(list(owned.values()), timeout=3)
    for process in alive:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(alive, timeout=3)
    if alive:
        raise RuntimeError("Failed UC processes are still running; refusing to reuse profile")
    if driver is not None:
        service = getattr(driver, 'service', None)
        if service is not None:
            service.stop()
    if profile_in_use:
        raise RuntimeError('Profile is already in use by another process; refusing to remove its locks')
    for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
        (Path(profile) / name).unlink(missing_ok=True)


class DriverManager:
    def __init__(self, *, undetected=True, headless=None, chrome_version_main=None,
                 view="desktop", user_data_dir=None):
        if undetected and sys.platform == "darwin" and platform.machine().lower() in {"arm64", "aarch64"}:
            logging.getLogger("scraper-bot").info(
                "ARM macOS: using standard Selenium; upstream UC downloads an x86 driver (Errno 86)."
            )
            undetected = False
        self._network = []
        self._network_enabled = False
        self._temporary_profile = None
        if undetected and not user_data_dir:
            self._temporary_profile = tempfile.TemporaryDirectory(prefix="scraper-bot-uc-")
            user_data_dir = self._temporary_profile.name
        profile = str(Path(user_data_dir).resolve()) if user_data_dir else None
        resolved_headless = resolve_headless(headless)

        def make_options(factory):
            options = factory()
            options.add_argument("--window-size=390,844" if view == "mobile" else "--window-size=1440,1000")
            options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
            if profile:
                options.add_argument(f"--user-data-dir={profile}")
            if sys.platform.startswith("linux"):
                options.add_argument("--no-sandbox")
                options.add_argument("--disable-dev-shm-usage")
            if resolved_headless:
                options.add_argument("--headless=new")
            if os.environ.get("SCRAPERBOT_CHROME_BINARY"):
                options.binary_location = os.environ["SCRAPERBOT_CHROME_BINARY"]
            return options

        self.driver = None
        if undetected:
            with _UC_START_LOCK:
                previous_processes = {p.pid for p in psutil.process_iter()}
                try:
                    import undetected_chromedriver as uc
                    self.driver = uc.Chrome(options=make_options(uc.ChromeOptions),
                                            version_main=chrome_version_main, use_subprocess=True)
                    if not self.driver.session_id:
                        raise RuntimeError("UC did not establish a session")
                    self.driver.execute_script("return 1")
                    self.driver.set_page_load_timeout(60)
                    return
                except Exception:
                    logging.getLogger("scraper-bot").warning(
                        "undetected-chromedriver failed to start a session (upstream/Chrome-version issue); "
                        "falling back to standard Selenium"
                    )
                    _cleanup_failed_uc(self.driver, profile, previous_processes)
        # UC mutates its options; rebuild the same requested options for Selenium.
        self.driver = webdriver.Chrome(options=make_options(webdriver.ChromeOptions))
        self.driver.set_page_load_timeout(60)

    def close(self):
        try:
            self.driver.quit()
        finally:
            if self._temporary_profile is not None:
                self._temporary_profile.cleanup()

    def get(self, url):
        self.driver.get(url)

    def get_current_url(self):
        return self.driver.current_url

    def get_page_source(self):
        return self.driver.page_source

    def execute_script(self, script, *args):
        return self.driver.execute_script(script, *args)

    def find_element_by_xpath(self, xpath):
        return self.driver.find_element(By.XPATH, xpath)

    def find_elements_by_xpath(self, xpath):
        return self.driver.find_elements(By.XPATH, xpath)

    def wait_for_selector(self, *, xpath=None, css=None, id_=None, timeout=10):
        by, value = (By.XPATH, xpath) if xpath else ((By.CSS_SELECTOR, css) if css else (By.ID, id_))
        if not value:
            raise ValueError("Provide xpath, css, or id")
        return WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located((by, value)))

    def scroll_to_view(self, element):
        self.driver.execute_script("arguments[0].scrollIntoView({block:'center'})", element)

    def scroll_by(self, amount):
        self.driver.execute_script("window.scrollBy(0, arguments[0])", amount)

    def select_by_value(self, id_, value):
        Select(self.driver.find_element(By.ID, id_)).select_by_value(value)

    def switch_to_main(self):
        self.driver.switch_to.default_content()

    def switch_to_iframe(self, iframe):
        self.driver.switch_to.frame(iframe)

    def get_browser_cookies(self):
        return self.driver.get_cookies()

    def enable_network_logging(self):
        self._network_enabled = True
        self.driver.get_log("performance")

    def get_network_requests(self, only_xhr=False):
        if self._network_enabled:
            for entry in self.driver.get_log("performance"):
                message = json.loads(entry["message"])["message"]
                if message["method"] == "Network.requestWillBeSent":
                    params = message["params"]
                    self._network.append({**params["request"], "resource_type": params.get("type", "").lower()})
        return [r for r in self._network if not only_xhr or r["resource_type"] in {"xhr", "fetch"}]

    def get_network_traffic(self):
        requests = self.get_network_requests()
        return {"requests": requests, "count": len(requests)}

    def clear_network(self):
        self.driver.get_log("performance")
        self._network.clear()
