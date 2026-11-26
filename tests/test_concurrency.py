"""Deterministic concurrency checks at the service layer.

Every writer runs in its own connection and uses ``BEGIN IMMEDIATE``, so
competing operations are serialised by SQLite. The assertions here are all
"outcome matches *some* commit order", never "a particular thread won".
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.errors import ApiError
from app.service import ConversationService


@pytest.fixture()
def service(tmp_path: Path) -> ConversationService:
    return ConversationService(str(tmp_path / "concurrency.sqlite3"))


def user(service: ConversationService, name: str) -> str:
    return service.create_user(name)["id"]


def run_concurrently(count: int, fn) -> list:
    barrier = threading.Barrier(count)

    def wrapped(index: int):
        barrier.wait()
        return fn(index)

    with ThreadPoolExecutor(max_workers=count) as pool:
        futures = [pool.submit(wrapped, i) for i in range(count)]
        results = []
        for future in futures:
            try:
                results.append(future.result())
            except ApiError as exc:
                results.append(exc)
        return results


def test_concurrent_appends_get_unique_ordered_positions(
    service: ConversationService,
) -> None:
    owner = user(service, "Owner")
    cid = service.create_conversation(owner, "Race")["id"]

    results = run_concurrently(
        12,
        lambda index: service.append_message(cid, owner, f"m{index}", None),
    )
    created = [result for result in results if isinstance(result, tuple)]
    assert len(created) == 12
    positions = sorted(message["position"] for message, _ in created)
    assert positions == list(range(1, 13))

    page = service.list_messages(cid, owner, None, None)
    assert len(page["items"]) == 12
    assert [i["position"] for i in page["items"]] == list(range(1, 13))


def test_concurrent_duplicate_tokens_create_one_message(
    service: ConversationService,
) -> None:
    owner = user(service, "Owner")
    cid = service.create_conversation(owner, "Retry wave")["id"]

    results = [
        result for result in run_concurrently(
            8,
            lambda _index: service.append_message(
                cid, owner, "only once", "same-token"
            ),
        )
        if isinstance(result, tuple)
    ]
    assert len(results) == 8
    assert len({message["message_id"] for message, _ in results}) == 1
    assert sum(1 for _, created in results if created is True) == 1
    assert len(service.list_messages(cid, owner, None, None)["items"]) == 1


def test_fork_and_parent_append_race_never_mixes_history(
    service: ConversationService,
) -> None:
    owner = user(service, "Owner")

    for round_no in range(6):
        parent = service.create_conversation(owner, f"Parent {round_no}")["id"]
        history = [
            service.append_message(parent, owner, f"p{i}", None)[0]
            for i in range(2)
        ]
        fork_message = history[-1]["message_id"]

        def do_fork(_index: int):
            return service.fork_conversation(parent, owner, fork_message, "Child")

        def do_append(_index: int):
            return service.append_message(parent, owner, "late parent", None)[0]

        results = run_concurrently(2, lambda index: (do_fork, do_append)[index](index))
        child = next(r for r in results if isinstance(r, dict))
        late = next(r for r in results if isinstance(r, dict) and "message_id" in r)

        child_items = service.list_messages(child["id"], owner, None, None)["items"]
        assert [i["position"] for i in child_items] == [1, 2]
        assert late["message_id"] not in {i["message_id"] for i in child_items}
        parent_items = service.list_messages(parent, owner, None, None)["items"]
        assert [i["position"] for i in parent_items] == [1, 2, 3]
        assert [i["message_id"] for i in parent_items[:2]] == [
            i["message_id"] for i in child_items
        ]
        # ...but the child marks the copied prefix as inherited, while the
        # parent marks the same messages as local. It is the same originals.
        assert all(i["source"] == "local" for i in parent_items)
        assert all(i["source"] == "inherited" for i in child_items)


def test_concurrent_retraction_and_forks_never_leak(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    bob = user(service, "Bob")
    root = service.create_conversation(alice, "Root")["id"]
    secret = service.append_message(root, alice, "TOP SECRET", None)[0]
    service.append_message(root, alice, "after", None)
    child = service.fork_conversation(
        root, alice, secret["message_id"], "Child"
    )["id"]
    grandchild = service.fork_conversation(
        child, alice, secret["message_id"], "Grandchild"
    )["id"]

    def concurrent_fork(index: int):
        source = (root, child)[index % 2]
        return service.fork_conversation(
            source, alice, secret["message_id"], f"Spawn {index}"
        )

    def concurrent_retract(_index: int):
        return service.retract_message(secret["message_id"], alice)

    results = run_concurrently(6, lambda index: (
        concurrent_fork(index) if index < 4 else concurrent_retract(index)
    ))
    spawned = [r for r in results if isinstance(r, dict) and "parent_conversation_id" in r]
    assert len(spawned) == 4

    retractions = [r for r in results if isinstance(r, dict) and "already_retracted" in r]
    assert len(retractions) == 2
    assert sum(1 for r in retractions if r["already_retracted"] is False) == 1

    # Every branch, old and newly spawned, plus any grandchild of a spawned
    # branch, sees a placeholder and no body.
    branches = [root, child, grandchild] + [s["id"] for s in spawned]
    for branch in branches:
        items = service.list_messages(branch, alice, None, None)["items"]
        target = next(i for i in items if i["message_id"] == secret["message_id"])
        assert target["body"] is None
        assert target["retracted"] is True
        assert target["origin"] == {
            "conversation_id": root,
            "position": secret["position"],
        }

    # Search cannot recover it anywhere.
    for branch in branches:
        assert service.search_messages(branch, alice, "SECRET", None, None)["items"] == []


def test_concurrent_retraction_of_distinct_messages_is_consistent(
    service: ConversationService,
) -> None:
    alice = user(service, "Alice")
    cid = service.create_conversation(alice, "Many")["id"]
    messages = [
        service.append_message(cid, alice, f"text {i}", None)[0] for i in range(6)
    ]

    def retract(index: int):
        return service.retract_message(messages[index]["message_id"], alice)

    results = run_concurrently(6, retract)
    assert len(results) == 6
    items = service.list_messages(cid, alice, None, None)["items"]
    assert len(items) == 6
    assert all(i["retracted"] and i["body"] is None for i in items)
    assert [i["position"] for i in items] == list(range(1, 7))
