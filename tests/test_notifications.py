"""Offline notifications: follows, mentions, leased claims, confirmation.

Service-level tests are deterministic (one ``ConversationService`` per test,
real SQLite file); HTTP-level tests exercise the routes end to end. The claim
lease is injected via ``claim_lease_seconds`` so timeout scenarios run fast.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.errors import ApiError
from app.service import ConversationService

from conftest import auth

LEASE = 0.25  # seconds; short enough for expiry tests, long enough to use


@pytest.fixture()
def service(tmp_path: Path) -> ConversationService:
    return ConversationService(
        str(tmp_path / "notify.sqlite3"), claim_lease_seconds=LEASE
    )


def user(service: ConversationService, name: str) -> str:
    return service.create_user(name)["id"]


def conversation_with(
    service: ConversationService, owner: str, *members: str
) -> str:
    cid = service.create_conversation(owner, "Inbox source")["id"]
    for member in members:
        service.add_member(cid, owner, member)
    return cid


def claim_ids(
    service: ConversationService, user_id: str, request_id: str, limit: int = 50
) -> list[str]:
    batch = service.claim_notifications(user_id, request_id, limit)
    return [item["notification_id"] for item in batch["items"]]


def api_error(exc_type, fn):
    with pytest.raises(exc_type) as caught:
        fn()
    return caught.value


# ---------------------------------------------------------------------------
# Creation rules
# ---------------------------------------------------------------------------


def test_followers_and_mentioned_get_one_notification_each(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")  # follows AND is mentioned -> one notification
    carol = user(service, "Carol")  # follows only
    dave = user(service, "Dave")  # mentioned only
    erin = user(service, "Erin")  # plain member: nothing
    cid = conversation_with(service, alice, bob, carol, dave, erin)
    service.follow_conversation(cid, bob)
    service.follow_conversation(cid, carol)
    service.follow_conversation(cid, alice)  # author follows: still nothing

    message, created = service.append_message(
        cid, alice, "hello all", None, [dave, bob, alice]
    )
    assert created is True

    bob_batch = service.claim_notifications(bob, "b1", 10)
    assert len(bob_batch["items"]) == 1
    item = bob_batch["items"][0]
    assert item["conversation_id"] == cid
    assert item["message"]["message_id"] == message["message_id"]
    assert item["message"]["body"] == "hello all"
    assert item["reasons"] == ["follow", "mention"]

    carol_items = service.claim_notifications(carol, "c1", 10)["items"]
    assert [i["reasons"] for i in carol_items] == [["follow"]]
    dave_items = service.claim_notifications(dave, "d1", 10)["items"]
    assert [i["reasons"] for i in dave_items] == [["mention"]]
    # The author and the plain member have empty inboxes.
    assert service.claim_notifications(alice, "a1", 10)["items"] == []
    assert service.claim_notifications(erin, "e1", 10)["items"] == []


def test_mention_requires_current_membership(service: ConversationService) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    outsider = user(service, "Outsider")
    cid = conversation_with(service, alice, bob)

    exc = api_error(
        ApiError,
        lambda: service.append_message(cid, alice, "ping", None, [outsider]),
    )
    assert exc.status_code == 400
    assert exc.code == "mention_not_member"
    # The whole append was rejected: no message, no notification.
    assert service.list_messages(cid, alice, None, None)["items"] == []

    # A removed member can no longer be mentioned either.
    service.remove_member(cid, alice, bob)
    exc = api_error(
        ApiError, lambda: service.append_message(cid, alice, "ping", None, [bob])
    )
    assert exc.code == "mention_not_member"


def test_append_retry_creates_no_notifications_and_keeps_recipients(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    carol = user(service, "Carol")
    cid = conversation_with(service, alice, bob, carol)
    service.follow_conversation(cid, bob)

    first, created = service.append_message(cid, alice, "once", "tok-1", [carol])
    assert created is True
    # Retry with the same token but a *different* mention list: the original
    # result comes back and the recipient set is untouched.
    again, created = service.append_message(cid, alice, "once", "tok-1", [bob])
    assert created is False
    assert again["message_id"] == first["message_id"]

    assert len(service.claim_notifications(carol, "c1", 10)["items"]) == 1
    assert len(service.claim_notifications(bob, "b1", 10)["items"]) == 1
    # Bob's single notification comes from following, not from the retried
    # mention list.
    assert service.claim_notifications(bob, "b1", 10)["items"][0]["reasons"] == [
        "follow"
    ]


def test_no_backfill_from_join_follow_or_fork(service: ConversationService) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    carol = user(service, "Carol")
    cid = conversation_with(service, alice)
    service.append_message(cid, alice, "before anyone watched", None)

    # Joining later does not backfill...
    service.add_member(cid, alice, bob)
    assert service.claim_notifications(bob, "b1", 10)["items"] == []
    # ...nor does starting to follow.
    service.follow_conversation(cid, bob)
    assert service.claim_notifications(bob, "b2", 10)["items"] == []

    # Forking inherits the message reference but creates no notifications.
    message = service.append_message(cid, alice, "fork me", None)[0]
    child = service.fork_conversation(cid, alice, message["message_id"], "Child")
    service.add_member(child["id"], alice, carol)
    assert service.claim_notifications(carol, "k1", 10)["items"] == []

    # Bob follows the parent, so the "fork me" append itself notified him once;
    # the fork added nothing. Drain it, then only genuinely new appends notify.
    assert [
        i["message"]["body"] for i in service.claim_notifications(bob, "b3", 10)["items"]
    ] == ["fork me"]
    service.append_message(cid, alice, "fresh", None)
    assert [
        i["message"]["body"] for i in service.claim_notifications(bob, "b4", 10)["items"]
    ] == ["fresh"]


# ---------------------------------------------------------------------------
# Reason lifecycles: the dual-source invalidation boundary
# ---------------------------------------------------------------------------


def test_unfollow_kills_only_the_follow_reason(service: ConversationService) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    service.append_message(cid, alice, "dual reasons", None, [bob])

    # Unfollowing kills the follow reason; the mention reason survives, so the
    # notification is still claimable and confirmable.
    service.unfollow_conversation(cid, bob)
    batch = service.claim_notifications(bob, "b1", 10)
    assert len(batch["items"]) == 1
    assert batch["items"][0]["reasons"] == ["mention"]
    confirmed = service.confirm_notification(
        bob, batch["batch_id"], batch["items"][0]["notification_id"]
    )
    assert confirmed["confirmed"] is True


def test_refollow_does_not_restore_the_old_follow_reason(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    service.append_message(cid, alice, "follow only", None)

    service.unfollow_conversation(cid, bob)
    assert service.claim_notifications(bob, "b1", 10)["items"] == []
    # Re-following starts a new subscription; it never revives the dead reason.
    service.follow_conversation(cid, bob)
    assert service.claim_notifications(bob, "b2", 10)["items"] == []
    # ...but genuinely new messages notify again.
    service.append_message(cid, alice, "after refollow", None)
    assert len(service.claim_notifications(bob, "b3", 10)["items"]) == 1


def test_member_removal_kills_every_reason_and_rejoin_restores_nothing(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    service.append_message(cid, alice, "dual reasons", None, [bob])
    service.append_message(cid, alice, "mention only", None, [bob])

    # Claim first: removal must invalidate even an already-claimed batch.
    batch = service.claim_notifications(bob, "b1", 10)
    assert len(batch["items"]) == 2
    service.remove_member(cid, alice, bob)

    # The claim replay no longer returns the bodies...
    assert service.claim_notifications(bob, "b1", 10)["items"] == []
    # ...and neither item can be confirmed any more.
    for item in batch["items"]:
        exc = api_error(
            ApiError,
            lambda i=item: service.confirm_notification(
                bob, batch["batch_id"], i["notification_id"]
            ),
        )
        assert exc.status_code == 409
        assert exc.code == "notification_inactive"

    # Rejoining (and even re-following) restores nothing.
    service.add_member(cid, alice, bob)
    service.follow_conversation(cid, bob)
    assert service.claim_notifications(bob, "b2", 10)["items"] == []


def test_removal_then_rejoin_then_new_message_notifies_normally(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    service.append_message(cid, alice, "old", None)
    service.remove_member(cid, alice, bob)
    service.add_member(cid, alice, bob)
    service.follow_conversation(cid, bob)

    service.append_message(cid, alice, "new", None)
    batch = service.claim_notifications(bob, "b1", 10)
    assert [item["message"]["body"] for item in batch["items"]] == ["new"]


def test_retraction_invalidates_notifications(service: ConversationService) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    message = service.append_message(cid, alice, "retract me", None)[0]

    batch = service.claim_notifications(bob, "b1", 10)
    assert len(batch["items"]) == 1
    service.retract_message(message["message_id"], alice)

    # Claimed but unconfirmed: the body can no longer be returned or confirmed.
    assert service.claim_notifications(bob, "b1", 10)["items"] == []
    exc = api_error(
        ApiError,
        lambda: service.confirm_notification(
            bob, batch["batch_id"], batch["items"][0]["notification_id"]
        ),
    )
    assert exc.code == "notification_inactive"
    # Fresh claims skip it too.
    assert service.claim_notifications(bob, "b2", 10)["items"] == []


# ---------------------------------------------------------------------------
# Claim batches: replay, filtering, no renewal
# ---------------------------------------------------------------------------


def test_claim_replay_returns_filtered_original_batch_only(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    messages = [
        service.append_message(cid, alice, f"m{i}", None)[0] for i in range(3)
    ]

    batch = service.claim_notifications(bob, "req-1", 2)
    assert len(batch["items"]) == 2
    first, second = batch["items"]
    # Ordered by original submission.
    assert first["message"]["body"] == "m0"
    assert second["message"]["body"] == "m1"

    service.confirm_notification(bob, batch["batch_id"], first["notification_id"])
    service.retract_message(messages[1]["message_id"], alice)
    service.append_message(cid, alice, "m3", None)  # must NOT join the replay

    replay = service.claim_notifications(bob, "req-1", 2)
    assert replay["batch_id"] == batch["batch_id"]
    assert replay["expires_at"] == batch["expires_at"]  # no renewal
    assert replay["items"] == []  # confirmed and retracted are both filtered

    # The third message was never part of req-1; a new request claims it.
    fresh = service.claim_notifications(bob, "req-2", 10)
    assert [i["message"]["body"] for i in fresh["items"]] == ["m2", "m3"]


def test_empty_batch_replays_empty(service: ConversationService) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)

    empty = service.claim_notifications(bob, "req-empty", 5)
    assert empty["items"] == []
    service.append_message(cid, alice, "later", None)
    # Replaying the empty request does not pick up the new notification.
    assert service.claim_notifications(bob, "req-empty", 5)["items"] == []
    assert len(service.claim_notifications(bob, "req-new", 5)["items"]) == 1


def test_confirm_happy_path_and_idempotent_replay_without_body(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    service.append_message(cid, alice, "confirm me", None)

    batch = service.claim_notifications(bob, "req-1", 5)
    notification_id = batch["items"][0]["notification_id"]
    first = service.confirm_notification(bob, batch["batch_id"], notification_id)
    assert first["already_confirmed"] is False
    assert first["message"]["message"]["body"] == "confirm me"

    # A retried confirmation changes nothing and returns no body.
    replay = service.confirm_notification(bob, batch["batch_id"], notification_id)
    assert replay["already_confirmed"] is True
    assert replay["message"] is None
    assert replay["confirmed_at"] == first["confirmed_at"]

    # Confirmed notifications never come back in new claims.
    assert service.claim_notifications(bob, "req-2", 5)["items"] == []


def test_confirm_rejects_foreign_and_unknown_batches(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    service.append_message(cid, alice, "hi", None)
    batch = service.claim_notifications(bob, "req-1", 5)
    notification_id = batch["items"][0]["notification_id"]

    exc = api_error(
        ApiError,
        lambda: service.confirm_notification(alice, batch["batch_id"], notification_id),
    )
    assert exc.status_code == 403
    assert exc.code == "not_batch_owner"

    exc = api_error(
        ApiError, lambda: service.confirm_notification(bob, "clm_nope", notification_id)
    )
    assert exc.status_code == 404
    assert exc.code == "batch_not_found"

    exc = api_error(
        ApiError,
        lambda: service.confirm_notification(bob, batch["batch_id"], "ntf_nope"),
    )
    assert exc.status_code == 404
    assert exc.code == "notification_not_in_batch"


# ---------------------------------------------------------------------------
# Two devices racing for the same inbox
# ---------------------------------------------------------------------------


def run_concurrently(count: int, fn) -> list:
    barrier = threading.Barrier(count)

    def wrapped(index: int):
        barrier.wait()
        return fn(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        return list(pool.map(wrapped, range(count)))


def test_concurrent_devices_partition_the_inbox(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    for i in range(10):
        service.append_message(cid, alice, f"m{i}", None)

    # Two phones and a tablet claim at the same time with distinct requests.
    batches = run_concurrently(
        3, lambda i: service.claim_notifications(bob, f"device-{i}", 4)
    )
    claimed = [
        item["notification_id"] for batch in batches for item in batch["items"]
    ]
    # Every notification went to exactly one batch; limits were respected.
    assert len(claimed) == len(set(claimed))
    assert all(len(batch["items"]) <= 4 for batch in batches)

    # Whatever is left can be picked up by a later claim.
    rest = service.claim_notifications(bob, "device-late", 50)["items"]
    assert len(claimed) + len(rest) == 10
    assert not {i["notification_id"] for i in rest} & set(claimed)


def test_concurrent_replays_of_one_request_share_one_batch(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    for i in range(3):
        service.append_message(cid, alice, f"m{i}", None)

    batches = run_concurrently(
        6, lambda _i: service.claim_notifications(bob, "same-request", 2)
    )
    assert {batch["batch_id"] for batch in batches} == {batches[0]["batch_id"]}
    for batch in batches:
        assert [
            item["notification_id"] for item in batch["items"]
        ] == [item["notification_id"] for item in batches[0]["items"]]


def test_claim_and_invalidation_race_is_serializable(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    service.append_message(cid, alice, "race me", None, [bob])

    results = run_concurrently(
        2,
        lambda i: (
            service.claim_notifications(bob, "race", 5)
            if i == 0
            else service.remove_member(cid, alice, bob)
        ),
    )
    batch = next(r for r in results if isinstance(r, dict))
    # Whatever the commit order was, the outcome matches it: either the claim
    # committed first (item leased, but confirmation now fails because the
    # reasons are gone) or the removal committed first (empty batch).
    if batch["items"]:
        exc = api_error(
            ApiError,
            lambda: service.confirm_notification(
                bob, batch["batch_id"], batch["items"][0]["notification_id"]
            ),
        )
        assert exc.code == "notification_inactive"
    assert service.claim_notifications(bob, "after", 5)["items"] == []


# ---------------------------------------------------------------------------
# Lease expiry and re-claim ("phone went offline, another device takes over")
# ---------------------------------------------------------------------------


def test_expired_lease_allows_reclaim_with_new_credential(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    service.append_message(cid, alice, "handoff", None)

    # Phone claims, then goes offline without confirming.
    phone = service.claim_notifications(bob, "phone", 5)
    notification_id = phone["items"][0]["notification_id"]

    # While the lease is live, the tablet's claim skips the held item.
    held = service.claim_notifications(bob, "tablet", 5)
    assert held["items"] == []

    time.sleep(LEASE + 0.15)

    # After expiry the tablet re-claims with a NEW request id and gets a fresh
    # credential (the expired "tablet" request stays an empty batch forever).
    assert service.claim_notifications(bob, "tablet", 5)["items"] == []
    tablet = service.claim_notifications(bob, "tablet-2", 5)
    assert [i["notification_id"] for i in tablet["items"]] == [notification_id]
    assert tablet["batch_id"] != phone["batch_id"]

    # The phone's stale credential cannot confirm the re-claimed item...
    exc = api_error(
        ApiError,
        lambda: service.confirm_notification(
            bob, phone["batch_id"], notification_id
        ),
    )
    assert exc.status_code == 409
    assert exc.code == "claim_expired"

    # ...and replaying the phone's request no longer returns it.
    assert service.claim_notifications(bob, "phone", 5)["items"] == []

    # The tablet's credential works.
    confirmed = service.confirm_notification(
        bob, tablet["batch_id"], notification_id
    )
    assert confirmed["already_confirmed"] is False
    assert confirmed["message"]["message"]["body"] == "handoff"


def test_expired_replay_returns_items_but_cannot_confirm(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    cid = conversation_with(service, alice, bob)
    service.follow_conversation(cid, bob)
    service.append_message(cid, alice, "stale replay", None)

    batch = service.claim_notifications(bob, "req-1", 5)
    notification_id = batch["items"][0]["notification_id"]
    time.sleep(LEASE + 0.15)

    # Replaying the request still shows the owned item (it was never
    # re-claimed), with the original expiry — but confirmation is refused.
    replay = service.claim_notifications(bob, "req-1", 5)
    assert [i["notification_id"] for i in replay["items"]] == [notification_id]
    assert replay["expires_at"] == batch["expires_at"]
    exc = api_error(
        ApiError,
        lambda: service.confirm_notification(
            bob, batch["batch_id"], notification_id
        ),
    )
    assert exc.code == "claim_expired"


# ---------------------------------------------------------------------------
# Restart: pending notifications, batch ownership and confirmations persist
# ---------------------------------------------------------------------------


def test_notifications_survive_restart(tmp_path: Path) -> None:
    db_path = str(tmp_path / "restart-notify.sqlite3")
    first = ConversationService(db_path, claim_lease_seconds=30.0)

    alice = user(first, "Alice")
    bob = user(first, "Bob")
    cid = conversation_with(first, alice, bob)
    first.follow_conversation(cid, bob)
    first.append_message(cid, alice, "one", None)
    first.append_message(cid, alice, "two", None)
    first.append_message(cid, alice, "three", None)

    batch = first.claim_notifications(bob, "req-1", 2)
    assert [i["message"]["body"] for i in batch["items"]] == ["one", "two"]
    first.confirm_notification(
        bob, batch["batch_id"], batch["items"][0]["notification_id"]
    )

    # --- restart boundary -------------------------------------------------
    second = ConversationService(db_path, claim_lease_seconds=30.0)

    # The confirmed item stays confirmed (idempotent replay, no body)...
    replayed = second.confirm_notification(
        bob, batch["batch_id"], batch["items"][0]["notification_id"]
    )
    assert replayed["already_confirmed"] is True
    assert replayed["message"] is None

    # ...the unconfirmed one is still leased to the same batch...
    replay = second.claim_notifications(bob, "req-1", 2)
    assert replay["batch_id"] == batch["batch_id"]
    assert [i["message"]["body"] for i in replay["items"]] == ["two"]
    # ...so a different request gets only what req-1 does not hold...
    assert [
        i["message"]["body"] for i in second.claim_notifications(bob, "req-2", 10)["items"]
    ] == ["three"]
    # ...and the held item can still be confirmed with the pre-restart credential.
    confirmed = second.confirm_notification(
        bob, batch["batch_id"], batch["items"][1]["notification_id"]
    )
    assert confirmed["message"]["message"]["body"] == "two"

    # --- second restart: nothing lost, nothing resurrected ----------------
    third = ConversationService(db_path, claim_lease_seconds=30.0)
    assert third.claim_notifications(bob, "req-4", 10)["items"] == []
    still_confirmed = third.confirm_notification(
        bob, batch["batch_id"], batch["items"][1]["notification_id"]
    )
    assert still_confirmed["already_confirmed"] is True


def test_lease_expiry_state_survives_restart(tmp_path: Path) -> None:
    db_path = str(tmp_path / "restart-lease.sqlite3")
    first = ConversationService(db_path, claim_lease_seconds=0.3)

    alice = user(first, "Alice")
    bob = user(first, "Bob")
    cid = conversation_with(first, alice, bob)
    first.follow_conversation(cid, bob)
    first.append_message(cid, alice, "offline handoff", None)

    phone = first.claim_notifications(bob, "phone", 5)
    notification_id = phone["items"][0]["notification_id"]

    # Restart while the lease is live: ownership is intact.
    second = ConversationService(db_path, claim_lease_seconds=0.3)
    assert second.claim_notifications(bob, "tablet", 5)["items"] == []

    time.sleep(0.45)

    # After expiry (still "offline"), the tablet takes over post-restart with a
    # new request id.
    tablet = second.claim_notifications(bob, "tablet-2", 5)
    assert [i["notification_id"] for i in tablet["items"]] == [notification_id]
    exc = api_error(
        ApiError,
        lambda: second.confirm_notification(
            bob, phone["batch_id"], notification_id
        ),
    )
    assert exc.code == "claim_expired"
    confirmed = second.confirm_notification(bob, tablet["batch_id"], notification_id)
    assert confirmed["already_confirmed"] is False


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------


def test_http_follow_mention_claim_confirm_flow(client: TestClient) -> None:
    alice = client.post("/users", json={"display_name": "Alice"}).json()
    bob = client.post("/users", json={"display_name": "Bob"}).json()
    conversation = client.post(
        "/conversations", json={"title": "t"}, headers=auth(alice)
    ).json()
    cid = conversation["id"]
    client.post(
        f"/conversations/{cid}/members",
        json={"user_id": bob["id"]},
        headers=auth(alice),
    )

    # Follow: 201 the first time, 200 on repeat, status visible via GET.
    response = client.post(f"/conversations/{cid}/follow", headers=auth(bob))
    assert response.status_code == 201
    assert response.json()["following"] is True
    assert client.post(f"/conversations/{cid}/follow", headers=auth(bob)).status_code == 200
    status = client.get(f"/conversations/{cid}/follow", headers=auth(bob)).json()
    assert status["following"] is True and status["followed_at"] is not None

    # Non-members cannot follow.
    carol = client.post("/users", json={"display_name": "Carol"}).json()
    response = client.post(f"/conversations/{cid}/follow", headers=auth(carol))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_a_member"

    # Mentioning a non-member is rejected with a structured error.
    response = client.post(
        f"/conversations/{cid}/messages",
        json={"body": "hi", "mention_user_ids": [carol["id"]]},
        headers=auth(alice),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "mention_not_member"

    # A normal append notifies the follower.
    message = client.post(
        f"/conversations/{cid}/messages",
        json={"body": "hello bob"},
        headers=auth(alice),
    ).json()

    batch = client.post(
        "/notifications/claim",
        json={"request_id": "dev-1", "limit": 10},
        headers=auth(bob),
    ).json()
    assert len(batch["items"]) == 1
    item = batch["items"][0]
    assert item["message"]["message_id"] == message["message_id"]
    assert item["message"]["body"] == "hello bob"
    assert item["conversation_id"] == cid
    assert item["reasons"] == ["follow"]

    # Replaying the request returns the same batch.
    replay = client.post(
        "/notifications/claim",
        json={"request_id": "dev-1", "limit": 10},
        headers=auth(bob),
    ).json()
    assert replay["batch_id"] == batch["batch_id"]
    assert [i["notification_id"] for i in replay["items"]] == [
        item["notification_id"]
    ]

    # Confirm, then confirm again: idempotent, no body the second time.
    confirmed = client.post(
        "/notifications/confirm",
        json={"batch_id": batch["batch_id"], "notification_id": item["notification_id"]},
        headers=auth(bob),
    ).json()
    assert confirmed["already_confirmed"] is False
    assert confirmed["message"]["message"]["body"] == "hello bob"
    again = client.post(
        "/notifications/confirm",
        json={"batch_id": batch["batch_id"], "notification_id": item["notification_id"]},
        headers=auth(bob),
    ).json()
    assert again["already_confirmed"] is True
    assert again["message"] is None

    # Unfollow is idempotent (204 both times) and flips the status.
    assert client.delete(f"/conversations/{cid}/follow", headers=auth(bob)).status_code == 204
    assert client.delete(f"/conversations/{cid}/follow", headers=auth(bob)).status_code == 204
    assert (
        client.get(f"/conversations/{cid}/follow", headers=auth(bob)).json()[
            "following"
        ]
        is False
    )


def test_http_claim_requires_known_user_and_valid_input(client: TestClient) -> None:
    response = client.post(
        "/notifications/claim",
        json={"request_id": "r"},
        headers={"X-User-Id": "usr_ghost"},
    )
    assert response.status_code == 401
    alice = client.post("/users", json={"display_name": "Alice"}).json()
    response = client.post(
        "/notifications/claim", json={"request_id": "  "}, headers=auth(alice)
    )
    assert response.status_code == 422
    response = client.post(
        "/notifications/claim",
        json={"request_id": "r", "limit": 0},
        headers=auth(alice),
    )
    assert response.status_code == 422
