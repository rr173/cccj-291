"""Signed, restart-surviving pagination cursors.

A cursor is opaque to clients, bound to one conversation, one user and one
membership instance, so a removed-and-re-added member cannot reuse an old
cursor. The signing key is persisted in the database, so cursors created before
a process restart still verify afterwards.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

MAX_CURSOR_LENGTH = 4096


class InvalidCursor(ValueError):
    pass


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class CursorCodec:
    def __init__(self, secret: bytes) -> None:
        self._secret = secret

    def encode(self, payload: dict[str, Any]) -> str:
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
        signature = hmac.new(self._secret, body, hashlib.sha256).digest()
        return f"{_b64encode(body)}.{_b64encode(signature)}"

    def decode(self, token: str) -> dict[str, Any]:
        if not token or len(token) > MAX_CURSOR_LENGTH:
            raise InvalidCursor("cursor is empty or too long")
        try:
            body_text, signature_text = token.split(".")
            body = _b64decode(body_text)
            signature = _b64decode(signature_text)
        except (ValueError, TypeError) as exc:
            raise InvalidCursor("cursor is not a signed token") from exc
        expected = hmac.new(self._secret, body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise InvalidCursor("cursor signature does not match")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise InvalidCursor("cursor payload is not valid JSON") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise InvalidCursor("unsupported cursor version")
        return payload


def query_fingerprint(query: str) -> str:
    return hashlib.sha256(query.casefold().encode("utf-8")).hexdigest()
