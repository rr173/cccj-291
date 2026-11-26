"""Cross-branch resolution proposals, atomic approval and board invalidation.

This module is mixed into :class:`app.service.ConversationService`.  All state
transitions run through the same ``BEGIN IMMEDIATE`` write transactions as the
rest of the service, so proposal submission, approval, retraction, membership
removal and board invalidation have one SQLite commit order.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from .db import read_transaction, write_transaction
from .errors import ApiError

TOPIC_MAX_LENGTH = 100
TEXT_MAX_LENGTH = 20_000
MAX_PACKAGE_ITEMS = 100


def utcnow() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def clean_topic_key(value: Any) -> str:
    if not isinstance(value, str):
        raise ApiError(400, "invalid_topic_key", "topic identifiers must be strings")
    cleaned = value.strip()
    if not cleaned:
        raise ApiError(400, "invalid_topic_key", "topic identifiers must not be blank")
    if len(cleaned) > TOPIC_MAX_LENGTH:
        raise ApiError(400, "invalid_topic_key", "topic identifier is too long")
    return cleaned


def clean_public_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, "invalid_resolution_text", "public_text must not be blank")
    if len(value) > TEXT_MAX_LENGTH:
        raise ApiError(400, "invalid_resolution_text", "public_text is too long")
    return value


class ResolutionMixin:
    """Proposal and resolution operations used by ConversationService."""

    # ------------------------------------------------------------------
    # Proposal submission and reading
    # ------------------------------------------------------------------

    def create_resolution_proposal(
        self,
        child_conversation_id: str,
        actor_id: str,
        items: list[dict[str, Any]],
        *,
        client_token: str | None = None,
        expected_parent_conversation_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                self._ensure_user(conn, actor_id)
                child = self._require_conversation(conn, child_conversation_id)
                membership = self._require_membership(
                    conn, child_conversation_id, actor_id
                )

                parent_id = child["parent_conversation_id"]
                if not parent_id:
                    raise ApiError(
                        400,
                        "no_parent_conversation",
                        "only a branched conversation can propose a resolution "
                        "to its direct parent",
                    )
                if (
                    expected_parent_conversation_id is not None
                    and parent_id != expected_parent_conversation_id
                ):
                    raise ApiError(
                        400,
                        "not_direct_child",
                        "the source conversation is not a direct child of the "
                        "parent resolution board",
                    )
                self._require_conversation(conn, parent_id)

                if client_token is not None:
                    existing = conn.execute(
                        """
                        SELECT proposal_id FROM resolution_proposals
                        WHERE child_conversation_id = ?
                          AND proposer_id = ?
                          AND client_token = ?
                        """,
                        (child_conversation_id, actor_id, client_token),
                    ).fetchone()
                    if existing is not None:
                        return (
                            self._proposal_dict(conn, existing["proposal_id"]),
                            False,
                        )

                normalized = self._normalize_proposal_items(
                    conn, parent_id, child_conversation_id, items
                )

                proposal_id = new_id("ppl")
                conn.execute(
                    """
                    INSERT INTO resolution_proposals
                        (proposal_id, parent_conversation_id,
                         child_conversation_id, proposer_id,
                         proposer_membership_id, client_token, status,
                         created_at, decided_at, approval_id)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, NULL, NULL)
                    """,
                    (
                        proposal_id,
                        parent_id,
                        child_conversation_id,
                        actor_id,
                        membership["membership_id"],
                        client_token,
                        now,
                    ),
                )

                for ordinal, item in enumerate(normalized):
                    item_id = new_id("pit")
                    board = self._board_row(conn, parent_id, item["topic_key"])
                    if board is None:
                        base_resolution_id = None
                        base_status = "absent"
                    else:
                        base_resolution_id = board["current_resolution_id"]
                        base_status = board["status"]

                    conn.execute(
                        """
                        INSERT INTO resolution_proposal_items
                            (item_id, proposal_id, topic_key, ordinal,
                             public_text, base_resolution_id, base_status)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            item_id,
                            proposal_id,
                            item["topic_key"],
                            ordinal,
                            item["public_text"],
                            base_resolution_id,
                            base_status,
                        ),
                    )
                    for evidence_ordinal, message_id in enumerate(
                        item["evidence_message_ids"]
                    ):
                        conn.execute(
                            """
                            INSERT INTO resolution_proposal_evidence
                                (proposal_item_id, message_id, ordinal)
                            VALUES (?, ?, ?)
                            """,
                            (item_id, message_id, evidence_ordinal),
                        )
                    for dep_ordinal, dependency in enumerate(
                        item["dependencies"]
                    ):
                        conn.execute(
                            """
                            INSERT INTO resolution_proposal_dependencies
                                (proposal_id, source_topic_key,
                                 depends_on_topic_key, scope,
                                 pinned_resolution_id, pinned_version, ordinal)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                proposal_id,
                                item["topic_key"],
                                dependency["topic_key"],
                                dependency["scope"],
                                dependency.get("pinned_resolution_id"),
                                dependency.get("pinned_version"),
                                dep_ordinal,
                            ),
                        )

                self._record_event(
                    conn,
                    "resolution_proposal.submitted",
                    child_conversation_id,
                    actor_id,
                    details={
                        "proposal_id": proposal_id,
                        "parent_conversation_id": parent_id,
                        "topic_keys": [item["topic_key"] for item in normalized],
                    },
                    occurred_at=now,
                )
                return self._proposal_dict(conn, proposal_id), True

    def list_resolution_proposals(
        self,
        parent_conversation_id: str,
        actor_id: str,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            with read_transaction(conn):
                self._ensure_user(conn, actor_id)
                self._require_conversation(conn, parent_conversation_id)
                self._require_owner(conn, parent_conversation_id, actor_id)
                if status not in (None, "pending", "approved", "rejected"):
                    raise ApiError(
                        400,
                        "invalid_status_filter",
                        "status must be pending, approved or rejected",
                    )
                if status is None:
                    rows = conn.execute(
                        """
                        SELECT proposal_id FROM resolution_proposals
                        WHERE parent_conversation_id = ?
                        ORDER BY created_at ASC, proposal_id ASC
                        """,
                        (parent_conversation_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT proposal_id FROM resolution_proposals
                        WHERE parent_conversation_id = ? AND status = ?
                        ORDER BY created_at ASC, proposal_id ASC
                        """,
                        (parent_conversation_id, status),
                    ).fetchall()
                return [self._proposal_dict(conn, row["proposal_id"]) for row in rows]

    def get_resolution_proposal(
        self,
        proposal_id: str,
        actor_id: str,
        *,
        expected_parent_conversation_id: str | None = None,
    ) -> dict[str, Any]:
        with self._connect() as conn:
            with read_transaction(conn):
                self._ensure_user(conn, actor_id)
                proposal = self._require_proposal(conn, proposal_id)
                if (
                    expected_parent_conversation_id is not None
                    and proposal["parent_conversation_id"]
                    != expected_parent_conversation_id
                ):
                    raise ApiError(
                        404,
                        "resolution_proposal_not_found",
                        "no proposal with that id belongs to this parent board",
                    )
                if proposal["proposer_id"] != actor_id:
                    # Only the direct parent's current owner may review someone
                    # else's proposal. Parent ordinary members get the board,
                    # not the review queue.
                    self._require_owner(
                        conn, proposal["parent_conversation_id"], actor_id
                    )
                return self._proposal_dict(conn, proposal_id)

    # ------------------------------------------------------------------
    # Approval
    # ------------------------------------------------------------------

    def approve_resolution_proposal(
        self,
        proposal_id: str,
        actor_id: str,
        request_id: str | None = None,
        *,
        expected_parent_conversation_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        now = utcnow()
        with self._connect() as conn:
            with write_transaction(conn):
                self._ensure_user(conn, actor_id)
                proposal = self._require_proposal(conn, proposal_id)
                parent_id = proposal["parent_conversation_id"]
                if (
                    expected_parent_conversation_id is not None
                    and parent_id != expected_parent_conversation_id
                ):
                    raise ApiError(
                        404,
                        "resolution_proposal_not_found",
                        "no proposal with that id belongs to this parent board",
                    )
                self._require_owner(conn, parent_id, actor_id)

                prior = conn.execute(
                    """
                    SELECT * FROM resolution_approvals
                    WHERE proposal_id = ?
                    """,
                    (proposal_id,),
                ).fetchone()
                if prior is not None:
                    result = json.loads(prior["result_json"] or "{}")
                    result["already_approved"] = True
                    return result, False

                if proposal["status"] == "rejected":
                    raise ApiError(
                        409,
                        "proposal_rejected",
                        "this proposal was rejected and cannot be approved",
                    )

                blockers = self._proposal_blockers(conn, proposal)
                if blockers:
                    code = blockers[0]
                    raise ApiError(
                        409,
                        code,
                        self._blocker_message(code),
                    )

                items = conn.execute(
                    """
                    SELECT * FROM resolution_proposal_items
                    WHERE proposal_id = ?
                    ORDER BY ordinal ASC
                    """,
                    (proposal_id,),
                ).fetchall()
                package_topics = {item["topic_key"] for item in items}
                old_target_ids = [
                    item["base_resolution_id"]
                    for item in items
                    if item["base_resolution_id"]
                    and item["base_status"] != "absent"
                ]
                impact = self._replacement_impact(
                    conn, parent_id, set(old_target_ids), package_topics
                )

                approval_id = new_id("apv")
                conn.execute(
                    """
                    INSERT INTO resolution_approvals
                        (approval_id, proposal_id, parent_conversation_id,
                         approver_id, request_id, result_json, approved_at)
                    VALUES (?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        approval_id,
                        proposal_id,
                        parent_id,
                        actor_id,
                        request_id,
                        now,
                    ),
                )
                topic_resolution_ids = {
                    item["topic_key"]: new_id("res") for item in items
                }
                topic_versions = {
                    item["topic_key"]: self._next_version(
                        conn, parent_id, item["topic_key"]
                    )
                    for item in items
                }

                # Insert every version first so internal package dependencies
                # can reference each other regardless of input ordering.
                for item in items:
                    topic = item["topic_key"]
                    conn.execute(
                        """
                        INSERT INTO resolutions
                            (resolution_id, parent_conversation_id, topic_key,
                             version, proposal_id, approval_id, public_text,
                             approved_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            topic_resolution_ids[topic],
                            parent_id,
                            topic,
                            topic_versions[topic],
                            proposal_id,
                            approval_id,
                            item["public_text"],
                            now,
                        ),
                    )

                approved_resolutions: list[dict[str, Any]] = []
                for item in items:
                    topic = item["topic_key"]
                    resolution_id = topic_resolution_ids[topic]
                    version = topic_versions[topic]
                    evidence_rows = conn.execute(
                        """
                        SELECT message_id FROM resolution_proposal_evidence
                        WHERE proposal_item_id = ?
                        ORDER BY ordinal ASC
                        """,
                        (item["item_id"],),
                    ).fetchall()
                    for evidence in evidence_rows:
                        conn.execute(
                            """
                            INSERT INTO resolution_evidence
                                (resolution_id, message_id)
                            VALUES (?, ?)
                            """,
                            (resolution_id, evidence["message_id"]),
                        )

                    dependencies = conn.execute(
                        """
                        SELECT * FROM resolution_proposal_dependencies
                        WHERE proposal_id = ? AND source_topic_key = ?
                        ORDER BY ordinal ASC
                        """,
                        (proposal_id, topic),
                    ).fetchall()
                    dependency_out: list[dict[str, Any]] = []
                    dependency_keys: list[str] = []
                    for dep in dependencies:
                        dep_topic = dep["depends_on_topic_key"]
                        dependency_keys.append(dep_topic)
                        if dep["scope"] == "internal":
                            target_id = topic_resolution_ids[dep_topic]
                            target_version = topic_versions[dep_topic]
                        else:
                            target_id = dep["pinned_resolution_id"]
                            target_version = dep["pinned_version"]
                        conn.execute(
                            """
                            INSERT INTO resolution_dependencies
                                (resolution_id, parent_conversation_id,
                                 source_topic_key, depends_on_topic_key,
                                 depends_on_resolution_id, depends_on_version)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                resolution_id,
                                parent_id,
                                topic,
                                dep_topic,
                                target_id,
                                target_version,
                            ),
                        )
                        dependency_out.append(
                            {
                                "topic_key": dep_topic,
                                "resolution_id": target_id,
                                "version": target_version,
                                "scope": dep["scope"],
                            }
                        )

                    approved_resolutions.append(
                        {
                            "topic_key": topic,
                            "resolution_id": resolution_id,
                            "version": version,
                            "public_text": item["public_text"],
                            "approved_at": now,
                            "approval_id": approval_id,
                            "depends_on_topic_keys": dependency_keys,
                            "dependencies": dependency_out,
                        }
                    )

                # Upsert every package topic in the same transaction. Before
                # commit, no reader sees half of these pointers move.
                for item in items:
                    topic = item["topic_key"]
                    resolution_id = topic_resolution_ids[topic]
                    conn.execute(
                        """
                        INSERT INTO resolution_board
                            (parent_conversation_id, topic_key,
                             current_resolution_id, status, pending_reason,
                             invalidated_at, updated_at)
                        VALUES (?, ?, ?, 'effective', NULL, NULL, ?)
                        ON CONFLICT(parent_conversation_id, topic_key)
                        DO UPDATE SET
                            current_resolution_id = excluded.current_resolution_id,
                            status = 'effective',
                            pending_reason = NULL,
                            invalidated_at = NULL,
                            updated_at = excluded.updated_at
                        """,
                        (parent_id, topic, resolution_id, now),
                    )

                invalidated_topics = self._apply_impact(
                    conn, impact, now=now, actor_id=actor_id
                )

                # Defence-in-depth: after moving every board pointer and
                # applying cascade state in this transaction, every external
                # pin must still be the exact effective submission-time version.
                external_pins = conn.execute(
                    """
                    SELECT depends_on_topic_key, pinned_resolution_id
                    FROM resolution_proposal_dependencies
                    WHERE proposal_id = ? AND scope = 'external'
                    """,
                    (proposal_id,),
                ).fetchall()
                for pin in external_pins:
                    board = self._board_row(
                        conn, parent_id, pin["depends_on_topic_key"]
                    )
                    if (
                        board is None
                        or board["current_resolution_id"]
                        != pin["pinned_resolution_id"]
                        or board["status"] != "effective"
                    ):
                        raise ApiError(
                            409,
                            "external_premise_would_lapse",
                            self._blocker_message(
                                "external_premise_would_lapse"
                            ),
                        )

                approver = self._author_ref(conn, actor_id)
                result = {
                    "approval_id": approval_id,
                    "proposal_id": proposal_id,
                    "parent_conversation_id": parent_id,
                    "approver": approver,
                    "approved_at": now,
                    "already_approved": False,
                    "package_topic_keys": [item["topic_key"] for item in items],
                    "resolution_ids": [
                        topic_resolution_ids[item["topic_key"]] for item in items
                    ],
                    "resolutions": approved_resolutions,
                    "invalidated_topics": invalidated_topics,
                }
                conn.execute(
                    """
                    UPDATE resolution_approvals
                    SET result_json = ?
                    WHERE approval_id = ?
                    """,
                    (
                        json.dumps(result, ensure_ascii=False, sort_keys=True),
                        approval_id,
                    ),
                )
                conn.execute(
                    """
                    UPDATE resolution_proposals
                    SET status = 'approved', decided_at = ?, approval_id = ?
                    WHERE proposal_id = ?
                    """,
                    (now, approval_id, proposal_id),
                )
                self._record_event(
                    conn,
                    "resolution_proposal.approved",
                    parent_id,
                    actor_id,
                    details={
                        "proposal_id": proposal_id,
                        "approval_id": approval_id,
                        "topic_keys": result["package_topic_keys"],
                        "resolution_ids": [
                            item["resolution_id"]
                            for item in approved_resolutions
                        ],
                        "invalidated_topics": invalidated_topics,
                    },
                    occurred_at=now,
                )
                return result, True

    # ------------------------------------------------------------------
    # Board reads and invalidation
    # ------------------------------------------------------------------

    def get_resolution_board(
        self, parent_conversation_id: str, actor_id: str
    ) -> dict[str, Any]:
        with self._connect() as conn:
            with read_transaction(conn):
                self._ensure_user(conn, actor_id)
                self._require_conversation(conn, parent_conversation_id)
                self._require_membership(conn, parent_conversation_id, actor_id)
                rows = conn.execute(
                    """
                    SELECT * FROM resolution_board
                    WHERE parent_conversation_id = ?
                    ORDER BY topic_key ASC
                    """,
                    (parent_conversation_id,),
                ).fetchall()
                topics = [self._board_topic_dict(conn, row) for row in rows]
                return {
                    "conversation_id": parent_conversation_id,
                    "topics": topics,
                    # Convenience aliases for clients that name the collection
                    # after the board resource rather than its topic rows.
                    "resolutions": topics,
                    "items": topics,
                }

    def get_resolution_topic(
        self, parent_conversation_id: str, actor_id: str, topic_key: str
    ) -> dict[str, Any]:
        topic_key = clean_topic_key(topic_key)
        with self._connect() as conn:
            with read_transaction(conn):
                self._ensure_user(conn, actor_id)
                self._require_conversation(conn, parent_conversation_id)
                self._require_membership(conn, parent_conversation_id, actor_id)
                row = self._board_row(conn, parent_conversation_id, topic_key)
                if row is None:
                    raise ApiError(
                        404,
                        "resolution_topic_not_found",
                        "that topic has no passed resolution on this board",
                    )
                return self._board_topic_dict(conn, row)

    def invalidate_board_for_retracted_message(
        self,
        conn: sqlite3.Connection,
        message_id: str,
        actor_id: str,
        now: str,
    ) -> list[dict[str, str]]:
        """Mark current resolutions backed by this message and descendants."""
        root_rows = conn.execute(
            """
            SELECT DISTINCT
                   r.resolution_id,
                   r.parent_conversation_id,
                   r.topic_key
            FROM resolution_evidence re
            JOIN resolutions r ON r.resolution_id = re.resolution_id
            JOIN resolution_board b ON b.current_resolution_id = r.resolution_id
            WHERE re.message_id = ? AND b.status = 'effective'
            """,
            (message_id,),
        ).fetchall()
        if not root_rows:
            return []

        impact: dict[str, dict[str, str]] = {}
        root_ids: list[str] = []
        for row in root_rows:
            root_ids.append(row["resolution_id"])
            impact[row["resolution_id"]] = {
                "resolution_id": row["resolution_id"],
                "parent_conversation_id": row["parent_conversation_id"],
                "topic_key": row["topic_key"],
                "reason": "basis_retracted",
            }

        downstream = self._dependent_impact(
            conn,
            root_ids,
            direct_reason="dependency_pending",
            indirect_reason="dependency_pending",
            exclude_topics=set(),
        )
        for resolution_id, data in downstream.items():
            impact.setdefault(resolution_id, data)

        return self._apply_impact(conn, impact, now=now, actor_id=actor_id)

    # ------------------------------------------------------------------
    # Validation and serialization helpers
    # ------------------------------------------------------------------

    def _require_proposal(
        self, conn: sqlite3.Connection, proposal_id: str
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM resolution_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchone()
        if row is None:
            raise ApiError(
                404,
                "resolution_proposal_not_found",
                "no resolution proposal has that id",
            )
        return row

    @staticmethod
    def _board_row(
        conn: sqlite3.Connection, parent_id: str, topic_key: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT * FROM resolution_board
            WHERE parent_conversation_id = ? AND topic_key = ?
            """,
            (parent_id, topic_key),
        ).fetchone()

    @staticmethod
    def _next_version(
        conn: sqlite3.Connection, parent_id: str, topic_key: str
    ) -> int:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(version), 0) + 1 AS next_version
            FROM resolutions
            WHERE parent_conversation_id = ? AND topic_key = ?
            """,
            (parent_id, topic_key),
        ).fetchone()
        return int(row["next_version"])

    def _normalize_proposal_items(
        self,
        conn: sqlite3.Connection,
        parent_id: str,
        child_id: str,
        raw_items: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        if not isinstance(raw_items, list) or not raw_items:
            raise ApiError(
                400, "empty_resolution_package", "a proposal needs at least one item"
            )
        if len(raw_items) > MAX_PACKAGE_ITEMS:
            raise ApiError(
                400,
                "resolution_package_too_large",
                f"a proposal may contain at most {MAX_PACKAGE_ITEMS} items",
            )

        items: list[dict[str, Any]] = []
        seen_topics: set[str] = set()
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ApiError(
                    400, "invalid_resolution_item", "each proposal item must be an object"
                )
            topic = clean_topic_key(raw.get("topic_key"))
            if topic in seen_topics:
                raise ApiError(
                    400,
                    "duplicate_resolution_topic",
                    f"topic {topic} appears more than once in this package",
                )
            seen_topics.add(topic)
            text = clean_public_text(raw.get("public_text"))

            raw_evidence = raw.get("evidence_message_ids") or []
            if not isinstance(raw_evidence, list) or not raw_evidence:
                raise ApiError(
                    400,
                    "missing_resolution_evidence",
                    "each resolution needs at least one local child message as evidence",
                )
            evidence: list[str] = []
            for message_id in raw_evidence:
                if not isinstance(message_id, str) or not message_id.strip():
                    raise ApiError(
                        400,
                        "invalid_resolution_evidence",
                        "evidence message ids must be non-empty strings",
                    )
                message_id = message_id.strip()
                if message_id not in evidence:
                    evidence.append(message_id)
            if not evidence:
                raise ApiError(
                    400,
                    "missing_resolution_evidence",
                    "each resolution needs at least one local child message as evidence",
                )

            for message_id in evidence:
                self._validate_evidence_message(conn, child_id, message_id)

            raw_deps = raw.get("depends_on_topic_keys") or []
            if not isinstance(raw_deps, list):
                raise ApiError(
                    400,
                    "invalid_resolution_dependency",
                    "depends_on_topic_keys must be a list of topic identifiers",
                )
            dependencies: list[dict[str, Any]] = []
            seen_deps: set[str] = set()
            for raw_dep in raw_deps:
                dep_topic = clean_topic_key(raw_dep)
                if dep_topic == topic:
                    raise ApiError(
                        400,
                        "resolution_dependency_cycle",
                        f"topic {topic} cannot depend on itself",
                    )
                if dep_topic in seen_deps:
                    continue
                seen_deps.add(dep_topic)
                dependencies.append({"topic_key": dep_topic})

            items.append(
                {
                    "topic_key": topic,
                    "public_text": text,
                    "evidence_message_ids": evidence,
                    "dependencies": dependencies,
                }
            )

        topic_set = {item["topic_key"] for item in items}
        self._reject_dependency_cycles(items, topic_set)

        # Resolve external pins only after all package topics are known.
        for item in items:
            for dependency in item["dependencies"]:
                dep_topic = dependency["topic_key"]
                if dep_topic in topic_set:
                    dependency["scope"] = "internal"
                    continue
                board = self._board_row(conn, parent_id, dep_topic)
                if board is None or board["status"] != "effective":
                    raise ApiError(
                        400,
                        "resolution_dependency_not_effective",
                        "an out-of-package dependency must already have an "
                        "effective resolution at proposal submission",
                    )
                version_row = conn.execute(
                    "SELECT version FROM resolutions WHERE resolution_id = ?",
                    (board["current_resolution_id"],),
                ).fetchone()
                dependency["scope"] = "external"
                dependency["pinned_resolution_id"] = board["current_resolution_id"]
                dependency["pinned_version"] = (
                    version_row["version"] if version_row is not None else None
                )

        return items

    @staticmethod
    def _reject_dependency_cycles(
        items: list[dict[str, Any]], topic_set: set[str]
    ) -> None:
        graph = {
            item["topic_key"]: [
                dep["topic_key"]
                for dep in item["dependencies"]
                if dep["topic_key"] in topic_set
            ]
            for item in items
        }
        state: dict[str, int] = {}

        def visit(topic: str, stack: list[str]) -> None:
            if state.get(topic) == 1:
                cycle = " -> ".join(stack + [topic])
                raise ApiError(
                    400,
                    "resolution_dependency_cycle",
                    f"resolution dependencies form a cycle: {cycle}",
                )
            if state.get(topic) == 2:
                return
            state[topic] = 1
            for dependency in graph.get(topic, []):
                visit(dependency, stack + [topic])
            state[topic] = 2

        for topic in graph:
            visit(topic, [])

    @staticmethod
    def _validate_evidence_message(
        conn: sqlite3.Connection, child_id: str, message_id: str
    ) -> None:
        message = conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()
        if message is None:
            raise ApiError(
                400,
                "invalid_resolution_evidence",
                "evidence must identify an existing message in the child "
                "conversation",
            )
        in_child = conn.execute(
            """
            SELECT 1 FROM conversation_messages
            WHERE conversation_id = ? AND message_id = ?
            """,
            (child_id, message_id),
        ).fetchone()
        if in_child is None:
            raise ApiError(
                400,
                "invalid_resolution_evidence",
                "evidence must be a message visible in the child conversation",
            )
        if message["origin_conversation_id"] != child_id:
            raise ApiError(
                400,
                "inherited_message_cannot_be_evidence",
                "only a message originally authored in the child conversation "
                "can support a proposal; inherited messages cannot be evidence",
            )
        if message["retracted"]:
            raise ApiError(
                400,
                "retracted_resolution_evidence",
                "evidence must be an unretracted child message at submission time",
            )

    def _proposal_blockers(
        self, conn: sqlite3.Connection, proposal: sqlite3.Row
    ) -> list[str]:
        proposal_id = proposal["proposal_id"]
        parent_id = proposal["parent_conversation_id"]
        blockers: list[str] = []

        membership = conn.execute(
            "SELECT 1 FROM memberships WHERE membership_id = ?",
            (proposal["proposer_membership_id"],),
        ).fetchone()
        if membership is None:
            blockers.append("proposer_membership_lapsed")

        items = conn.execute(
            """
            SELECT * FROM resolution_proposal_items
            WHERE proposal_id = ?
            ORDER BY ordinal ASC
            """,
            (proposal_id,),
        ).fetchall()
        package_topics = {item["topic_key"] for item in items}
        old_target_ids: set[str] = set()

        for item in items:
            retracted_evidence = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM resolution_proposal_evidence pe
                JOIN messages m ON m.id = pe.message_id
                WHERE pe.proposal_item_id = ? AND m.retracted = 1
                """,
                (item["item_id"],),
            ).fetchone()["count"]
            if retracted_evidence:
                blockers.append("basis_retracted")

            board = self._board_row(conn, parent_id, item["topic_key"])
            if item["base_status"] == "absent":
                if board is not None:
                    blockers.append("topic_updated_elsewhere")
            else:
                if (
                    board is None
                    or board["current_resolution_id"]
                    != item["base_resolution_id"]
                ):
                    blockers.append("topic_updated_elsewhere")
                elif (
                    item["base_status"] == "effective"
                    and board["status"] != "effective"
                ):
                    blockers.append("topic_pending_reconsideration")
                if item["base_resolution_id"]:
                    old_target_ids.add(item["base_resolution_id"])

        external_deps = conn.execute(
            """
            SELECT * FROM resolution_proposal_dependencies
            WHERE proposal_id = ? AND scope = 'external'
            ORDER BY ordinal ASC
            """,
            (proposal_id,),
        ).fetchall()
        for dep in external_deps:
            board = self._board_row(
                conn, parent_id, dep["depends_on_topic_key"]
            )
            if (
                board is None
                or board["current_resolution_id"] != dep["pinned_resolution_id"]
            ):
                blockers.append("external_dependency_changed")
            elif board["status"] != "effective":
                blockers.append("external_dependency_lapsed")

        impact = self._replacement_impact(
            conn, parent_id, old_target_ids, package_topics
        )
        for dep in external_deps:
            pinned_id = dep["pinned_resolution_id"]
            if pinned_id in impact:
                blockers.append("external_premise_would_lapse")

        # Preserve deterministic order while de-duplicating repeated codes.
        seen: set[str] = set()
        ordered: list[str] = []
        for code in blockers:
            if code not in seen:
                seen.add(code)
                ordered.append(code)
        return ordered

    @staticmethod
    def _blocker_message(code: str) -> str:
        messages = {
            "proposer_membership_lapsed": (
                "the proposer has left the child conversation since submission; "
                "rejoining issues a new membership and does not revive the proposal"
            ),
            "basis_retracted": (
                "a supporting child message has been retracted, so the package "
                "can no longer be approved"
            ),
            "topic_updated_elsewhere": (
                "a topic in this package was replaced by another approved proposal "
                "after submission"
            ),
            "topic_pending_reconsideration": (
                "a topic that was effective at submission has become pending "
                "reconsideration"
            ),
            "external_dependency_changed": (
                "an out-of-package dependency was replaced after submission"
            ),
            "external_dependency_lapsed": (
                "an out-of-package dependency has become pending reconsideration"
            ),
            "external_premise_would_lapse": (
                "applying this package would invalidate an external premise in "
                "its submission-time effective version"
            ),
        }
        return messages.get(code, "the proposal cannot be approved")

    def _replacement_impact(
        self,
        conn: sqlite3.Connection,
        parent_id: str,
        old_resolution_ids: set[str],
        package_topics: set[str],
    ) -> dict[str, dict[str, str]]:
        if not old_resolution_ids:
            return {}
        return self._dependent_impact(
            conn,
            list(old_resolution_ids),
            direct_reason="dependency_replaced",
            indirect_reason="dependency_pending",
            exclude_topics=package_topics,
        )

    def _dependent_impact(
        self,
        conn: sqlite3.Connection,
        target_resolution_ids: list[str],
        *,
        direct_reason: str,
        indirect_reason: str,
        exclude_topics: set[str],
    ) -> dict[str, dict[str, str]]:
        """Return current resolutions depending on targets, transitively."""
        impact: dict[str, dict[str, str]] = {}
        if not target_resolution_ids:
            return impact

        def rows_for(target_id: str) -> list[sqlite3.Row]:
            return conn.execute(
                """
                SELECT rd.resolution_id,
                       rd.parent_conversation_id,
                       rd.source_topic_key AS topic_key,
                       b.status AS board_status
                FROM resolution_dependencies rd
                JOIN resolution_board b
                  ON b.current_resolution_id = rd.resolution_id
                WHERE rd.depends_on_resolution_id = ?
                """,
                (target_id,),
            ).fetchall()

        queue: list[str] = []
        for target_id in target_resolution_ids:
            for row in rows_for(target_id):
                resolution_id = row["resolution_id"]
                topic = row["topic_key"]
                if topic in exclude_topics or resolution_id in impact:
                    continue
                impact[resolution_id] = {
                    "resolution_id": resolution_id,
                    "parent_conversation_id": row["parent_conversation_id"],
                    "topic_key": topic,
                    "reason": direct_reason,
                }
                queue.append(resolution_id)

        while queue:
            current_id = queue.pop(0)
            for row in rows_for(current_id):
                resolution_id = row["resolution_id"]
                topic = row["topic_key"]
                if topic in exclude_topics or resolution_id in impact:
                    continue
                impact[resolution_id] = {
                    "resolution_id": resolution_id,
                    "parent_conversation_id": row["parent_conversation_id"],
                    "topic_key": topic,
                    "reason": indirect_reason,
                }
                queue.append(resolution_id)

        return impact

    def _apply_impact(
        self,
        conn: sqlite3.Connection,
        impact: dict[str, dict[str, str]],
        *,
        now: str,
        actor_id: str,
    ) -> list[dict[str, str]]:
        changed: list[dict[str, str]] = []
        for resolution_id, data in impact.items():
            board = conn.execute(
                """
                SELECT parent_conversation_id, topic_key, status
                FROM resolution_board
                WHERE current_resolution_id = ?
                """,
                (resolution_id,),
            ).fetchone()
            if board is None or board["status"] != "effective":
                continue
            reason = data["reason"]
            conn.execute(
                """
                UPDATE resolution_board
                SET status = 'pending_reconsideration',
                    pending_reason = ?,
                    invalidated_at = ?,
                    updated_at = ?
                WHERE current_resolution_id = ? AND status = 'effective'
                """,
                (reason, now, now, resolution_id),
            )
            changed.append(
                {"topic_key": board["topic_key"], "pending_reason": reason}
            )
            self._record_event(
                conn,
                "resolution_board.topic_pending",
                board["parent_conversation_id"],
                actor_id,
                details={"topic_key": board["topic_key"], "reason": reason},
                occurred_at=now,
            )
        changed.sort(key=lambda item: item["topic_key"])
        return changed

    def _proposal_dict(
        self, conn: sqlite3.Connection, proposal_id: str
    ) -> dict[str, Any]:
        proposal = self._require_proposal(conn, proposal_id)
        items_rows = conn.execute(
            """
            SELECT * FROM resolution_proposal_items
            WHERE proposal_id = ?
            ORDER BY ordinal ASC
            """,
            (proposal_id,),
        ).fetchall()
        blockers = (
            self._proposal_blockers(conn, proposal)
            if proposal["status"] == "pending"
            else []
        )

        items: list[dict[str, Any]] = []
        for item in items_rows:
            evidence = conn.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN m.retracted = 1 THEN 1 ELSE 0 END), 0)
                        AS retracted_count
                FROM resolution_proposal_evidence pe
                JOIN messages m ON m.id = pe.message_id
                WHERE pe.proposal_item_id = ?
                """,
                (item["item_id"],),
            ).fetchone()
            basis_valid = (
                int(evidence["total"]) > 0
                and int(evidence["retracted_count"]) == 0
            )
            dependencies = conn.execute(
                """
                SELECT * FROM resolution_proposal_dependencies
                WHERE proposal_id = ? AND source_topic_key = ?
                ORDER BY ordinal ASC
                """,
                (proposal_id, item["topic_key"]),
            ).fetchall()
            dep_list = [
                {
                    "topic_key": dep["depends_on_topic_key"],
                    "scope": dep["scope"],
                    "pinned_resolution_id": dep["pinned_resolution_id"],
                    "pinned_version": dep["pinned_version"],
                }
                for dep in dependencies
            ]
            dep_keys = [dep["topic_key"] for dep in dep_list]

            # A pending proposal with a retracted basis must not expose its
            # proposed public text through the review/status API.
            text_visible = proposal["status"] != "pending" or basis_valid
            items.append(
                {
                    "topic_key": item["topic_key"],
                    "text_visible": text_visible,
                    "public_text": item["public_text"] if text_visible else None,
                    "depends_on_topic_keys": dep_keys,
                    "dependencies": dep_list,
                }
            )

        return {
            "proposal_id": proposal_id,
            "parent_conversation_id": proposal["parent_conversation_id"],
            "child_conversation_id": proposal["child_conversation_id"],
            "proposer_id": proposal["proposer_id"],
            "proposer": self._author_ref(conn, proposal["proposer_id"]),
            "status": proposal["status"],
            "approvable": proposal["status"] == "pending" and not blockers,
            "block_code": blockers[0] if blockers else None,
            "block_codes": blockers,
            "created_at": proposal["created_at"],
            "decided_at": proposal["decided_at"],
            "approval_id": proposal["approval_id"],
            "items": items,
        }

    def _board_topic_dict(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        resolution = None
        if row["status"] == "effective":
            res = conn.execute(
                "SELECT * FROM resolutions WHERE resolution_id = ?",
                (row["current_resolution_id"],),
            ).fetchone()
            if res is not None:
                dep_rows = conn.execute(
                    """
                    SELECT * FROM resolution_dependencies
                    WHERE resolution_id = ?
                    ORDER BY depends_on_topic_key ASC
                    """,
                    (res["resolution_id"],),
                ).fetchall()
                dependencies = [
                    {
                        "topic_key": dep["depends_on_topic_key"],
                        "resolution_id": dep["depends_on_resolution_id"],
                        "version": dep["depends_on_version"],
                    }
                    for dep in dep_rows
                ]
                resolution = {
                    "resolution_id": res["resolution_id"],
                    "version": res["version"],
                    "public_text": res["public_text"],
                    "approved_at": res["approved_at"],
                    "approval_id": res["approval_id"],
                    "depends_on_topic_keys": [
                        dep["topic_key"] for dep in dependencies
                    ],
                    "dependencies": dependencies,
                }
        return {
            "topic_key": row["topic_key"],
            "status": row["status"],
            "resolution": resolution,
            "pending_reason": row["pending_reason"],
            "invalidated_at": row["invalidated_at"],
            "updated_at": row["updated_at"],
        }
