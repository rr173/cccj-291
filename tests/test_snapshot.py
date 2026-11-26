"""Readers never observe a half-applied retraction, and audits show commit order."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from app.service import ConversationService


@pytest.fixture()
def service(tmp_path: Path) -> ConversationService:
    return ConversationService(str(tmp_path / "snapshot.sqlite3"))


def test_reader_snapshot_is_not_torn_by_retraction(service: ConversationService) -> None:
    alice = service.create_user("Alice")["id"]
    cid = service.create_conversation(alice, "Snapshot")["id"]
    messages = [
        service.append_message(cid, alice, f"body {i}", None)[0] for i in range(6)
    ]

    # A reader holds a deferred transaction open (BEGIN, then reads). Retraction
    # must not be visible until that reader's transaction ends.
    reader = service.db.connect()
    reader.execute("BEGIN")
    before = reader.execute(
        """
        SELECT m.id, m.body, m.retracted
        FROM conversation_messages cm JOIN messages m ON m.id = cm.message_id
        WHERE cm.conversation_id = ?
        ORDER BY cm.position
        """,
        (cid,),
    ).fetchall()
    assert len(before) == 6
    assert all(row["retracted"] == 0 for row in before)

    retracted_at_holder: list[str] = []

    def retract() -> None:
        result = service.retract_message(messages[0]["message_id"], alice)
        retracted_at_holder.append(result["retracted_at"])

    worker = threading.Thread(target=retract)
    worker.start()
    # Give the writer a chance. It may briefly block on the WAL checkpoint, but
    # the commit itself is already durable; it must not hang.
    worker.join(timeout=10)
    assert not worker.is_alive()

    snapshot_again = reader.execute(
        """
        SELECT m.retracted FROM conversation_messages cm
        JOIN messages m ON m.id = cm.message_id
        WHERE cm.conversation_id = ? ORDER BY cm.position
        """,
        (cid,),
    ).fetchall()
    # Still the pre-retraction state inside the open snapshot.
    assert all(row["retracted"] == 0 for row in snapshot_again)
    reader.execute("ROLLBACK")
    reader.close()

    after = service.list_messages(cid, alice, 50, None)["items"]
    assert [i["retracted"] for i in after] == [True, False, False, False, False, False]
    assert after[0]["body"] is None
    assert all(i["body"] == f"body {i['position'] - 1}" for i in after[1:])


def test_competing_operations_have_a_total_commit_order(
    service: ConversationService,
) -> None:
    alice = service.create_user("Alice")["id"]
    cid = service.create_conversation(alice, "Ordering")["id"]

    # Interleave append, fork, and retract from separate threads. Whatever the
    # interleaving, the resulting state must correspond to *some* serial order,
    # and the event log must be monotonic.
    m1 = service.append_message(cid, alice, "one", None)[0]
    m2 = service.append_message(cid, alice, "two", "tok-two")[0]
    service.append_message(cid, alice, "three", "tok-two")  # dedup, no-op

    errors: list[BaseException] = []

    def do_fork() -> None:
        try:
            service.fork_conversation(cid, alice, m2["message_id"], "Child")
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def do_append() -> None:
        try:
            service.append_message(cid, alice, "four", None)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def do_retract() -> None:
        try:
            service.retract_message(m1["message_id"], alice)
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [
        threading.Thread(target=target)
        for target in (do_fork, do_append, do_retract, do_append, do_fork)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
        assert not thread.is_alive()
    assert errors == []

    events = service.list_events(cid, alice, after_seq=0, limit=100)
    seqs = [event["seq"] for event in events]
    assert seqs == sorted(seqs)
    assert len(seqs) == len(set(seqs))

    # Sequence numbers reflect a real commit order: each append's position is
    # chosen inside its own BEGIN IMMEDIATE and matches insertion order.
    appends = [
        event for event in events if event["kind"] == "message.appended"
    ]
    positions = [event["details"]["position"] for event in appends]
    assert positions == sorted(positions)

    # Retraction is reflected in every branch, and every branch's own audit log
    # records the change (or, for branches created later, the inherited
    # placeholder).
    child_ids = [
        conv["id"]
        for conv in service.list_conversations(alice)
        if conv["parent_conversation_id"] == cid
    ]
    assert len(child_ids) == 2
    for branch in [cid] + child_ids:
        items = service.list_messages(branch, alice, 50, None)["items"]
        target = next(i for i in items if i["message_id"] == m1["message_id"])
        assert target["retracted"] and target["body"] is None
        events = service.list_events(branch, alice, 0, 100)
        related = [
            e
            for e in events
            if e["kind"] in {"message.retracted", "message.inherited_retracted"}
            and e["message_id"] == m1["message_id"]
        ]
        assert related, f"no audit row in branch {branch}"


def test_retraction_clears_the_stored_body(service: ConversationService) -> None:
    alice = service.create_user("Alice")["id"]
    cid = service.create_conversation(alice, "Erase")["id"]
    message = service.append_message(cid, alice, "erase me completely", None)[0]
    service.retract_message(message["message_id"], alice)

    raw = service.db.connect()
    try:
        row = raw.execute(
            "SELECT body, retracted FROM messages WHERE id = ?",
            (message["message_id"],),
        ).fetchone()
    finally:
        raw.close()
    assert row["body"] is None
    assert row["retracted"] == 1

    # Defence in depth: the database refuses to resurrect or rewrite a body.
    raw = service.db.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "UPDATE messages SET body = 'resurrected', retracted = 0 WHERE id = ?",
                (message["message_id"],),
            )
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute("DELETE FROM messages WHERE id = ?", (message["message_id"],))
        with pytest.raises(sqlite3.IntegrityError):
            raw.execute(
                "UPDATE messages SET author_id = 'usr_forged' WHERE id = ?",
                (message["message_id"],),
            )
    finally:
        raw.close()


def test_forks_created_after_retraction_reference_the_placeholder(
    service: ConversationService,
) -> None:
    alice = service.create_user("Alice")["id"]
    cid = service.create_conversation(alice, "Late forks")["id"]
    first = service.append_message(cid, alice, "gone soon", None)[0]
    third = service.append_message(cid, alice, "stays", None)[0]
    service.retract_message(first["message_id"], alice)

    for fork_point in (first, third):
        branch = service.fork_conversation(
            cid, alice, fork_point["message_id"], "Late"
        )
        items = service.list_messages(branch["id"], alice, 50, None)["items"]
        assert items[0]["message_id"] == first["message_id"]
        assert items[0]["retracted"] is True
        assert items[0]["body"] is None
        assert items[0]["origin"] == {
            "conversation_id": cid,
            "position": first["position"],
        }
