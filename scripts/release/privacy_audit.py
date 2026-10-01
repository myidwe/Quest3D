"""Inspect release bytes without emitting matched private values.

Run against the exported repository and *final* signed bundles. All files,
including native libraries and fonts, are scanned. Archives are read in memory;
none of their code is executed or extracted. zstandard==0.25.0 is required for
Godot token buffers and .tar.zst sources. A missing decoder fails the gate.

Review records are exact SHA256/category contracts, not filename exemptions.
Known private markers can never be waived, including inside upstream archives.
Generic publisher/example home paths remain review findings unless their exact
bytes or enclosing upstream archive have a recorded public origin.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import struct
import tarfile
import time
import zipfile


ARCHIVE_SUFFIXES = (".zip", ".apk", ".aar", ".jar", ".whl", ".tar", ".tar.gz", ".tgz",
                    ".tar.xz", ".txz", ".tar.bz2", ".tbz2", ".tar.zst")
PRIVATE_SUFFIXES = {".p12", ".pfx", ".jks", ".keystore", ".dpapi", ".clixml", ".pdb"}
PRIVATE_NAMES = {".env", "sunshine_state.json", "sunshine.conf", "hosts.yml", "hosts.yaml",
                 "credentials.json", "paired-clients.json", "pairing-state.json", "app_state.cfg",
                 "host_state.cfg", "desktop.json", "development-headsets.json"}
PATTERNS = {
    "github-token": rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b",
    "aws-access-key": rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "huggingface-token": rb"\bhf_[A-Za-z0-9]{30,}\b",
    "openai-token": rb"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}\b",
    "generic-user-home-path": rb"(?i)(?:[A-Z]:[\\/](?:Users|Documents and Settings)[\\/][^\s\"\x00<>]{1,100}|\/home\/[^\s/\"\x00<>]{1,80}/|\/Users\/[^\s/\"\x00<>]{1,80}/)",
}
TOKEN_CATEGORIES = {"github-token", "aws-access-key", "huggingface-token", "openai-token"}
# A leading zero-width \b makes the regex engine visit every byte, including
# gigabytes of source archives. Find the fixed token prefix first, then enforce
# the same ASCII word boundaries on the few actual candidates below.
COMPILED_PATTERNS = {category: re.compile(pattern.removeprefix(rb"\b") if category in TOKEN_CATEGORIES else pattern)
                     for category, pattern in PATTERNS.items()}
PEM = re.compile(rb"-----BEGIN ((?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY)-----([\s\S]{0,16384}?)-----END \1-----")
UTF16 = {"utf-16le": re.compile(rb"(?:[\x09\x0a\x0d\x20-\x7e]\x00){8,}"),
         "utf-16be": re.compile(rb"(?:\x00[\x09\x0a\x0d\x20-\x7e]){8,}")}
SHA256 = re.compile(r"^[a-f0-9]{64}$")
SETUP_MAGIC_PREFIX = b"Q3DSETUPZIP"
SETUP_BOOTSTRAP_PREFIX = "Q3D_SETUP_BOOTSTRAP_"
_SETUP_PAYLOAD_MODULE = None


def claims_setup_payload(data: bytes) -> bool:
    """Recognize reserved installer claims even when their footer is damaged."""
    return SETUP_MAGIC_PREFIX in data or any(
        SETUP_BOOTSTRAP_PREFIX.encode(encoding) in data
        for encoding in ("ascii", "utf-16le", "utf-16be"))


def setup_payload_module():
    """Load the sibling format validator under CLI and spec-based callers."""
    global _SETUP_PAYLOAD_MODULE
    if _SETUP_PAYLOAD_MODULE is None:
        spec = importlib.util.spec_from_file_location("quest3d_privacy_exe_payload", Path(__file__).with_name("exe_payload.py"))
        if spec is None or spec.loader is None:
            raise ImportError("Setup payload validator unavailable")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _SETUP_PAYLOAD_MODULE = module
    return _SETUP_PAYLOAD_MODULE


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def unicode_marker_pattern(value: str, encoding: str) -> re.Pattern:
    """Case variants of characters, without lowercasing arbitrary UTF-16 bytes."""
    units = []
    for character in value:
        variants = []
        for variant in dict.fromkeys((character, character.casefold(), character.upper())):
            encoded = []
            for letter in variant:
                if letter.isascii() and letter.isalpha():
                    encoded.append(b"(?:" + re.escape(letter.lower().encode(encoding)) + b"|" + re.escape(letter.upper().encode(encoding)) + b")")
                else:
                    encoded.append(re.escape(letter.encode(encoding)))
            variants.append(b"".join(encoded))
        units.append(b"(?:" + b"|".join(variants) + b")")
    return re.compile(b"".join(units))


def default_private_markers() -> dict[str, list[str]]:
    """Discover local identity without embedding an owner's identity in source."""
    home = Path.home()
    markers: dict[str, list[str]] = {"private-home": [str(home), home.as_posix()]}
    accounts = {home.name, os.environ.get("USERNAME", ""), os.environ.get("USER", "")}
    generic = {"root", "user", "admin", "administrator", "runner", "runneradmin", "public", "default"}
    markers["private-account"] = sorted(v for v in accounts if len(v) >= 3 and v.casefold() not in generic)
    computers = {os.environ.get("COMPUTERNAME", ""), socket.gethostname()}
    markers["private-computer"] = sorted(v for v in computers if len(v) >= 3)
    return {category: list(dict.fromkeys(values)) for category, values in markers.items() if values}


def read_markers(path: Path | None) -> dict[str, list[str]]:
    markers = default_private_markers()
    if path is not None:
        # Keep this file outside the public export. Neither values nor its path
        # are included in the result. Serial/email markers belong here.
        additional = json.loads(path.read_text("utf-8-sig"))
        if not isinstance(additional, dict):
            raise ValueError("Invalid private marker configuration")
        for category, values in additional.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", category):
                raise ValueError("Invalid private marker category")
            if not isinstance(values, list) or not all(isinstance(v, str) and len(v) >= 3 for v in values):
                raise ValueError("Invalid private marker values")
            markers.setdefault(category, []).extend(values)
    return markers


def read_reviews(path: Path | None) -> list[dict]:
    if path is None:
        return []
    records = json.loads(path.read_text("utf-8-sig"))
    if not isinstance(records, list):
        raise ValueError("Invalid review record list")
    for record in records:
        if not SHA256.fullmatch(record.get("sha256", "")):
            raise ValueError("Review requires an exact SHA256")
        scope = record.get("scope", "payload")
        if scope not in {"payload", "upstream-archive", "synthetic-fixture"}:
            raise ValueError("Invalid review scope")
        if not record.get("reason"):
            raise ValueError("Review requires a reason")
        if scope == "synthetic-fixture":
            origin = record.get("origin", "")
            if not re.fullmatch(r"repository:tests/[A-Za-z0-9_./-]+", origin) or ".." in origin:
                raise ValueError("Synthetic review requires a repository test source origin")
        elif not record.get("origin", "").startswith("https://"):
            raise ValueError("Review requires a public HTTPS origin")
        if not isinstance(record.get("categories"), list) or not record["categories"]:
            raise ValueError("Review requires explicit categories")
        if any(category.startswith("private-marker:") for category in record["categories"]):
            raise ValueError("Private marker findings cannot be waived")
    return records


class Auditor:
    def __init__(self, markers: dict[str, list[str]] | None = None, reviews: list[dict] | None = None,
                 max_depth: int = 12, max_expanded_bytes: int = 16 * 1024**3):
        self.markers = markers if markers is not None else default_private_markers()
        self.reviews = reviews or []
        self.max_depth = max_depth
        self.max_expanded_bytes = max_expanded_bytes
        self.findings: list[dict] = []
        self.errors: list[dict] = []
        self.inputs: list[dict] = []
        self.counts: collections.Counter = collections.Counter()
        self.cache: dict[str, list[dict]] = {}
        self.unicode_patterns = {(value, encoding): unicode_marker_pattern(value, encoding)
                                 for values in self.markers.values() for value in values if not value.isascii()
                                 for encoding in ("utf-8", "utf-16le", "utf-16be")}
        self.started = time.monotonic()

    def safe_path(self, path: str) -> str:
        for values in self.markers.values():
            for value in values:
                path = re.sub(re.escape(value), "<redacted>", path, flags=re.IGNORECASE)
        # Unrecognized personal archive names must not be repeated in reports.
        path = re.sub(r"(?i)(?:[A-Z]:[\\/]Users[\\/]|\/home\/|\/Users\/)[^/\\!\s]+", "<redacted-home>", path)
        path = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "<redacted-email>", path, flags=re.I)
        return path

    def error(self, path: str, category: str, error: Exception | None = None) -> None:
        record = {"path": self.safe_path(path), "category": category}
        if error is not None:
            record["error_type"] = type(error).__name__
        self.errors.append(record)  # Exception text may contain private names.

    def scan_bytes(self, data: bytes) -> list[dict]:
        sha = digest(data)
        if sha in self.cache:
            self.counts["identical_payload_scan_reused"] += 1
            return self.cache[sha]
        result: list[dict] = []
        lower = data.lower()
        for category, values in self.markers.items():
            for encoding in ("utf-8", "utf-16le", "utf-16be"):
                offsets: set[int] = set()
                for value in values:
                    if not value.isascii():
                        offsets.update(m.start() for m in self.unicode_patterns[value, encoding].finditer(data))
                        continue
                    needle = value.casefold().encode(encoding)
                    pos = lower.find(needle)
                    while pos >= 0:
                        offsets.add(pos)
                        pos = lower.find(needle, pos + 1)
                if offsets:
                    result.append({"category": "private-marker:" + category, "encoding": encoding,
                                   "count": len(offsets), "offsets": sorted(offsets)[:20]})

        def patterns(buffer: bytes, encoding: str, start: int = 0, multiplier: int = 1) -> None:
            for category, pattern in COMPILED_PATTERNS.items():
                if category in TOKEN_CATEGORIES:
                    def word(value: int) -> bool:
                        return 48 <= value <= 57 or 65 <= value <= 90 or 97 <= value <= 122 or value == 95
                    matches = []
                    position = 0
                    while match := pattern.search(buffer, position):
                        if match.start() and word(buffer[match.start() - 1]):
                            # A rejected enclosing candidate must not hide a
                            # valid token beginning inside its greedy body.
                            position = match.start() + 1
                        else:
                            matches.append(match)
                            position = match.end()
                else:
                    matches = list(pattern.finditer(buffer))
                if matches:
                    result.append({"category": category, "encoding": encoding, "count": len(matches),
                                   "offsets": [start + m.start() * multiplier for m in matches[:20]]})
            # TLS parsers contain BEGIN/END labels without a private key. Require
            # substantial base64 payload as well as the complete matching labels.
            matches = [m for m in PEM.finditer(buffer)
                       if re.search(rb"[A-Za-z0-9+/]{64,}", m.group(2))
                       and len(re.sub(rb"[^A-Za-z0-9+/=]", b"", m.group(2))) >= 128]
            if matches:
                result.append({"category": "private-key-block", "encoding": encoding, "count": len(matches),
                               "offsets": [start + m.start() * multiplier for m in matches[:20]]})

        patterns(data, "byte-ascii")
        if b"\x00" in data:  # Exact non-ASCII UTF-16 owner markers were still scanned above.
            for encoding, pattern in UTF16.items():
                for string in pattern.finditer(data):
                    patterns(string.group().decode(encoding).encode("ascii"), encoding, string.start(), 2)
        self.cache[sha] = result
        self.counts["unique_content_scanned"] += 1
        return result

    def record(self, path: str, sha: str, finding: dict, authorities: tuple[str, ...]) -> None:
        category = finding["category"]
        reviewed = None
        # Exact private markers have absolute precedence over every review.
        if not category.startswith("private-marker:"):
            for review in self.reviews:
                scope = review.get("scope", "payload")
                # An uncompressed original tar also contains its fixture text
                # directly. The reviewed archive itself and its descendants
                # have the same public-origin contract, never private markers.
                match = (review["sha256"] == sha or review["sha256"] in authorities) if scope == "upstream-archive" else review["sha256"] == sha
                if match and category in review["categories"]:
                    if finding["count"] <= review.get("max_count", finding["count"]):
                        reviewed = {"origin": review["origin"], "reason": review["reason"], "authority_sha256": review["sha256"], "scope": scope}
                        break
        status = ("reviewed-synthetic-fixture" if reviewed["scope"] == "synthetic-fixture" else "reviewed-public-origin") if reviewed else "review-required" if category == "generic-user-home-path" else "blocked"
        record = dict(finding, path=self.safe_path(path), sha256=sha, status=status)
        if reviewed:
            record["review"] = reviewed
        self.findings.append(record)

    def inspect(self, path: str, data: bytes, depth: int = 0, authorities: tuple[str, ...] = ()) -> None:
        self.counts["payload_occurrences"] += 1
        self.counts["bytes_read"] += len(data)
        if self.counts["bytes_read"] > self.max_expanded_bytes:
            self.error(path, "expanded-byte-limit")
            return
        sha = digest(data)
        for finding in self.scan_bytes(data):
            self.record(path, sha, finding, authorities)
        name = PurePosixPath(path.split("!")[-1].replace("\\", "/")).name.lower()
        if PurePosixPath(name).suffix in PRIVATE_SUFFIXES or name in PRIVATE_NAMES:
            self.record(path, sha, {"category": "private-state-or-key-file", "count": 1, "encoding": "filename", "offsets": []}, authorities)
        is_setup = (data.startswith(b"MZ") or name.endswith(".exe")) and claims_setup_payload(data)
        is_archive = name.endswith(ARCHIVE_SUFFIXES) or data.startswith(b"PK\x03\x04")
        if is_setup:
            if depth >= self.max_depth:
                self.error(path, "setup-exe-depth-limit")
            else:
                self.setup_exe(path, data, depth, authorities + (sha,))
        elif is_archive:
            if depth >= self.max_depth:
                self.error(path, "archive-depth-limit")
            else:
                self.archive(path, data, depth, authorities + (sha,))
        elif name.endswith(".gdc") or data.startswith(b"GDSC"):
            self.godot(path, data, authorities)

    def setup_exe(self, path: str, data: bytes, depth: int, authorities: tuple[str, ...]) -> None:
        try:
            validator = setup_payload_module()
            if not validator.has_setup_marker(data):
                raise ValueError("Unrecognized Quest3D setup format")
            payload, metadata = validator.read_payload(data)
            self.counts["setup_exe_payloads_decoded"] += 1
            self.counts["setup_exe_stub_bytes_inspected"] += metadata["stub_bytes"]
            self.counts["setup_exe_payload_bytes_inspected"] += metadata["payload_bytes"]
            # The EXE itself was scanned above. Inspect the verified ZIP as a
            # separate container, including every filename, comment and child.
            self.inspect(path + "!embedded-payload.zip", payload, depth + 1, authorities)
        except Exception as error:
            self.error(path, "setup-exe-inspection-failed", error)

    def archive(self, path: str, data: bytes, depth: int, authorities: tuple[str, ...]) -> None:
        self.counts["archives_opened"] += 1
        try:
            stream = io.BytesIO(data)
            if zipfile.is_zipfile(stream):
                stream.seek(0)
                with zipfile.ZipFile(stream) as archive:
                    for member in archive.infolist():
                        # Filenames/comments are data too, not just payloads.
                        self.inspect_metadata(path, member.filename.encode("utf-8") + member.comment, authorities)
                        if not member.is_dir():
                            self.inspect(path + "!" + member.filename, archive.read(member), depth + 1, authorities)
                    self.inspect_metadata(path, archive.comment, authorities)
            else:
                stream.seek(0)
                if path.lower().endswith(".zst"):
                    import zstandard
                    with zstandard.ZstdDecompressor().stream_reader(stream) as reader:
                        expanded = reader.read(self.max_expanded_bytes + 1)
                    if len(expanded) > self.max_expanded_bytes:
                        raise ValueError("Expanded archive exceeds limit")
                    stream = io.BytesIO(expanded)
                with tarfile.open(fileobj=stream, mode="r:*") as archive:
                    for member in archive:
                        metadata = "\n".join((member.name, member.uname, member.gname, member.linkname,
                                               json.dumps(member.pax_headers, ensure_ascii=False))).encode("utf-8")
                        self.inspect_metadata(path, metadata, authorities)
                        if member.isfile():
                            file = archive.extractfile(member)
                            if file is None:
                                raise ValueError("Missing archive member")
                            self.inspect(path + "!" + member.name, file.read(), depth + 1, authorities)
        except Exception as error:
            self.error(path, "archive-inspection-failed", error)

    def inspect_metadata(self, path: str, data: bytes, authorities: tuple[str, ...]) -> None:
        for finding in self.scan_bytes(data):
            self.record(path + "::archive-metadata", digest(data), finding, authorities)
        self.counts["archive_metadata_records_scanned"] += 1

    def godot(self, path: str, raw: bytes, authorities: tuple[str, ...]) -> None:
        try:
            if len(raw) < 12 or raw[:4] != b"GDSC":
                raise ValueError("Invalid compiled Godot container")
            version, expected = struct.unpack_from("<II", raw, 4)
            if version != 101 or expected > self.max_expanded_bytes:
                raise ValueError("Unsupported Godot format")
            if expected:
                import zstandard
                data = zstandard.ZstdDecompressor().decompress(raw[12:], max_output_size=expected)
                if len(data) != expected:
                    raise ValueError("Godot decoded size mismatch")
            else:
                data = raw[12:]
            count, _, _, _ = struct.unpack_from("<IIII", data)
            pos = 16
            identifiers = []
            for _ in range(count):
                length = struct.unpack_from("<I", data, pos)[0]
                pos += 4
                end = pos + length * 4
                if end > len(data):
                    raise ValueError("Truncated Godot identifier")
                identifiers.append(bytes(v ^ 0xB6 for v in data[pos:end]).decode("utf-32le"))
                pos = end
            for label, decoded in (("decoded-token-buffer", data), ("decoded-identifiers", "\n".join(identifiers).encode("utf-8"))):
                for finding in self.scan_bytes(decoded):
                    self.record(path + "::" + label, digest(raw), finding, authorities)
            self.counts["godot_scripts_decoded"] += 1
            self.counts["godot_identifiers_decoded"] += count
        except Exception as error:
            self.error(path, "godot-inspection-failed", error)

    def audit(self, paths: list[Path]) -> dict:
        for path in paths:
            if path.is_dir():
                for file in sorted(path.rglob("*")):
                    relative = file.relative_to(path)
                    if ".git" in relative.parts:
                        continue  # History is a distinct required audit step.
                    if file.is_symlink():
                        self.error(relative.as_posix(), "directory-symlink-not-followed")
                    elif file.is_file():
                        self.input_file(file, relative.as_posix())
            elif path.is_file():
                self.input_file(path, path.name)
            else:
                self.error(path.name, "input-not-found")
        return self.report()

    def input_file(self, path: Path, name: str) -> None:
        try:
            data = path.read_bytes()
            self.inputs.append({"path": self.safe_path(name), "sha256": digest(data), "bytes": len(data)})
            self.inspect_metadata(name, name.encode("utf-8"), ())
            self.inspect(name, data)
        except Exception as error:
            self.error(name, "input-read-failed", error)

    def report(self) -> dict:
        statuses = collections.Counter(f["status"] for f in self.findings)
        return {"schema": 1, "kind": "actual-final-byte-privacy-gate", "passed": not self.errors and not statuses["blocked"] and not statuses["review-required"],
                "private_marker_gate_passed": not self.errors and not any(f["category"].startswith("private-marker:") for f in self.findings),
                "inputs": self.inputs, "counts": dict(self.counts), "findings": self.findings, "errors": self.errors,
                "findings_by_status": dict(statuses), "findings_by_category": dict(collections.Counter(f["category"] for f in self.findings)),
                "private_marker_categories": sorted(self.markers), "matched_values_emitted": False,
                "elapsed_seconds": round(time.monotonic() - self.started, 2),
                "limitations": ["Known local identifiers and recognizable credential formats are inspected; unknown encrypted/obfuscated data cannot be proven absent.",
                                "Public upstream test keys and publisher paths require exact byte-origin review and are never described as owner secrets.",
                                "Git reachable history, remote caches and media ownership/rights need separate checks."]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", type=Path, nargs="+")
    parser.add_argument("--marker-file", type=Path, help="Private JSON category-to-values map; never copy it into a release")
    parser.add_argument("--review-file", type=Path, help="Exact SHA256/category public-origin review records")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = Auditor(read_markers(args.marker_file), read_reviews(args.review_file)).audit(args.paths)
    except Exception as error:
        # Never print parser values, user paths, exception messages or secrets.
        print(json.dumps({"passed": False, "error_type": type(error).__name__, "matched_values_emitted": False}))
        return 2
    try:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", "utf-8")
    except Exception as error:
        print(json.dumps({"passed": False, "category": "audit-report-write-failed", "error_type": type(error).__name__, "matched_values_emitted": False}))
        return 2
    print(json.dumps({"passed": report["passed"], "counts": report["counts"], "findings_by_status": report["findings_by_status"],
                      "errors": len(report["errors"]), "elapsed_seconds": report["elapsed_seconds"]}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
