"""Bounded private parent/child control messages, not a remote authorization API."""
import json
import re

MAX_LINE_BYTES = 8192
MAX_SEQUENCE = (1 << 64) - 1


def identifier(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{32}", value) or value == "0" * 32:
        raise ValueError("Expected a nonzero lowercase 128-bit identifier")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate control key")
        result[key] = value
    return result


def decode_request(line: bytes, worker_nonce: str, expected_sequence: int):
    """Validate one complete bounded UTF-8 line before dispatching any operation."""
    identifier(worker_nonce)
    if (type(line) is not bytes or not line.endswith(b"\n") or len(line) > MAX_LINE_BYTES + 1
            or not line[:-1] or b"\n" in line[:-1] or b"\r" in line or b"\0" in line):
        raise ValueError("Invalid bounded control line")
    try:
        value = json.loads(line[:-1].decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Invalid control JSON") from error
    if type(value) is not dict or set(value) != {"v", "worker_nonce", "seq", "op", "data"}:
        raise ValueError("Unknown or missing control envelope fields")
    if type(value["v"]) is not int or value["v"] != 1 or value["worker_nonce"] != worker_nonce:
        raise ValueError("Wrong private worker protocol or nonce")
    seq = value["seq"]
    if (type(seq) is not str or not re.fullmatch(r"[1-9][0-9]{0,19}", seq)
            or int(seq) != expected_sequence or int(seq) > MAX_SEQUENCE):
        raise ValueError("Stale, skipped or invalid control sequence")
    if (type(value["op"]) is not str or not re.fullmatch(r"[a-z_]{1,32}", value["op"])
            or type(value["data"]) is not dict):
        raise ValueError("Invalid operation or data object")
    return value


def encode_message(value):
    line = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if not line or len(line) > MAX_LINE_BYTES or b"\n" in line or b"\r" in line or b"\0" in line:
        raise ValueError("Control response exceeds bounded line")
    return line + b"\n"


def fields(data, required=(), optional=()):
    if type(data) is not dict or not set(required) <= set(data) or set(data) - set(required) - set(optional):
        raise ValueError("Unknown or missing operation fields")


def unsigned_decimal(value, name):
    if (type(value) is not str or not re.fullmatch(r"0|[1-9][0-9]{0,19}", value)
            or int(value) > MAX_SEQUENCE):
        raise ValueError(f"Invalid {name}")
    return int(value)
