"""SQLite persistence primitives.

A single SQLite database holds users, conversations, messages, the
per-conversation message references that make up each branch's view, explicit
memberships, and an append-only audit log.

Writers use ``BEGIN IMMEDIATE`` so that sequence allocation, fork materialisation
and retraction are serialised. Readers use a deferred transaction so that a
single page (membership check + rows) observes one SQLite snapshot.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id           TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id                       TEXT PRIMARY KEY,
    title                    TEXT NOT NULL,
    created_by               TEXT NOT NULL REFERENCES users(id),
    created_at               TEXT NOT NULL,
    parent_conversation_id   TEXT REFERENCES conversations(id),
    fork_point_message_id    TEXT REFERENCES messages(id),
    fork_point_position      INTEGER,
    inherited_message_count  INTEGER NOT NULL DEFAULT 0,
    CHECK (
        (parent_conversation_id IS NULL
         AND fork_point_message_id IS NULL
         AND fork_point_position IS NULL
         AND inherited_message_count = 0)
        OR
        (parent_conversation_id IS NOT NULL
         AND fork_point_message_id IS NOT NULL
         AND fork_point_position IS NOT NULL
         AND inherited_message_count >= 1)
    )
);

CREATE TABLE IF NOT EXISTS messages (
    id                      TEXT PRIMARY KEY,
    origin_conversation_id  TEXT NOT NULL REFERENCES conversations(id),
    origin_position         INTEGER NOT NULL,
    author_id               TEXT NOT NULL REFERENCES users(id),
    body                    TEXT,
    client_token            TEXT,
    created_at              TEXT NOT NULL,
    retracted               INTEGER NOT NULL DEFAULT 0 CHECK (retracted IN (0, 1)),
    retracted_at            TEXT,
    retracted_by            TEXT REFERENCES users(id),
    UNIQUE (origin_conversation_id, origin_position),
    CHECK (
        (retracted = 0 AND body IS NOT NULL AND retracted_at IS NULL)
        OR
        (retracted = 1 AND body IS NULL AND retracted_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_idempotency
    ON messages (origin_conversation_id, author_id, client_token)
    WHERE client_token IS NOT NULL;

-- A reference, never a copy, of an original message in a conversation's view.
-- Forking copies only these references, which is why the message id, author and
-- live retraction state are shared by every descendant branch.
CREATE TABLE IF NOT EXISTS conversation_messages (
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    position        INTEGER NOT NULL,
    message_id      TEXT NOT NULL REFERENCES messages(id),
    PRIMARY KEY (conversation_id, position),
    UNIQUE (conversation_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_conversation_messages_message
    ON conversation_messages (message_id);

CREATE TABLE IF NOT EXISTS memberships (
    membership_id   TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    user_id         TEXT NOT NULL REFERENCES users(id),
    role            TEXT NOT NULL CHECK (role IN ('owner', 'member')),
    added_by        TEXT REFERENCES users(id),
    joined_at       TEXT NOT NULL,
    UNIQUE (conversation_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_memberships_user
    ON memberships (user_id);

CREATE TABLE IF NOT EXISTS events (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    actor_id        TEXT NOT NULL REFERENCES users(id),
    message_id      TEXT REFERENCES messages(id),
    details         TEXT NOT NULL,
    occurred_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_conversation
    ON events (conversation_id, seq);

-- One row per follow *instance*. Unfollowing (or losing membership) deletes
-- the row, so a notification reason that points at this follow_id goes dead
-- and can never be revived: re-following inserts a brand new follow_id.
CREATE TABLE IF NOT EXISTS follows (
    follow_id       TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    user_id         TEXT NOT NULL REFERENCES users(id),
    created_at      TEXT NOT NULL,
    UNIQUE (conversation_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_follows_user
    ON follows (user_id);

-- One offline notification per (original message, recipient). Created only on
-- the append path of the origin conversation; forks, joins and follows never
-- backfill. seq orders claims by original submission (commit) order.
CREATE TABLE IF NOT EXISTS notifications (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id TEXT NOT NULL UNIQUE,
    message_id      TEXT NOT NULL REFERENCES messages(id),
    recipient_id    TEXT NOT NULL REFERENCES users(id),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    created_at      TEXT NOT NULL,
    UNIQUE (message_id, recipient_id)
);

CREATE INDEX IF NOT EXISTS idx_notifications_recipient
    ON notifications (recipient_id, seq);

-- Why a notification exists. A reason is live iff its source row still
-- exists: a follow reason needs its follows row, a mention reason needs the
-- membership instance it was minted from. Deleting the source (unfollow,
-- member removal) kills the reason without touching history here.
CREATE TABLE IF NOT EXISTS notification_reasons (
    notification_id TEXT NOT NULL REFERENCES notifications(notification_id),
    kind            TEXT NOT NULL CHECK (kind IN ('follow', 'mention')),
    source_id       TEXT NOT NULL,
    PRIMARY KEY (notification_id, kind, source_id)
);

-- One claim batch per (recipient, request_id): retrying a request returns the
-- original batch instead of claiming again. expires_at is stored so leases
-- keep expiring across process restarts.
CREATE TABLE IF NOT EXISTS claim_batches (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT NOT NULL UNIQUE,
    request_id   TEXT NOT NULL,
    recipient_id TEXT NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    UNIQUE (recipient_id, request_id)
);

-- Batch membership plus the confirmation marker. A notification appears in
-- several batches over time (re-claim after expiry); the newest batch owns it.
CREATE TABLE IF NOT EXISTS claim_items (
    batch_id        TEXT NOT NULL REFERENCES claim_batches(batch_id),
    notification_id TEXT NOT NULL REFERENCES notifications(notification_id),
    confirmed_at    TEXT,
    PRIMARY KEY (batch_id, notification_id)
);

CREATE INDEX IF NOT EXISTS idx_claim_items_notification
    ON claim_items (notification_id);

-- Defence in depth: messages are append-only. The only permitted UPDATE is the
-- one that clears the body and flips the retraction marker. Origin identity and
-- authorship can never be rewritten, and rows can never be deleted.
CREATE TRIGGER IF NOT EXISTS messages_no_delete
BEFORE DELETE ON messages
BEGIN
    SELECT RAISE(ABORT, 'messages are immutable and cannot be deleted');
END;

CREATE TRIGGER IF NOT EXISTS messages_no_identity_change
BEFORE UPDATE ON messages
WHEN NEW.id IS NOT OLD.id
  OR NEW.origin_conversation_id IS NOT OLD.origin_conversation_id
  OR NEW.origin_position IS NOT OLD.origin_position
  OR NEW.author_id IS NOT OLD.author_id
  OR NEW.client_token IS NOT OLD.client_token
  OR NEW.created_at IS NOT OLD.created_at
BEGIN
    SELECT RAISE(ABORT, 'message identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS messages_retraction_is_terminal
BEFORE UPDATE ON messages
WHEN OLD.retracted = 1
  OR (NEW.retracted = 0 AND NEW.body IS NOT OLD.body)
BEGIN
    SELECT RAISE(ABORT, 'message body is immutable after append');
END;
"""


def _casefold(value: Any) -> Any:
    """Unicode-aware case folding for keyword search (Python 3.11 sqlite has it)."""
    if value is None:
        return None
    return str(value).casefold()


class Database:
    """Owns the database path, schema and the persistent cursor-signing secret."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = str(path)
        self._secret: bytes | None = None

    @property
    def secret(self) -> bytes:
        if self._secret is None:
            raise RuntimeError("Database.initialize() must be called first")
        return self._secret

    def initialize(self) -> None:
        parent = Path(self.path).expanduser().resolve().parent
        parent.mkdir(parents=True, exist_ok=True)

        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(SCHEMA)
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO meta (key, value) VALUES ('cursor_secret', ?)",
                    (secrets.token_hex(32),),
                )
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'cursor_secret'"
                ).fetchone()
                assert row is not None
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            self._secret = bytes.fromhex(row[0])
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        # Overwrite freed cells rather than leaving retracted bodies behind in
        # page slack space.
        conn.execute("PRAGMA secure_delete=ON")
        conn.create_function("py_casefold", 1, _casefold, deterministic=True)
        return conn


@contextmanager
def read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """A consistent snapshot for one page/one response."""
    conn.execute("BEGIN")
    try:
        yield
    finally:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Serialise all writers; sequence and ordering are decided here."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
