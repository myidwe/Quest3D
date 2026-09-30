"""Mock HTTP/POST only. Fixture credentials/PIN/cert bytes are synthetic."""
import base64
import ctypes as C
from dataclasses import replace
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import ssl
import sys
from types import SimpleNamespace

import pytest

HELPER = Path(__file__).resolve().parents[1] / "native/host/pairing_host.py"
spec = importlib.util.spec_from_file_location("quest3d_pairing_host_tested", HELPER)
pairing = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = pairing
spec.loader.exec_module(pairing)

PAIRING_ID = "a" * 32
PEER_DER = b"synthetic certificate bytes; no actual TLS or private key"


@pytest.fixture(autouse=True)
def isolated_configuration(tmp_path, monkeypatch):
    """Select an explicit synthetic target; never depend on a developer default."""
    monkeypatch.setattr(pairing, "ROOT", tmp_path)
    write_development_headsets(tmp_path, [dict(address="192.168.47.4", name="Meta Quest 2")])


def request(mode="inspect"):
    value = dict(mode=mode, username="fixture-user", password="fixture-password-not-real")
    if mode == "submit":
        value.update(pin="2468", pairing_id=PAIRING_ID)
    return value


def row(**changes):
    return dict(id=PAIRING_ID, name="Fixture Quest", address="192.168.47.4", **changes)


class Client:
    def __init__(self, pending=None, accepted=True):
        self.calls = []
        self.pending = [row()] if pending is None else pending
        self.accepted = accepted
    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return dict(pairings=self.pending) if method == "GET" else dict(status=self.accepted)


def test_default_inspection_never_posts_or_claims_quest_complete():
    client = Client()
    value = pairing.execute(request(), client)
    assert [call[0] for call in client.calls] == ["GET"]
    assert value["exact_quest_requests"] == 1 and not value["pairing_changed"] and not value["quest_pairing_completed"]


def test_explicit_selected_request_is_rechecked_then_submitted_once():
    client = Client()
    value = pairing.execute(request("submit"), client)
    assert [(call[0], call[1]) for call in client.calls] == [("GET", "/api/pin"), ("POST", "/api/pin")]
    assert client.calls[1][2] == dict(pairing_id=PAIRING_ID, pin="2468", name="Meta Quest 2")
    assert value["host_accepted"] and not value["quest_pairing_completed"]
    assert "pin" not in value and "password" not in json.dumps(value)


@pytest.mark.parametrize("pending", [[], [dict(row(), address="192.168.47.40")],
    [dict(row(), address="::ffff:192.168.47.4")], [dict(row(), id="b" * 32)],
    [row(), dict(row(), id="b" * 32)]])
def test_wrong_address_missing_duplicate_target_or_replaced_id_never_posts(pending):
    client = Client(pending)
    with pytest.raises(pairing.PairingError):
        pairing.execute(request("submit"), client)
    assert len(client.calls) == 1 and client.calls[0][0] == "GET"


def test_another_address_does_not_change_single_exact_selection():
    client = Client([row(), dict(row(), id="b" * 32, address="192.168.47.5")])
    assert pairing.execute(request("submit"), client)["host_accepted"]


@pytest.mark.parametrize("pin", [1234, "１２３４", "123", "12345", "1234\n", "abcd", None])
def test_invalid_pin_is_rejected_before_any_get(pin):
    value = dict(request("submit"), pin=pin)
    client = Client()
    with pytest.raises(pairing.PairingError, match="four_ascii"):
        pairing.execute(value, client)
    assert not client.calls


@pytest.mark.parametrize("changes", [dict(mode="delete"), dict(pairing_id=PAIRING_ID), dict(pin="2468"),
    dict(username="bad:user"), dict(password=""), dict(password=7), dict(url="https://remote.invalid")])
def test_inspect_request_cannot_smuggle_submit_or_remote_fields(changes):
    with pytest.raises(pairing.PairingError):
        pairing.validate_request(dict(request(), **changes))


@pytest.mark.parametrize("value", [dict(pairings=[row(), row()]), dict(pairings=[dict(row(), clientcert="never-echo")]),
    dict(pairings=[dict(row(), address="192.168.47.4.evil")]), dict(pairings=[dict(row(), name="bad\nname")]),
    dict(pairings="bad"), dict(pairings=[], extra="bad")])
def test_unexpected_server_fields_and_duplicate_ids_are_not_exposed(value):
    with pytest.raises(pairing.PairingError):
        pairing.parse_pending(value)


def test_host_rejection_has_no_retry_or_quest_success():
    client = Client(accepted=False)
    value = pairing.execute(request("submit"), client)
    assert not value["host_accepted"] and not value["quest_pairing_completed"] and len(client.calls) == 2


def test_duplicate_json_keys_are_rejected_without_echoing_values():
    with pytest.raises(pairing.PairingError, match="^duplicate_json_fields$"):
        pairing.strict_json('{"mode":"inspect","mode":"submit","pin":"fixture-secret"}')


class Guard:
    def __init__(self, failure_at=None):
        self.calls = 0
        self.failure_at = failure_at
    def verify(self):
        self.calls += 1
        if self.calls == self.failure_at:
            raise pairing.PairingError("host_pid_lifetime_mismatch")


class Connection:
    def __init__(self, certificate=PEER_DER, peer=("127.0.0.1", 47990), value=None, failure=False):
        self.calls, self.closed = [], False
        self.sock = SimpleNamespace(getpeername=lambda: peer, getpeercert=lambda **_: certificate)
        self.value = dict(pairings=[]) if value is None else value
        self.status, self.content_type, self.failure = 200, "application/json", failure
    def connect(self):
        self.calls.append("connect")
    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
    def getresponse(self):
        if self.failure:
            raise OSError("fixture-password-must-not-be-echoed")
        return SimpleNamespace(status=self.status, getheader=lambda *args: self.content_type,
            read=lambda maximum: json.dumps(self.value).encode()[:maximum])
    def close(self):
        self.closed = True


def pinned(connection, guard=None):
    factory_args = []
    def factory(*args, **kwargs):
        factory_args.append((args, kwargs))
        return connection
    binding = SimpleNamespace(certificate_sha256=hashlib.sha256(PEER_DER).hexdigest())
    return pairing.PinnedClient(binding, guard or Guard(), pairing.Credentials("fixture-user", "fixture-password"), factory), factory_args


@pytest.mark.parametrize("connection", [Connection(certificate=b"wrong"), Connection(peer=("192.168.47.4", 47990)),
    Connection(peer=("127.0.0.1", 443))])
def test_no_http_or_authentication_before_exact_tls_peer_pin(connection):
    client, _ = pinned(connection)
    with pytest.raises(pairing.PairingError):
        client.request("GET", "/api/pin")
    assert connection.calls == ["connect"] and connection.closed


def test_authenticated_request_only_after_pin_and_second_listener_lifetime_check():
    connection, guard = Connection(), Guard()
    client, args = pinned(connection, guard)
    assert client.request("GET", "/api/pin") == dict(pairings=[])
    assert guard.calls == 3 and connection.closed
    assert args[0][0] == ("127.0.0.1", 47990)
    assert args[0][1]["context"].minimum_version == ssl.TLSVersion.TLSv1_2
    sent = connection.calls[1]
    assert sent[:2] == ("GET", "/api/pin")
    assert base64.b64decode(sent[2]["headers"]["Authorization"][6:]) == b"fixture-user:fixture-password"
    assert "Origin" not in sent[2]["headers"] and "Referer" not in sent[2]["headers"]


def test_host_restart_during_tls_connect_prevents_http():
    connection = Connection()
    client, _ = pinned(connection, Guard(failure_at=2))
    with pytest.raises(pairing.PairingError, match="lifetime"):
        client.request("GET", "/api/pin")
    assert connection.calls == ["connect"] and connection.closed


def test_post_transport_error_is_unknown_not_retried_or_leaked():
    connection = Connection(failure=True)
    client, _ = pinned(connection)
    with pytest.raises(pairing.PairingError, match="^submit_result_unknown_no_retry$"):
        client.request("POST", "/api/pin", dict(pin="2468", pairing_id=PAIRING_ID))
    assert len(connection.calls) == 2 and connection.closed


def test_host_restart_after_post_reports_unknown():
    connection = Connection(value=dict(status=True))
    client, _ = pinned(connection, Guard(failure_at=3))
    with pytest.raises(pairing.PairingError, match="^submit_result_unknown_no_retry$"):
        client.request("POST", "/api/pin", dict(pin="2468", pairing_id=PAIRING_ID))


def test_redirect_is_not_followed_and_response_body_not_printed():
    connection = Connection(value=dict(secret="not public"))
    connection.status = 302
    client, _ = pinned(connection)
    with pytest.raises(pairing.PairingError, match="^pairing_http_status_not_ok$"):
        client.request("GET", "/api/pin")
    assert len(connection.calls) == 2


@pytest.fixture
def staged(tmp_path):
    host = tmp_path / "artifacts/host"
    dev, runtime = host / "dev", host / "runtime-fixture"
    dev.mkdir(parents=True)
    runtime.mkdir()
    cert = dev / "public.pem"
    cert.write_text(ssl.DER_cert_to_PEM_cert(PEER_DER))
    exe = runtime / "sunshine.exe"
    exe.write_bytes(b"synthetic executable fixture, never executed")
    access = dev / "web-access.clixml"
    access.write_text("synthetic fixture, never decrypted")
    config = runtime / "sunshine.conf"
    config.write_text(f"port = 47989\ncert = {cert}\n")
    manifest = dict(runtime=str(runtime), config=str(config), host_sha256=pairing.digest(exe),
        encrypted_web_access=str(access), port=47989, web_url="https://localhost:47990")
    for path in (dev / "launch.json", runtime / "manifest.json"):
        path.write_text(json.dumps(manifest))
    process = dict(process_id=567, owner_creation_filetime="1234567890", runtime=str(runtime), executable=str(exe))
    (dev / "process.json").write_text(json.dumps(process))
    return tmp_path, manifest, process


def test_staged_public_certificate_and_exact_process_lifetime_binding(staged):
    root, _, _ = staged
    binding = pairing.load_binding(root)
    assert binding.process_id == 567 and binding.creation_filetime == 1234567890
    assert binding.certificate_sha256 == hashlib.sha256(PEER_DER).hexdigest()
    binding.verify_files()
    assert "password" not in json.dumps(binding.public())


@pytest.mark.parametrize("filename", ["launch.json", "process.json"])
def test_metadata_replacement_after_binding_is_detected(staged, filename):
    root, _, _ = staged
    binding = pairing.load_binding(root)
    (root / "artifacts/host/dev" / filename).write_text("{}")
    with pytest.raises(pairing.PairingError, match="changed"):
        binding.verify_files()


@pytest.mark.parametrize("field,value", [("process_id", True), ("owner_creation_filetime", None),
    ("owner_creation_filetime", "123.0"), ("executable", "does-not-exist")])
def test_bad_process_record_rejected(staged, field, value):
    root, _, process = staged
    process[field] = value
    (root / "artifacts/host/dev/process.json").write_text(json.dumps(process))
    with pytest.raises((pairing.PairingError, FileNotFoundError)):
        pairing.load_binding(root)


@pytest.mark.parametrize("change", [dict(web_url="https://192.168.47.4:47990"), dict(port=443),
    dict(host_sha256="0" * 64), dict(encrypted_web_access="wrong-file")])
def test_untrusted_manifest_cannot_change_destination_or_binary(staged, change):
    root, manifest, _ = staged
    manifest.update(change)
    for path in (root / "artifacts/host/dev/launch.json", root / "artifacts/host/runtime-fixture/manifest.json"):
        path.write_text(json.dumps(manifest))
    with pytest.raises((pairing.PairingError, FileNotFoundError)):
        pairing.load_binding(root)


@pytest.mark.parametrize("bad", ["lifetime", "exe", "listener", "missing_listener", "exited"])
def test_native_process_or_port_identity_failure_blocks_http(staged, monkeypatch, bad):
    root, _, _ = staged
    binding = pairing.load_binding(root)
    guard = object.__new__(pairing.LiveHostGuard)
    guard.binding, guard.handle = binding, 1
    class Kernel:
        def WaitForSingleObject(self, *args):
            return 0 if bad == "exited" else 258
        def GetProcessTimes(self, handle, created, *args):
            value = C.cast(created, C.POINTER(pairing.W.FILETIME)).contents
            value.dwLowDateTime = binding.creation_filetime + (bad == "lifetime")
            return 1
        def QueryFullProcessImageNameW(self, handle, flags, buffer, size):
            buffer.value = str(binding.config if bad == "exe" else binding.executable)
            return 1
    guard.k = Kernel()
    listeners = [] if bad == "missing_listener" else [SimpleNamespace(status=pairing.psutil.CONN_LISTEN,
        laddr=SimpleNamespace(ip="0.0.0.0", port=47990), pid=999 if bad == "listener" else binding.process_id)]
    monkeypatch.setattr(pairing.psutil, "net_connections", lambda **_: listeners)
    with pytest.raises(pairing.PairingError):
        guard.verify()


def test_cli_never_echoes_secret_exception_content(monkeypatch, capsys):
    value = request("submit")
    monkeypatch.setattr(pairing.sys, "argv", ["pairing_host.py"])
    monkeypatch.setattr(pairing.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(value).encode())))
    def broken_binding():
        raise OSError("fixture-password-not-real and synthetic PIN must stay redacted")
    monkeypatch.setattr(pairing, "load_binding", broken_binding)
    assert pairing.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err)["code"] == "local_pairing_validation_failed"
    assert value["password"] not in captured.err and value["pin"] not in captured.err


def test_cli_rejects_arguments_before_stdin_is_read(monkeypatch, capsys):
    monkeypatch.setattr(pairing.sys, "argv", ["pairing_host.py", "--pin", "synthetic-not-accepted"])
    assert pairing.main() == 1
    captured = capsys.readouterr()
    assert json.loads(captured.err)["code"] == "use_stdin_not_arguments"
    assert "synthetic-not-accepted" not in captured.err


def test_response_limit_is_applied_before_exposing_server_fields():
    connection = Connection(value=dict(pairings=[], padding="x" * pairing.MAX_RESPONSE))
    client, _ = pinned(connection)
    with pytest.raises(pairing.PairingError, match="^pairing_response_too_large$"):
        client.request("GET", "/api/pin")


def test_public_install_pairs_one_private_lan_headset_without_developer_ip(tmp_path, monkeypatch):
    (tmp_path/'config').mkdir(exist_ok=True)
    (tmp_path/'config/distribution.json').write_text('{}')
    monkeypatch.setattr(pairing, 'ROOT', tmp_path)
    client = Client([dict(row(), address='192.168.25.83')])
    result = pairing.execute(request('submit'), client)
    assert result['host_accepted'] and result['quest_address'] == '192.168.25.83'
    assert len(client.calls) == 2


@pytest.mark.parametrize('addresses', [['127.0.0.1'], ['8.8.8.8'], ['169.254.1.2'],
    ['::1'], ['192.168.25.83', '10.10.1.9']])
def test_public_install_rejects_non_lan_or_ambiguous_requests(tmp_path, monkeypatch, addresses):
    (tmp_path/'config').mkdir(exist_ok=True)
    (tmp_path/'config/distribution.json').write_text('{}')
    monkeypatch.setattr(pairing, 'ROOT', tmp_path)
    client = Client([dict(row(), address=address, id=chr(97+i)*32) for i,address in enumerate(addresses)])
    with pytest.raises(pairing.PairingError): pairing.execute(request('submit'), client)
    assert len(client.calls) == 1


def write_development_headsets(root, headsets=None):
    path = root / pairing.DEVELOPMENT_HEADSETS
    path.parent.mkdir(exist_ok=True)
    value = dict(schema=1, headsets=headsets if headsets is not None else [
        dict(address="192.168.47.4", name="Meta Quest 2"),
        dict(address="192.168.47.16", name="Meta Quest 3")])
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


@pytest.mark.parametrize("address,name", [("192.168.47.4", "Meta Quest 2"), ("192.168.47.16", "Meta Quest 3")])
def test_development_allowlist_selects_one_headset_and_uses_its_local_name(tmp_path, address, name):
    write_development_headsets(tmp_path)
    client = Client([dict(row(), address=address, name="Untrusted client name"),
                     dict(row(), id="b" * 32, address="192.168.47.25")])
    inspected = pairing.execute(request(), client)
    assert inspected["quest_address"] == address
    assert inspected["exact_quest_requests"] == 1
    assert [item["address"] for item in inspected["eligible_requests"]] == [address]
    result = pairing.execute(request("submit"), client)
    assert result["host_accepted"] and result["quest_address"] == address
    assert client.calls[-1] == ("POST", "/api/pin", dict(pairing_id=PAIRING_ID, pin="2468", name=name))
    assert len(client.calls) == 3 and not result["quest_pairing_completed"]


@pytest.mark.parametrize("pending", [
    [dict(row(), address="192.168.47.25")],
    [dict(row(), address="::ffff:192.168.47.16")],
    [row(), dict(row(), id="b" * 32, address="192.168.47.16")],
    [dict(row(), id="b" * 32, address="192.168.47.16")],
    [dict(row(), address="192.168.47.16"), dict(row(), id="b" * 32, address="192.168.47.16")]])
def test_development_allowlist_still_requires_one_fresh_observed_request(tmp_path, pending):
    write_development_headsets(tmp_path)
    client = Client(pending)
    with pytest.raises(pairing.PairingError):
        pairing.execute(request("submit"), client)
    assert len(client.calls) == 1 and client.calls[0][0] == "GET"


def test_development_override_does_not_implicitly_keep_legacy_address(tmp_path):
    write_development_headsets(tmp_path, [dict(address="192.168.47.16", name="Meta Quest 3")])
    client = Client()
    with pytest.raises(pairing.PairingError, match="one_exact"):
        pairing.execute(request("submit"), client)
    assert len(client.calls) == 1


@pytest.mark.parametrize("data", [
    b"not json", b"\xff", b"[]", b"{}", b'{"schema":1,"headsets":[],"extra":1}',
    b'{"schema":true,"headsets":[]}', b'{"schema":1.0,"headsets":[]}',
    b'{"schema":2,"headsets":[]}', b'{"schema":1,"headsets":[]}',
    b'{"schema":1,"schema":1,"headsets":[]}', b'{"schema":1,"headsets":{}}',
    b'{"schema":1,"headsets":[null]}', b'{"schema":1,"headsets":[{"address":"192.168.47.16"}]}',
    b'{"schema":1,"headsets":[{"address":"192.168.47.16","address":"192.168.47.4","name":"Quest"}]}',
    b" " * (pairing.MAX_REQUEST + 1)])
def test_invalid_development_configuration_never_falls_back_or_contacts_host(tmp_path, data):
    path = write_development_headsets(tmp_path)
    path.write_bytes(data)
    client = Client()
    with pytest.raises(pairing.PairingError, match="^invalid_development_headsets$"):
        pairing.execute(request("submit"), client)
    assert not client.calls


@pytest.mark.parametrize("change", [
    dict(address="192.168.47.0/24"), dict(address="192.168.47.16:47984"), dict(address="192.168.47.016"),
    dict(address=" 192.168.47.16"), dict(address="127.0.0.1"), dict(address="8.8.8.8"),
    dict(address="169.254.1.1"), dict(address="100.64.1.1"), dict(address="::1"),
    dict(address="::ffff:192.168.47.16"), dict(address="fc00::1"), dict(address=123),
    dict(name=""), dict(name=" Quest"), dict(name="Quest\n3"), dict(name="Quest\x7f"),
    dict(name="x" * 129), dict(name=3), dict(extra=True)])
def test_development_configuration_requires_exact_rfc1918_ip_and_safe_local_name(tmp_path, change):
    write_development_headsets(tmp_path, [dict(dict(address="192.168.47.16", name="Meta Quest 3"), **change)])
    client = Client()
    with pytest.raises(pairing.PairingError, match="^invalid_development_headsets$"):
        pairing.execute(request("submit"), client)
    assert not client.calls


@pytest.mark.parametrize("rows", [
    [dict(address="192.168.47.16", name="Quest 3"), dict(address="192.168.47.16", name="Another Quest")],
    [dict(address=f"192.168.47.{number + 1}", name=f"Quest {number}") for number in range(33)]])
def test_development_configuration_rejects_duplicate_addresses_and_excess_targets(tmp_path, rows):
    write_development_headsets(tmp_path, rows)
    with pytest.raises(pairing.PairingError, match="^invalid_development_headsets$"):
        pairing.development_headsets(tmp_path)


@pytest.mark.parametrize("address", ["10.20.30.40", "172.16.1.2", "172.31.254.3", "192.168.25.83"])
def test_development_allowlist_accepts_explicit_ips_in_all_rfc1918_ranges(tmp_path, address):
    write_development_headsets(tmp_path, [dict(address=address, name="Quest 3")])
    client = Client([dict(row(), address=address)])
    assert pairing.execute(request("submit"), client)["host_accepted"]


@pytest.mark.parametrize("initial", [True, False])
def test_development_override_change_during_fresh_get_prevents_submit(tmp_path, initial):
    if initial:
        write_development_headsets(tmp_path)
    else:
        (tmp_path / pairing.DEVELOPMENT_HEADSETS).unlink()
    class ChangingClient(Client):
        def request(self, method, path, body=None):
            result = super().request(method, path, body)
            if method == "GET":
                write_development_headsets(tmp_path, [dict(address="192.168.47.4", name="Changed target")])
            return result
    client = ChangingClient()
    expected = "development_headsets_changed" if initial else "submit_requires_one_exact_quest_request"
    with pytest.raises(pairing.PairingError, match="^" + expected + "$"):
        pairing.execute(request("submit"), client)
    assert len(client.calls) == 1


def test_public_install_ignores_private_development_override(tmp_path):
    path = write_development_headsets(tmp_path)
    path.write_text("intentionally malformed private configuration", encoding="utf-8")
    (tmp_path / "config/distribution.json").write_text("{}")
    client = Client([dict(row(), address="192.168.25.83")])
    assert pairing.execute(request("submit"), client)["host_accepted"]
    assert len(client.calls) == 2


def test_absent_development_configuration_has_no_implicit_target(tmp_path):
    (tmp_path / pairing.DEVELOPMENT_HEADSETS).unlink()
    assert pairing.development_headsets(tmp_path) == ({}, None)
    client = Client()
    value = pairing.execute(request(), client)
    assert value["pending"] == [row()]
    assert value["quest_address"] is None
    assert value["exact_quest_requests"] == 0 and value["eligible_requests"] == []
    assert not value["pairing_changed"] and not value["quest_pairing_completed"]
    assert [call[0] for call in client.calls] == ["GET"]


def test_absent_development_configuration_never_posts_observed_lan_request(tmp_path):
    (tmp_path / pairing.DEVELOPMENT_HEADSETS).unlink()
    client = Client()
    with pytest.raises(pairing.PairingError, match="^submit_requires_one_exact_quest_request$"):
        pairing.execute(request("submit"), client)
    assert [call[0] for call in client.calls] == ["GET"]


def test_public_install_requires_fresh_exact_id_before_post(tmp_path):
    (tmp_path / "config/distribution.json").write_text("{}")
    client = Client([dict(row(), address="192.168.25.83", id="b" * 32)])
    with pytest.raises(pairing.PairingError, match="^pending_id_changed_or_no_longer_selected$"):
        pairing.execute(request("submit"), client)
    assert [call[0] for call in client.calls] == ["GET"]
