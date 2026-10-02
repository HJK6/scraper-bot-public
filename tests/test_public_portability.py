"""Observable storage and process-ownership boundaries for public instances."""
from unittest import mock
import server


def test_db_creates_configured_parent(tmp_path, monkeypatch):
    db = tmp_path / 'new' / 'storage' / 'sessions.db'
    monkeypatch.setattr(server, 'DB_PATH', str(db))
    conn = server._open_db()
    try:
        assert db.is_file()
        assert conn.execute('select count(*) from sessions').fetchone()[0] == 0
    finally:
        conn.close()


def test_orphan_scan_excludes_other_instances(tmp_path, monkeypatch):
    root = tmp_path / 'our-profiles'
    monkeypatch.setattr(server, 'PROFILES_ROOT', str(root))
    own = server.MachineWideChromeCandidate(101, 1, 'chrome --user-data-dir=own', str(root / 'own'))
    other = server.MachineWideChromeCandidate(102, 1, 'chrome --user-data-dir=other', str(tmp_path / 'other-profiles'))
    commands = [(101,1,'chrome AutomationControlled --user-data-dir=own'), (102,1,'chrome AutomationControlled --user-data-dir=other'), (103,1,'chromedriver')]
    with mock.patch.object(server,'_iter_machine_process_commands',return_value=commands), mock.patch.object(server,'_match_machine_wide_uc_orphan',side_effect=[own,other]), mock.patch.object(server,'_match_machine_wide_chromedriver_orphan',return_value=server.MachineWideChromeCandidate(103,1,'chromedriver','')):
        assert server._get_machine_wide_uc_orphan_candidates(managed_pids=set(),active_user_data_dirs=set()) == {101:own}


def test_profile_root_boundary(tmp_path, monkeypatch):
    root = tmp_path / 'profiles'
    monkeypatch.setattr(server,'PROFILES_ROOT',str(root))
    assert server._is_under_profiles_root(str(root / 'owned'))
    assert not server._is_under_profiles_root(str(root))
    assert not server._is_under_profiles_root(str(tmp_path / 'profiles-other' / 'foreign'))
