"""Domain logic for branched, multi-user conversations.

The central design choice: a message is stored exactly once, in ``messages``.
A conversation owns a list of *references* to those originals
(``conversation_messages``). Forking copies references, never content, so:

* every branch shows the same original message id and author;
* retraction flips one canonical row, so it is visible in every descendant
  branch at once and cannot leak through a grandchild;
* inherited history is decrypted/read with the child conversation's own
  membership only -- no ancestor membership is required.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .cursor import CursorCodec, InvalidCursor, query_fingerprint
from .db import Database, read_transaction, write_transaction
from .errors import ApiError

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_SEARCH_TERMS = 16

_MESSAGE_COLUMNS = """
    cm.position          AS conv_position,
    m.id                 AS message_id,
    m.origin_conversation_id,
    m.origin_position,
    m.author_id,
    u.display_name       AS author_display_name,
    m.body,
    m.retracted,
    m.retracted_at,
    m.created_at
"""

_MESSAGE_FROM = """
    FROM conversation_messages cm
    JOIN messages m ON m.id = cm.message_id
    JOIN users u    ON u.id = m.author_id
"""


def utcnow() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(int(limit), MAX_PAGE_SIZE))


class ConversationService:
    def __init__(self, db_path: str) -> None:
        self.db = Database(db_path)
        self.db.initialize()
        self.codec = CursorCodec(self.db.secret)

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = self.db.connect()
        try:
            yield conn
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    def create_user(self, display_name: str) -> dict[str, Any]:
        user_id = new_id("usr")
        created_at = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                conn.execute(
                    "INSERT INTO users (id, display_name, created_at) VALUES (?, ?, ?)",
                    (user_id, display_name, created_at),
                )
        return {"id": user_id, "display_name": display_name, "created_at": created_at}

    def find_user(self, user_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, display_name, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_user(self, user_id: str) -> dict[str, Any]:
        user = self.find_user(user_id)
        if user is None:
            raise ApiError(404, "user_not_found", "no user has that id")
        return user

    def authenticate(self, user_id: str) -> dict[str, Any]:
        """Validate an ``X-User-Id`` header before a handler does any work."""
        user = self.find_user(user_id)
        if user is None:
            raise ApiError(
                401, "unknown_user", "the user in X-User-Id does not exist"
            )
        return user

    # ------------------------------------------------------------------
    # Conversations and memberships
    # ------------------------------------------------------------------

    def create_conversation(self, user_id: str, title: str) -> dict[str, Any]:
        conversation_id = new_id("cvs")
        now = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                self._ensure_user(conn, user_id)
                conn.execute(
                    """
                    INSERT INTO conversations
                        (id, title, created_by, created_at,
                         parent_conversation_id, fork_point_message_id,
                         fork_point_position, inherited_message_count)
                    VALUES (?, ?, ?, ?, NULL, NULL, NULL, 0)
                    """,
                    (conversation_id, title, user_id, now),
                )
                self._add_membership_row(
                    conn, conversation_id, user_id, "owner", None, now
                )
                self._record_event(
                    conn,
                    "conversation.created",
                    conversation_id,
                    user_id,
                    details={"title": title},
                    occurred_at=now,
                )
                row = self._conversation_row(conn, conversation_id)
                return self._conversation_dict(conn, row, include_members=True)

    def list_conversations(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with read_transaction(conn):
                rows = conn.execute(
                    """
                    SELECT c.*
                    FROM conversations c
                    JOIN memberships mem ON mem.conversation_id = c.id
                    WHERE mem.user_id = ?
                    ORDER BY c.created_at ASC, c.id ASC
                    """,
                    (user_id,),
                ).fetchall()
                return [self._conversation_dict(conn, row) for row in rows]

    def get_conversation(self, conversation_id: str, user_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            with read_transaction(conn):
                conversation = self._require_conversation(conn, conversation_id)
                self._require_membership(conn, conversation_id, user_id)
                return self._conversation_dict(
                    conn, conversation, include_members=True
                )

    def list_members(self, conversation_id: str, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with read_transaction(conn):
                self._require_conversation(conn, conversation_id)
                self._require_membership(conn, conversation_id, user_id)
                return self._members(conn, conversation_id)

    def add_member(
        self, conversation_id: str, actor_id: str, user_id: str
    ) -> tuple[dict[str, Any], bool]:
        now = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                self._require_conversation(conn, conversation_id)
                self._require_owner(conn, conversation_id, actor_id)
                if self._user_row(conn, user_id) is None:
                    raise ApiError(404, "user_not_found", "no user has that id")

                existing = conn.execute(
                    "SELECT * FROM memberships WHERE conversation_id = ? AND user_id = ?",
                    (conversation_id, user_id),
                ).fetchone()
                if existing is not None:
                    return self._member_dict(conn, existing), False

                self._add_membership_row(
                    conn,
                    conversation_id,
                    user_id,
                    "member",
                    actor_id,
                    now,
                )
                row = conn.execute(
                    "SELECT * FROM memberships WHERE conversation_id = ? AND user_id = ?",
                    (conversation_id, user_id),
                ).fetchone()
                assert row is not None
                self._record_event(
                    conn,
                    "member.added",
                    conversation_id,
                    actor_id,
                    details={"user_id": user_id, "membership_id": row["membership_id"]},
                    occurred_at=now,
                )
                return self._member_dict(conn, row), True

    def remove_member(
        self, conversation_id: str, actor_id: str, user_id: str
    ) -> None:
        now = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                self._require_conversation(conn, conversation_id)
                self._require_owner(conn, conversation_id, actor_id)
                target = conn.execute(
                    "SELECT * FROM memberships WHERE conversation_id = ? AND user_id = ?",
                    (conversation_id, user_id),
                ).fetchone()
                if target is None:
                    raise ApiError(
                        404,
                        "member_not_found",
                        "that user is not a member of this conversation",
                    )
                if target["role"] == "owner":
                    raise ApiError(
                        400,
                        "cannot_remove_owner",
                        "the conversation owner cannot be removed",
                    )
                conn.execute(
                    "DELETE FROM memberships WHERE membership_id = ?",
                    (target["membership_id"],),
                )
                self._record_event(
                    conn,
                    "member.removed",
                    conversation_id,
                    actor_id,
                    details={
                        "user_id": user_id,
                        "membership_id": target["membership_id"],
                    },
                    occurred_at=now,
                )

    # ------------------------------------------------------------------
    # Messages: append, read, search, fork
    # ------------------------------------------------------------------

    def append_message(
        self,
        conversation_id: str,
        user_id: str,
        body: str,
        client_token: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """Append an immutable message; write transactions give a commit order.

        ``client_token`` deduplicates retries. Note that the inserted message is
        the original; subsequent forks reference this very row.
        """
        now = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                self._require_conversation(conn, conversation_id)
                self._require_membership(conn, conversation_id, user_id)

                if client_token is not None:
                    existing = conn.execute(
                        """
                        SELECT id FROM messages
                        WHERE origin_conversation_id = ?
                          AND author_id = ?
                          AND client_token = ?
                        """,
                        (conversation_id, user_id, client_token),
                    ).fetchone()
                    if existing is not None:
                        return (
                            self._message_in_conversation(
                                conn, conversation_id, existing["id"]
                            ),
                            False,
                        )

                position = conn.execute(
                    """
                    SELECT COALESCE(MAX(position), 0) + 1
                    FROM conversation_messages
                    WHERE conversation_id = ?
                    """,
                    (conversation_id,),
                ).fetchone()[0]
                message_id = new_id("msg")
                conn.execute(
                    """
                    INSERT INTO messages
                        (id, origin_conversation_id, origin_position, author_id,
                         body, client_token, created_at, retracted)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        message_id,
                        conversation_id,
                        position,
                        user_id,
                        body,
                        client_token,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO conversation_messages
                        (conversation_id, position, message_id)
                    VALUES (?, ?, ?)
                    """,
                    (conversation_id, position, message_id),
                )
                self._record_event(
                    conn,
                    "message.appended",
                    conversation_id,
                    user_id,
                    message_id=message_id,
                    details={"position": position},
                    occurred_at=now,
                )
                return (
                    self._message_in_conversation(
                        conn, conversation_id, message_id
                    ),
                    True,
                )

    def fork_conversation(
        self,
        conversation_id: str,
        user_id: str,
        fork_point_message_id: str,
        title: str | None,
    ) -> dict[str, Any]:
        now = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                parent = self._require_conversation(conn, conversation_id)
                self._require_membership(conn, conversation_id, user_id)

                point = conn.execute(
                    """
                    SELECT position FROM conversation_messages
                    WHERE conversation_id = ? AND message_id = ?
                    """,
                    (conversation_id, fork_point_message_id),
                ).fetchone()
                if point is None:
                    raise ApiError(
                        400,
                        "fork_point_not_visible",
                        "the fork point must be a message currently visible in "
                        "this conversation",
                    )
                position = point["position"]

                child_id = new_id("cvs")
                child_title = title or f"Fork of {parent['title']}"
                conn.execute(
                    """
                    INSERT INTO conversations
                        (id, title, created_by, created_at,
                         parent_conversation_id, fork_point_message_id,
                         fork_point_position, inherited_message_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        child_id,
                        child_title,
                        user_id,
                        now,
                        conversation_id,
                        fork_point_message_id,
                        position,
                        position,
                    ),
                )
                # Only references are copied: same message ids, same authors,
                # same global retraction state. Later parent messages and later
                # child messages never cross the boundary.
                conn.execute(
                    """
                    INSERT INTO conversation_messages
                        (conversation_id, position, message_id)
                    SELECT ?, position, message_id
                    FROM conversation_messages
                    WHERE conversation_id = ? AND position <= ?
                    ORDER BY position
                    """,
                    (child_id, conversation_id, position),
                )
                self._add_membership_row(conn, child_id, user_id, "owner", None, now)
                self._record_event(
                    conn,
                    "conversation.forked",
                    child_id,
                    user_id,
                    details={
                        "parent_conversation_id": conversation_id,
                        "fork_point_message_id": fork_point_message_id,
                        "fork_point_position": position,
                        "inherited_message_count": position,
                    },
                    occurred_at=now,
                )
                # Branches created after a retraction never witness the
                # retraction itself. Log the placeholders they inherit so every
                # branch's audit trail explains its own view.
                inherited_retracted = conn.execute(
                    """
                    SELECT message_id FROM conversation_messages
                    WHERE conversation_id = ? AND position <= ?
                      AND message_id IN (
                          SELECT id FROM messages WHERE retracted = 1
                      )
                    ORDER BY position
                    """,
                    (child_id, position),
                ).fetchall()
                for row in inherited_retracted:
                    self._record_event(
                        conn,
                        "message.inherited_retracted",
                        child_id,
                        user_id,
                        message_id=row["message_id"],
                        details={
                            "fork_point_message_id": fork_point_message_id,
                            "inherited_message_count": position,
                        },
                        occurred_at=now,
                    )
                self._record_event(
                    conn,
                    "conversation.forked_out",
                    conversation_id,
                    user_id,
                    details={
                        "child_conversation_id": child_id,
                        "fork_point_message_id": fork_point_message_id,
                        "fork_point_position": position,
                    },
                    occurred_at=now,
                )
                row = self._conversation_row(conn, child_id)
                return self._conversation_dict(conn, row, include_members=True)

    def list_messages(
        self,
        conversation_id: str,
        user_id: str,
        limit: int | None,
        cursor: str | None,
    ) -> dict[str, Any]:
        page_size = clamp_limit(limit)
        with self._connect() as conn:
            # One SQLite snapshot per page: membership, positions and retraction
            # flags all belong to the same committed state.
            with read_transaction(conn):
                conversation = self._require_conversation(conn, conversation_id)
                membership = self._require_membership(conn, conversation_id, user_id)

                after = 0
                if cursor is not None:
                    after = self._decode_cursor(
                        cursor,
                        kind="messages",
                        conversation_id=conversation_id,
                        user_id=user_id,
                        membership_id=membership["membership_id"],
                    )["after"]

                rows = conn.execute(
                    f"""
                    SELECT {_MESSAGE_COLUMNS}
                    {_MESSAGE_FROM}
                    WHERE cm.conversation_id = ? AND cm.position > ?
                    ORDER BY cm.position ASC
                    LIMIT ?
                    """,
                    (conversation_id, after, page_size + 1),
                ).fetchall()
                has_more = len(rows) > page_size
                rows = rows[:page_size]
                inherited = conversation["inherited_message_count"]
                items = [self._row_to_message(row, inherited) for row in rows]

                next_cursor = None
                if has_more and items:
                    next_cursor = self.codec.encode(
                        {
                            "v": 1,
                            "kind": "messages",
                            "conv": conversation_id,
                            "user": user_id,
                            "member": membership["membership_id"],
                            "after": items[-1]["position"],
                        }
                    )
                return {
                    "conversation_id": conversation_id,
                    "items": items,
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                }

    def get_message(
        self, conversation_id: str, user_id: str, message_id: str
    ) -> dict[str, Any]:
        """Read one message through one conversation's view.

        The response carries the same ``origin`` triple that the message has in
        every branch, so a reader always knows where the original utterance was
        authored -- even when reading an inherited reference.
        """
        with self._connect() as conn:
            with read_transaction(conn):
                conversation = self._require_conversation(conn, conversation_id)
                self._require_membership(conn, conversation_id, user_id)
                row = conn.execute(
                    f"""
                    SELECT {_MESSAGE_COLUMNS}
                    {_MESSAGE_FROM}
                    WHERE cm.conversation_id = ? AND cm.message_id = ?
                    """,
                    (conversation_id, message_id),
                ).fetchone()
                if row is None:
                    raise ApiError(
                        404,
                        "message_not_in_conversation",
                        "that message is not visible in this conversation",
                    )
                return self._row_to_message(
                    row, conversation["inherited_message_count"]
                )

    def search_messages(
        self,
        conversation_id: str,
        user_id: str,
        query: str,
        limit: int | None,
        cursor: str | None,
    ) -> dict[str, Any]:
        page_size = clamp_limit(limit)
        terms = [term.casefold() for term in query.split() if term.strip()]
        if not terms:
            raise ApiError(
                400, "invalid_query", "query must contain at least one keyword"
            )
        if len(terms) > MAX_SEARCH_TERMS:
            raise ApiError(
                400,
                "invalid_query",
                f"query may contain at most {MAX_SEARCH_TERMS} keywords",
            )

        with self._connect() as conn:
            with read_transaction(conn):
                conversation = self._require_conversation(conn, conversation_id)
                membership = self._require_membership(conn, conversation_id, user_id)

                after = 0
                if cursor is not None:
                    after = self._decode_cursor(
                        cursor,
                        kind="search",
                        conversation_id=conversation_id,
                        user_id=user_id,
                        membership_id=membership["membership_id"],
                        query_fingerprint=query_fingerprint(query),
                    )["after"]

                # Case-insensitive, all terms must occur. Retracted rows have a
                # NULL body and are filtered out, so retracted text is not
                # searchable anywhere.
                clauses = ["cm.conversation_id = ?", "cm.position > ?"]
                params: list[Any] = [conversation_id, after]
                clauses.append("m.retracted = 0")
                clauses.append("m.body IS NOT NULL")
                for term in terms:
                    clauses.append("instr(py_casefold(m.body), ?) > 0")
                    params.append(term)
                params.append(page_size + 1)

                rows = conn.execute(
                    f"""
                    SELECT {_MESSAGE_COLUMNS}
                    {_MESSAGE_FROM}
                    WHERE {' AND '.join(clauses)}
                    ORDER BY cm.position ASC
                    LIMIT ?
                    """,
                    params,
                ).fetchall()
                has_more = len(rows) > page_size
                rows = rows[:page_size]
                inherited = conversation["inherited_message_count"]
                items = [self._row_to_message(row, inherited) for row in rows]

                next_cursor = None
                if has_more and items:
                    next_cursor = self.codec.encode(
                        {
                            "v": 1,
                            "kind": "search",
                            "conv": conversation_id,
                            "user": user_id,
                            "member": membership["membership_id"],
                            "q": query_fingerprint(query),
                            "after": items[-1]["position"],
                        }
                    )
                return {
                    "conversation_id": conversation_id,
                    "query": query,
                    "items": items,
                    "has_more": has_more,
                    "next_cursor": next_cursor,
                }

    # ------------------------------------------------------------------
    # Retraction
    # ------------------------------------------------------------------

    def retract_message(self, message_id: str, actor_id: str) -> dict[str, Any]:
        """Globally retract an original message authored by ``actor_id``.

        The body is removed from the single canonical row. Every conversation
        that references it -- including conversations the author is not a
        member of -- immediately reads a placeholder. No branch holds a private
        copy that could leak the content.
        """
        now = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                message = conn.execute(
                    "SELECT * FROM messages WHERE id = ?", (message_id,)
                ).fetchone()
                if message is None:
                    raise ApiError(404, "message_not_found", "no message has that id")
                if message["author_id"] != actor_id:
                    raise ApiError(
                        403,
                        "not_message_author",
                        "only the author of the original message may retract it",
                    )
                affected = conn.execute(
                    "SELECT COUNT(*) FROM conversation_messages WHERE message_id = ?",
                    (message_id,),
                ).fetchone()[0]
                affected_ids = [
                    row[0]
                    for row in conn.execute(
                        """
                        SELECT conversation_id FROM conversation_messages
                        WHERE message_id = ?
                        ORDER BY conversation_id
                        """,
                        (message_id,),
                    ).fetchall()
                ]

                descendants = [
                    {
                        "conversation_id": row["id"],
                        "fork_point_position": row["fork_point_position"],
                    }
                    for row in conn.execute(
                        """
                        SELECT id, fork_point_position
                        FROM conversations
                        WHERE fork_point_message_id = ?
                        ORDER BY created_at ASC, id ASC
                        """,
                        (message_id,),
                    ).fetchall()
                ]

                if message["retracted"]:
                    return {
                        "message_id": message_id,
                        "author": self._author_ref(conn, message["author_id"]),
                        "origin": {
                            "conversation_id": message["origin_conversation_id"],
                            "position": message["origin_position"],
                        },
                        "retracted": True,
                        "retracted_at": message["retracted_at"],
                        "affected_conversations": affected,
                        "already_retracted": True,
                        "forked_at_retracted_point": descendants,
                    }

                conn.execute(
                    """
                    UPDATE messages
                    SET body = NULL, retracted = 1, retracted_at = ?, retracted_by = ?
                    WHERE id = ?
                    """,
                    (now, actor_id, message_id),
                )
                # One audit row in the origin and one in each branch that holds
                # a reference: every branch's log explains why its view changed,
                # even for readers who never had access to the author's branch.
                for branch_id in affected_ids:
                    self._record_event(
                        conn,
                        "message.retracted",
                        branch_id,
                        actor_id,
                        message_id=message_id,
                        details={
                            "origin_conversation_id": message[
                                "origin_conversation_id"
                            ],
                            "origin_position": message["origin_position"],
                            "affected_conversations": affected,
                        },
                        occurred_at=now,
                    )
                result = {
                    "message_id": message_id,
                    "author": self._author_ref(conn, message["author_id"]),
                    "origin": {
                        "conversation_id": message["origin_conversation_id"],
                        "position": message["origin_position"],
                    },
                    "retracted": True,
                    "retracted_at": now,
                    "affected_conversations": affected,
                    "already_retracted": False,
                    "forked_at_retracted_point": descendants,
                }
            # After the update commits, drop WAL frames that still hold the
            # pre-retraction body. This is best effort and must never block a
            # reader's snapshot for long or turn a successful retraction into an
            # error.
            try:
                conn.execute("PRAGMA busy_timeout=250")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:  # pragma: no cover - environmental
                pass
            finally:
                conn.execute("PRAGMA busy_timeout=30000")
        return result

    # ------------------------------------------------------------------
    # Events (commit order audit)
    # ------------------------------------------------------------------

    def list_events(
        self,
        conversation_id: str,
        user_id: str,
        after_seq: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        page_size = clamp_limit(limit)
        with self._connect() as conn:
            with read_transaction(conn):
                self._require_conversation(conn, conversation_id)
                self._require_membership(conn, conversation_id, user_id)
                rows = conn.execute(
                    """
                    SELECT * FROM events
                    WHERE conversation_id = ? AND seq > ?
                    ORDER BY seq ASC
                    LIMIT ?
                    """,
                    (conversation_id, max(after_seq, 0), page_size),
                ).fetchall()
                return [
                    {
                        "seq": row["seq"],
                        "kind": row["kind"],
                        "conversation_id": row["conversation_id"],
                        "actor_id": row["actor_id"],
                        "message_id": row["message_id"],
                        "details": json.loads(row["details"]),
                        "occurred_at": row["occurred_at"],
                    }
                    for row in rows
                ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_user(self, conn: sqlite3.Connection, user_id: str) -> sqlite3.Row:
        row = self._user_row(conn, user_id)
        if row is None:
            raise ApiError(401, "unknown_user", "the user in X-User-Id does not exist")
        return row

    def _user_row(self, conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT id, display_name FROM users WHERE id = ?", (user_id,)
        ).fetchone()

    def _conversation_row(
        self, conn: sqlite3.Connection, conversation_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise ApiError(
                404, "conversation_not_found", "no conversation has that id"
            )
        return row

    def _require_conversation(
        self, conn: sqlite3.Connection, conversation_id: str
    ) -> sqlite3.Row:
        return self._conversation_row(conn, conversation_id)

    def _require_membership(
        self, conn: sqlite3.Connection, conversation_id: str, user_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT * FROM memberships
            WHERE conversation_id = ? AND user_id = ?
            """,
            (conversation_id, user_id),
        ).fetchone()
        if row is None:
            raise ApiError(
                403,
                "not_a_member",
                "you are not a member of this conversation",
            )
        return row

    def _require_owner(
        self, conn: sqlite3.Connection, conversation_id: str, user_id: str
    ) -> sqlite3.Row:
        membership = self._require_membership(conn, conversation_id, user_id)
        if membership["role"] != "owner":
            raise ApiError(
                403,
                "owner_required",
                "only the conversation owner may change membership",
            )
        return membership

    def _add_membership_row(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        user_id: str,
        role: str,
        added_by: str | None,
        now: str,
    ) -> str:
        membership_id = new_id("mem")
        conn.execute(
            """
            INSERT INTO memberships
                (membership_id, conversation_id, user_id, role, added_by, joined_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (membership_id, conversation_id, user_id, role, added_by, now),
        )
        return membership_id

    def _author_ref(self, conn: sqlite3.Connection, user_id: str) -> dict[str, str]:
        row = self._user_row(conn, user_id)
        if row is None:  # pragma: no cover - FK guarantees existence
            return {"id": user_id, "display_name": user_id}
        return {"id": row["id"], "display_name": row["display_name"]}

    def _members(
        self, conn: sqlite3.Connection, conversation_id: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            """
            SELECT mem.*, u.display_name
            FROM memberships mem
            JOIN users u ON u.id = mem.user_id
            WHERE mem.conversation_id = ?
            ORDER BY mem.joined_at ASC, mem.membership_id ASC
            """,
            (conversation_id,),
        ).fetchall()
        return [self._member_dict(conn, row) for row in rows]

    def _member_dict(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        display_name = (
            row["display_name"]
            if "display_name" in row.keys()
            else self._author_ref(conn, row["user_id"])["display_name"]
        )
        return {
            "membership_id": row["membership_id"],
            "conversation_id": row["conversation_id"],
            "user": {"id": row["user_id"], "display_name": display_name},
            "role": row["role"],
            "added_by": row["added_by"],
            "joined_at": row["joined_at"],
        }

    def _conversation_dict(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_members: bool = False,
    ) -> dict[str, Any]:
        fork_point = None
        if row["fork_point_message_id"] is not None:
            message = conn.execute(
                """
                SELECT id, origin_conversation_id, origin_position, retracted
                FROM messages WHERE id = ?
                """,
                (row["fork_point_message_id"],),
            ).fetchone()
            if message is not None:
                fork_point = {
                    "message_id": message["id"],
                    "parent_position": row["fork_point_position"],
                    "origin": {
                        "conversation_id": message["origin_conversation_id"],
                        "position": message["origin_position"],
                    },
                    "retracted": bool(message["retracted"]),
                }
        data: dict[str, Any] = {
            "id": row["id"],
            "title": row["title"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "parent_conversation_id": row["parent_conversation_id"],
            "inherited_message_count": row["inherited_message_count"],
            "fork_point": fork_point,
        }
        if include_members:
            data["members"] = self._members(conn, row["id"])
        return data

    def _message_in_conversation(
        self, conn: sqlite3.Connection, conversation_id: str, message_id: str
    ) -> dict[str, Any]:
        row = conn.execute(
            f"""
            SELECT {_MESSAGE_COLUMNS}
            {_MESSAGE_FROM}
            WHERE cm.conversation_id = ? AND cm.message_id = ?
            """,
            (conversation_id, message_id),
        ).fetchone()
        if row is None:  # pragma: no cover - internal invariant
            raise ApiError(500, "internal_error", "message reference is missing")
        # A message appended to a conversation at position N is local there;
        # inherited_message_count <= N-1 holds for every append, so this is
        # always "local" for this helper's callers. Compute it from the same
        # rule anyway rather than special-casing.
        inherited = conn.execute(
            "SELECT inherited_message_count FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()[0]
        return self._row_to_message(row, inherited)

    def _row_to_message(
        self, row: sqlite3.Row, inherited_message_count: int
    ) -> dict[str, Any]:
        position = row["conv_position"]
        # A retracted message keeps its identity, author, position and marker,
        # but has no body anywhere.
        body = None if row["retracted"] else row["body"]
        return {
            "message_id": row["message_id"],
            "position": position,
            "source": "inherited" if position <= inherited_message_count else "local",
            "author": {
                "id": row["author_id"],
                "display_name": row["author_display_name"],
            },
            "origin": {
                "conversation_id": row["origin_conversation_id"],
                "position": row["origin_position"],
            },
            "body": body,
            "retracted": bool(row["retracted"]),
            "retracted_at": row["retracted_at"],
            "created_at": row["created_at"],
        }

    def _record_event(
        self,
        conn: sqlite3.Connection,
        kind: str,
        conversation_id: str,
        actor_id: str,
        *,
        message_id: str | None = None,
        details: dict[str, Any] | None = None,
        occurred_at: str | None = None,
    ) -> int:
        cursor = conn.execute(
            """
            INSERT INTO events
                (kind, conversation_id, actor_id, message_id, details, occurred_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                kind,
                conversation_id,
                actor_id,
                message_id,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
                occurred_at or utcnow(),
            ),
        )
        return int(cursor.lastrowid or 0)

    def _decode_cursor(
        self,
        token: str,
        *,
        kind: str,
        conversation_id: str,
        user_id: str,
        membership_id: str,
        query_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        try:
            payload = self.codec.decode(token)
        except InvalidCursor as exc:
            raise ApiError(400, "invalid_cursor", str(exc)) from exc

        if (
            payload.get("kind") != kind
            or payload.get("conv") != conversation_id
            or payload.get("user") != user_id
        ):
            raise ApiError(
                400,
                "invalid_cursor",
                "cursor does not belong to this conversation, user or endpoint",
            )
        if payload.get("member") != membership_id:
            # Covers remove + re-add: a new membership instance is a new
            # authorisation context, so an old cursor cannot be replayed.
            raise ApiError(
                400,
                "invalid_cursor",
                "cursor was issued for a membership that no longer exists",
            )
        if query_fingerprint is not None and payload.get("q") != query_fingerprint:
            raise ApiError(
                400, "invalid_cursor", "cursor was issued for a different query"
            )
        after = payload.get("after")
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ApiError(400, "invalid_cursor", "cursor has an invalid position")
        return payload
