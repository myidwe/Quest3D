"""Actual production Curl streaming worker over private mutually authenticated TLS.

PKI/server setup follows test_tls.py. Only loopback and new artifact files are used.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import ssl
import subprocess
import tempfile
import threading
from urllib.parse import urlparse, parse_qs

root = Path(__file__).resolve().parents[2]
out = root / 'artifacts/quest/file-audio-transport'
out.mkdir(exist_ok=True)
work = Path(tempfile.mkdtemp(prefix='pki-', dir=out))
no_exceptions = os.environ.get('FILE_AUDIO_NO_EXCEPTIONS') == '1'
exe = out / ('file-audio-client-no-exceptions' if no_exceptions else 'file-audio-client')
ordinary = out / 'tls-client'
for name in ('server', 'other', 'client', 'unpaired'):
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
        '-keyout', str(work / (name + '.key')), '-out', str(work / (name + '.pem')),
        '-subj', '/CN=No-IP-Hostname-Match-' + name], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
wire = out / 'fixture.pcm-wire'
subprocess.run([str(exe), 'generate', str(wire)], check=True)
data = wire.read_bytes()
expected_request = dict(file_session='01'*16, transport='02'*16, channel='03'*16,
                        epoch=str(2**64-1), generation='17')
vectors = root / 'artifacts/audio/file-audio-vectors-20260910-a'
manifest = json.loads((vectors / 'manifest.json').read_text())
actual_cases = {case['name']: case for case in manifest['cases']}
server_stop = threading.Event()
first_callback = threading.Event()
record_lock = threading.Lock()
requests = []
errors = []

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def finish(self):
        super().finish()
        if 'case=raw-tls-close' not in self.path:
            try:
                self.connection.settimeout(2)
                self.connection.unwrap().close()  # Actual TLS close_notify for close-delimited HTTP.
            except (OSError, ssl.SSLError): pass
    def log_message(self, *args): pass
    def do_GET(self):
        with record_lock: requests.append(('ordinary', self.path))
        if self.path == '/redirect':
            self.send_response(302); self.send_header('Location', '/redirect-target')
            self.send_header('Content-Length', '0'); self.end_headers(); return
        assert self.headers.get('Accept') == 'application/json'
        body = b'{"mode":"2d"}'
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        try:
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/ordinary':
                assert request == {'mode':'2d'}; return self.do_GET()
            assert self.headers.get('Connection') == 'close'
            assert self.headers.get('Content-Type') == 'application/json'
            assert self.headers.get('Accept') == 'application/vnd.quest3d.pcm-v1'
            case = parse_qs(urlparse(self.path).query).get('case', ['coalesced'])[0]
            actual = case[7:] if case.startswith('actual-') else None
            expected = actual_cases[actual]['scope'] if actual else expected_request.copy()
            if case == 'restart' and request['epoch'] == '42': expected['epoch'] = '42'
            assert request == expected
            with record_lock: requests.append(('file', case))
            status = 503 if case == 'status' else 302 if case == 'redirect' else 200
            self.send_response(status)
            if case != 'missing-mime': self.send_header('Content-Type', 'text/plain' if case == 'mime' else 'application/vnd.quest3d.pcm-v1')
            if case == 'duplicate-mime': self.send_header('Content-Type', 'application/vnd.quest3d.pcm-v1')
            if case == 'redirect': self.send_header('Location', '/redirect-target')
            if case == 'encoding': self.send_header('Content-Encoding', 'gzip')
            if case == 'oversize-header': self.send_header('X-Large', 'a' * 5000)
            if case == 'transfer-encoding': self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Connection', 'close'); self.end_headers(); self.wfile.flush()
            if case in ('idle', 'idlecancel'):
                server_stop.wait(2); return
            payload = bytearray((vectors / (actual + '.wire')).read_bytes() if actual else data)
            if case == 'restart' and request['epoch'] == '42':
                for offset in (0, 3968, 4232): payload[offset+64:offset+72] = (42).to_bytes(8, 'little')
            if case == 'oldscope': payload[16] ^= 1
            if case == 'truncated': payload = payload[:-1]
            if case == 'noeof': payload = payload[:-128]
            if case == 'trailing': payload += b'x'
            if case == 'incremental':
                self.wfile.write(payload[:3968]); self.wfile.flush()
                assert first_callback.wait(5), 'Actual first PCM callback must run before the server sends its next block/EOF'
                self.wfile.write(payload[3968:])
            elif case == 'fragment' or actual:
                pieces = [1, 2, 5, 11, 37, 100, 1999, 3, 577, 17]
                offset = 0
                for length in pieces:
                    self.wfile.write(payload[offset:offset+length]); self.wfile.flush(); offset += length
                self.wfile.write(payload[offset:])
            else: self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
            pass  # Rejected status/header or cancelled callback closes real socket.
        except Exception as error:
            errors.append(repr(error)); raise

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, *_): pass

server = Server(('127.0.0.1', 0), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain(work / 'server.pem', work / 'server.key')
ctx.load_verify_locations(work / 'client.pem'); ctx.verify_mode = ssl.CERT_REQUIRED
server.socket = ctx.wrap_socket(server.socket, server_side=True)
thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
url = f'https://127.0.0.1:{server.server_port}'
results = []

def run(case, expected='complete', mode='normal', pin='server', client='client'):
    command = [str(exe), url + '/quest3d/v1/file-audio?case=' + case,
        str(work / (pin + '.pem')), str(work / (client + '.pem')), str(work / (client + '.key')), expected, mode]
    if mode.startswith('actual-'): command.append(str(vectors / (mode[7:] + '.pcm')))
    # Invalid environment proxy must never receive the paired certificate or request.
    environment = dict(os.environ, HTTPS_PROXY='http://127.0.0.1:1', ALL_PROXY='http://127.0.0.1:1')
    if mode == 'incremental':
        first_callback.clear()
        process = subprocess.Popen(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        line = process.stdout.readline()
        first_callback.set()
        stdout, stderr = process.communicate(timeout=15)
        assert line == 'FIRST_BLOCK\n', (line, stdout, stderr)
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    else:
        result = subprocess.run(command, text=True, capture_output=True, timeout=15, env=environment)
    assert result.returncode == 0, (case, mode, result.stdout, result.stderr)
    evidence = json.loads(result.stdout)
    results.append({'case':case, 'mode':mode, 'pin':pin, 'client':client, **evidence})
    print('PASS', case, mode, evidence, flush=True)
    return evidence

try:
    run('coalesced'); run('fragment', mode='fragment')
    run('incremental', mode='incremental')
    run('actual-first', mode='actual-first'); run('actual-seek', mode='actual-seek')
    before = len(requests); run('restart', mode='restart'); assert len(requests) == before + 2
    for case in ('status','redirect','mime','missing-mime','duplicate-mime','encoding','oversize-header','transfer-encoding','oldscope','truncated','noeof','trailing','idle','raw-tls-close'):
        result = run(case, 'failed')
        if case not in ('truncated','noeof','trailing','raw-tls-close'): assert result['records'] == 0
    before = len(requests)
    run('coalesced', 'failed', pin='other'); run('coalesced', 'failed', client='unpaired')
    assert len(requests) == before, 'Rejected TLS must not reach HTTP handler'
    for mode in (('reject',) if no_exceptions else ('reject','throw')): run('coalesced', 'failed', mode)
    before = len(requests); run('coalesced', 'failed', 'workerfail'); assert len(requests) == before
    run('coalesced', mode='joinfail')
    for mode in ('hold','cancel','selfstop'): run('coalesced', 'cancel', mode)
    run('coalesced', mode='backpressure'); run('coalesced', 'cancel', 'backpressurecancel')
    run('idlecancel', 'cancel', 'idlecancel'); run('coalesced', 'failed', 'invalid')
    run('coalesced', 'failed', 'invalid-scope'); run('coalesced', 'failed', 'invalid-cert')
    assert not any(path == '/redirect-target' for _, path in requests)

    # Existing production CurlHttpClient test program, compiled from this tree.
    def old(mode='get', pin='server', client='client', path='/ordinary'):
        return subprocess.check_output([str(ordinary), url + path, str(work/(pin+'.pem')),
            str(work/(client+'.pem')), str(work/(client+'.key')), mode, '{"mode":"2d"}'], text=True)
    assert old().startswith('200\n'); assert old('post').startswith('200\n')
    assert old(pin='other').startswith('-1\n'); assert old(client='unpaired').startswith('-1\n')
    assert old('insecure').startswith('-1\n'); assert 'Headers rejected' in old('inject')
    before = len(requests); assert old(path='/redirect').startswith('302\n'); assert len(requests) == before + 1
    subprocess.run([str(ordinary), 'proof', str(work/'server.pem'), str(work/'server.key')], check=True)
    print('PASS existing Curl program: exact pin/mTLS GET/POST, wrong pin/client, verification denial, header injection, redirect refusal, pairing proof', flush=True)
    assert not errors, errors
    (out/('results-no-exceptions.json' if no_exceptions else 'results.json')).write_text(json.dumps({'results':results,'server_requests':requests,
        'actual':'private loopback TLS and production client/decoder; no Sunshine endpoint/audio backend/Quest'}, indent=2)+'\n')
finally:
    server_stop.set(); server.shutdown(); server.server_close(); thread.join()
