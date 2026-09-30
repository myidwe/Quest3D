"""Pinned-loopback development pairing helper; secrets arrive on stdin only."""
from __future__ import annotations

import base64
import ctypes as C
from ctypes import wintypes as W
from dataclasses import dataclass, field
import hashlib
import hmac
import http.client
import ipaddress
import json
from pathlib import Path
import re
import ssl
import sys

import psutil

ROOT = Path(__file__).resolve().parents[2]
PORT = 47990
DEVELOPMENT_HEADSETS = "config/development-headsets.json"
PRIVATE_LAN_RANGES = tuple(ipaddress.ip_network(value) for value in
    ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
MAX_REQUEST = 8192
MAX_RESPONSE = 65536
ID_PATTERN = re.compile(r"[0-9a-fA-F]{32}\Z")


class PairingError(Exception):
    """Only fixed nonsecret error codes cross the process boundary."""


def require(condition, code):
    if not condition:
        raise PairingError(code)


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def strict_json(data):
    def unique_fields(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, "duplicate_json_fields")
            value[key] = item
        return value
    return json.loads(data, object_pairs_hook=unique_fields)


def read_bytes(path, snapshots, maximum=MAX_RESPONSE):
    with path.open("rb") as stream:
        value = stream.read(maximum + 1)
    require(len(value) <= maximum, "local_metadata_too_large")
    snapshots[path] = hashlib.sha256(value).hexdigest()
    return value


def read_json(path, snapshots):
    value = strict_json(read_bytes(path, snapshots).decode("utf-8-sig"))
    require(isinstance(value, dict), "local_metadata_not_object")
    return value


def safe_path(value, parent):
    require(isinstance(value, str), "invalid_local_path")
    path = Path(value).resolve(strict=True)
    require(path.is_relative_to(parent.resolve(strict=True)), "local_path_outside_dev_root")
    return path


def positive_integer(value, code):
    require(type(value) in (str, int) and re.fullmatch(r"[0-9]{1,20}", str(value)) is not None, code)
    result = int(value)
    require(0 < result < 1 << 64, code)
    return result


@dataclass(frozen=True)
class HostBinding:
    process_id: int
    creation_filetime: int
    executable: Path
    executable_sha256: str
    runtime: Path
    config: Path
    encrypted_access: Path
    certificate: Path
    certificate_sha256: str
    local_files: tuple[tuple[Path, str], ...]

    def verify_files(self):
        for path, expected in self.local_files:
            require(hmac.compare_digest(digest(path), expected), "local_host_metadata_changed")

    def public(self):
        return dict(process_id=self.process_id, creation_filetime=str(self.creation_filetime),
            executable=str(self.executable), executable_sha256=self.executable_sha256,
            endpoint="https://127.0.0.1:47990", certificate_sha256=self.certificate_sha256,
            tls_peer_pinned=True)


def load_binding(root=ROOT):
    host_root = (root / "artifacts/host").resolve(strict=True)
    dev = (host_root / "dev").resolve(strict=True)
    snapshots = {}
    launch_path, process_path = dev / "launch.json", dev / "process.json"
    launch, process = read_json(launch_path, snapshots), read_json(process_path, snapshots)
    runtime = safe_path(launch.get("runtime"), host_root)
    require(runtime.name.startswith("runtime-") and runtime.parent == host_root, "unexpected_runtime_directory")
    staged_path = runtime / "manifest.json"
    staged = read_json(staged_path, snapshots)
    for key in ("runtime", "config", "host_sha256", "encrypted_web_access", "port", "web_url"):
        require(staged.get(key) == launch.get(key), "staged_launch_manifest_mismatch")
    require(type(launch.get("port")) is int and launch["port"] == PORT - 1, "unsupported_dev_port")
    require(launch.get("web_url") in ("https://localhost:47990", "https://127.0.0.1:47990"), "nonloopback_web_url")
    config = safe_path(launch.get("config"), runtime)
    require(config == runtime / "sunshine.conf", "unexpected_runtime_config")
    executable = (runtime / "sunshine.exe").resolve(strict=True)
    require(executable.parent == runtime, "executable_outside_runtime")
    require(safe_path(process.get("runtime"), host_root) == runtime, "process_runtime_mismatch")
    require(safe_path(process.get("executable"), runtime) == executable, "process_executable_mismatch")
    expected_sha = launch.get("host_sha256", "")
    require(isinstance(expected_sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha) is not None, "invalid_executable_digest")
    expected_sha = expected_sha.lower()
    require(hmac.compare_digest(digest(executable), expected_sha), "runtime_executable_digest_mismatch")
    snapshots[executable] = expected_sha
    access = safe_path(launch.get("encrypted_web_access"), dev)
    require(access == dev / "web-access.clixml", "unexpected_credential_file")
    settings = read_bytes(config, snapshots).decode("utf-8-sig")
    cert_lines = re.findall(r"(?m)^\s*cert\s*=\s*([^\r\n]+)", settings)
    port_lines = re.findall(r"(?m)^\s*port\s*=\s*([^\r\n]+)", settings)
    require(len(cert_lines) == 1 and port_lines == [str(PORT - 1)], "ambiguous_runtime_tls_configuration")
    certificate = safe_path(cert_lines[0].strip(), host_root)
    pem = read_bytes(certificate, snapshots).decode("ascii").strip()
    require(re.fullmatch(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\r\n]+\s*-----END CERTIFICATE-----", pem) is not None,
            "expected_one_public_server_certificate")
    certificate_sha = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest()
    return HostBinding(positive_integer(process.get("process_id"), "invalid_host_pid"),
        positive_integer(process.get("owner_creation_filetime"), "missing_exact_host_birth"),
        executable, expected_sha, runtime, config, access, certificate, certificate_sha,
        tuple(snapshots.items()))


class LiveHostGuard:
    """Retain the exact process lifetime and recheck the IPv4 listener per request."""
    def __init__(self, binding):
        self.binding = binding
        self.k = C.WinDLL("kernel32", use_last_error=True)
        for name, arguments, result in (
            ("OpenProcess", [W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            ("CloseHandle", [W.HANDLE], W.BOOL),
            ("WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD),
            ("GetProcessTimes", [W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
            ("QueryFullProcessImageNameW", [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)], W.BOOL)):
            method = getattr(self.k, name)
            method.argtypes, method.restype = arguments, result
        self.handle = self.k.OpenProcess(0x101000, False, binding.process_id)
        require(bool(self.handle), "cannot_open_expected_host")

    def verify(self):
        self.binding.verify_files()
        require(self.k.WaitForSingleObject(self.handle, 0) == 258, "expected_host_has_exited")
        values = [W.FILETIME() for _ in range(4)]
        require(bool(self.k.GetProcessTimes(self.handle, *(C.byref(value) for value in values))), "cannot_query_host_birth")
        birth = values[0].dwHighDateTime << 32 | values[0].dwLowDateTime
        require(birth == self.binding.creation_filetime, "host_pid_lifetime_mismatch")
        size = W.DWORD(32768)
        buffer = C.create_unicode_buffer(size.value)
        require(bool(self.k.QueryFullProcessImageNameW(self.handle, 0, buffer, C.byref(size))), "cannot_query_host_executable")
        require(Path(buffer.value).resolve(strict=True) == self.binding.executable, "live_executable_path_mismatch")
        listeners = [row.pid for row in psutil.net_connections(kind="tcp4")
            if row.status == psutil.CONN_LISTEN and row.laddr.port == PORT
            and row.laddr.ip in ("0.0.0.0", "127.0.0.1")]
        require(bool(listeners) and set(listeners) == {self.binding.process_id}, "loopback_listener_owner_mismatch")

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


@dataclass(repr=False)
class Credentials:
    username: str = field(repr=False)
    password: str = field(repr=False)


class PinnedClient:
    """No proxy, redirect, cookie jar or credentials before peer DER pin matching."""
    def __init__(self, binding, guard, credentials, connection_factory=http.client.HTTPSConnection):
        self.binding, self.guard, self.credentials = binding, guard, credentials
        self.connection_factory = connection_factory

    def request(self, method, path, body=None):
        require((method, path) in (("GET", "/api/pin"), ("POST", "/api/pin")), "unsupported_pairing_operation")
        self.guard.verify()
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        # The staged server certificate, rather than system CA/hostname trust,
        # is the sole trust anchor; compare before sending any HTTP bytes.
        context.verify_mode = ssl.CERT_NONE
        connection = self.connection_factory("127.0.0.1", PORT, timeout=5, context=context)
        sent = False
        try:
            connection.connect()
            peer = connection.sock.getpeername()
            require(peer[0] == "127.0.0.1" and peer[1] == PORT, "tls_peer_not_exact_loopback")
            certificate = connection.sock.getpeercert(binary_form=True)
            require(bool(certificate) and hmac.compare_digest(hashlib.sha256(certificate).hexdigest(), self.binding.certificate_sha256),
                    "tls_server_certificate_pin_mismatch")
            self.guard.verify()
            auth = base64.b64encode((self.credentials.username + ":" + self.credentials.password).encode("utf-8")).decode("ascii")
            headers = {"Authorization": "Basic " + auth, "Accept": "application/json", "Connection": "close"}
            payload = None
            if body is not None:
                payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
                headers["Content-Type"] = "application/json"
            sent = True
            connection.request(method, path, body=payload, headers=headers)
            response = connection.getresponse()
            require(response.status == 200, "pairing_http_status_not_ok")
            require(response.getheader("Content-Type", "").split(";")[0].strip().lower() == "application/json", "pairing_response_not_json")
            data = response.read(MAX_RESPONSE + 1)
            require(len(data) <= MAX_RESPONSE, "pairing_response_too_large")
            value = strict_json(data)
            require(isinstance(value, dict), "pairing_response_not_object")
            self.guard.verify()
            return value
        except PairingError:
            if method == "POST" and sent:
                raise PairingError("submit_result_unknown_no_retry") from None
            raise
        except Exception:
            raise PairingError("submit_result_unknown_no_retry" if method == "POST" and sent else "local_pairing_transport_failed") from None
        finally:
            connection.close()


def parse_pending(value):
    require(set(value) == {"pairings"} and isinstance(value["pairings"], list), "invalid_pending_response")
    require(len(value["pairings"]) <= 128, "too_many_pending_requests")
    pending, seen = [], set()
    for row in value["pairings"]:
        require(isinstance(row, dict) and set(row) == {"id", "name", "address"}, "invalid_pending_fields")
        pairing_id, name, address = row["id"], row["name"], row["address"]
        require(isinstance(pairing_id, str) and ID_PATTERN.fullmatch(pairing_id) is not None, "invalid_pending_id")
        pairing_id = pairing_id.lower()
        require(pairing_id not in seen, "duplicate_pending_id")
        seen.add(pairing_id)
        require(isinstance(name, str) and len(name.encode("utf-8")) <= 128 and all(ord(char) >= 32 for char in name), "invalid_pending_name")
        require(isinstance(address, str) and len(address) <= 64, "invalid_pending_address")
        try:
            ipaddress.ip_address(address)
        except ValueError:
            raise PairingError("invalid_pending_address") from None
        pending.append(dict(id=pairing_id, name=name, address=address))
    return pending


def validate_request(value):
    require(isinstance(value, dict), "stdin_request_not_object")
    mode = value.get("mode")
    require(mode in ("inspect", "submit"), "invalid_pairing_mode")
    fields = {"mode", "username", "password"} | ({"pin", "pairing_id"} if mode == "submit" else set())
    require(set(value) == fields, "unexpected_stdin_fields")
    require(isinstance(value["username"], str) and 1 <= len(value["username"]) <= 128
        and ":" not in value["username"] and all(ord(char) >= 32 for char in value["username"]), "invalid_local_username")
    require(isinstance(value["password"], str) and 1 <= len(value["password"]) <= 4096, "invalid_local_password")
    if mode == "submit":
        require(isinstance(value["pin"], str) and re.fullmatch(r"[0-9]{4}", value["pin"]) is not None, "pin_must_be_four_ascii_digits")
        require(isinstance(value["pairing_id"], str) and ID_PATTERN.fullmatch(value["pairing_id"]) is not None, "submit_requires_observed_pairing_id")
    return Credentials(value["username"], value["password"])


def development_headsets(root):
    """Explicit development targets; a malformed file never widens eligibility."""
    path = root / DEVELOPMENT_HEADSETS
    try:
        with path.open("rb") as stream:
            data = stream.read(MAX_REQUEST + 1)
    except FileNotFoundError:
        # A dangling link is a broken configuration, not an absent override.
        require(not path.is_symlink(), "invalid_development_headsets")
        return {}, None
    except OSError:
        raise PairingError("invalid_development_headsets") from None
    try:
        require(len(data) <= MAX_REQUEST, "invalid_development_headsets")
        value = strict_json(data.decode("utf-8-sig"))
        require(isinstance(value, dict) and set(value) == {"schema", "headsets"}, "invalid_development_headsets")
        require(type(value["schema"]) is int and value["schema"] == 1, "invalid_development_headsets")
        rows = value["headsets"]
        require(isinstance(rows, list) and 1 <= len(rows) <= 32, "invalid_development_headsets")
        selected = {}
        for row in rows:
            require(isinstance(row, dict) and set(row) == {"address", "name"}, "invalid_development_headsets")
            address, name = row["address"], row["name"]
            require(isinstance(address, str), "invalid_development_headsets")
            ip = ipaddress.ip_address(address)
            require(ip.version == 4 and str(ip) == address and any(ip in network for network in PRIVATE_LAN_RANGES),
                    "invalid_development_headsets")
            require(address not in selected, "invalid_development_headsets")
            require(isinstance(name, str) and name == name.strip() and 1 <= len(name.encode("utf-8")) <= 128
                    and all(char.isprintable() for char in name), "invalid_development_headsets")
            selected[address] = name
        return selected, hashlib.sha256(data).hexdigest()
    except (PairingError, ValueError, UnicodeError):
        raise PairingError("invalid_development_headsets") from None


def execute(request, client):
    validate_request(request)
    # The private development build keeps explicitly approved individual IPs.
    # Public installs have no developer's address: one fresh RFC1918 LAN request
    # and the PIN shown by that headset are required. Never approve all requests.
    installed = (ROOT / 'config/distribution.json').is_file()
    allowed, configuration_digest = ({}, None) if installed else development_headsets(ROOT)
    pending = parse_pending(client.request("GET", "/api/pin"))
    quest = [row for row in pending if (any(ipaddress.ip_address(row['address']) in n for n in PRIVATE_LAN_RANGES)
             if installed else row['address'] in allowed)]
    if request["mode"] == "inspect":
        address = quest[0]["address"] if len(quest) == 1 else (next(iter(allowed)) if len(allowed) == 1 else None)
        return dict(operation="inspect", pending=pending, quest_address=address,
                    exact_quest_requests=len(quest), eligible_requests=quest,
                    pairing_changed=False, quest_pairing_completed=False)
    require(len(quest) == 1, "submit_requires_one_exact_quest_request")
    require(quest[0]["id"] == request["pairing_id"].lower(), "pending_id_changed_or_no_longer_selected")
    if not installed:
        # A config replacement during the fresh pending read invalidates this
        # attempt. An absent configuration never selects a pending headset.
        require(development_headsets(ROOT) == (allowed, configuration_digest), "development_headsets_changed")
    name = "Meta Quest 2" if installed else allowed[quest[0]["address"]]
    response = client.request("POST", "/api/pin", dict(pairing_id=quest[0]["id"], pin=request["pin"], name=name))
    require(set(response) == {"status"} and type(response["status"]) is bool, "unexpected_submit_response_no_retry")
    return dict(operation="submit", pairing_id=quest[0]["id"], quest_address=quest[0]['address'],
        host_accepted=response["status"], pairing_changed=response["status"], quest_pairing_completed=False,
        note="Host PIN acceptance does not confirm the Quest handshake completed; do not automatically retry.")


def main():
    guard = None
    request = None
    try:
        require(len(sys.argv) == 1, "use_stdin_not_arguments")
        data = sys.stdin.buffer.read(MAX_REQUEST + 1)
        require(len(data) <= MAX_REQUEST, "stdin_request_too_large")
        request = strict_json(data)
        credentials = validate_request(request)
        binding = load_binding()
        guard = LiveHostGuard(binding)
        result = execute(request, PinnedClient(binding, guard, credentials))
        result["host"] = binding.public()
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result.get("host_accepted", True) else 2
    except PairingError as exc:
        print(json.dumps(dict(operation="error", code=str(exc), secrets_redacted=True)), file=sys.stderr)
        return 1
    except Exception:
        # Never echo response bodies, credential-related exceptions or traceback locals.
        print('{"operation":"error","code":"local_pairing_validation_failed","secrets_redacted":true}', file=sys.stderr)
        return 1
    finally:
        if guard:
            guard.close()
        if isinstance(request, dict):
            request.clear()


if __name__ == "__main__":
    raise SystemExit(main())
