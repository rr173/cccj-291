#!/usr/bin/env python3
"""Reproducible end-to-end check for cross-branch resolution approval.

This script uses the real FastAPI HTTP stack through TestClient against a
temporary SQLite file. It demonstrates:

* a mutually dependent resolution package being approved atomically;
* parent-only review/approval and branch-scoped proposal status;
* privacy of child evidence (ids/bodies are not exposed to the parent owner);
* retraction invalidating one topic and its downstream dependents;
* unrelated topics remaining effective;
* idempotent approval replay and persistence across an app restart.

Run from the repository root:

    .venv/bin/python scripts/verify_resolutions.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402

CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(message)
    CHECKS += 1
    print(f"  ok  {message}")


def request(
    client: TestClient,
    method: str,
    path: str,
    user: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
):
    headers = {"X-User-Id": user["id"]} if user else {}
    return client.request(method, path, json=body, headers=headers)


def create_user(client: TestClient, name: str) -> dict[str, Any]:
    response = request(client, "POST", "/users", body={"display_name": name})
    assert response.status_code == 201, response.text
    return response.json()


def append(client: TestClient, user: dict, conversation_id: str, body: str) -> dict:
    response = request(
        client,
        "POST",
        f"/conversations/{conversation_id}/messages",
        user,
        {"body": body},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def resolution_item(
    topic: str,
    text: str,
    evidence: list[str],
    dependencies: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "topic_key": topic,
        "public_text": text,
        "evidence_message_ids": evidence,
        "depends_on_topic_keys": dependencies or [],
    }


def submit(
    client: TestClient, user: dict, child_id: str, items: list[dict]
) -> dict[str, Any]:
    response = request(
        client,
        "POST",
        f"/conversations/{child_id}/resolution-proposals",
        user,
        {"items": items},
    )
    assert response.status_code == 201, response.text
    return response.json()


def approve(client: TestClient, user: dict, proposal_id: str):
    return request(
        client,
        "POST",
        f"/resolution-proposals/{proposal_id}/approve",
        user,
        {},
    )


def board(client: TestClient, user: dict, parent_id: str) -> dict[str, dict]:
    response = request(
        client, "GET", f"/conversations/{parent_id}/resolution-board", user
    )
    assert response.status_code == 200, response.text
    return {topic["topic_key"]: topic for topic in response.json()["topics"]}


def print_board(topics: dict[str, dict]) -> None:
    print("\nResolution board:")
    for key in sorted(topics):
        topic = topics[key]
        if topic["status"] == "effective":
            version = topic["resolution"]["version"]
            print(f"  - {key:<10} effective (v{version})")
        else:
            print(f"  - {key:<10} PENDING ({topic['pending_reason']})")
    print()


def main() -> int:
    db_fd, db_path = tempfile.mkstemp(prefix="resolution-demo-", suffix=".sqlite3")
    os.close(db_fd)
    print(f"Using temporary SQLite database: {db_path}")

    with TestClient(create_app(db_path)) as client:
        print("\n1. Users and branched discussion")
        alice = create_user(client, "Alice (parent owner)")
        bob = create_user(client, "Bob (child proposer)")
        carol = create_user(client, "Carol (parent member)")

        parent = request(
            client, "POST", "/conversations", alice, {"title": "Formal parent board"}
        ).json()
        parent_id = parent["id"]
        fork_point = append(client, alice, parent_id, "fork point")
        child = request(
            client,
            "POST",
            f"/conversations/{parent_id}/fork",
            alice,
            {"parent_message_id": fork_point["message_id"], "title": "Working group"},
        ).json()
        child_id = child["id"]
        check(
            request(
                client,
                "POST",
                f"/conversations/{child_id}/members",
                alice,
                {"user_id": bob["id"]},
            ).status_code
            == 201,
            "Alice adds Bob to the child branch",
        )
        check(
            request(
                client,
                "POST",
                f"/conversations/{parent_id}/members",
                alice,
                {"user_id": carol["id"]},
            ).status_code
            == 201,
            "Alice adds Carol to the parent board",
        )

        charter_basis = append(client, bob, child_id, "local charter evidence")
        safety_basis = append(client, bob, child_id, "local safety evidence")
        refund_basis_1 = append(client, bob, child_id, "refund evidence one")
        refund_basis_2 = append(client, bob, child_id, "refund evidence two")
        support_basis = append(client, bob, child_id, "support evidence")

        print("\n2. Establish an external premise")
        charter_proposal = submit(
            client,
            bob,
            child_id,
            [
                resolution_item(
                    "charter", "Charter v1: lawful and transparent",
                    [charter_basis["message_id"]],
                )
            ],
        )
        charter_approval = approve(client, alice, charter_proposal["proposal_id"])
        check(charter_approval.status_code == 201, "owner approves charter v1")

        print("\n3. Submit and review an atomic package")
        package = submit(
            client,
            bob,
            child_id,
            [
                resolution_item(
                    "refund",
                    "Refund rule: 30 days, no questions asked",
                    [refund_basis_1["message_id"], refund_basis_2["message_id"]],
                    ["safety"],
                ),
                resolution_item(
                    "safety",
                    "Safety rule: escalate harmful requests",
                    [safety_basis["message_id"]],
                    ["charter"],
                ),
            ],
        )
        proposal_id = package["proposal_id"]
        check(package["approvable"] is True, "package is initially approvable")

        review_response = request(
            client, "GET", f"/resolution-proposals/{proposal_id}", alice
        )
        check(review_response.status_code == 200, "parent owner can review package")
        review_dump = json.dumps(review_response.json(), ensure_ascii=False)
        check(
            refund_basis_1["message_id"] not in review_dump
            and safety_basis["message_id"] not in review_dump,
            "review omits child message ids",
        )
        check(
            "refund evidence one" not in review_dump
            and "local safety evidence" not in review_dump,
            "review omits child message bodies",
        )
        check(
            "Refund rule: 30 days" in review_dump
            and "safety" in review_dump
            and "charter" in review_dump,
            "review shares public wording, topics and dependencies",
        )
        check(
            request(
                client,
                "GET",
                f"/conversations/{parent_id}/resolution-proposals",
                carol,
            ).status_code
            == 403,
            "ordinary parent member cannot open review queue",
        )
        check(
            request(
                client,
                "GET",
                f"/conversations/{parent_id}/resolution-board",
                bob,
            ).status_code
            == 403,
            "submission does not give Bob parent board read access",
        )
        check(
            request(
                client, "GET", f"/resolution-proposals/{proposal_id}", bob
            ).status_code
            == 200,
            "Bob can read his own proposal status",
        )

        print("\n4. Approve the package as one visible commit")
        approval_response = approve(client, alice, proposal_id)
        check(approval_response.status_code == 201, "first approval returns 201")
        approval = approval_response.json()
        check(
            set(approval["package_topic_keys"]) == {"refund", "safety"},
            "both package topics are in the approval result",
        )
        topics = board(client, carol, parent_id)
        check(
            all(topics[name]["status"] == "effective" for name in ("refund", "safety")),
            "refund and safety are simultaneously effective",
        )
        check(
            topics["refund"]["resolution"]["dependencies"][0]["topic_key"]
            == "safety",
            "internal dependency is pinned to the new package resolution",
        )
        check(
            topics["safety"]["resolution"]["dependencies"][0]["version"] == 1,
            "external charter dependency is pinned to submission-time v1",
        )

        support_proposal = submit(
            client,
            bob,
            child_id,
            [
                resolution_item(
                    "support",
                    "Support workflow follows refund",
                    [support_basis["message_id"]],
                    ["refund"],
                )
            ],
        )
        support_approval = approve(client, alice, support_proposal["proposal_id"])
        check(support_approval.status_code == 201, "downstream support resolution passes")
        topics = board(client, alice, parent_id)
        check(
            all(
                topics[name]["status"] == "effective"
                for name in ("charter", "safety", "refund", "support")
            ),
            "charter, safety, refund and support are effective",
        )

        replay = approve(client, alice, proposal_id)
        check(replay.status_code == 200, "retrying approval returns 200")
        check(
            replay.json()["approval_id"] == approval["approval_id"]
            and replay.json()["already_approved"] is True,
            "approval retry returns the original approval id",
        )
        check(
            len(board(client, alice, parent_id)["refund"]["resolution"]["dependencies"])
            == 1,
            "retry did not rewrite or duplicate dependency rows",
        )

        print("\n5. Retract a basis and show the impact radius")
        retraction = request(
            client,
            "POST",
            f"/messages/{refund_basis_1['message_id']}/retraction",
            bob,
        )
        check(retraction.status_code == 200, "basis author retracts one refund basis")
        topics = board(client, alice, parent_id)
        print_board(topics)
        check(
            topics["refund"]["status"] == "pending_reconsideration"
            and topics["refund"]["resolution"] is None,
            "refund hides its text and is pending",
        )
        check(
            topics["support"]["status"] == "pending_reconsideration",
            "downstream support is pending",
        )
        check(
            topics["safety"]["status"] == "effective"
            and topics["charter"]["status"] == "effective",
            "prerequisite safety and unrelated charter remain effective",
        )
        board_dump = json.dumps(
            request(
                client,
                "GET",
                f"/conversations/{parent_id}/resolution-board",
                alice,
            ).json(),
            ensure_ascii=False,
        )
        check(
            "Refund rule: 30 days" not in board_dump
            and "Support workflow follows refund" not in board_dump,
            "pending topics do not echo their old wording",
        )

        append(client, bob, child_id, "refund evidence one")
        topics = board(client, alice, parent_id)
        check(
            topics["refund"]["status"] == "pending_reconsideration"
            and topics["support"]["status"] == "pending_reconsideration",
            "same-text chat repost does not restore the invalidated decision",
        )

        print("\n6. Pending proposal redaction")
        pending_basis = append(client, bob, child_id, "pending basis")
        pending = submit(
            client,
            bob,
            child_id,
            [resolution_item("watch", "This wording must disappear", [pending_basis["message_id"]])],
        )
        request(
            client,
            "POST",
            f"/messages/{pending_basis['message_id']}/retraction",
            bob,
        )
        pending_review = request(
            client, "GET", f"/resolution-proposals/{pending['proposal_id']}", alice
        ).json()
        check(
            pending_review["items"][0]["text_visible"] is False
            and pending_review["items"][0]["public_text"] is None,
            "retracted-basis pending proposal no longer shows public wording",
        )

        saved_approval_id = approval["approval_id"]
        saved_resolution_ids = {
            item["topic_key"]: item["resolution_id"]
            for item in approval["resolutions"]
        }

    print("7. Restart HTTP service against the same SQLite file")
    with TestClient(create_app(db_path)) as restarted:
        restarted_board = request(
            restarted,
            "GET",
            f"/conversations/{parent_id}/resolution-board",
            alice,
        )
        check(restarted_board.status_code == 200, "board reads after restart")
        statuses = {
            topic["topic_key"]: topic["status"]
            for topic in restarted_board.json()["topics"]
        }
        check(
            statuses.get("refund") == "pending_reconsideration"
            and statuses.get("support") == "pending_reconsideration"
            and statuses.get("safety") == "effective",
            "invalidation chain survived restart",
        )
        replay = approve(restarted, alice, proposal_id)
        check(replay.status_code == 200, "approval replay works after restart")
        check(
            replay.json()["approval_id"] == saved_approval_id,
            "restarted service returns the original approval result",
        )
        replay_ids = {
            item["topic_key"]: item["resolution_id"]
            for item in replay.json()["resolutions"]
        }
        check(
            replay_ids == saved_resolution_ids,
            "approval replay preserves resolution ids and writes no new versions",
        )

    print(f"\n{CHECKS} checks passed")
    print(f"Database remains available for inspection at {db_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("\nVERIFICATION FAILED", file=sys.stderr)
        raise
