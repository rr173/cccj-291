"""Core conversation and fork semantics."""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import (
    add_member,
    append,
    append_ok,
    auth,
    create_conversation,
    fork,
    read_all,
    remove_member,
)


def test_participants_read_their_own_profiles(client: TestClient, alice: dict) -> None:
    response = client.get(f"/users/{alice['id']}")
    assert response.status_code == 200
    assert response.json()["display_name"] == "Alice"

    assert client.post("/users", json={"display_name": "   "}).status_code == 422
    assert client.post(
        "/conversations", json={"title": "x"}, headers={"X-User-Id": "usr_nope"}
    ).status_code == 401


def test_conversation_has_explicit_membership(
    client: TestClient, alice: dict, bob: dict
) -> None:
    conversation = create_conversation(client, alice, "Plan")
    cid = conversation["id"]

    assert [m["user"]["id"] for m in conversation["members"]] == [alice["id"]]
    assert conversation["inherited_message_count"] == 0
    assert conversation["parent_conversation_id"] is None

    message = append_ok(client, alice, cid, "first")
    # Bob exists but was not invited, so he cannot read the conversation.
    denied = client.get(f"/conversations/{cid}/messages", headers=auth(bob))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "not_a_member"
    assert append(client, bob, cid, "sneak").status_code == 403

    invited = add_member(client, alice, cid, bob)
    assert invited.status_code == 201
    assert invited.json()["role"] == "member"
    reply = append_ok(client, bob, cid, "reply")
    assert reply["author"]["id"] == bob["id"]

    # A member cannot change membership; only the owner can.
    assert add_member(client, bob, cid, {"id": "usr_whatever"}).status_code == 403
    assert client.get(f"/conversations/{cid}/messages", headers=auth(bob)).status_code == 200

    listed = client.get("/conversations", headers=auth(bob)).json()
    assert [c["id"] for c in listed] == [cid]
    assert message["message_id"] != reply["message_id"]


def test_fork_inherits_history_by_reference_only(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    m1 = append_ok(client, alice, root["id"], "one")
    m2 = append_ok(client, alice, root["id"], "two")
    m3 = append_ok(client, alice, root["id"], "three")

    child = fork(client, alice, root["id"], m2["message_id"], "Side")
    inherited = read_all(client, alice, child["id"])
    assert [item["message_id"] for item in inherited] == [
        m1["message_id"],
        m2["message_id"],
    ]
    # Same original ids and authors, positions 1..2, and flagged inherited.
    assert [item["position"] for item in inherited] == [1, 2]
    assert all(item["source"] == "inherited" for item in inherited)
    assert all(item["author"]["id"] == alice["id"] for item in inherited)
    assert inherited[1]["origin"] == {
        "conversation_id": root["id"],
        "position": 2,
    }

    # New parent content does not enter the child.
    append_ok(client, alice, root["id"], "after fork in parent")
    # And child appends never flow back to the parent.
    child_msg = append_ok(client, alice, child["id"], "child only")
    assert child_msg["position"] == 3
    assert child_msg["source"] == "local"
    assert child_msg["origin"]["conversation_id"] == child["id"]

    parent_ids = [item["message_id"] for item in read_all(client, alice, root["id"])]
    assert child_msg["message_id"] not in parent_ids
    assert [m["message_id"] for m in read_all(client, alice, child["id"])] == [
        m1["message_id"],
        m2["message_id"],
        child_msg["message_id"],
    ]

    # Metadata exposes fork provenance.
    meta = client.get(f"/conversations/{child['id']}", headers=auth(alice)).json()
    assert meta["parent_conversation_id"] == root["id"]
    assert meta["fork_point"]["message_id"] == m2["message_id"]
    assert meta["fork_point"]["parent_position"] == 2
    assert meta["inherited_message_count"] == 2
    assert m3["message_id"] not in {i["message_id"] for i in inherited}


def test_two_level_fork_keeps_original_identity(
    client: TestClient, alice: dict, bob: dict, carol: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    a1 = append_ok(client, alice, root["id"], "root one")
    a2 = append_ok(client, alice, root["id"], "root two")

    child = fork(client, alice, root["id"], a2["message_id"], "Child")
    b1 = append_ok(client, alice, child["id"], "child one")

    # Fork the child, at a message the child inherited, from a different user.
    assert add_member(client, alice, child["id"], bob).status_code == 201
    grandchild = fork(client, bob, child["id"], a2["message_id"], "Grandchild")
    c1 = append_ok(client, bob, grandchild["id"], "grandchild one")

    grand_items = read_all(client, bob, grandchild["id"])
    assert [i["message_id"] for i in grand_items] == [
        a1["message_id"],
        a2["message_id"],
        c1["message_id"],
    ]
    assert [i["position"] for i in grand_items] == [1, 2, 3]
    assert grand_items[0]["origin"] == {"conversation_id": root["id"], "position": 1}
    assert grand_items[1]["origin"] == {"conversation_id": root["id"], "position": 2}
    assert grand_items[2]["origin"]["conversation_id"] == grandchild["id"]
    assert b1["message_id"] not in {i["message_id"] for i in grand_items}

    assert grandchild["parent_conversation_id"] == child["id"]
    assert grandchild["fork_point"]["origin"] == {
        "conversation_id": root["id"],
        "position": 2,
    }

    # Carol is a member of the grandchild only and can see the inherited prefix.
    assert add_member(client, bob, grandchild["id"], carol).status_code == 201
    carol_view = read_all(client, carol, grandchild["id"])
    assert [i["message_id"] for i in carol_view] == [
        a1["message_id"],
        a2["message_id"],
        c1["message_id"],
    ]
    # But not the root or the intermediate child.
    assert client.get(
        f"/conversations/{root['id']}/messages", headers=auth(carol)
    ).status_code == 403
    assert client.get(
        f"/conversations/{child['id']}/messages", headers=auth(carol)
    ).status_code == 403


def test_membership_is_scoped_to_one_conversation(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    msg = append_ok(client, alice, root["id"], "hello")
    assert add_member(client, alice, root["id"], bob).status_code == 201

    child = fork(client, alice, root["id"], msg["message_id"], "Child")
    # Bob is in the parent but not automatically in the child.
    assert client.get(
        f"/conversations/{child['id']}/messages", headers=auth(bob)
    ).status_code == 403
    assert append(client, bob, child["id"], "nope").status_code == 403

    assert add_member(client, alice, child["id"], bob).status_code == 201
    assert client.get(
        f"/conversations/{child['id']}/messages", headers=auth(bob)
    ).status_code == 200

    # Removing Bob from the child leaves the parent membership intact.
    assert remove_member(client, alice, child["id"], bob).status_code == 204
    assert client.get(
        f"/conversations/{child['id']}/messages", headers=auth(bob)
    ).status_code == 403
    assert client.get(
        f"/conversations/{root['id']}/messages", headers=auth(bob)
    ).status_code == 200


def test_fork_must_target_a_visible_message_in_that_conversation(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    m1 = append_ok(client, alice, root["id"], "one")
    m2 = append_ok(client, alice, root["id"], "two")
    child = fork(client, alice, root["id"], m1["message_id"], "Child")

    # An unrelated message id.
    missing = client.post(
        f"/conversations/{root['id']}/fork",
        json={"parent_message_id": "msg_missing"},
        headers=auth(alice),
    )
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "fork_point_not_visible"

    # A message id that exists in the parent but was not inherited by the child
    # is not visible there, so it cannot be used as the child's fork point.
    not_in_view = client.post(
        f"/conversations/{child['id']}/fork",
        json={"parent_message_id": m2["message_id"]},
        headers=auth(alice),
    )
    assert not_in_view.status_code == 400

    # A non-member cannot fork.
    assert client.post(
        f"/conversations/{root['id']}/fork",
        json={"parent_message_id": m2["message_id"]},
        headers=auth(bob),
    ).status_code == 403


def test_message_submission_is_deduplicated(
    client: TestClient, alice: dict
) -> None:
    conversation = create_conversation(client, alice, "Retries")
    cid = conversation["id"]

    first = append(client, alice, cid, "send once", "token-1")
    assert first.status_code == 201
    second = append(client, alice, cid, "send once", "token-1")
    assert second.status_code == 200
    assert second.json()["message_id"] == first.json()["message_id"]

    # Same token, different body still resolves to the original message.
    third = append(client, alice, cid, "different", "token-1")
    assert third.status_code == 200
    assert third.json()["message_id"] == first.json()["message_id"]
    assert third.json()["body"] == "send once"

    # A different token creates a genuinely new message.
    fourth = append(client, alice, cid, "send once", "token-2")
    assert fourth.status_code == 201
    assert fourth.json()["message_id"] != first.json()["message_id"]

    # Tokens are scoped per author.
    assert append(client, alice, cid, "no token").status_code == 201
    assert append(client, alice, cid, "no token").status_code == 201

    items = read_all(client, alice, cid)
    assert [i["message_id"] for i in items] == [
        first.json()["message_id"],
        fourth.json()["message_id"],
        items[2]["message_id"],
        items[3]["message_id"],
    ]
    assert [i["position"] for i in items] == [1, 2, 3, 4]
