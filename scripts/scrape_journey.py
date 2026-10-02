"""A real browser scrape against a deterministic, locally served catalog."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from client import ScraperBot

PAGE = b'''<!doctype html><title>Public catalog</title><table id="catalog">
<tr data-sku="A1"><td>Notebook</td><td>12.50</td></tr>
<tr data-sku="B2"><td>Pencil</td><td>1.25</td></tr></table>'''

class Catalog(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(PAGE)
    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:9020')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--undetected', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--headless', dest='headless', action='store_true')
    mode.add_argument('--headful', dest='headless', action='store_false')
    parser.set_defaults(headless=None)
    args = parser.parse_args()
    catalog = ThreadingHTTPServer(('127.0.0.1', 0), Catalog)
    thread = threading.Thread(target=catalog.serve_forever, daemon=True)
    thread.start()
    bot = ScraperBot(args.base_url)
    sid = None
    proof = {'status': 'failed', 'driver': 'chromedriver', 'undetected': args.undetected, 'requested_headless': args.headless}
    try:
        sid = bot.create_session(headless=args.headless, undetected=args.undetected, name='public-catalog-journey')
        proof['session_id'] = sid
        bot.navigate(sid, f'http://127.0.0.1:{catalog.server_port}/catalog')
        rows = bot.execute(sid, '''return Array.from(document.querySelectorAll('#catalog tr')).map(row => ({sku:row.dataset.sku,name:row.cells[0].textContent,price:row.cells[1].textContent}))''')
        assert rows == [{'sku':'A1','name':'Notebook','price':'12.50'}, {'sku':'B2','name':'Pencil','price':'1.25'}], rows
        proof['rows'] = rows
        proof['close'] = bot.close(sid)
        assert proof['close']['status'] == 'closed', proof['close']
        proof['terminal_state'] = bot._get(f'/sessions/{sid}')['state']
        assert proof['terminal_state'] == 'closed'
        sid = None
        proof['status'] = 'passed'
    finally:
        if sid:
            bot.close(sid)
        catalog.shutdown()
        catalog.server_close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(proof, indent=2) + '\n')
    print(json.dumps(proof, indent=2))

if __name__ == '__main__':
    main()
