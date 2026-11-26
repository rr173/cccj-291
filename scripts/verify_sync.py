#!/usr/bin/env python3
"""Reproducible end-to-end check of the desktop replica sync protocol.

Uses the real FastAPI HTTP stack through ``TestClient`` against a temporary
SQLite file. Every claim is checked against the batches actually returned: the
replica in ``scripts/replica.py`` is driven only by those batches, then compared
to the existing message API.

Scenarios:

* an empty replica loads every conversation of the account and each readable
  message, with inherited ids/authors/origins preserved;
* adding a member while a history walk is still paging, plus an append and a
  retraction during that walk, still converges without wiping the replica;
* a lost response is retried from the old progress: nothing is skipped and a
  message retracted in between is delivered as a placeholder only;
* the author retracts a message in a branch they do not belong to and every
  cached copy is corrected;
* a member is removed offline, then re-invited: the old identity's copy is
  cleared, the new identity's copy is rebuilt from scratch, and clearing one
  conversation leaves the same original message visible in another;
* two devices of the same account progress independently, and a cursor is
  rejected for another account or another device;
* stopping writes makes consecutive pulls converge to empty batches;
* the service is restarted and resumes from saved per-device progress.

Run from the repository root:

    python3 scripts/verify_sync.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from replica import Replica  # noqa: E402

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(message)
    CHECKS += 1
    print(f"  ok  {message}")


def request(client, method, path, user=None, body=None):
    headers = {"X-User-Id": user["id"]} if user else {}
    return client.request(method, path, json=body, headers=headers)


def create_user(client, name):
    response = request(client, "POST", "/users", body={"display_name": name})
    assert response.status_code == 201, response.text
    return response.json()


def create_conversation(client, user, title):
    response = request(client, "POST", "/conversations", user, {"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def append(client, user, conversation_id, body):
    response = request(
        client, "POST", f"/conversations/{conversation_id}/messages", user,
        {"body": body},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def add_member(client, actor, conversation_id, user):
    return request(
        client, "POST", f"/conversations/{conversation_id}/members", actor,
        {"user_id": user["id"]},
    )


def remove_member(client, actor, conversation_id, user):
    return request(
        client, "DELETE", f"/conversations/{conversation_id}/members/{user['id']}",
        actor,
    )


def fork(client, user, conversation_id, message_id, title):
    response = request(
        client, "POST", f"/conversations/{conversation_id}/fork", user,
        {"parent_message_id": message_id, "title": title},
    )
    assert response.status_code == 201, response.text
    return response.json()


def pull(client, user, device, cursor=None, limit=None):
    payload: dict[str, Any] = {"device_id": device}
    if cursor is not None:
        payload["cursor"] = cursor
    if limit is not None:
        payload["limit"] = limit
    return request(client, "POST", "/sync/pull", user, payload)


def catch_up(client, user, device, replica, limit=None):
    """Pull batches into the replica until the server says caught up."""
    batches = 0
    while True:
        response = pull(client, user, device, replica.cursor, limit)
        assert response.status_code == 200, response.text
        batch = response.json()
        replica.apply_batch(batch)
        batches += 1
        if not batch["has_more"]:
            return batches
        assert batches < 500, "sync did not converge"


def api_messages(client, user, conversation_id):
    response = request(
        client, "GET",
        f"/conversations/{conversation_id}/messages?limit=100", user,
    )
    assert response.status_code == 200, response.text
    return response.json()["items"]


def compare_with_api(client, user, replica, conversation_id, label):
    expected = api_messages(client, user, conversation_id)
    actual = replica.messages(conversation_id)
    check(
        [i["message_id"] for i in actual] == [i["message_id"] for i in expected],
        f"{label}: replica holds the same message ids in the same order",
    )
    check(
        [i["position"] for i in actual] == [i["position"] for i in expected],
        f"{label}: positions match the branch order",
    )
    check(
        all(
            a["body"] == b["body"]
            and a["author"] == b["author"]
            and a["origin"] == b["origin"]
            and a["source"] == b["source"]
            and a["retracted"] == b["retracted"]
            for a, b in zip(actual, expected)
        ),
        f"{label}: bodies, authors, origins, source and placeholders match",
    )


def main() -> int:
    db_fd, db_path = tempfile.mkstemp(prefix="sync-demo-", suffix=".sqlite3")
    os.close(db_fd)
    print(f"Using temporary SQLite database: {db_path}")

    with TestClient(create_app(db_path)) as client:
        alice = create_user(client, "Alice")
        bob = create_user(client, "Bob")
        carol = create_user(client, "Carol")

        print("\n1. Build branched conversations for Bob")
        root = create_conversation(client, alice, "Root")
        root_messages = [append(client, alice, root["id"], f"root {i}") for i in range(5)]
        child = fork(client, alice, root["id"], root_messages[-1]["message_id"], "Child")
        check(add_member(client, alice, child["id"], bob).status_code == 201,
              "Alice invites Bob to the child branch")
        child_messages = [append(client, bob, child["id"], f"child {i}") for i in range(3)]
        # A second child branch owned by Carol that inherited from the root.
        check(add_member(client, alice, root["id"], carol).status_code == 201,
              "Alice invites Carol to the root")
        carol_branch = fork(client, carol, root["id"], root_messages[2]["message_id"], "Carol")
        bob_other = append(client, carol, carol_branch["id"], "carol local")

        print("\n2. Empty replica loads every joined conversation and message")
        bob_laptop = Replica()
        catch_up(client, bob, "bob-laptop", bob_laptop)
        check(bob_laptop.conversation_ids() == {child["id"]},
              "Bob only loads conversations he currently belongs to")
        check(bob_laptop.fully_loaded(child["id"]),
              "the child's history walk is complete")
        inherited = bob_laptop.messages(child["id"])[0]
        check(
            inherited["message_id"] == root_messages[0]["message_id"]
            and inherited["author"]["id"] == alice["id"]
            and inherited["source"] == "inherited"
            and inherited["origin"] == {"conversation_id": root["id"], "position": 1},
            "an inherited message keeps its original id, author and origin",
        )
        check(
            [m["message_id"] for m in bob_laptop.messages(child["id"])]
            == [m["message_id"] for m in root_messages + child_messages],
            "replica order is inherited prefix then local messages",
        )
        compare_with_api(client, bob, bob_laptop, child["id"], "Bob's child")

        carol_mac = Replica()
        catch_up(client, carol, "carol-mac", carol_mac)
        check(carol_mac.conversation_ids() == {root["id"], carol_branch["id"]},
              "Carol loads her own two conversations, not Bob's branch")
        compare_with_api(client, carol, carol_mac, root["id"], "Carol's root")
        compare_with_api(client, carol, carol_mac, carol_branch["id"], "Carol's fork")

        print("\n3. A lost response is retried from the old progress")
        saved = bob_laptop.cursor
        new_message = append(client, bob, child["id"], "delivered once")
        lost = pull(client, bob, "bob-laptop", saved).json()
        check(
            [i["message"]["message_id"] for i in lost["items"] if i["type"] == "message"]
            == [new_message["message_id"]],
            "the response the device will never apply carried the new message",
        )
        assert request(client, "POST", f"/messages/{new_message['message_id']}/retraction", bob).status_code == 200
        retried = pull(client, bob, "bob-laptop", saved).json()
        retried_messages = [i["message"] for i in retried["items"] if i["type"] == "message"]
        check(
            [m["message_id"] for m in retried_messages] == [new_message["message_id"]],
            "a retry from the same progress does not skip the unapplied append",
        )
        check(
            retried_messages[0]["body"] is None and retried_messages[0]["retracted"],
            "the retried body is a placeholder now that it is unreadable",
        )
        bob_laptop.apply_batch(retried)
        compare_with_api(client, bob, bob_laptop, child["id"], "after retry")

        print("\n4. Retraction reaches a branch the author never joined")
        check(
            request(client, "POST", f"/messages/{root_messages[1]['message_id']}/retraction", alice).status_code
            == 200,
            "Alice retracts a root message she authored",
        )
        catch_up(client, carol, "carol-mac", carol_mac)
        carol_copies = [
            m for cid in carol_mac.conversation_ids() for m in carol_mac.messages(cid)
            if m["message_id"] == root_messages[1]["message_id"]
        ]
        check(len(carol_copies) == 2, "Carol cached that original in both branches")
        check(
            all(m["retracted"] and m["body"] is None for m in carol_copies),
            "both of Carol's cached copies became placeholders",
        )
        compare_with_api(client, carol, carol_mac, root["id"], "Carol's root retracted")
        compare_with_api(client, carol, carol_mac, carol_branch["id"], "Carol's fork retracted")

        print("\n5. Paging interrupted by an append, retraction and membership change")
        slow = Replica()
        first_batch = pull(client, bob, "bob-slow", None, limit=2).json()
        slow.apply_batch(first_batch)
        check(
            first_batch["has_more"] and first_batch["progress"]["loading_conversation_id"] == child["id"],
            "the first small batch leaves the history walk mid-flight",
        )
        append(client, bob, child["id"], "while the device is still loading")
        request(client, "POST", f"/messages/{child_messages[0]['message_id']}/retraction", bob)
        # Bob is removed from and re-added to the child during the walk.
        check(remove_member(client, alice, child["id"], bob).status_code == 204,
              "Bob is removed while his device is mid-bootstrap")
        mid = pull(client, bob, "bob-slow", slow.cursor, limit=2)
        check(mid.status_code == 200, "the mid-bootstrap pull still works")
        slow.apply_batch(mid.json())
        check(add_member(client, alice, child["id"], bob).status_code == 201,
              "Bob is re-invited while his device keeps paging")
        batches = catch_up(client, bob, "bob-slow", slow, limit=2)
        check(batches < 500, "the device converges in finitely many batches")
        check(slow.fully_loaded(child["id"]), "the new identity's history walk completes")
        compare_with_api(client, bob, slow, child["id"], "after re-invite mid-paging")
        check(
            any(m["body"] == "while the device is still loading" for m in slow.messages(child["id"])),
            "the append made during the walk is delivered",
        )
        # The message retracted during the walk is a placeholder under the new
        # identity too.
        placeholder = next(
            m for m in slow.messages(child["id"])
            if m["message_id"] == child_messages[0]["message_id"]
        )
        check(placeholder["retracted"] and placeholder["body"] is None,
              "a message retracted during the interrupted walk is a placeholder")

        print("\n6. Offline removal and re-invite: identity-scoped clear and rebuild")
        before = bob_laptop.conversations[child["id"]]["membership_id"]
        check(remove_member(client, alice, child["id"], bob).status_code == 204,
              "Bob is removed while his laptop is offline")
        append(client, alice, child["id"], "said while Bob is out")
        check(add_member(client, alice, child["id"], bob).status_code == 201,
              "Bob is re-invited before his laptop syncs")
        catch_up(client, bob, "bob-laptop", bob_laptop)
        after = bob_laptop.conversations[child["id"]]["membership_id"]
        check(after != before, "the re-invite minted a new sync identity")
        check(
            any(c["membership_id"] == before and c["conversation_id"] == child["id"]
                for c in bob_laptop.last_cleared),
            "the old identity's copy was cleared by a conversation.cleared item",
        )
        compare_with_api(client, bob, bob_laptop, child["id"], "rebuilt after re-invite")
        check(
            any(m["body"] == "said while Bob is out" for m in bob_laptop.messages(child["id"])),
            "the rebuilt copy includes what was said while Bob was away",
        )

        print("\n7. Clearing one conversation leaves another's copy of a message")
        # Bob shares the root's message with Carol's fork, but Bob never joined
        # Carol's branch; instead demonstrate the root/child sharing here: Bob
        # is still in the root? No -- assert the child's inherited original
        # survives while the child is cleared.
        check(add_member(client, alice, root["id"], bob).status_code == 201,
              "Bob joins the root too")
        catch_up(client, bob, "bob-laptop", bob_laptop)
        shared_id = root_messages[3]["message_id"]
        check(
            shared_id in {m["message_id"] for m in bob_laptop.messages(root["id"])}
            and shared_id in {m["message_id"] for m in bob_laptop.messages(child["id"])},
            "the same original message is cached in both conversations",
        )
        check(remove_member(client, alice, child["id"], bob).status_code == 204,
              "Bob loses only the child")
        catch_up(client, bob, "bob-laptop", bob_laptop)
        check(child["id"] not in bob_laptop.conversation_ids(),
              "the child and its copy are cleared")
        check(
            shared_id in {m["message_id"] for m in bob_laptop.messages(root["id"])},
            "the same original remains in the root, untouched",
        )
        compare_with_api(client, bob, bob_laptop, root["id"], "root after child cleared")

        print("\n8. Device isolation and progress ownership")
        phone = Replica()
        catch_up(client, bob, "bob-phone", phone)
        check(
            [m["message_id"] for m in phone.messages(root["id"])]
            == [m["message_id"] for m in bob_laptop.messages(root["id"])],
            "a second device starting empty converges to the same root view",
        )
        laptop_cursor = bob_laptop.cursor
        phone_cursor = phone.cursor
        check(
            pull(client, bob, "bob-phone", laptop_cursor).status_code == 400,
            "the laptop's progress is rejected on the phone",
        )
        check(
            pull(client, carol, "bob-laptop", laptop_cursor).status_code == 400,
            "the progress token is rejected for another account",
        )
        late = append(client, bob, root["id"], "only the laptop pulls this")
        catch_up(client, bob, "bob-laptop", bob_laptop)
        check(
            late["message_id"] not in {m["message_id"] for m in phone.messages(root["id"])},
            "the laptop pulling did not advance the phone's progress",
        )
        catch_up(client, bob, "bob-phone", phone)
        check(
            late["message_id"] in {m["message_id"] for m in phone.messages(root["id"])},
            "the phone then pulls the same change under its own progress",
        )
        check(phone_cursor != phone.cursor, "the phone's own progress advanced")

        print("\n9. Caught-up pulls are empty and stable")
        idle = pull(client, bob, "bob-laptop", bob_laptop.cursor).json()
        check(idle["caught_up"] and idle["items"] == [],
              "a caught-up pull resends no history")
        idle2 = pull(client, bob, "bob-laptop", idle["next_cursor"]).json()
        check(idle2["caught_up"] and idle2["items"] == [],
              "consecutive pulls keep converging to empty batches")

        laptop_cursor_before_restart = bob_laptop.cursor
        phone_state = {
            cid: [m["message_id"] for m in phone.messages(cid)]
            for cid in phone.conversation_ids()
        }

    print("\n10. Restart the service and resume from saved device progress")
    with TestClient(create_app(db_path)) as restarted:
        new_late = append(restarted, bob, root["id"], "after restart")
        response = pull(restarted, bob, "bob-laptop", laptop_cursor_before_restart)
        check(response.status_code == 200, "the pre-restart cursor still verifies")
        batch = response.json()
        bob_laptop.apply_batch(batch)
        check(
            [i["message"]["message_id"] for i in batch["items"] if i["type"] == "message"]
            == [new_late["message_id"]],
            "after restart the laptop resumes exactly at the saved progress",
        )
        catch_up(restarted, bob, "bob-phone", phone)
        check(
            {cid: [m["message_id"] for m in phone.messages(cid)]
             for cid in phone.conversation_ids()} == {
                **phone_state,
                root["id"]: phone_state[root["id"]] + [new_late["message_id"]],
            },
            "the other device resumes from its own saved progress",
        )
        compare_with_api(restarted, bob, bob_laptop, root["id"], "after restart")

    print(f"\n{CHECKS} checks passed")
    print(f"Database remains available for inspection at {db_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("\nVERIFICATION FAILED", file=sys.stderr)
        raise
