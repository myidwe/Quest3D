"""Private mTLS server for actual production epoch session/Null integration."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hashlib
import json
import os
import ssl
import subprocess
import struct
import sys
import threading
from urllib.parse import parse_qs, urlparse

root = Path(__file__).resolve().parents[2]
out = Path(sys.argv[1])
vectors = root / 'artifacts/audio/file-audio-vectors-20260910-a'
manifest = json.loads((vectors / 'manifest.json').read_text())
cases = {case['name']: case for case in manifest['cases']}
for name in ('server', 'wrong', 'client'):
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
        '-keyout', str(out / (name + '.key')), '-out', str(out / (name + '.pem')),
        '-subj', '/CN=EpochFixture-' + name], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
errors = []
requests = []

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args): pass
    def finish(self):
        try:
            super().finish()
            self.connection.settimeout(2)
            self.connection.unwrap().close()
        except (OSError, ssl.SSLError): pass
    def do_POST(self):
        try:
            case = parse_qs(urlparse(self.path).query)['case'][0]
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            name = 'seek' if case == 'seek' else 'first'
            assert request == cases[name]['scope']
            assert self.headers['Connection'] == 'close'
            requests.append(case)
            self.send_response(200)
            self.send_header('Content-Type', 'application/vnd.quest3d.pcm-v1')
            self.send_header('Connection', 'close')
            self.end_headers()
            wire = (vectors / (name + '.wire')).read_bytes()
            if case == 'ledger':
                template = bytearray(wire[:128])
                parts = []
                for i in range(301):
                    header = template.copy()
                    eof = i == 300
                    struct.pack_into('<I', header, 12, 0 if eof else 8)
                    struct.pack_into('<QQq', header, 80, i, i, round(i * 1e9 / 48000))
                    struct.pack_into('<HHH', header, 108, 0 if eof else 1, 0, 0 if eof else 1)
                    struct.pack_into('<I', header, 116, int(eof))
                    header[120] = int(i == 0)
                    parts.append(bytes(header) + (b'' if eof else struct.pack('<ff', .1, -.1)))
                wire = b''.join(parts)
            if case == 'trailing': wire += b'x'
            offset = 0
            while offset < len(wire):
                count = min(1 + offset % 4999, len(wire) - offset)
                self.wfile.write(wire[offset:offset+count]); self.wfile.flush()
                offset += count
        except (OSError, ssl.SSLError): pass
        except Exception as exc:
            errors.append(repr(exc)); raise

class Server(ThreadingHTTPServer):
    daemon_threads = True
    def handle_error(self, *_): pass

server = Server(('127.0.0.1', 0), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(out / 'server.pem', out / 'server.key')
context.load_verify_locations(out / 'client.pem'); context.verify_mode = ssl.CERT_REQUIRED
server.socket = context.wrap_socket(server.socket, server_side=True)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
results = []
try:
    for mode in ('first-seek', 'cancel', 'timeout', 'held-close', 'bad-body', 'wrong-pin', 'initfail', 'ledger-stall', 'join-retry'):
        before = len(requests)
        command = [str(out / 'epoch-test'), f'https://127.0.0.1:{server.server_port}',
            str(out / ('wrong.pem' if mode == 'wrong-pin' else 'server.pem')),
            str(out / 'client.pem'), str(out / 'client.key'), mode, str(vectors)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20,
            env=dict(os.environ, HTTPS_PROXY='http://127.0.0.1:1', ALL_PROXY='http://127.0.0.1:1'))
        (out / (mode + '.log')).write_text(result.stdout + result.stderr)
        assert result.returncode == 0, (mode, result.returncode, result.stdout, result.stderr)
        if mode in ('wrong-pin', 'initfail'): assert len(requests) == before
        if mode in ('held-close', 'join-retry'): assert len(requests) == before + 1
        results.append(dict(case=mode, passed=True, server_requests=len(requests)-before))
        print(result.stdout, end='', flush=True)
    assert not errors, errors
    (out / 'verification.json').write_text(json.dumps(dict(passed=True, results=results,
        actual='MediaAudioReader vectors -> production incremental pinned mTLS -> epoch session -> actual miniaudio Null callbacks',
        exact_first_frames=5003, exact_seek_frames=4043, no_physical_audio_or_quest=True,
        sunshine_endpoint=False, product_coordinator=False, video_alignment=False,
        input_manifest_sha256=hashlib.sha256((vectors/'manifest.json').read_bytes()).hexdigest()), indent=2)+'\n')
finally:
    server.shutdown(); server.server_close(); thread.join()
