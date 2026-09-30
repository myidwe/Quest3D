"""Exercise the production CurlHttpClient against local mutually authenticated TLS."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import ssl
import subprocess
import threading
import tempfile

cache = Path(os.environ['QUEST_CACHE'])
test_root = cache / 'tls-tests'
test_root.mkdir(exist_ok=True)
work = Path(tempfile.mkdtemp(prefix='fixture-', dir=test_root))
exe = test_root / 'tls-client'
for name in ('server', 'other', 'client', 'unpaired'):
    cert, key = work / (name + '.pem'), work / (name + '.key')
    if not cert.exists():
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
            '-keyout', str(key), '-out', str(cert), '-subj', '/CN=No-IP-Hostname-Match-' + name],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

class Handler(BaseHTTPRequestHandler):
    count = 0
    v2_status = {'version': 1, 'session_id': 'fixture-v2-session', 'revision': 0, 'last_seq': 0,
        'requested_mode': '2d', 'effective_mode': '2d', 'disparity': 12.0, 'eye_width': 1280,
        'eye_height': 720, 'publisher_age_ms': 1, 'stream_epoch': '18446744073709551615',
        'applied_request': '', 'rejected_request': '', 'input_enabled': False}
    def log_message(self, *args): pass
    def reply(self, code, body):
        encoded = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Length', str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
    def do_GET(self):
        Handler.count += 1
        assert self.headers.get('Connection') == 'close'
        assert self.headers.get('Accept') == 'application/json'
        if self.path == '/serverinfo':
            return self.reply(200, '<root status_code="200"><PairStatus>1</PairStatus><currentgame>0</currentgame>'
                '<Quest3DProfileVersion>1</Quest3DProfileVersion><Quest3DLayout>full_sbs</Quest3DLayout>'
                '<Quest3DWidth>2560</Quest3DWidth><Quest3DHeight>720</Quest3DHeight></root>')
        if self.path == '/quest3d/v1/status':
            return self.reply(200, Handler.v2_status)
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/redirect-target')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        self.send_response(200)
        self.send_header('Content-Length', '13')
        self.end_headers()
        self.wfile.write(b'{"mode":"2d"}')
    def do_POST(self):
        assert self.headers.get('Content-Type') == 'application/json'
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.path == '/quest3d/v1/control':
            assert set(body) == {'version', 'session_id', 'request_id', 'seq', 'expected_revision', 'mode', 'disparity'}
            assert body['session_id'] == Handler.v2_status['session_id'] and body['mode'] in ('2d', '3d')
            assert body['expected_revision'] == Handler.v2_status['revision']
            assert body['seq'] == Handler.v2_status['last_seq'] + 1
            Handler.v2_status.update(requested_mode=body['mode'], effective_mode=body['mode'],
                applied_request=body['request_id'], disparity=body['disparity'], last_seq=body['seq'],
                revision=Handler.v2_status['revision'] + 1)
            return self.reply(202, {'version': 1, 'request_id': body['request_id'], 'outcome': 'pending', 'duplicate': False})
        assert body == {'mode': '2d'}
        self.do_GET()

class TlsServer(ThreadingHTTPServer):
    def get_request(self):
        try:
            return super().get_request()
        except OSError as error:
            print('TLS fixture rejected handshake:', error, flush=True)
            raise

server = TlsServer(('127.0.0.1', 0), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(work / 'server.pem', work / 'server.key')
ctx.load_verify_locations(work / 'client.pem')
ctx.verify_mode = ssl.CERT_REQUIRED
server.socket = ctx.wrap_socket(server.socket, server_side=True)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
url = f'https://127.0.0.1:{server.server_port}'

def request(path='/', pin='server', client='client', mode='get'):
    pin_path = 'none' if pin == 'none' else str(work / (pin + '.pem'))
    client_path = 'none' if client == 'none' else str(work / (client + '.pem'))
    result = subprocess.check_output([str(exe), url + path, pin_path, client_path,
        str(work / (client + '.key')), mode, '{"mode":"2d"}'], text=True)
    return result

try:
    initial = request()
    assert initial.startswith('200\n'), 'Exact cert must authenticate despite IP hostname mismatch: ' + initial
    assert request(mode='post').startswith('200\n'), 'JSON headers/body must arrive unchanged'
    before = Handler.count
    assert request(pin='other').startswith('-1\n'), 'Wrong pinned cert must fail'
    assert request(pin='none').startswith('-1\n'), 'Unknown self-signed cert must fail'
    assert request(client='unpaired').startswith('-1\n'), 'Unpaired client cert must fail'
    assert request(client='none').startswith('-1\n'), 'Missing client cert must fail'
    assert request(mode='insecure').startswith('-1\n'), 'Verification disable must fail'
    assert Handler.count == before, 'Rejected TLS must not deliver an HTTP command'
    assert request('/redirect').startswith('302\n')
    assert Handler.count == before + 1, 'Redirect must not be followed'
    assert 'Headers rejected' in request(mode='inject')
    subprocess.run([str(exe), 'proof', str(work / 'server.pem'), str(work / 'server.key')], check=True)
    v2_env = dict(os.environ, QUEST_V2_TLS_URL=url, QUEST_V2_TLS_ROOT=str(work))
    test = subprocess.run([str(cache / 'linux/Godot_v4.7-stable_linux.x86_64'), '--headless', '--xr-mode', 'off',
        '--path', str(cache / 'source'), '--script', str(Path(__file__).with_name('test_v2_compat.gd'))],
        env=v2_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
    (Path(os.environ['QUEST_ARTIFACTS']) / 'v2-compat.log').write_text(test.stdout)
    assert test.returncode == 0 and 'SCRIPT ERROR:' not in test.stdout and 'V2 compatibility real TLS PASS' in test.stdout, test.stdout
    print('V2 compatibility real TLS PASS: production Curl + Godot Full-SBS / PC3D / PC2D, actual input OFF')
    print('TLS tests passed: exact cert, mTLS, JSON POST, wrong/missing identities, redirects, header injection')
finally:
    server.shutdown()
    server.server_close()
