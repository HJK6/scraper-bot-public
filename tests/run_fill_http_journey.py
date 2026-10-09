"""Isolated real HTTP/client/Chrome journey. Never use a production session/profile.

Invoke with the repository's supported Python. Lifespan is disabled to prevent
machine-wide maintenance; real session creation/actions/close and DB are retained.
"""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import client as api

SAMPLES = [('pan', '000000000000000'), ('cvv', '963'),
           ('password', 'synthetic-password-private'), ('totp', '963852')]
HTML = '<input id="short" maxlength="2"><input id="plain">'



def close_owned(bot, session, process, receipt):
    """A close failure stays failed, but never skips owned server teardown."""
    try:
        if session:
            bot.close(session)
            receipt['owned_sessions_closed'] = 1
    except Exception as error:
        receipt['session_cleanup_failure'] = type(error).__name__
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        receipt['server_stopped'] = process.poll() is not None

def main():
    with tempfile.TemporaryDirectory(prefix='fill-http-journey-') as data:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = dict(os.environ, SCRAPERBOT_DATA_DIR=data, SCRAPERBOT_DB_PATH=str(Path(data)/'sessions.db'),
                   SCRAPERBOT_LOG_PATH=str(Path(data)/'server.log'))
        # No lifespan means no production census, reapers, reconcile or power assertions.
        startup = ('import server,uvicorn,json; '
                   'server._db_conn=server._open_db(); '
                   'server._dashboard_token="synthetic-test-token"; '
                   '\n@server.app.get("/synthetic-capture-check/{session_id}")\n'
                   'def capture_check(session_id):\n'
                   '    entries=server._sessions[session_id].dm.driver.get_log("performance")\n'
                   '    captured=any("/synthetic-capture?value=" in entry.get("message","") for entry in entries)\n'
                   '    return {"automatic_query_capture_absent":not captured}\n'
                   f'uvicorn.run(server.app,host="127.0.0.1",port={port},lifespan="off",log_level="warning",access_log=False)')
        with open(Path(data)/'process.log', 'w+') as logs:
            process = subprocess.Popen([sys.executable, '-c', startup], cwd=ROOT, env=env,
                                       stdout=logs, stderr=subprocess.STDOUT)
            bot = api.ScraperBot(f'http://127.0.0.1:{port}')
            api._cached_token = 'synthetic-test-token'
            session = None
            receipt = {'synthetic_only': True, 'isolated_http': True, 'cases': [], 'owned_sessions_expected': 1,
                       'owned_sessions_closed': 0, 'server_stopped': False}
            try:
                for _ in range(100):
                    if process.poll() is not None:
                        raise RuntimeError('isolated server stopped before ready')
                    try:
                        if requests.get(bot.base+'/health', timeout=1).ok:
                            break
                    except requests.RequestException:
                        pass
                    time.sleep(.1)
                else:
                    raise RuntimeError('isolated server startup timeout')
                session = bot.create_session(headless=True, name='Synthetic fill journey', owner='test-harness',
                                             user_data_dir=str(Path(data)/'profile'))
                html = HTML.replace('<input ', '<input oninput="fetch(\'http://127.0.0.1:' + str(port) + '/synthetic-capture?value=\'+encodeURIComponent(this.value))" ')
                bot.navigate(session, 'data:text/html,' + quote(html))
                for label, value in SAMPLES:
                    r = requests.post(bot.base+f'/sessions/{session}/input/type', json={
                        'text': value, 'css': '#short', 'clear_first': True, 'mode': 'send_keys'}, timeout=20)
                    helper_safe = value not in r.text and value[:2] not in json.dumps(r.json().get('detail', {}))
                    try:
                        bot.trusted_type(session, value, css='#short', clear_first=True, mode='send_keys')
                    except api.ScraperBotInputError as error:
                        surfaces = str(error)+repr(error)+repr(error.args)+str(error.detail)+''.join(traceback.format_exception(error))
                        client_safe = value not in surfaces and value[:2] not in str(error.detail)
                        mismatch_detected = error.reason == 'effect_not_observed' and error.status_code == 409
                    else:
                        client_safe = mismatch_detected = False
                    success = bot.trusted_type(session, value, css='#plain', clear_first=True, mode='send_keys')
                    receipt['cases'].append({'class': label, 'http_safe': helper_safe, 'client_safe': client_safe,
                                             'mismatch_detected': mismatch_detected,
                                             'success_verified': success.get('verified') is True,
                                             'success_safe': value not in str(success)})
                receipt['selection_cases'] = []
                for label, value in SAMPLES:
                    selection_html = ('<button id="trigger" onclick="document.querySelector(\'#items\').style.display=\'block\';'
                                      'fetch(\'http://127.0.0.1:' + str(port) + '/synthetic-capture?value=' + value + '\')">Open</button>'
                                      '<ul id="items" style="display:none"><li onclick="document.querySelector(\'#committed\').textContent=this.textContent">'
                                      + value + '</li></ul><div id="committed">before</div>')
                    bot.navigate(session, 'data:text/html,' + quote(selection_html))
                    selection = bot.select_option(session, item_text=value, item_scope_css='#items', item_tag='li',
                                                  verify_scope_css='#committed', open_via_trigger=True, trigger_css='#trigger')
                    committed = bot.execute(session, 'return document.querySelector("#committed").textContent;')
                    receipt['selection_cases'].append({'class': label, 'verified': selection.get('verified') is True,
                                                       'committed': committed == value, 'success_safe': value not in str(selection)})
                time.sleep(.2)
                receipt['automatic_query_capture_absent'] = requests.get(bot.base+f'/synthetic-capture-check/{session}', timeout=10).json()['automatic_query_capture_absent']
                # Explicit recording must resume on every adapter, then refuse new fills.
                bot._post(f'/sessions/{session}/network/enable')
                bot.execute(session, 'fetch(arguments[0]);return true;', [bot.base+'/synthetic-capture?value=nonsecret-probe'])
                time.sleep(.2)
                receipt['explicit_capture_resumes'] = not requests.get(bot.base+f'/synthetic-capture-check/{session}', timeout=10).json()['automatic_query_capture_absent']
                try:
                    bot.trusted_type(session, 'synthetic-private-capture', css='#plain')
                except api.ScraperBotInputError as error:
                    receipt['enabled_capture_refuses_fill'] = error.reason == 'bad_request' and error.status_code == 400
                else:
                    receipt['enabled_capture_refuses_fill'] = False
                # Read only our synthetic session, never any production session/log.
                metadata = requests.get(bot.base+f'/sessions/{session}', timeout=10).json()
                receipt['session_diagnostic_safe'] = all(v not in str(metadata.get('last_error')) for _, v in SAMPLES)
                logs.flush()
                log_text = (Path(data)/'server.log').read_text() + (Path(data)/'process.log').read_text()
                receipt['logs_safe'] = all(v not in log_text for _, v in SAMPLES)
            finally:
                close_owned(bot, session, process, receipt)
            passed = all(all(v for k,v in case.items() if k!='class') for case in receipt['cases'])
            passed = passed and len(receipt['selection_cases']) == 4 and all(all(v for k,v in c.items() if k != 'class') for c in receipt['selection_cases'])
            passed = passed and receipt['automatic_query_capture_absent'] and receipt['enabled_capture_refuses_fill'] and receipt['explicit_capture_resumes']
            passed = passed and len(receipt['cases']) == 4 and receipt.get('session_diagnostic_safe') and receipt.get('logs_safe')
            passed = passed and receipt['owned_sessions_closed'] == 1 and receipt['server_stopped']
            receipt['result'] = 'GREEN' if passed else 'RED'
            print(json.dumps(receipt, indent=2))
            return 0 if passed else 1


if __name__ == '__main__':
    sys.exit(main())
