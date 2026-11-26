"""State must survive a process-style restart: a fresh service over the same file."""

from __future__ import annotations

from pathlib import Path

from app.service import ConversationService


def test_lineage_retraction_and_cursors_survive_restart(tmp_path: Path) -> None:
    db_path = str(tmp_path / "restart.sqlite3")
    first = ConversationService(db_path)

    alice = first.create_user("Alice")["id"]
    bob = first.create_user("Bob")["id"]
    root = first.create_conversation(alice, "Root")["id"]
    m1 = first.append_message(root, alice, "root one", None)[0]
    m2 = first.append_message(root, alice, "root two", None)[0]
    child = first.fork_conversation(root, alice, m2["message_id"], "Child")["id"]
    first.add_member(child, alice, bob)
    c1 = first.append_message(child, bob, "child local", None)[0]
    grandchild = first.fork_conversation(child, bob, c1["message_id"], "Grand")["id"]

    # Page cursor issued before the "restart".
    page_before = first.list_messages(grandchild, bob, 1, None)
    cursor_before = page_before["next_cursor"]
    assert cursor_before is not None

    retracted = first.retract_message(m1["message_id"], alice)
    assert retracted["affected_conversations"] == 3

    # --- restart boundary -------------------------------------------------
    second = ConversationService(db_path)

    assert second.get_conversation(root, alice)["title"] == "Root"
    gc_items = second.list_messages(grandchild, bob, 50, None)["items"]
    assert [i["message_id"] for i in gc_items] == [
        m1["message_id"],
        m2["message_id"],
        c1["message_id"],
    ]
    assert gc_items[0]["retracted"] is True and gc_items[0]["body"] is None
    assert gc_items[1]["body"] == "root two"
    assert gc_items[2]["body"] == "child local"
    assert second.get_conversation(grandchild, bob)["parent_conversation_id"] == child

    # The old cursor still verifies after restart and continues the sequence.
    continued = second.list_messages(grandchild, bob, 1, cursor_before)
    assert [i["position"] for i in continued["items"]] == [2]
    assert continued["items"][0]["message_id"] == m2["message_id"]

    # Search agrees with reads after restart.
    assert second.search_messages(grandchild, bob, "root one", None, None)["items"] == []
    assert [
        i["message_id"]
        for i in second.search_messages(grandchild, bob, "child", None, None)["items"]
    ] == [c1["message_id"]]

    # Append retry deduplication also survives restart.
    first_token = first.append_message(child, bob, "retry me", "tok-child")[0]
    again_message, created = second.append_message(child, bob, "retry me", "tok-child")
    assert created is False
    assert again_message["message_id"] == first_token["message_id"]
    assert len(second.list_messages(child, bob, 50, None)["items"]) == 4

    # A message cannot be retracted twice into a different state.
    repeat = second.retract_message(m1["message_id"], alice)
    assert repeat["already_retracted"] is True
    assert repeat["retracted_at"] == retracted["retracted_at"]

    # A fresh service also recreates the schema without losing rows.
    third = ConversationService(db_path)
    assert len(third.list_messages(root, alice, 50, None)["items"]) == 2
