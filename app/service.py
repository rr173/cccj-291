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
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .cursor import CursorCodec, InvalidCursor, query_fingerprint
from .db import Database, read_transaction, write_transaction
from .errors import ApiError
from .resolutions import ResolutionMixin

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100
MAX_SEARCH_TERMS = 16
# How long a claimed batch stays confirmable and stays off other devices'
# claims. Stored per batch, so it keeps expiring across restarts.
DEFAULT_CLAIM_LEASE_SECONDS = 30.0

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


class ConversationService(ResolutionMixin):
    def __init__(
        self,
        db_path: str,
        *,
        claim_lease_seconds: float = DEFAULT_CLAIM_LEASE_SECONDS,
    ) -> None:
        self.db = Database(db_path)
        self.db.initialize()
        self.codec = CursorCodec(self.db.secret)
        if claim_lease_seconds <= 0:
            raise ValueError("claim_lease_seconds must be positive")
        self._claim_lease = timedelta(seconds=claim_lease_seconds)

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
                # Losing membership ends every reason this membership instance
                # carried: mention reasons reference the deleted membership_id
                # directly, and follow reasons die with the follow rows here.
                # Re-invitation mints new ids and revives nothing.
                conn.execute(
                    "DELETE FROM follows WHERE conversation_id = ? AND user_id = ?",
                    (conversation_id, user_id),
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
    # Follows (per-member notification subscription)
    # ------------------------------------------------------------------

    def follow_conversation(
        self, conversation_id: str, user_id: str
    ) -> tuple[dict[str, Any], bool]:
        """Subscribe a member to notifications for new original messages.

        Following is not retroactive: only messages appended after this call
        notify the follower. Re-following after an unfollow mints a new follow
        instance; it never revives reasons the old instance left behind.
        """
        now = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                self._require_conversation(conn, conversation_id)
                self._require_membership(conn, conversation_id, user_id)
                existing = conn.execute(
                    """
                    SELECT follow_id, created_at FROM follows
                    WHERE conversation_id = ? AND user_id = ?
                    """,
                    (conversation_id, user_id),
                ).fetchone()
                if existing is not None:
                    return (
                        self._follow_dict(conversation_id, user_id, existing),
                        False,
                    )
                follow_id = new_id("fol")
                conn.execute(
                    """
                    INSERT INTO follows (follow_id, conversation_id, user_id, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (follow_id, conversation_id, user_id, now),
                )
                self._record_event(
                    conn,
                    "conversation.followed",
                    conversation_id,
                    user_id,
                    details={"follow_id": follow_id},
                    occurred_at=now,
                )
                row = conn.execute(
                    "SELECT follow_id, created_at FROM follows WHERE follow_id = ?",
                    (follow_id,),
                ).fetchone()
                assert row is not None
                return self._follow_dict(conversation_id, user_id, row), True

    def unfollow_conversation(self, conversation_id: str, user_id: str) -> None:
        """End the current follow instance (idempotent).

        Deleting the follow row kills exactly the follow reasons minted from
        it; mention reasons on the same notifications are untouched.
        """
        now = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                self._require_conversation(conn, conversation_id)
                self._require_membership(conn, conversation_id, user_id)
                cursor = conn.execute(
                    """
                    DELETE FROM follows
                    WHERE conversation_id = ? AND user_id = ?
                    """,
                    (conversation_id, user_id),
                )
                if cursor.rowcount:
                    self._record_event(
                        conn,
                        "conversation.unfollowed",
                        conversation_id,
                        user_id,
                        occurred_at=now,
                    )

    def get_follow_status(
        self, conversation_id: str, user_id: str
    ) -> dict[str, Any]:
        with self._connect() as conn:
            with read_transaction(conn):
                self._require_conversation(conn, conversation_id)
                self._require_membership(conn, conversation_id, user_id)
                row = conn.execute(
                    """
                    SELECT follow_id, created_at FROM follows
                    WHERE conversation_id = ? AND user_id = ?
                    """,
                    (conversation_id, user_id),
                ).fetchone()
                return self._follow_dict(conversation_id, user_id, row)

    @staticmethod
    def _follow_dict(
        conversation_id: str, user_id: str, row: sqlite3.Row | None
    ) -> dict[str, Any]:
        return {
            "conversation_id": conversation_id,
            "user_id": user_id,
            "following": row is not None,
            "followed_at": row["created_at"] if row is not None else None,
        }

    # ------------------------------------------------------------------
    # Messages: append, read, search, fork
    # ------------------------------------------------------------------

    def append_message(
        self,
        conversation_id: str,
        user_id: str,
        body: str,
        client_token: str | None,
        mention_user_ids: list[str] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Append an immutable message; write transactions give a commit order.

        ``client_token`` deduplicates retries. Note that the inserted message is
        the original; subsequent forks reference this very row.

        ``mention_user_ids`` names current members to notify explicitly. A
        successful append also creates offline notifications for the members
        who follow the conversation right now; a deduplicated retry creates
        none and never changes the original recipients.
        """
        mentions = list(mention_user_ids or [])
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

                # Mentions are validated only on the create path: a retry of an
                # already-committed append must return the original result
                # unchanged, whatever the retried payload carries.
                mention_memberships = self._resolve_mentions(
                    conn, conversation_id, mentions
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
                notified = self._create_notifications(
                    conn,
                    message_id=message_id,
                    conversation_id=conversation_id,
                    author_id=user_id,
                    mention_memberships=mention_memberships,
                    now=now,
                )
                self._record_event(
                    conn,
                    "message.appended",
                    conversation_id,
                    user_id,
                    message_id=message_id,
                    details={
                        "position": position,
                        "mentions": sorted(mention_memberships),
                        "notified_recipients": len(notified),
                    },
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
                # In the same commit, hide any board resolution whose evidence
                # was just retracted and propagate pending state downstream.
                self.invalidate_board_for_retracted_message(
                    conn, message_id, actor_id, now
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
    # Offline notifications: personal inbox, leased claims, confirmation
    # ------------------------------------------------------------------

    def claim_notifications(
        self, user_id: str, request_id: str, limit: int | None
    ) -> dict[str, Any]:
        """Claim a leased batch of the caller's pending notifications.

        A fresh ``request_id`` takes the oldest claimable notifications (origin
        submission order), skipping items another live lease still holds, and
        leases them to this batch for ``claim_lease_seconds``. Retrying an
        existing ``request_id`` is a pure replay: it returns the still-valid,
        still-unconfirmed, still-owned slice of the original batch — never new
        items, never a longer lease.
        """
        page_size = clamp_limit(limit)
        now = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                self._ensure_user(conn, user_id)
                batch = conn.execute(
                    """
                    SELECT * FROM claim_batches
                    WHERE recipient_id = ? AND request_id = ?
                    """,
                    (user_id, request_id),
                ).fetchone()
                if batch is not None:
                    items = self._claim_replay_items(conn, batch)
                    return self._claim_dict(batch, items)

                batch_id = new_id("clm")
                expires_at = self._lease_expiry(now)
                conn.execute(
                    """
                    INSERT INTO claim_batches
                        (batch_id, request_id, recipient_id, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (batch_id, request_id, user_id, now, expires_at),
                )
                rows = conn.execute(
                    """
                    SELECT n.notification_id
                    FROM notifications n
                    JOIN messages m ON m.id = n.message_id
                    WHERE n.recipient_id = :recipient
                      AND m.retracted = 0
                      AND EXISTS (
                          SELECT 1 FROM notification_reasons r
                          WHERE r.notification_id = n.notification_id
                            AND (
                              (r.kind = 'follow' AND EXISTS (
                                  SELECT 1 FROM follows f
                                  WHERE f.follow_id = r.source_id))
                              OR
                              (r.kind = 'mention' AND EXISTS (
                                  SELECT 1 FROM memberships mem
                                  WHERE mem.membership_id = r.source_id))
                            )
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM claim_items done
                          WHERE done.notification_id = n.notification_id
                            AND done.confirmed_at IS NOT NULL
                      )
                      AND NOT EXISTS (
                          SELECT 1 FROM claim_items held
                          JOIN claim_batches other
                            ON other.batch_id = held.batch_id
                          WHERE held.notification_id = n.notification_id
                            AND held.confirmed_at IS NULL
                            AND other.expires_at > :now
                      )
                    ORDER BY n.seq ASC
                    LIMIT :limit
                    """,
                    {"recipient": user_id, "now": now, "limit": page_size},
                ).fetchall()
                for row in rows:
                    conn.execute(
                        """
                        INSERT INTO claim_items (batch_id, notification_id)
                        VALUES (?, ?)
                        """,
                        (batch_id, row["notification_id"]),
                    )
                items = [
                    self._notification_dict(conn, row["notification_id"])
                    for row in rows
                ]
                batch = conn.execute(
                    "SELECT * FROM claim_batches WHERE batch_id = ?", (batch_id,)
                ).fetchone()
                assert batch is not None
                return self._claim_dict(batch, items)

    def confirm_notification(
        self, user_id: str, batch_id: str, notification_id: str
    ) -> dict[str, Any]:
        """Confirm one claimed notification with its batch credential.

        Confirmation requires a live lease and a still-valid notification. A
        retried confirmation of an already-confirmed item is a no-op that
        succeeds without returning the body again.
        """
        now = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                self._ensure_user(conn, user_id)
                batch = conn.execute(
                    "SELECT * FROM claim_batches WHERE batch_id = ?", (batch_id,)
                ).fetchone()
                if batch is None:
                    raise ApiError(404, "batch_not_found", "no claim batch has that id")
                if batch["recipient_id"] != user_id:
                    raise ApiError(
                        403,
                        "not_batch_owner",
                        "this claim batch belongs to another user",
                    )
                item = conn.execute(
                    """
                    SELECT * FROM claim_items
                    WHERE batch_id = ? AND notification_id = ?
                    """,
                    (batch_id, notification_id),
                ).fetchone()
                if item is None:
                    raise ApiError(
                        404,
                        "notification_not_in_batch",
                        "that notification is not part of this claim batch",
                    )
                if item["confirmed_at"] is not None:
                    # Idempotent replay of a successful confirmation: no state
                    # change, no body, valid even after the lease expired.
                    return self._confirm_dict(
                        batch_id, notification_id, item["confirmed_at"],
                        already=True, message=None,
                    )
                if now > batch["expires_at"]:
                    raise ApiError(
                        409,
                        "claim_expired",
                        "this claim's lease has expired; claim again with a new "
                        "request id to get a fresh credential",
                    )
                if self._reclaimed_elsewhere(conn, batch, notification_id):
                    raise ApiError(
                        409,
                        "notification_reclaimed",
                        "this notification has been re-claimed by a newer batch",
                    )
                if not self._notification_active(conn, notification_id):
                    raise ApiError(
                        409,
                        "notification_inactive",
                        "every trigger reason for this notification has lapsed",
                    )
                conn.execute(
                    """
                    UPDATE claim_items SET confirmed_at = ?
                    WHERE batch_id = ? AND notification_id = ?
                    """,
                    (now, batch_id, notification_id),
                )
                return self._confirm_dict(
                    batch_id,
                    notification_id,
                    now,
                    already=False,
                    message=self._notification_dict(conn, notification_id),
                )

    def _claim_replay_items(
        self, conn: sqlite3.Connection, batch: sqlite3.Row
    ) -> list[dict[str, Any]]:
        """The replayable slice of an existing batch (no new items, no renewal)."""
        rows = conn.execute(
            """
            SELECT ci.notification_id, ci.confirmed_at, n.seq
            FROM claim_items ci
            JOIN notifications n ON n.notification_id = ci.notification_id
            WHERE ci.batch_id = ?
            ORDER BY n.seq
            """,
            (batch["batch_id"],),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            if row["confirmed_at"] is not None:
                continue
            if not self._notification_active(conn, row["notification_id"]):
                continue
            if self._reclaimed_elsewhere(conn, batch, row["notification_id"]):
                continue
            items.append(self._notification_dict(conn, row["notification_id"]))
        return items

    def _claim_dict(
        self, batch: sqlite3.Row, items: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return {
            "batch_id": batch["batch_id"],
            "request_id": batch["request_id"],
            "expires_at": batch["expires_at"],
            "items": items,
        }

    @staticmethod
    def _confirm_dict(
        batch_id: str,
        notification_id: str,
        confirmed_at: str,
        *,
        already: bool,
        message: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "notification_id": notification_id,
            "batch_id": batch_id,
            "confirmed": True,
            "already_confirmed": already,
            "confirmed_at": confirmed_at,
            "message": message,
        }

    def _lease_expiry(self, now_iso: str) -> str:
        moment = datetime.strptime(now_iso, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
        return (
            (moment + self._claim_lease)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )

    @staticmethod
    def _reclaimed_elsewhere(
        conn: sqlite3.Connection, batch: sqlite3.Row, notification_id: str
    ) -> bool:
        """True when a newer batch also holds this notification (ownership moved)."""
        row = conn.execute(
            """
            SELECT 1 FROM claim_items ci
            JOIN claim_batches b ON b.batch_id = ci.batch_id
            WHERE ci.notification_id = ? AND b.seq > ?
            LIMIT 1
            """,
            (notification_id, batch["seq"]),
        ).fetchone()
        return row is not None

    def _notification_active(
        self, conn: sqlite3.Connection, notification_id: str
    ) -> bool:
        """A notification lives while its message is unretracted and at least
        one trigger reason's source row (follow / membership) still exists."""
        return bool(self._live_reason_kinds(conn, notification_id))

    def _live_reason_kinds(
        self, conn: sqlite3.Connection, notification_id: str
    ) -> list[str]:
        retracted = conn.execute(
            """
            SELECT m.retracted FROM notifications n
            JOIN messages m ON m.id = n.message_id
            WHERE n.notification_id = ?
            """,
            (notification_id,),
        ).fetchone()
        if retracted is None or retracted[0]:
            return []
        rows = conn.execute(
            """
            SELECT kind, source_id FROM notification_reasons
            WHERE notification_id = ?
            ORDER BY kind
            """,
            (notification_id,),
        ).fetchall()
        live: list[str] = []
        for row in rows:
            if row["kind"] == "follow":
                found = conn.execute(
                    "SELECT 1 FROM follows WHERE follow_id = ?",
                    (row["source_id"],),
                ).fetchone()
            else:
                found = conn.execute(
                    "SELECT 1 FROM memberships WHERE membership_id = ?",
                    (row["source_id"],),
                ).fetchone()
            if found is not None:
                live.append(row["kind"])
        return live

    def _notification_dict(
        self, conn: sqlite3.Connection, notification_id: str
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT notification_id, conversation_id, message_id, created_at
            FROM notifications WHERE notification_id = ?
            """,
            (notification_id,),
        ).fetchone()
        if row is None:  # pragma: no cover - internal invariant
            raise ApiError(500, "internal_error", "notification is missing")
        return {
            "notification_id": row["notification_id"],
            "conversation_id": row["conversation_id"],
            "message": self._message_in_conversation(
                conn, row["conversation_id"], row["message_id"]
            ),
            "reasons": self._live_reason_kinds(conn, notification_id),
            "created_at": row["created_at"],
        }

    def _resolve_mentions(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        mention_user_ids: list[str],
    ) -> dict[str, str]:
        """Map each mentioned user to their current membership id.

        Mentioning a non-member rejects the whole append: the author must not
        be able to aim notifications outside the conversation.
        """
        memberships: dict[str, str] = {}
        for mentioned_id in mention_user_ids:
            row = conn.execute(
                """
                SELECT membership_id FROM memberships
                WHERE conversation_id = ? AND user_id = ?
                """,
                (conversation_id, mentioned_id),
            ).fetchone()
            if row is None:
                raise ApiError(
                    400,
                    "mention_not_member",
                    f"user {mentioned_id} is not a current member of this "
                    "conversation and cannot be mentioned",
                )
            memberships[mentioned_id] = row["membership_id"]
        return memberships

    def _create_notifications(
        self,
        conn: sqlite3.Connection,
        *,
        message_id: str,
        conversation_id: str,
        author_id: str,
        mention_memberships: dict[str, str],
        now: str,
    ) -> list[str]:
        """Fan out one notification per recipient for a fresh original message.

        Recipients are the members following right now plus the mentioned
        members, minus the author; a recipient in both groups gets a single
        notification carrying both reasons. Nothing else in the system creates
        notifications, so forks, joins and follows never backfill.
        """
        reasons: dict[str, dict[str, str]] = {}
        follow_rows = conn.execute(
            "SELECT follow_id, user_id FROM follows WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchall()
        for row in follow_rows:
            reasons.setdefault(row["user_id"], {})["follow"] = row["follow_id"]
        for mentioned_id, membership_id in mention_memberships.items():
            reasons.setdefault(mentioned_id, {})["mention"] = membership_id
        reasons.pop(author_id, None)

        notified: list[str] = []
        for recipient_id, kinds in reasons.items():
            notification_id = new_id("ntf")
            conn.execute(
                """
                INSERT INTO notifications
                    (notification_id, message_id, recipient_id,
                     conversation_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (notification_id, message_id, recipient_id, conversation_id, now),
            )
            for kind, source_id in kinds.items():
                conn.execute(
                    """
                    INSERT INTO notification_reasons
                        (notification_id, kind, source_id)
                    VALUES (?, ?, ?)
                    """,
                    (notification_id, kind, source_id),
                )
            notified.append(recipient_id)
        return notified


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
