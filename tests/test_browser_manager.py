from types import SimpleNamespace
from unittest import mock
import browser_manager as bm
import pytest


@pytest.fixture(autouse=True)
def isolated_process_inventory():
    with mock.patch.object(bm.psutil, "Process") as process, mock.patch.object(bm.psutil, "process_iter", return_value=[]):
        process.return_value.children.return_value = []
        yield


def test_arm_mac_automatically_uses_standard_selenium(monkeypatch, caplog):
    monkeypatch.setattr(bm.sys, 'platform', 'darwin')
    monkeypatch.setattr(bm.platform, 'machine', lambda: 'arm64')
    with mock.patch.object(bm.webdriver, 'Chrome') as chrome, caplog.at_level('INFO'):
        manager = bm.DriverManager(undetected=True, headless=True)
        assert manager.driver is chrome.return_value
        assert 'Errno 86' in caplog.text
        chrome.return_value.set_page_load_timeout.assert_called_once_with(60)


def test_x86_linux_retains_uc(monkeypatch):
    monkeypatch.setattr(bm.sys, 'platform', 'linux')
    monkeypatch.setattr(bm.platform, 'machine', lambda: 'x86_64')
    uc = SimpleNamespace(ChromeOptions=bm.webdriver.ChromeOptions, Chrome=mock.Mock())
    with mock.patch.dict('sys.modules', {'undetected_chromedriver': uc}), mock.patch.object(bm.webdriver,'Chrome') as standard:
        manager = bm.DriverManager(undetected=True, headless=True, chrome_version_main=149)
        standard.assert_not_called()
        assert manager.driver is uc.Chrome.return_value
        assert uc.Chrome.call_args.kwargs['version_main'] == 149


def test_explicit_standard_choice_on_linux(monkeypatch):
    monkeypatch.setattr(bm.sys, 'platform', 'linux')
    with mock.patch.object(bm.webdriver,'Chrome') as chrome:
        bm.DriverManager(undetected=False)
        chrome.assert_called_once()


def test_linux_options_and_automatic_headless(monkeypatch):
    import platform_linux
    monkeypatch.setattr(bm.sys, 'platform', 'linux')
    monkeypatch.setattr(platform_linux.LinuxAdapter, 'headful_display_check', lambda self: (False, 'no display'))
    uc = SimpleNamespace(ChromeOptions=bm.webdriver.ChromeOptions, Chrome=mock.Mock())
    with mock.patch.dict('sys.modules', {'undetected_chromedriver': uc}), mock.patch.object(bm.webdriver, 'Chrome') as standard:
        for use_uc, factory in [(True, uc.Chrome), (False, standard)]:
            bm.DriverManager(undetected=use_uc)
            args = factory.call_args.kwargs['options'].arguments
            assert '--no-sandbox' in args
            assert '--disable-dev-shm-usage' in args
            assert '--headless=new' in args
            bm.DriverManager(undetected=use_uc, headless=False)
            assert '--headless=new' not in factory.call_args.kwargs['options'].arguments


def test_display_and_mac_defaults(monkeypatch):
    import platform_linux
    monkeypatch.setattr(platform_linux.LinuxAdapter, 'headful_display_check', lambda self: (True, 'display'))
    for os_name in ['linux', 'darwin']:
        monkeypatch.setattr(bm.sys, 'platform', os_name)
        with mock.patch.object(bm.webdriver, 'Chrome') as chrome:
            bm.DriverManager(undetected=False)
            args = chrome.call_args.kwargs['options'].arguments
            assert '--headless=new' not in args
            if os_name == 'darwin':
                assert '--no-sandbox' not in args
                assert '--disable-dev-shm-usage' not in args
            bm.DriverManager(undetected=False, headless=True)
            assert '--headless=new' in chrome.call_args.kwargs['options'].arguments


def test_client_preserves_automatic_and_explicit_modes():
    from client import ScraperBot, ScrapeJob
    bot = ScraperBot()
    with mock.patch.object(bot, '_post', return_value={'session_id': 'test'}) as post:
        for requested in [None, False, True]:
            kwargs = {} if requested is None else {'headless': requested}
            bot.create_session(**kwargs)
            assert post.call_args.args[1]['headless'] is requested
            assert ScrapeJob(**kwargs)._chrome_kwargs['headless'] is requested


def test_server_default_mode_can_resolve_without_display(monkeypatch):
    import server
    import platform_linux
    monkeypatch.setattr(bm.sys, 'platform', 'linux')
    monkeypatch.setattr(platform_linux.LinuxAdapter, 'headful_display_check', lambda self: (False, 'no display'))
    assert bm.resolve_headless(server.CreateSessionRequest().headless) is True
    assert bm.resolve_headless(server.CreateSessionRequest(headless=False).headless) is False


def test_uc_failure_falls_back_with_same_linux_options(monkeypatch, tmp_path, caplog):
    import platform_linux
    monkeypatch.setattr(bm.sys, 'platform', 'linux')
    monkeypatch.setattr(platform_linux.LinuxAdapter, 'headful_display_check', lambda self: (False, 'no display'))
    monkeypatch.setenv('SCRAPERBOT_CHROME_BINARY', '/custom/chrome')
    uc = SimpleNamespace(ChromeOptions=bm.webdriver.ChromeOptions, Chrome=mock.Mock(side_effect=RuntimeError('UC startup failed')))
    with mock.patch.dict('sys.modules', {'undetected_chromedriver': uc}), mock.patch.object(bm.webdriver, 'Chrome') as standard, caplog.at_level('WARNING'):
        manager = bm.DriverManager(user_data_dir=tmp_path, view='mobile')
        assert manager.driver is standard.return_value
        standard.assert_called_once()
        args = standard.call_args.kwargs['options'].arguments
        assert set(args) == {'--window-size=390,844', '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', f'--user-data-dir={tmp_path}'}
        options = standard.call_args.kwargs['options']
        assert options.binary_location == '/custom/chrome'
        assert options.capabilities['goog:loggingPrefs'] == {'performance': 'ALL'}
        assert len([r for r in caplog.records if 'falling back to standard Selenium' in r.message]) == 1


@pytest.mark.parametrize("failure", ["missing_session", "unusable_session"])
def test_uc_unusable_session_falls_back(monkeypatch, tmp_path, failure):
    monkeypatch.setattr(bm.sys, 'platform', 'linux')
    driver = mock.Mock(session_id=None if failure == 'missing_session' else 'session')
    if failure == 'unusable_session':
        driver.execute_script.side_effect = RuntimeError('connection closed')
    uc = SimpleNamespace(ChromeOptions=bm.webdriver.ChromeOptions, Chrome=mock.Mock(return_value=driver))
    with mock.patch.dict('sys.modules', {'undetected_chromedriver': uc}), mock.patch.object(bm.webdriver, 'Chrome') as standard:
        manager = bm.DriverManager(headless=True, user_data_dir=tmp_path)
        assert manager.driver is standard.return_value
        driver.service.stop.assert_called_once()
        standard.assert_called_once()


def test_standard_failure_is_raised_without_retry(monkeypatch, tmp_path):
    monkeypatch.setattr(bm.sys, 'platform', 'linux')
    uc = SimpleNamespace(ChromeOptions=bm.webdriver.ChromeOptions, Chrome=mock.Mock(side_effect=RuntimeError('UC failed')))
    with mock.patch.dict('sys.modules', {'undetected_chromedriver': uc}), mock.patch.object(bm.webdriver, 'Chrome', side_effect=RuntimeError('standard failed')) as standard:
        with pytest.raises(RuntimeError, match='standard failed'):
            bm.DriverManager(headless=True, user_data_dir=tmp_path)
        uc.Chrome.assert_called_once()
        standard.assert_called_once()


def test_failed_uc_cleanup_preserves_foreign_process_and_removes_locks(tmp_path):
    owned = mock.Mock(pid=10, info={'cmdline': ['chrome', f'--user-data-dir={tmp_path}']})
    owned.children.return_value = []
    uc_driver = mock.Mock(pid=11, info={'cmdline': ['/drivers/undetected_chromedriver', '--port=123']})
    uc_driver.children.return_value = []
    foreign = mock.Mock(pid=12, info={'cmdline': ['chrome', '--user-data-dir=/other/profile']})
    for name in ('SingletonLock', 'SingletonSocket', 'SingletonCookie'):
        (tmp_path / name).symlink_to('/nonexistent-target')
    with mock.patch.object(bm.psutil, 'process_iter', return_value=[owned, uc_driver, foreign]), mock.patch.object(bm.psutil, 'Process') as current, mock.patch.object(bm.psutil, 'wait_procs', return_value=([], [])):
        current.return_value.children.return_value = [uc_driver]
        bm._cleanup_failed_uc(None, str(tmp_path), {12})
    owned.terminate.assert_called_once()
    uc_driver.terminate.assert_called_once()
    foreign.terminate.assert_not_called()
    assert not any(p.is_symlink() for p in tmp_path.iterdir())



def test_failed_uc_cleanup_preserves_preexisting_profile_locks(tmp_path):
    existing = mock.Mock(pid=12, info={'cmdline': ['chrome', f'--user-data-dir={tmp_path}']})
    lock = tmp_path / 'SingletonLock'
    lock.symlink_to('/nonexistent-target')
    with mock.patch.object(bm.psutil, 'process_iter', return_value=[existing]):
        with pytest.raises(RuntimeError, match='already in use'):
            bm._cleanup_failed_uc(None, str(tmp_path), {12})
    existing.terminate.assert_not_called()
    assert lock.is_symlink()
