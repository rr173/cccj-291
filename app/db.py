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

-- ---------------------------------------------------------------------------
-- Desktop replica sync
--
-- One row per *user-visible* change. Rows are fanned out to every membership
-- instance that can observe the change at commit time, so a late pull is a
-- simple indexed scan of this user's rows -- never a diff over all messages.
--
-- membership_id pins the authorisation identity the change was delivered
-- under. If that membership has since been removed (or replaced by a new
-- instance after a re-invite), the row is stale: the pull skips its body and
-- the matching `membership.revoked` change tells the device to clear the
-- conversation. max_position on a grant is the conversation's history
-- watermark at grant time; the grant-driven history load walks exactly that
-- prefix, while later appends arrive as ordinary change rows.
CREATE TABLE IF NOT EXISTS sync_changes (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         TEXT NOT NULL REFERENCES users(id),
    membership_id   TEXT NOT NULL,
    kind            TEXT NOT NULL
        CHECK (kind IN (
            'message.appended',
            'message.retracted',
            'membership.granted',
            'membership.revoked'
        )),
    conversation_id TEXT NOT NULL REFERENCES conversations(id),
    message_id      TEXT REFERENCES messages(id),
    position        INTEGER,
    max_position    INTEGER,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sync_changes_user_seq
    ON sync_changes (user_id, seq);

CREATE INDEX IF NOT EXISTS idx_sync_changes_membership
    ON sync_changes (membership_id);

-- A membership instance is granted once and revoked once. These partial
-- indexes make the bootstrap/backfill idempotent and reject duplicate rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sync_grant_membership
    ON sync_changes (membership_id)
    WHERE kind = 'membership.granted';

CREATE UNIQUE INDEX IF NOT EXISTS uq_sync_revocation_membership
    ON sync_changes (membership_id)
    WHERE kind = 'membership.revoked';

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

-- ---------------------------------------------------------------------------
-- Cross-branch resolutions
--
-- A child conversation proposes one atomic package to its direct parent. The
-- proposal keeps private evidence references in the child; the parent-facing
-- review model exposes only topics, public text and dependencies. Approved
-- packages create immutable resolution versions, while resolution_board points
-- at the current version or marks a topic pending reconsideration.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS resolution_approvals (
    approval_id    TEXT PRIMARY KEY,
    proposal_id    TEXT NOT NULL UNIQUE,
    parent_conversation_id TEXT NOT NULL REFERENCES conversations(id),
    approver_id    TEXT NOT NULL REFERENCES users(id),
    request_id     TEXT,
    result_json    TEXT,
    approved_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resolution_proposals (
    proposal_id              TEXT PRIMARY KEY,
    parent_conversation_id   TEXT NOT NULL REFERENCES conversations(id),
    child_conversation_id    TEXT NOT NULL REFERENCES conversations(id),
    proposer_id              TEXT NOT NULL REFERENCES users(id),
    -- Intentionally not a foreign key: removing the child membership deletes
    -- the membership row, and approval must be able to observe that the exact
    -- submission-time membership instance is gone even if the user rejoins.
    proposer_membership_id   TEXT NOT NULL,
    client_token             TEXT,
    status                   TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected')),
    rejection_code           TEXT,
    created_at               TEXT NOT NULL,
    decided_at               TEXT,
    approval_id              TEXT REFERENCES resolution_approvals(approval_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_resolution_proposal_client_token
    ON resolution_proposals (child_conversation_id, proposer_id, client_token)
    WHERE client_token IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_resolution_proposals_parent_status
    ON resolution_proposals (parent_conversation_id, status, created_at);

CREATE TABLE IF NOT EXISTS resolution_proposal_items (
    item_id              TEXT PRIMARY KEY,
    proposal_id          TEXT NOT NULL
        REFERENCES resolution_proposals(proposal_id),
    topic_key            TEXT NOT NULL,
    ordinal              INTEGER NOT NULL,
    public_text          TEXT NOT NULL,
    -- The board pointer observed at submission. base_status='absent' means the
    -- topic had never been passed; effective/pending include the exact version.
    base_resolution_id   TEXT,
    base_status          TEXT NOT NULL
        CHECK (base_status IN ('absent', 'effective', 'pending_reconsideration')),
    UNIQUE (proposal_id, topic_key),
    UNIQUE (proposal_id, ordinal)
);

CREATE TABLE IF NOT EXISTS resolution_proposal_evidence (
    proposal_item_id TEXT NOT NULL
        REFERENCES resolution_proposal_items(item_id),
    message_id       TEXT NOT NULL REFERENCES messages(id),
    ordinal          INTEGER NOT NULL,
    PRIMARY KEY (proposal_item_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_proposal_evidence_message
    ON resolution_proposal_evidence (message_id);

CREATE TABLE IF NOT EXISTS resolution_proposal_dependencies (
    proposal_id              TEXT NOT NULL
        REFERENCES resolution_proposals(proposal_id),
    source_topic_key         TEXT NOT NULL,
    depends_on_topic_key     TEXT NOT NULL,
    scope                    TEXT NOT NULL CHECK (scope IN ('internal', 'external')),
    pinned_resolution_id     TEXT,
    pinned_version           INTEGER,
    ordinal                  INTEGER NOT NULL,
    PRIMARY KEY (proposal_id, source_topic_key, depends_on_topic_key)
);

CREATE TABLE IF NOT EXISTS resolutions (
    resolution_id          TEXT PRIMARY KEY,
    parent_conversation_id TEXT NOT NULL REFERENCES conversations(id),
    topic_key              TEXT NOT NULL,
    version                INTEGER NOT NULL,
    proposal_id            TEXT NOT NULL
        REFERENCES resolution_proposals(proposal_id),
    approval_id            TEXT NOT NULL
        REFERENCES resolution_approvals(approval_id),
    public_text            TEXT NOT NULL,
    approved_at           TEXT NOT NULL,
    UNIQUE (parent_conversation_id, topic_key, version)
);

CREATE INDEX IF NOT EXISTS idx_resolutions_parent_topic
    ON resolutions (parent_conversation_id, topic_key, version);

CREATE TABLE IF NOT EXISTS resolution_evidence (
    resolution_id TEXT NOT NULL REFERENCES resolutions(resolution_id),
    message_id    TEXT NOT NULL REFERENCES messages(id),
    PRIMARY KEY (resolution_id, message_id)
);

CREATE INDEX IF NOT EXISTS idx_resolution_evidence_message
    ON resolution_evidence (message_id);

CREATE TABLE IF NOT EXISTS resolution_dependencies (
    dependency_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    resolution_id             TEXT NOT NULL REFERENCES resolutions(resolution_id),
    parent_conversation_id    TEXT NOT NULL REFERENCES conversations(id),
    source_topic_key          TEXT NOT NULL,
    depends_on_topic_key      TEXT NOT NULL,
    depends_on_resolution_id  TEXT NOT NULL REFERENCES resolutions(resolution_id),
    depends_on_version        INTEGER NOT NULL,
    UNIQUE (resolution_id, depends_on_topic_key)
);

CREATE INDEX IF NOT EXISTS idx_resolution_dependencies_target
    ON resolution_dependencies (depends_on_resolution_id);

-- One current board row per topic that has ever had a passed resolution.
-- Pending rows retain current_resolution_id internally for cascade bookkeeping,
-- but the HTTP board serializes resolution=NULL so old text is not echoed.
CREATE TABLE IF NOT EXISTS resolution_board (
    parent_conversation_id TEXT NOT NULL REFERENCES conversations(id),
    topic_key              TEXT NOT NULL,
    current_resolution_id  TEXT NOT NULL UNIQUE REFERENCES resolutions(resolution_id),
    status                 TEXT NOT NULL
        CHECK (status IN ('effective', 'pending_reconsideration')),
    pending_reason         TEXT,
    invalidated_at         TEXT,
    updated_at             TEXT NOT NULL,
    PRIMARY KEY (parent_conversation_id, topic_key)
);

CREATE INDEX IF NOT EXISTS idx_resolution_board_parent_status
    ON resolution_board (parent_conversation_id, status);

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
