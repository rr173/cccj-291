"""Per-device replica sync over the existing branch data.

A desktop replica keeps its own copy of the chats of one account. The server
does not remember replicas; instead every user-visible change is appended to
``sync_changes`` (see ``app/db.py``) at the moment it commits, fanned out to each
membership instance that can observe it. A pull is then an indexed walk of that
log plus, for a newly granted conversation, a paged walk of its history. Nothing
in the incremental path scans or diffs the message table.

Protocol
--------

``POST /sync/pull`` takes a signed cursor (bound to the account *and* the
device) and an item limit. One response is read from one SQLite snapshot, so
every body it carries is readable by the account in that snapshot, and a
retracted message is already a placeholder.

Items:

``conversation.granted``
    The membership instance ``membership_id`` may read this conversation. Its
    history up to ``history_max_position`` follows as ``message`` items (possibly
    over several batches). Applying a grant means: reset this conversation's
    local copy for this identity, then apply the history.

``message``
    A message at its position in that conversation, shaped exactly like the
    existing message API (same original id/author/origin, ``source``,
    ``retracted`` placeholder). Upsert by ``(conversation_id, position)``.

``conversation.cleared``
    The membership instance is gone; delete this conversation's local copy, but
    only if the local copy was loaded under the same ``membership_id``. Other
    conversations are untouched, even when they show the same original message.

Applying a response is atomic with saving ``next_cursor``: only after the whole
``items`` list is stored may the device move its progress. Retrying with the old
cursor recomputes the same sequence range against a fresh snapshot, so nothing
is skipped; stale rows of a removed identity are skipped by the server, so an
unreadable body is never re-sent. A new identity (re-invite) has a new
``membership_id``, its own grant and its own history, so an unfinished walk
under the old identity can never be mistaken for the new one.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from .cursor import InvalidCursor
from .db import read_transaction, write_transaction
from .errors import ApiError

DEFAULT_SYNC_BATCH = 100
MAX_SYNC_BATCH = 500
# Even when every scanned row is stale or a grant history page is short, one
# HTTP response stays bounded; progress still advances, so consecutive pulls
# converge.
MIN_SCAN_BUDGET = 2000


def _utcnow() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def clamp_sync_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_SYNC_BATCH
    return max(1, min(int(limit), MAX_SYNC_BATCH))


class SyncMixin:
    """Change capture and the pull protocol, used by ConversationService."""

    # ------------------------------------------------------------------
    # Capture side: called inside the writer's transaction
    # ------------------------------------------------------------------

    def _sync_record_grant(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        membership_id: str,
        now: str,
    ) -> None:
        """Record a new membership instance and the history watermark it may read.

        Grants are written for owners created with a conversation, forked
        members, and invited members. The watermark is the branch's current
        maximum position: everything at or below it is loaded as history, while
        later appends arrive as ordinary change rows with a higher ``seq``.
        """
        max_position = conn.execute(
            """
            SELECT COALESCE(MAX(position), 0) FROM conversation_messages
            WHERE conversation_id = ?
            """,
            (conversation_id,),
        ).fetchone()[0]
        user_id = conn.execute(
            "SELECT user_id FROM memberships WHERE membership_id = ?",
            (membership_id,),
        ).fetchone()
        if user_id is None:  # pragma: no cover - caller just inserted the row
            return
        conn.execute(
            """
            INSERT OR IGNORE INTO sync_changes
                (user_id, membership_id, kind, conversation_id,
                 message_id, position, max_position, created_at)
            VALUES (?, ?, 'membership.granted', ?, NULL, NULL, ?, ?)
            """,
            (user_id[0], membership_id, conversation_id, int(max_position), now),
        )

    def _sync_record_revocation(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        membership_id: str,
        user_id: str,
        now: str,
    ) -> None:
        """Record the loss of a membership instance (one clearable change)."""
        conn.execute(
            """
            INSERT OR IGNORE INTO sync_changes
                (user_id, membership_id, kind, conversation_id,
                 message_id, position, max_position, created_at)
            VALUES (?, ?, 'membership.revoked', ?, NULL, NULL, NULL, ?)
            """,
            (user_id, membership_id, conversation_id, now),
        )

    def _sync_record_append(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        position: int,
        message_id: str,
        now: str,
    ) -> None:
        """Fan one new original message out to every current member instance."""
        conn.execute(
            """
            INSERT INTO sync_changes
                (user_id, membership_id, kind, conversation_id,
                 message_id, position, max_position, created_at)
            SELECT mem.user_id, mem.membership_id, 'message.appended', ?, ?, ?, NULL, ?
            FROM memberships mem
            WHERE mem.conversation_id = ?
            """,
            (conversation_id, message_id, position, now, conversation_id),
        )

    def _sync_record_retraction(
        self, conn: sqlite3.Connection, message_id: str, now: str
    ) -> None:
        """Fan a retraction out to *every branch* that references the message.

        The recipient list comes from each branch's own memberships, never from
        the author's, so a cached copy in a branch the author never joined is
        corrected too. The canonical row is already retracted, so the item a
        device receives is a placeholder even if it missed the append.
        """
        conn.execute(
            """
            INSERT INTO sync_changes
                (user_id, membership_id, kind, conversation_id,
                 message_id, position, max_position, created_at)
            SELECT mem.user_id, mem.membership_id, 'message.retracted',
                   cm.conversation_id, cm.message_id, cm.position, NULL, ?
            FROM conversation_messages cm
            JOIN memberships mem ON mem.conversation_id = cm.conversation_id
            WHERE cm.message_id = ?
            """,
            (now, message_id),
        )

    # ------------------------------------------------------------------
    # Bootstrap for databases that predate the sync log
    # ------------------------------------------------------------------

    def ensure_sync_grants(self, user_id: str) -> None:
        """Synthesize grants for memberships that existed before the log.

        A no-op after the first call for a user. Grants created this way carry
        the current history watermark, so the first pull loads that history
        rather than replaying a non-existent change history.
        """
        with self._connect() as conn:
            with read_transaction(conn):
                missing = conn.execute(
                    """
                    SELECT mem.membership_id
                    FROM memberships mem
                    LEFT JOIN sync_changes g
                      ON g.membership_id = mem.membership_id
                     AND g.kind = 'membership.granted'
                    WHERE mem.user_id = ? AND g.seq IS NULL
                    LIMIT 1
                    """,
                    (user_id,),
                ).fetchone()
                if missing is None:
                    return
        now = self._sync_now()
        with self._connect() as conn:
            with write_transaction(conn):
                rows = conn.execute(
                    """
                    SELECT mem.membership_id, mem.conversation_id
                    FROM memberships mem
                    LEFT JOIN sync_changes g
                      ON g.membership_id = mem.membership_id
                     AND g.kind = 'membership.granted'
                    WHERE mem.user_id = ? AND g.seq IS NULL
                    ORDER BY mem.joined_at ASC, mem.membership_id ASC
                    """,
                    (user_id,),
                ).fetchall()
                for row in rows:
                    self._sync_record_grant(
                        conn, row["conversation_id"], row["membership_id"], now
                    )

    @staticmethod
    def _sync_now() -> str:
        return _utcnow()

    # ------------------------------------------------------------------
    # Pull
    # ------------------------------------------------------------------

    def pull_sync(
        self,
        user_id: str,
        device_id: str,
        cursor: str | None,
        limit: int | None,
    ) -> dict[str, Any]:
        page_size = clamp_sync_limit(limit)
        scan_budget = max(MIN_SCAN_BUDGET, page_size * 4)
        state = self._decode_sync_cursor(cursor, user_id, device_id)
        after: int = state["after"]
        boot: dict[str, Any] | None = state["boot"]

        self.ensure_sync_grants(user_id)

        items: list[dict[str, Any]] = []
        scanned = 0
        # Within one response a message may be referenced by more than one
        # change row (append then retraction of the same message). The body is
        # read once per item from the same snapshot, so duplicates are
        # identical; emit it once and let the client upsert by position.
        emitted: set[tuple[str, str]] = set()
        with self._connect() as conn:
            # One snapshot per response: membership liveness, history pages,
            # retraction state and bodies all come from the same committed
            # database state.
            with read_transaction(conn):
                self._ensure_user(conn, user_id)
                live: dict[str, str] = {
                    row["membership_id"]: row["conversation_id"]
                    for row in conn.execute(
                        """
                        SELECT membership_id, conversation_id FROM memberships
                        WHERE user_id = ?
                        """,
                        (user_id,),
                    ).fetchall()
                }

                while len(items) < page_size and scanned < scan_budget:
                    if boot is not None:
                        if (
                            live.get(boot["m"]) != boot["c"]
                        ):
                            # The identity this walk belonged to is gone. Drop
                            # the walk; the revocation change (if not yet
                            # applied) will clear the old copy.
                            boot = None
                            continue
                        loaded = self._sync_load_history(
                            conn,
                            boot,
                            page_size - len(items),
                        )
                        items.extend(loaded["items"])
                        emitted.update(loaded["keys"])
                        scanned += loaded["scanned"]
                        if boot is not None and loaded["finished"]:
                            boot = None
                        if not loaded["scanned"]:
                            # No progress possible in this snapshot.
                            boot = None
                        continue

                    row = conn.execute(
                        """
                        SELECT * FROM sync_changes
                        WHERE user_id = ? AND seq > ?
                        ORDER BY seq ASC
                        LIMIT 1
                        """,
                        (user_id, after),
                    ).fetchone()
                    if row is None:
                        break
                    scanned += 1
                    after = int(row["seq"])
                    kind = row["kind"]

                    if kind == "membership.revoked":
                        # Always delivered: it is the instruction to clear.
                        items.append(
                            {
                                "type": "conversation.cleared",
                                "conversation_id": row["conversation_id"],
                                "membership_id": row["membership_id"],
                                "reason": "membership_revoked",
                                "seq": after,
                            }
                        )
                        continue

                    # Every other item belongs to a specific membership
                    # instance. If it no longer exists, the row is stale: skip
                    # it (progress still advances) rather than send a body the
                    # account can no longer read.
                    if live.get(row["membership_id"]) != row["conversation_id"]:
                        continue

                    if kind == "membership.granted":
                        conversation = conn.execute(
                            "SELECT * FROM conversations WHERE id = ?",
                            (row["conversation_id"],),
                        ).fetchone()
                        if conversation is None:  # pragma: no cover
                            continue
                        item = {
                            "type": "conversation.granted",
                            "conversation_id": row["conversation_id"],
                            "membership_id": row["membership_id"],
                            "history_max_position": int(
                                row["max_position"] or 0
                            ),
                            "conversation": self._conversation_dict(
                                conn, conversation
                            ),
                            "seq": after,
                        }
                        items.append(item)
                        boot = {
                            "c": row["conversation_id"],
                            "m": row["membership_id"],
                            "p": 0,
                            "max": int(row["max_position"] or 0),
                        }
                        continue

                    if kind in ("message.appended", "message.retracted"):
                        key = (row["conversation_id"], row["message_id"] or "")
                        if key in emitted:
                            continue
                        message = self._sync_message_item(
                            conn,
                            row["conversation_id"],
                            row["message_id"],
                            row["membership_id"],
                            after,
                        )
                        if message is not None:
                            emitted.add(key)
                            items.append(message)
                        continue

                # Anything still pending (a boot walk, or change rows past the
                # point we scanned) means another pull is needed.
                pending_change = conn.execute(
                    """
                    SELECT 1 FROM sync_changes
                    WHERE user_id = ? AND seq > ?
                    LIMIT 1
                    """,
                    (user_id, after),
                ).fetchone()
                has_more = boot is not None or pending_change is not None

        next_cursor = self.codec.encode(
            {
                "v": 1,
                "kind": "sync",
                "user": user_id,
                "device": device_id,
                "after": after,
                "boot": boot,
            }
        )
        return {
            "items": items,
            "has_more": has_more,
            "caught_up": not has_more,
            "next_cursor": next_cursor,
            "progress": {
                "after_seq": after,
                "loading_conversation_id": boot["c"] if boot is not None else None,
            },
        }

    def _sync_load_history(
        self,
        conn: sqlite3.Connection,
        boot: dict[str, Any],
        capacity: int,
    ) -> dict[str, Any]:
        """Load the next slice of a conversation's history at a grant watermark."""
        rows = conn.execute(
            """
            SELECT position, message_id
            FROM conversation_messages
            WHERE conversation_id = ?
              AND position > ?
              AND position <= ?
            ORDER BY position ASC
            LIMIT ?
            """,
            (boot["c"], boot["p"], boot["max"], capacity),
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            boot["p"] = int(row["position"])
            item = self._sync_message_item(
                conn,
                boot["c"],
                row["message_id"],
                boot["m"],
                None,
            )
            if item is not None:
                items.append(item)
        finished = not rows or boot["p"] >= boot["max"]
        return {
            "items": items,
            "keys": {(boot["c"], row["message_id"]) for row in rows},
            "scanned": len(rows),
            "finished": finished,
        }

    def _sync_message_item(
        self,
        conn: sqlite3.Connection,
        conversation_id: str,
        message_id: str | None,
        membership_id: str,
        seq: int | None,
    ) -> dict[str, Any] | None:
        """Render one message through the branch view at the current snapshot.

        The body is read live: a message retracted after the change row was
        written is returned as a placeholder, never with its old text.
        """
        if message_id is None:  # pragma: no cover - schema keeps it non-null
            return None
        exists = conn.execute(
            """
            SELECT 1 FROM conversation_messages
            WHERE conversation_id = ? AND message_id = ?
            """,
            (conversation_id, message_id),
        ).fetchone()
        if exists is None:  # pragma: no cover - references are immutable
            return None
        message = self._message_in_conversation(conn, conversation_id, message_id)
        return {
            "type": "message",
            "conversation_id": conversation_id,
            "membership_id": membership_id,
            "position": message["position"],
            "message": message,
            "seq": seq,
        }

    # ------------------------------------------------------------------
    # Cursor binding
    # ------------------------------------------------------------------

    def _decode_sync_cursor(
        self, cursor: str | None, user_id: str, device_id: str
    ) -> dict[str, Any]:
        if cursor is None:
            return {"after": 0, "boot": None}
        try:
            payload = self.codec.decode(cursor)
        except InvalidCursor as exc:
            raise ApiError(400, "invalid_cursor", str(exc)) from exc
        if payload.get("kind") != "sync":
            raise ApiError(
                400, "invalid_cursor", "cursor does not belong to the sync endpoint"
            )
        if payload.get("user") != user_id:
            raise ApiError(
                400, "invalid_cursor", "sync progress belongs to another account"
            )
        if payload.get("device") != device_id:
            raise ApiError(
                400,
                "invalid_cursor",
                "sync progress belongs to another device; start from an empty "
                "cursor for this device",
            )
        after = payload.get("after")
        if not isinstance(after, int) or isinstance(after, bool) or after < 0:
            raise ApiError(400, "invalid_cursor", "sync cursor has invalid progress")
        boot = payload.get("boot")
        if boot is not None:
            valid = (
                isinstance(boot, dict)
                and isinstance(boot.get("c"), str)
                and isinstance(boot.get("m"), str)
                and isinstance(boot.get("p"), int)
                and isinstance(boot.get("max"), int)
            )
            if not valid:  # pragma: no cover - cursor is signed
                raise ApiError(
                    400, "invalid_cursor", "sync cursor has invalid history state"
                )
        return {"after": after, "boot": boot}
