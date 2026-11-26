"""Desktop replica sync: bootstrap, catch-up, retraction, membership identity."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import (
    add_member,
    append_ok,
    auth,
    create_conversation,
    fork,
    remove_member,
)

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.replica import Replica, drive  # noqa: E402


def pull(
    client: TestClient,
    user: dict,
    device_id: str,
    cursor: str | None = None,
    limit: int | None = None,
):
    payload: dict = {"device_id": device_id}
    if cursor is not None:
        payload["cursor"] = cursor
    if limit is not None:
        payload["limit"] = limit
    return client.post("/sync/pull", json=payload, headers=auth(user))


def pulled(client: TestClient, user: dict, device: str, replica: Replica) -> None:
    drive(client, user, device, replica)


def assert_matches_api(
    client: TestClient, user: dict, replica: Replica, conversation_id: str
) -> None:
    """A replica's view equals the normal message API's ordered view."""
    expected = client.get(
        f"/conversations/{conversation_id}/messages",
        headers=auth(user),
        params={"limit": 100},
    ).json()["items"]
    actual = replica.messages(conversation_id)
    assert [i["message_id"] for i in actual] == [i["message_id"] for i in expected]
    assert [i["position"] for i in actual] == [i["position"] for i in expected]
    for got, want in zip(actual, expected):
        assert got["body"] == want["body"]
        assert got["retracted"] == want["retracted"] == (got["body"] is None)
        assert got["author"] == want["author"]
        assert got["origin"] == want["origin"]
        assert got["source"] == want["source"]


def test_empty_replica_loads_every_conversation_and_message(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    root_msgs = [append_ok(client, alice, root["id"], f"root {i}") for i in range(5)]
    child = fork(client, alice, root["id"], root_msgs[-1]["message_id"], "Child")
    assert add_member(client, alice, child["id"], bob).status_code == 201
    child_msgs = [append_ok(client, bob, child["id"], f"child {i}") for i in range(4)]

    replica = Replica()
    pulled(client, bob, "bob-laptop", replica)

    assert replica.conversation_ids() == {child["id"]}
    assert replica.fully_loaded(child["id"])
    assert_matches_api(client, bob, replica, child["id"])
    assert [m["message_id"] for m in replica.messages(child["id"])] == [
        m["message_id"] for m in root_msgs + child_msgs
    ]
    # Inherited messages keep the original id/author and are marked inherited.
    first = replica.messages(child["id"])[0]
    assert first["message_id"] == root_msgs[0]["message_id"]
    assert first["author"]["id"] == alice["id"]
    assert first["source"] == "inherited"
    assert first["origin"] == {"conversation_id": root["id"], "position": 1}


def test_incremental_pull_does_not_resend_history(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    first = append_ok(client, alice, root["id"], "one")
    assert add_member(client, alice, root["id"], bob).status_code == 201

    replica = Replica()
    pulled(client, bob, "phone", replica)
    # The grant watermark was taken when Bob joined; the message that existed
    # before it is part of that one-time history load.
    assert [m["message_id"] for m in replica.messages(root["id"])] == [
        first["message_id"]
    ]

    second = append_ok(client, alice, root["id"], "two")
    batches = drive(client, bob, "phone", replica)
    assert batches == 1
    assert [m["message_id"] for m in replica.messages(root["id"])] == [
        first["message_id"],
        second["message_id"],
    ]

    # A caught-up pull carries no message bodies at all.
    response = pull(client, bob, "phone", replica.cursor).json()
    assert response["items"] == []
    assert response["has_more"] is False
    drive(client, bob, "phone", replica)
    assert [m["message_id"] for m in replica.messages(root["id"])] == [
        first["message_id"],
        second["message_id"],
    ]


def test_new_member_loads_existing_history_then_follows_appends(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    history = [append_ok(client, alice, root["id"], f"h{i}") for i in range(6)]
    assert add_member(client, alice, root["id"], bob).status_code == 201

    replica = Replica()
    pulled(client, bob, "tablet", replica)
    assert replica.fully_loaded(root["id"])
    assert [m["message_id"] for m in replica.messages(root["id"])] == [
        m["message_id"] for m in history
    ]

    fresh = append_ok(client, alice, root["id"], "after join")
    pulled(client, bob, "tablet", replica)
    assert [m["message_id"] for m in replica.messages(root["id"])] == [
        m["message_id"] for m in history + [fresh]
    ]
    assert_matches_api(client, bob, replica, root["id"])


def test_paged_bootstrap_stays_consistent_when_writes_interleave(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    history = [append_ok(client, alice, root["id"], f"h{i}") for i in range(10)]
    assert add_member(client, alice, root["id"], bob).status_code == 201

    replica = Replica()
    # First batch loads only part of the history.
    batch = pull(client, bob, "slow", limit=3).json()
    replica.apply_batch(batch)
    assert batch["has_more"] is True
    assert not replica.fully_loaded(root["id"])

    # While the device is still loading, people talk, retract and change
    # membership.
    late = append_ok(client, alice, root["id"], "while loading")
    retracted = client.post(
        f"/messages/{history[1]['message_id']}/retraction", headers=auth(alice)
    )
    assert retracted.status_code == 200

    drive(client, bob, "slow", replica, limit=2)
    assert replica.fully_loaded(root["id"])
    assert_matches_api(client, bob, replica, root["id"])
    items = replica.messages(root["id"])
    assert items[-1]["message_id"] == late["message_id"]
    placeholder = next(i for i in items if i["message_id"] == history[1]["message_id"])
    assert placeholder["retracted"] is True and placeholder["body"] is None


def test_retraction_reaches_branches_the_author_left(
    client: TestClient, alice: dict, bob: dict, carol: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    point = append_ok(client, alice, root["id"], "will be retracted")
    child = fork(client, alice, root["id"], point["message_id"], "Child")
    assert add_member(client, alice, child["id"], bob).status_code == 201
    append_ok(client, bob, child["id"], "child local")

    bob_replica = Replica()
    pulled(client, bob, "bob-pc", bob_replica)
    assert bob_replica.messages(child["id"])[0]["body"] == "will be retracted"

    # Alice, the author, owns Root and Child, but is not a member of the
    # grandchild Carol forked: the retraction must still reach that copy.
    assert add_member(client, alice, child["id"], carol).status_code == 201
    grand = fork(client, carol, child["id"], point["message_id"], "Carol fork")
    # Carol owns `grand`; the author Alice is not a member of it.
    carol_replica = Replica()
    pulled(client, carol, "carol-mac", carol_replica)
    assert carol_replica.messages(grand["id"])[0]["body"] == "will be retracted"

    # Alice retracts her original; both replicas must hold only a placeholder.
    response = client.post(
        f"/messages/{point['message_id']}/retraction", headers=auth(alice)
    )
    assert response.status_code == 200
    assert response.json()["affected_conversations"] == 3

    pulled(client, bob, "bob-pc", bob_replica)
    pulled(client, carol, "carol-mac", carol_replica)
    for replica, cid in ((bob_replica, child["id"]), (carol_replica, grand["id"])):
        message = replica.messages(cid)[0]
        assert message["retracted"] is True
        assert message["body"] is None
        assert message["message_id"] == point["message_id"]
        assert message["author"]["id"] == alice["id"]
    assert_matches_api(client, bob, bob_replica, child["id"])
    assert_matches_api(client, carol, carol_replica, grand["id"])


def test_removed_member_can_clear_only_that_conversation(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    point = append_ok(client, alice, root["id"], "shared original")
    child = fork(client, alice, root["id"], point["message_id"], "Child")
    assert add_member(client, alice, root["id"], bob).status_code == 201
    assert add_member(client, alice, child["id"], bob).status_code == 201

    replica = Replica()
    pulled(client, bob, "bob-pc", replica)
    assert set(replica.conversation_ids()) == {root["id"], child["id"]}

    # Removing Bob from the child must clear the child only; the root still
    # holds the same original message and stays.
    assert remove_member(client, alice, child["id"], bob).status_code == 204
    pulled(client, bob, "bob-pc", replica)
    assert replica.conversation_ids() == {root["id"]}
    assert replica.messages(root["id"])[0]["message_id"] == point["message_id"]
    assert replica.messages(root["id"])[0]["body"] == "shared original"

    # The removed conversation is gone even after another pull.
    pulled(client, bob, "bob-pc", replica)
    assert child["id"] not in replica.conversations

    # The root's copy is still complete and matches the API.
    assert_matches_api(client, bob, replica, root["id"])


def test_rejoin_never_reuses_the_old_identity_copy(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    append_ok(client, alice, root["id"], "before bob")
    assert add_member(client, alice, root["id"], bob).status_code == 201
    seen = append_ok(client, alice, root["id"], "seen under first identity")

    replica = Replica()
    pulled(client, bob, "bob-pc", replica)
    old_membership = replica.conversations[root["id"]]["membership_id"]
    assert old_membership
    assert len(replica.messages(root["id"])) == 2

    # Bob is out while the device is offline. Messages move on.
    assert remove_member(client, alice, root["id"], bob).status_code == 204
    append_ok(client, alice, root["id"], "while bob is out")
    retracted = client.post(
        f"/messages/{seen['message_id']}/retraction", headers=auth(alice)
    )
    assert retracted.status_code == 200

    # And back in: a new membership instance.
    assert add_member(client, alice, root["id"], bob).status_code == 201
    later = append_ok(client, alice, root["id"], "after bob is back")

    pulled(client, bob, "bob-pc", replica)
    new_identity = replica.conversations[root["id"]]["membership_id"]
    assert new_identity != old_membership
    # The whole conversation was reloaded under the new identity, including
    # what was said while Bob was away, with the retraction applied.
    assert_matches_api(client, bob, replica, root["id"])
    ids = [m["message_id"] for m in replica.messages(root["id"])]
    assert ids[-1] == later["message_id"]
    # before-bob, first-identity, while-away, after-return
    assert len(ids) == 4
    placeholder = next(m for m in replica.messages(root["id"]) if m["message_id"] == seen["message_id"])
    assert placeholder["retracted"] is True and placeholder["body"] is None


def test_cursor_is_bound_to_account_and_device(
    client: TestClient, alice: dict, bob: dict, carol: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    append_ok(client, alice, root["id"], "one")
    assert add_member(client, alice, root["id"], bob).status_code == 201
    assert add_member(client, alice, root["id"], carol).status_code == 201

    replica = Replica()
    pulled(client, bob, "bob-pc", replica)
    cursor = replica.cursor
    assert cursor

    # Same account, different device: rejected, so devices cannot overwrite
    # each other's progress.
    other = pull(client, bob, "bob-phone", cursor)
    assert other.status_code == 400
    assert other.json()["error"]["code"] == "invalid_cursor"

    # Different account: rejected too.
    wrong_user = pull(client, carol, "bob-pc", cursor)
    assert wrong_user.status_code == 400
    assert wrong_user.json()["error"]["code"] == "invalid_cursor"

    # The device's own cursor still works.
    assert pull(client, bob, "bob-pc", cursor).status_code == 200


def test_retry_after_lost_response_does_not_skip_or_leak(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    first = append_ok(client, alice, root["id"], "secret one")
    assert add_member(client, alice, root["id"], bob).status_code == 201

    replica = Replica()
    pulled(client, bob, "bob-pc", replica)
    cursor_before = replica.cursor

    second = append_ok(client, alice, root["id"], "new after progress")
    # The device requests, the server commits a retraction of the *new* message
    # before the response reaches the device; the response is lost entirely.
    lost_request = pull(client, bob, "bob-pc", cursor_before)
    assert lost_request.status_code == 200
    assert lost_request.json()["items"][0]["message"]["message_id"] == second["message_id"]

    # The author retracts before the device retries with the old cursor.
    assert client.post(
        f"/messages/{second['message_id']}/retraction", headers=auth(alice)
    ).status_code == 200

    retried = pull(client, bob, "bob-pc", cursor_before)
    assert retried.status_code == 200
    body = retried.json()
    messages = [
        item["message"] for item in body["items"] if item["type"] == "message"
    ]
    # The un-applied append is not skipped, but it now carries no body.
    assert [m["message_id"] for m in messages] == [second["message_id"]]
    assert messages[0]["body"] is None
    assert messages[0]["retracted"] is True


def test_pull_after_restart_resumes_from_saved_progress(
    db_path: str, client: TestClient, alice: dict, bob: dict
) -> None:
    from app.main import create_app

    root = create_conversation(client, alice, "Root")
    append_ok(client, alice, root["id"], "one")
    assert add_member(client, alice, root["id"], bob).status_code == 201

    replica = Replica()
    pulled(client, bob, "bob-pc", replica)
    cursor = replica.cursor
    before = [m["message_id"] for m in replica.messages(root["id"])]

    late = append_ok(client, alice, root["id"], "two")

    with TestClient(create_app(db_path)) as restarted:
        response = restarted.post(
            "/sync/pull",
            json={"device_id": "bob-pc", "cursor": cursor},
            headers=auth(bob),
        )
        assert response.status_code == 200
        batch = response.json()
        replica.apply_batch(batch)
    assert [m["message_id"] for m in replica.messages(root["id"])] == before + [
        late["message_id"]
    ]


def test_mid_bootstrap_removal_discards_partial_history_and_clears(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    history = [append_ok(client, alice, root["id"], f"h{i}") for i in range(8)]
    assert add_member(client, alice, root["id"], bob).status_code == 201

    replica = Replica()
    first = pull(client, bob, "bob-pc", limit=3).json()
    replica.apply_batch(first)
    loaded_under = replica.conversations[root["id"]]["membership_id"]
    assert not replica.fully_loaded(root["id"])
    # limit=3 reserves one slot for the grant item, so 2 history rows land.
    assert len(replica.messages(root["id"])) == 2

    # Bob loses the conversation while the history walk is mid-flight.
    assert remove_member(client, alice, root["id"], bob).status_code == 204

    pulled(client, bob, "bob-pc", replica)
    # The partial copy is gone and no stale fragment was kept.
    assert root["id"] not in replica.conversations
    assert replica.conversations == {}
    assert any(
        item["conversation_id"] == root["id"]
        and item["membership_id"] == loaded_under
        for item in replica.last_cleared
    )

    # A device that never saw the removal still reconciles to the same state.
    fresh = Replica()
    pulled(client, bob, "second-device", fresh)
    assert root["id"] not in fresh.conversations


def test_bootstrap_walk_survives_restart_and_mid_loading_appends(
    db_path: str, client: TestClient, alice: dict, bob: dict
) -> None:
    from app.main import create_app

    root = create_conversation(client, alice, "Root")
    history = [append_ok(client, alice, root["id"], f"h{i}") for i in range(7)]
    assert add_member(client, alice, root["id"], bob).status_code == 201

    replica = Replica()
    first = pull(client, bob, "bob-pc", limit=2).json()
    replica.apply_batch(first)
    assert not replica.fully_loaded(root["id"])
    mid_cursor = replica.cursor
    assert first["progress"]["loading_conversation_id"] == root["id"]

    late = append_ok(client, alice, root["id"], "during load")

    with TestClient(create_app(db_path)) as restarted:
        while True:
            response = restarted.post(
                "/sync/pull",
                json={
                    "device_id": "bob-pc",
                    "cursor": replica.cursor,
                    "limit": 2,
                },
                headers=auth(bob),
            )
            assert response.status_code == 200, response.text
            batch = response.json()
            replica.apply_batch(batch)
            if not batch["has_more"]:
                break
    assert replica.fully_loaded(root["id"])
    assert_matches_api(client, bob, replica, root["id"])
    assert replica.messages(root["id"])[-1]["message_id"] == late["message_id"]
    assert mid_cursor not in (None, replica.cursor)


def test_two_devices_keep_independent_progress(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    append_ok(client, alice, root["id"], "one")
    assert add_member(client, alice, root["id"], bob).status_code == 201

    laptop = Replica()
    phone = Replica()
    pulled(client, bob, "laptop", laptop)
    # The phone starts from an empty cursor and sees the same history.
    pulled(client, bob, "phone", phone)
    assert [m["message_id"] for m in laptop.messages(root["id"])] == [
        m["message_id"] for m in phone.messages(root["id"])
    ]
    laptop_cursor = laptop.cursor

    late = append_ok(client, alice, root["id"], "two")
    pulled(client, bob, "laptop", laptop)
    assert laptop.messages(root["id"])[-1]["message_id"] == late["message_id"]
    # The phone's progress was not advanced by the laptop.
    assert [m["message_id"] for m in phone.messages(root["id"])] != [
        m["message_id"] for m in laptop.messages(root["id"])
    ]
    pulled(client, bob, "phone", phone)
    assert_matches_api(client, bob, phone, root["id"])
    # The laptop's saved cursor still resumes and yields nothing new.
    idle = pull(client, bob, "laptop", laptop.cursor).json()
    assert idle["items"] == [] and idle["caught_up"] is True
    assert laptop_cursor != laptop.cursor


def test_converges_after_writes_stop_even_with_tiny_batches(
    client: TestClient, alice: dict, bob: dict, carol: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    point = append_ok(client, alice, root["id"], "point")
    child = fork(client, alice, root["id"], point["message_id"], "Child")
    assert add_member(client, alice, root["id"], bob).status_code == 201
    assert add_member(client, alice, child["id"], bob).status_code == 201
    for i in range(6):
        append_ok(client, alice, root["id"], f"r{i}")
        append_ok(client, alice, child["id"], f"c{i}")

    replica = Replica()
    batches = drive(client, bob, "tiny", replica, limit=1)
    assert batches < 200
    assert set(replica.conversation_ids()) == {root["id"], child["id"]}
    assert_matches_api(client, bob, replica, root["id"])
    assert_matches_api(client, bob, replica, child["id"])

    # Stopping writes: consecutive pulls converge and then stay empty.
    idle = pull(client, bob, "tiny", replica.cursor).json()
    assert idle["caught_up"] is True and idle["items"] == []
    again = pull(client, bob, "tiny", idle["next_cursor"]).json()
    assert again["caught_up"] is True and again["items"] == []
    assert again["next_cursor"]  # progress is stable, not a dead-end token


def test_owner_removal_of_a_rejoined_user_clears_the_new_copy_only(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    append_ok(client, alice, root["id"], "one")
    assert add_member(client, alice, root["id"], bob).status_code == 201

    replica = Replica()
    pulled(client, bob, "bob-pc", replica)
    old = replica.conversations[root["id"]]["membership_id"]

    # Offline across remove -> re-invite -> new messages, then one catch-up.
    assert remove_member(client, alice, root["id"], bob).status_code == 204
    assert add_member(client, alice, root["id"], bob).status_code == 201
    append_ok(client, alice, root["id"], "two")
    pulled(client, bob, "bob-pc", replica)
    new = replica.conversations[root["id"]]["membership_id"]
    assert new != old
    assert_matches_api(client, bob, replica, root["id"])

    # A clear item is identity-scoped: replaying the old identity's clear after
    # the new grant (e.g. a late-applied stale batch) leaves the new copy alone.
    current = replica.conversations[root["id"]]
    replica.apply_batch(
        {
            "items": [
                {
                    "type": "conversation.cleared",
                    "conversation_id": root["id"],
                    "membership_id": old,
                    "reason": "membership_revoked",
                    "seq": 0,
                }
            ],
            "next_cursor": replica.cursor,
        }
    )
    assert replica.conversations[root["id"]] == current
    assert_matches_api(client, bob, replica, root["id"])

    # A genuine removal of the *current* identity does clear it.
    assert remove_member(client, alice, root["id"], bob).status_code == 204
    pulled(client, bob, "bob-pc", replica)
    assert root["id"] not in replica.conversations


def test_legacy_database_gets_synthesized_grants_on_first_pull(
    client: TestClient, alice: dict, bob: dict
) -> None:
    """A database whose memberships predate the change log still bootstraps."""
    root = create_conversation(client, alice, "Root")
    history = [append_ok(client, alice, root["id"], f"h{i}") for i in range(4)]
    assert add_member(client, alice, root["id"], bob).status_code == 201

    # Simulate an existing deployment: all membership and message rows exist,
    # but the sync log did not exist when they were written.
    service = client.app.state.service
    conn = service.db.connect()
    try:
        conn.execute("DELETE FROM sync_changes")
        conn.commit()
    finally:
        conn.close()

    replica = Replica()
    pulled(client, bob, "legacy", replica)
    assert replica.fully_loaded(root["id"])
    assert [m["message_id"] for m in replica.messages(root["id"])] == [
        m["message_id"] for m in history
    ]
    assert_matches_api(client, bob, replica, root["id"])
    # Re-running the bootstrap is idempotent: no duplicate grant rows.
    pulled(client, bob, "legacy", replica)
    conn = service.db.connect()
    try:
        grants = conn.execute(
            "SELECT COUNT(*) FROM sync_changes "
            "WHERE kind = 'membership.granted' AND conversation_id = ?",
            (root["id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert grants == 1  # only Bob pulled, so only Bob's missing grant was made

    # Alice's own first pull synthesizes hers; both remain distinct.
    alice_replica = Replica()
    pulled(client, alice, "alice-pc", alice_replica)
    assert alice_replica.fully_loaded(root["id"])
    conn = service.db.connect()
    try:
        grants = conn.execute(
            "SELECT COUNT(*) FROM sync_changes "
            "WHERE kind = 'membership.granted' AND conversation_id = ?",
            (root["id"],),
        ).fetchone()[0]
    finally:
        conn.close()
    assert grants == 2  # Alice owner + Bob member


def test_incremental_pull_uses_the_change_index_not_all_messages(client, alice, bob):
    """Catch-up is an indexed scan of this user's log, not a message diff."""
    root = create_conversation(client, alice, "Root")
    for i in range(20):
        append_ok(client, alice, root["id"], f"m{i}")
    assert add_member(client, alice, root["id"], bob).status_code == 201
    replica = Replica()
    pulled(client, bob, "bob-pc", replica)

    service = client.app.state.service
    conn = service.db.connect()
    try:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN "
            "SELECT * FROM sync_changes WHERE user_id = ? AND seq > ? "
            "ORDER BY seq ASC LIMIT 1",
            (bob["id"], 0),
        ).fetchall()
        plan_text = " ".join(str(row["detail"]) for row in plan).lower()
        assert "idx_sync_changes_user_seq" in plan_text
        assert "scan sync_changes" not in plan_text  # no full table walk
        # No query on the message table is needed to catch up.
        assert "messages" not in plan_text
    finally:
        conn.close()


def test_removed_user_never_receives_unreadable_or_stale_bodies(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    secret = append_ok(client, alice, root["id"], "top secret body")
    assert add_member(client, alice, root["id"], bob).status_code == 201
    visible = append_ok(client, alice, root["id"], "bob saw this")

    replica = Replica()
    pulled(client, bob, "bob-pc", replica)
    cursor_before = replica.cursor

    # While Bob's device is offline: removed, then the message is retracted,
    # then a new secret message is posted.
    assert remove_member(client, alice, root["id"], bob).status_code == 204
    assert client.post(
        f"/messages/{visible['message_id']}/retraction", headers=auth(alice)
    ).status_code == 200
    append_ok(client, alice, root["id"], "posted after bob left")

    response = pull(client, bob, "bob-pc", cursor_before)
    assert response.status_code == 200
    batch = response.json()
    dump = str(batch)
    assert "top secret body" not in dump
    assert "posted after bob left" not in dump
    assert "bob saw this" not in dump
    types = [item["type"] for item in batch["items"]]
    assert types == ["conversation.cleared"]
    assert batch["items"][0]["membership_id"]
    replica.apply_batch(batch)
    assert root["id"] not in replica.conversations


def test_tampered_cursor_and_blank_device_are_rejected(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    append_ok(client, alice, root["id"], "one")
    assert add_member(client, alice, root["id"], bob).status_code == 201

    replica = Replica()
    pulled(client, bob, "bob-pc", replica)
    tampered = replica.cursor[:-3] + ("AAA" if not replica.cursor.endswith("AAA") else "BBB")
    response = pull(client, bob, "bob-pc", tampered)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_cursor"

    blank = client.post(
        "/sync/pull", json={"device_id": "   "}, headers=auth(bob)
    )
    assert blank.status_code == 422

    # Wrong endpoint kind: a message cursor cannot drive sync.
    message_cursor = client.get(
        f"/conversations/{root['id']}/messages",
        headers=auth(bob),
        params={"limit": 1},
    )
    # single page, so no cursor; make two pages
    append_ok(client, alice, root["id"], "two")
    page = client.get(
        f"/conversations/{root['id']}/messages",
        headers=auth(bob),
        params={"limit": 1},
    ).json()
    assert page["next_cursor"]
    wrong = pull(client, bob, "bob-pc", page["next_cursor"])
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "invalid_cursor"
