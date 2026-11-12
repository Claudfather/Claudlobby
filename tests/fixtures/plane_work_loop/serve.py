"""Opt-in loopback UI example. No fleet paths, databases, credentials or bot calls.

python3 tests/fixtures/plane_work_loop/serve.py --port 4312
Use --ui-dir to compare a separately exported renderer. Restart clears receipts.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import threading
from urllib.parse import urlsplit

FIXTURE = Path(__file__).resolve().parent
ROOMS = ('Workshop / Web app', 'Notebook / Web app')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=4312)
    parser.add_argument('--ui-dir', type=Path, default=FIXTURE.parents[2] / 'claudlobby/plane/ui')
    args = parser.parse_args()
    records = {}
    attempts = {}
    lookups = {}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, data, code=200, content_type='application/json'):
            body = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path).path
            if path == '/fixture/evidence':
                with lock:
                    return self.reply(dict(action_attempts=attempts, receipt_lookups=lookups, unique_requests=len(records)))
            if path == '/fixture/records':
                with lock:
                    return self.reply(list(records.values()))
            name = 'index.html' if path == '/' else path.removeprefix('/')
            if '/' in name or name.startswith('.'):
                return self.reply({'error':'not_found'}, 404)
            file = FIXTURE / name if name == 'api-client.js' else args.ui_dir / name
            if not file.is_file():
                return self.reply({'error':'not_found'}, 404)
            self.reply(file.read_bytes(), content_type=mimetypes.guess_type(name)[0] or 'text/plain')

        def do_POST(self):
            # Even this synthetic service accepts only its own loopback page.
            origin = self.headers.get('Origin')
            if origin != f'http://127.0.0.1:{self.server.server_port}':
                return self.reply({'error':'origin'}, 403)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 12000:
                    raise ValueError('size')
                data = json.loads(self.rfile.read(size))
                r = data['request']
                scope = r['scope']
                room = scope['fleet']
                assert room in ROOMS and scope == dict(workspace='Example studio', host=room.split(' / ')[0], fleet=room, viewer='example-owner')
                assert r['kind'] in ('message','feedback','nudge')
                assert r['target']['recipient'] in ('lead','engineer','reviewer')
                task = r['target']['task_id']
                assert task is None and r['kind'] == 'message' or task in [room.split(' / ')[0].lower()+'-'+s for s in ('signup','audience','checklist')]
                assert isinstance(r['request_id'], str) and 0 < len(r['request_id']) <= 100
                assert isinstance(r['submitted_at'], str)
            except (AssertionError, KeyError, ValueError, TypeError):
                return self.reply({'error':'invalid_fixture_request'}, 400)
            with lock:
                existing = records.get(r['request_id'])
                if existing and any(existing[k] != r[k] for k in ('scope','target','kind','submitted_at')):
                    return self.reply({'error':'request_conflict'}, 409)
                if self.path == '/fixture/receipt':
                    lookups[r['request_id']] = lookups.get(r['request_id'], 0) + 1
                    return self.reply(existing or {'error':'unknown'}, 200 if existing else 404)
                if self.path != '/fixture/action':
                    return self.reply({'error':'not_found'}, 404)
                if not isinstance(r.get('body'), str) or not r['body'].strip() or len(r['body']) > 2000:
                    return self.reply({'error':'body'}, 400)
                if existing and existing['body'] != r['body']:
                    return self.reply({'error':'request_conflict'}, 409)
                outcome = data.get('outcome', 'delivered')
                if outcome not in ('delivered','recorded','rejected','drop'):
                    return self.reply({'error':'outcome'}, 400)
                receipt = existing or dict(r, version=1, status='delivered' if outcome == 'drop' else outcome)
                attempts[r['request_id']] = attempts.get(r['request_id'], 0) + 1
                records[r['request_id']] = receipt
            if outcome == 'drop':
                self.close_connection = True
                return
            self.reply(receipt)

    server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    print(f'SYNTHETIC ONLY http://127.0.0.1:{server.server_port}/ assets={args.ui_dir}', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
