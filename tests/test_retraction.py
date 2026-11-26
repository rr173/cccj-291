"""Retraction: global, terminal, non-leaking, order-preserving."""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import (
    add_member,
    append_ok,
    auth,
    create_conversation,
    fork,
    read_all,
)


def retract(client: TestClient, user: dict, message_id: str):
    return client.post(f"/messages/{message_id}/retraction", headers=auth(user))


def messages_by_id(items: list[dict]) -> dict[str, dict]:
    return {item["message_id"]: item for item in items}


def test_retraction_removes_content_everywhere_but_keeps_place(
    client: TestClient, alice: dict, bob: dict, carol: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    secret = append_ok(client, alice, root["id"], "the secret plan")
    after = append_ok(client, alice, root["id"], "unrelated")

    child = fork(client, alice, root["id"], secret["message_id"], "Child")
    grandchild = fork(client, alice, child["id"], secret["message_id"], "Grandchild")
    assert add_member(client, alice, grandchild["id"], carol).status_code == 201

    # Everyone sees the content before retraction.
    for conv in (root["id"], child["id"], grandchild["id"]):
        assert messages_by_id(read_all(client, alice, conv))[
            secret["message_id"]
        ]["body"] == "the secret plan"

    # Only a non-member of the origin can still be the author? No: authorship is
    # what matters, and a non-author member is refused.
    forbidden = retract(client, bob, secret["message_id"])
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "not_message_author"

    response = retract(client, alice, secret["message_id"])
    assert response.status_code == 200
    body = response.json()
    assert body["retracted"] is True
    assert body["already_retracted"] is False
    assert body["origin"] == {"conversation_id": root["id"], "position": 1}
    assert body["affected_conversations"] == 3
    assert {d["conversation_id"] for d in body["forked_at_retracted_point"]} == {
        child["id"],
        grandchild["id"],
    }

    # Every branch (including one the author is not a member of, reachable by
    # Carol) shows a placeholder with the same id, author and position, and no
    # body. Neither the parent, child nor grandchild leaks the text.
    for conv, reader in (
        (root["id"], alice),
        (child["id"], alice),
        (grandchild["id"], carol),
    ):
        items = read_all(client, reader, conv)
        assert secret["message_id"] in messages_by_id(items)
        placeholder = messages_by_id(items)[secret["message_id"]]
        assert placeholder["body"] is None
        assert placeholder["retracted"] is True
        assert placeholder["retracted_at"] is not None
        assert placeholder["author"]["id"] == alice["id"]
        assert placeholder["origin"] == {
            "conversation_id": root["id"],
            "position": 1,
        }
        assert placeholder["created_at"] == secret["created_at"]

    # The position survives: "unrelated" is still position 2 in the root, and
    # the grandchild's inherited prefix still has length 1.
    root_items = read_all(client, alice, root["id"])
    assert [i["position"] for i in root_items] == [1, 2]
    assert root_items[1]["message_id"] == after["message_id"]

    # Searching does not resurrect retracted text.
    hits = client.get(
        f"/conversations/{root['id']}/search",
        params={"q": "secret"},
        headers=auth(alice),
    ).json()
    assert hits["items"] == []
    for conv, reader in ((child["id"], alice), (grandchild["id"], carol)):
        hits = client.get(
            f"/conversations/{conv}/search",
            params={"q": "plan"},
            headers=auth(reader),
        ).json()
        assert hits["items"] == []

    # A new fork either side of the retracted message only inherits the
    # placeholder; it cannot recover the body.
    fresh = fork(client, alice, grandchild["id"], secret["message_id"], "Fresh")
    fresh_items = read_all(client, alice, fresh["id"])
    assert fresh_items[0]["message_id"] == secret["message_id"]
    assert fresh_items[0]["body"] is None
    assert fresh_items[0]["retracted"] is True

    # Retraction is idempotent and keeps the original timestamp.
    again = retract(client, alice, secret["message_id"])
    assert again.status_code == 200
    assert again.json()["already_retracted"] is True
    assert again.json()["retracted_at"] == body["retracted_at"]
    assert again.json()["affected_conversations"] == 4


def test_retraction_before_a_fork_is_inherited_as_placeholder(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    first = append_ok(client, alice, root["id"], "will vanish")
    second = append_ok(client, alice, root["id"], "stays")

    assert retract(client, alice, first["message_id"]).status_code == 200

    child = fork(client, alice, root["id"], second["message_id"], "Late")
    assert add_member(client, alice, child["id"], bob).status_code == 201
    items = read_all(client, bob, child["id"])
    assert [i["message_id"] for i in items] == [
        first["message_id"],
        second["message_id"],
    ]
    assert items[0]["retracted"] is True and items[0]["body"] is None
    assert items[1]["body"] == "stays"

    # Forking directly at the retracted message also just inherits the
    # placeholder.
    at_placeholder = fork(client, alice, root["id"], first["message_id"], "At")
    only = read_all(client, alice, at_placeholder["id"])
    assert len(only) == 1
    assert only[0]["retracted"] is True and only[0]["body"] is None


def test_retraction_is_author_scoped_even_across_branches(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    alice_msg = append_ok(client, alice, root["id"], "alice says hi")
    assert add_member(client, alice, root["id"], bob).status_code == 201
    bob_msg = append_ok(client, bob, root["id"], "bob says hi")

    child = fork(client, bob, root["id"], bob_msg["message_id"], "Bobs branch")
    # Bob authored bob_msg, so he can retract it globally, including from the
    # parent root where Alice is the owner.
    assert retract(client, bob, bob_msg["message_id"]).status_code == 200
    root_items = messages_by_id(read_all(client, alice, root["id"]))
    child_items = messages_by_id(read_all(client, bob, child["id"]))
    assert root_items[bob_msg["message_id"]]["body"] is None
    assert child_items[bob_msg["message_id"]]["body"] is None
    assert root_items[alice_msg["message_id"]]["body"] == "alice says hi"

    # Alice cannot retract Bob's message, and Bob cannot retract Alice's.
    assert retract(client, alice, bob_msg["message_id"]).status_code == 403
    assert retract(client, bob, alice_msg["message_id"]).status_code == 403

    unknown = retract(client, alice, "msg_missing")
    assert unknown.status_code == 404


def test_author_can_retract_after_losing_membership(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    assert add_member(client, alice, root["id"], bob).status_code == 201
    bob_msg = append_ok(client, bob, root["id"], "bob wrote this")
    child = fork(client, bob, root["id"], bob_msg["message_id"], "Bobs branch")

    # Bob is removed from the origin conversation. His message still exists
    # there and in his own branch; authorship, not membership, governs
    # retraction.
    assert client.delete(
        f"/conversations/{root['id']}/members/{bob['id']}",
        headers=auth(alice),
    ).status_code == 204
    assert client.get(
        f"/conversations/{root['id']}/messages", headers=auth(bob)
    ).status_code == 403

    assert retract(client, bob, bob_msg["message_id"]).status_code == 200
    alice_view = messages_by_id(read_all(client, alice, root["id"]))
    bob_view = messages_by_id(read_all(client, bob, child["id"]))
    assert alice_view[bob_msg["message_id"]]["body"] is None
    assert bob_view[bob_msg["message_id"]]["body"] is None
    assert alice_view[bob_msg["message_id"]]["author"]["id"] == bob["id"]


def test_retracted_message_position_is_never_reused(
    client: TestClient, alice: dict
) -> None:
    conv = create_conversation(client, alice, "Ordering")
    m1 = append_ok(client, alice, conv["id"], "one")
    m2 = append_ok(client, alice, conv["id"], "two")
    assert retract(client, alice, m1["message_id"]).status_code == 200
    m3 = append_ok(client, alice, conv["id"], "three")
    assert m3["position"] == 3
    items = read_all(client, alice, conv["id"])
    assert [i["position"] for i in items] == [1, 2, 3]
    assert items[0]["retracted"] is True
    assert items[1]["message_id"] == m2["message_id"]
