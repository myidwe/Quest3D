"""Redact local identity and credentials when preparing diagnostics for sharing.

This transforms copies only. It never edits live settings, pairing state or logs.
Pattern matching is a precaution, not proof that arbitrary text is safe to publish;
the exported notice still asks the user to review a report before sharing it.
"""
from __future__ import annotations

import ipaddress
import json
import re
from collections.abc import Mapping

REDACTED = "[redacted]"

_SECRET_KEYS = re.compile(
    r"password|passwd|passphrase|credential|secret|token|authorization|"
    r"privatekey|sessionkey|apikey|pairingkey|pairingid|clientcertificate|"
    r"certificatepem|pin(?:code)?$|^pwd$|^cookie$|^cookies$", re.IGNORECASE)
_IDENTITY_KEYS = re.compile(
    r"address|hostname|computername|username|email|serial|deviceid|devicename|"
    r"endpointid|macaddress|sessionid|requestid|appliedrequest|seenrequest|"
    r"rejectedrequest|selectedmonitor|monitordevice|friendlyname|"
    r"^user$|^owner$|^name$|^id$|^ip$|^hostip$|^clientip$", re.IGNORECASE)
_PATH_KEYS = re.compile(
    r"path|directory|filename|^file$|^exe$|^executable$|^cwd$|^home$|"
    r"^arguments$|^argv$|^commandline$|^lastdiagnostics$", re.IGNORECASE)
_PEM = re.compile(
    r"-----BEGIN [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----.*?"
    r"(?:-----END [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----|\Z)", re.DOTALL)
_URL = re.compile(r"\b(?:https?|rtsp|wss?)://[^\s\"'<>]+", re.IGNORECASE)
_EMAIL = re.compile(r"\b[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_MAC = re.compile(r"(?<![\w])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![\w])", re.IGNORECASE)
_UUID = re.compile(r"\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b", re.IGNORECASE)
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")
_IPV6 = re.compile(r"(?<![\w:])[0-9a-f:]*:[0-9a-f:]+(?:%[\w.-]+)?(?![\w:])", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"(?:\b[A-Z]:[\\/]|\\\\)[^\r\n\"'<>|]+", re.IGNORECASE)
_POSIX_PATH = re.compile(r"(?<![\w])/(?:home|Users|mnt|media|tmp|var|opt|usr|srv|build|workspace|workspaces|root|etc)(?:/[^\r\n\"'<>|]*)?", re.IGNORECASE)
_MEDIA_NAME = re.compile(r"(?<![\w])[^\s\"'<>|]+\.(?:jpe?g|png|webp|bmp|gif|mp4|mkv|webm|mov|avi|mp3|wav|flac)(?![\w])", re.IGNORECASE)
_SECRET_VALUE = re.compile(
    r"(?i)(?:--creds\b|--(?:password|passwd|token|secret|api-key|pin|key)\b|"
    r"\b(?:password|passwd|passphrase|pwd|token|secret|api[_-]?key|"
    r"authorization|bearer|cookie|pin|serial|hostname|username|device[_-]?id)"
    r"\s*(?:=|:|\bis\b))[^\r\n]*")
_OPAQUE = re.compile(r"(?<![\w])(?:[A-Za-z0-9_-]{32,}|[A-Za-z0-9+/]{40,}={0,2})(?![\w])")
_ADB_SERIAL = re.compile(r"\badb(?:\.exe)?\s+-s\s+\S+", re.IGNORECASE)


class DiagnosticRedactor:
    """A reusable, deterministic redactor; supplied markers never enter output."""

    def __init__(self, private_markers=()):
        self._markers = tuple(sorted(
            {str(value) for value in private_markers if value and len(str(value)) >= 3},
            key=len, reverse=True))
        self.redactions = 0

    def _replacement(self, _match=None):
        self.redactions += 1
        return REDACTED

    def text(self, value: str) -> str:
        # A bounded log tail may start inside a PEM rather than at its header.
        # Remove a partial PEM remainder if an END marker is visible.
        value = _PEM.sub(self._replacement, value)
        if re.search(r"-----END [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----", value):
            value = re.sub(r"\A.*?-----END [A-Z0-9 ]*(?:PRIVATE KEY|CERTIFICATE)-----",
                           self._replacement, value, count=1, flags=re.DOTALL)
        value = _SECRET_VALUE.sub(self._replacement, value)
        value = _ADB_SERIAL.sub(self._replacement, value)
        value = _URL.sub(self._replacement, value)
        # Redact the complete path before replacing its private root marker.
        # Otherwise a replacement root could leave a private filename behind.
        for pattern in (_WINDOWS_PATH, _POSIX_PATH, _MEDIA_NAME):
            value = pattern.sub(self._replacement, value)
        for marker in self._markers:
            value = re.sub(re.escape(marker), self._replacement, value, flags=re.IGNORECASE)
        for pattern in (_EMAIL, _MAC, _UUID, _IPV4, _IPV6):
            if pattern in (_IPV4, _IPV6):
                def replace_address(match):
                    try:
                        ipaddress.ip_address(match.group(0).split('%', 1)[0])
                    except ValueError:
                        return match.group(0)
                    return self._replacement(match)
                value = pattern.sub(replace_address, value)
            else:
                value = pattern.sub(self._replacement, value)
        value = _OPAQUE.sub(self._replacement, value)
        return value

    def value(self, value):
        """Return a redacted JSON-safe copy, keeping timing and quality numbers."""
        if isinstance(value, Mapping):
            result = {}
            for key, item in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
                sensitive = (_SECRET_KEYS.search(normalized) or
                             _IDENTITY_KEYS.search(normalized) or
                             _PATH_KEYS.search(normalized) or
                             (normalized in {'host', 'client', 'device', 'endpoint'} and isinstance(item, str)))
                result[self.text(str(key))] = self._replacement() if sensitive and item is not None else self.value(item)
            return result
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        if isinstance(value, str):
            return self.text(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        # Do not invoke repr() on unknown objects; it may contain private fields.
        return self._replacement()

    def log(self, value: str) -> str:
        """Parse JSONL first so escaped paths and credential values are covered."""
        lines = []
        for line in value.splitlines(keepends=True):
            try:
                parsed = json.loads(line)
            except (ValueError, TypeError):
                lines.append(line)
            else:
                lines.append(json.dumps(self.value(parsed), ensure_ascii=False) + ('\n' if line.endswith('\n') else ''))
        # Cross-line PEM blocks are handled after the JSONL pass.
        return self.text(''.join(lines))
