"""Cross-branch resolution packages, approval and board invalidation."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import ConversationService
from conftest import add_member, append_ok, auth, create_conversation, fork


def item(topic: str, text: str, evidence: list[str], deps: list[str] | None = None):
    return {
        "topic_key": topic,
        "public_text": text,
        "evidence_message_ids": evidence,
        "depends_on_topic_keys": deps or [],
    }


def submit(
    client: TestClient,
    user: dict,
    child_id: str,
    items: list[dict],
    token: str | None = None,
):
    body: dict = {"items": items}
    if token is not None:
        body["client_token"] = token
    return client.post(
        f"/conversations/{child_id}/resolution-proposals",
        json=body,
        headers=auth(user),
    )


def approve(client: TestClient, user: dict, proposal_id: str):
    return client.post(
        f"/resolution-proposals/{proposal_id}/approve",
        json={},
        headers=auth(user),
    )


def board_map(client: TestClient, user: dict, parent_id: str) -> dict[str, dict]:
    response = client.get(
        f"/conversations/{parent_id}/resolution-board", headers=auth(user)
    )
    assert response.status_code == 200, response.text
    return {topic["topic_key"]: topic for topic in response.json()["topics"]}


@pytest.fixture()
def branch(client: TestClient, alice: dict, bob: dict, carol: dict) -> dict:
    parent = create_conversation(client, alice, "Parent board")
    fork_point = append_ok(client, alice, parent["id"], "fork point")
    child = fork(client, alice, parent["id"], fork_point["message_id"], "Child")
    assert add_member(client, alice, child["id"], bob).status_code == 201
    assert add_member(client, alice, parent["id"], carol).status_code == 201
    return {
        "parent": parent,
        "fork_point": fork_point,
        "child": child,
        "alice": alice,
        "bob": bob,
        "carol": carol,
    }


def test_atomic_package_approval_privacy_and_idempotent_retry(
    client: TestClient, branch: dict
) -> None:
    alice = branch["alice"]
    bob = branch["bob"]
    carol = branch["carol"]
    parent_id = branch["parent"]["id"]
    child_id = branch["child"]["id"]

    safety_basis = append_ok(client, bob, child_id, "local safety evidence")
    refund_basis_1 = append_ok(client, bob, child_id, "local refund evidence one")
    refund_basis_2 = append_ok(client, bob, child_id, "local refund evidence two")

    response = submit(
        client,
        bob,
        child_id,
        [
            item(
                "refunds",
                "Public refund rule",
                [refund_basis_1["message_id"], refund_basis_2["message_id"]],
                ["safety"],
            ),
            item("safety", "Public safety rule", [safety_basis["message_id"]]),
        ],
        token="package-1",
    )
    assert response.status_code == 201, response.text
    proposal = response.json()
    proposal_id = proposal["proposal_id"]
    assert proposal["status"] == "pending"
    assert proposal["approvable"] is True

    # Submission retries with the same client token return the same proposal.
    retry = submit(
        client,
        bob,
        child_id,
        [item("different", "different", [safety_basis["message_id"]])],
        token="package-1",
    )
    assert retry.status_code == 200
    assert retry.json()["proposal_id"] == proposal_id

    # The proposer can read status without parent membership. Bob was never a
    # parent member here; the board remains forbidden to him.
    own = client.get(
        f"/resolution-proposals/{proposal_id}", headers=auth(bob)
    )
    assert own.status_code == 200
    assert client.get(
        f"/conversations/{parent_id}/resolution-board", headers=auth(bob)
    ).status_code == 403

    # A normal parent member can read the board but cannot open review queues.
    assert client.get(
        f"/conversations/{parent_id}/resolution-proposals", headers=auth(carol)
    ).status_code == 403
    assert client.get(
        f"/resolution-proposals/{proposal_id}", headers=auth(carol)
    ).status_code == 403

    review = client.get(
        f"/resolution-proposals/{proposal_id}", headers=auth(alice)
    )
    assert review.status_code == 200
    review_text = review.text
    assert safety_basis["message_id"] not in review_text
    assert refund_basis_1["message_id"] not in review_text
    assert "local safety evidence" not in review_text
    assert "local refund evidence one" not in review_text
    assert "Public safety rule" in review_text

    # Before approval the board is empty; after approval both topics appear.
    assert board_map(client, carol, parent_id) == {}
    approved = approve(client, alice, proposal_id)
    assert approved.status_code == 201, approved.text
    result = approved.json()
    assert result["already_approved"] is False
    assert set(result["package_topic_keys"]) == {"refunds", "safety"}
    assert result["invalidated_topics"] == []
    resolution_ids = {r["topic_key"]: r["resolution_id"] for r in result["resolutions"]}
    assert len(set(resolution_ids.values())) == 2

    topics = board_map(client, carol, parent_id)
    assert set(topics) == {"refunds", "safety"}
    assert topics["safety"]["status"] == "effective"
    assert topics["safety"]["resolution"]["public_text"] == "Public safety rule"
    refunds = topics["refunds"]["resolution"]
    assert refunds["version"] == 1
    assert refunds["depends_on_topic_keys"] == ["safety"]
    assert refunds["dependencies"][0]["resolution_id"] == resolution_ids["safety"]

    # Repeating the same approval returns the original result and writes no
    # additional versions.
    repeated = approve(client, alice, proposal_id)
    assert repeated.status_code == 200
    repeated_result = repeated.json()
    assert repeated_result["already_approved"] is True
    assert repeated_result["approval_id"] == result["approval_id"]
    assert {
        r["topic_key"]: r["resolution_id"] for r in repeated_result["resolutions"]
    } == resolution_ids
    topics = board_map(client, carol, parent_id)
    assert topics["safety"]["resolution"]["version"] == 1
    assert topics["refunds"]["resolution"]["version"] == 1


def test_submission_rejects_inherited_retracted_cyclic_and_missing_premises(
    client: TestClient, branch: dict
) -> None:
    alice = branch["alice"]
    bob = branch["bob"]
    carol = branch["carol"]
    parent_id = branch["parent"]["id"]
    child_id = branch["child"]["id"]
    inherited = branch["fork_point"]

    local = append_ok(client, bob, child_id, "valid local basis")
    retracted = append_ok(client, bob, child_id, "will retract")
    assert client.post(
        f"/messages/{retracted['message_id']}/retraction", headers=auth(bob)
    ).status_code == 200

    cases = [
        (
            [item("x", "text", [inherited["message_id"]])],
            "inherited_message_cannot_be_evidence",
        ),
        (
            [item("x", "text", ["msg_does_not_exist"])],
            "invalid_resolution_evidence",
        ),
        (
            [item("x", "text", [retracted["message_id"]])],
            "retracted_resolution_evidence",
        ),
        (
            [
                item("a", "A", [local["message_id"]], ["b"]),
                item("b", "B", [local["message_id"]], ["a"]),
            ],
            "resolution_dependency_cycle",
        ),
        (
            [item("x", "X", [local["message_id"]], ["missing-topic"])],
            "resolution_dependency_not_effective",
        ),
        (
            [
                item("dup", "one", [local["message_id"]]),
                item("dup", "two", [local["message_id"]]),
            ],
            "duplicate_resolution_topic",
        ),
    ]
    for items, expected_code in cases:
        response = submit(client, bob, child_id, items)
        assert response.status_code == 400, (expected_code, response.text)
        assert response.json()["error"]["code"] == expected_code

    # Parent membership does not grant child submission rights.
    response = submit(client, carol, child_id, [item("x", "X", [local["message_id"]])])
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "not_a_member"

    # A root conversation has no direct parent board.
    root = create_conversation(client, alice, "Root without parent")
    root_basis = append_ok(client, alice, root["id"], "root local")
    response = submit(client, alice, root["id"], [item("x", "X", [root_basis["message_id"]])])
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "no_parent_conversation"


def test_proposer_membership_instance_must_survive_even_after_rejoin(
    client: TestClient, alice: dict, bob: dict, carol: dict
) -> None:
    parent = create_conversation(client, alice, "Parent")
    point = append_ok(client, alice, parent["id"], "point")
    child = fork(client, alice, parent["id"], point["message_id"], "Child owned by Carol")
    assert add_member(client, alice, child["id"], carol).status_code == 201
    # Carol must own the child so she can remove Bob. Alice forked above and is
    # child owner too; create a Carol-owned branch instead for the exact exit.
    carol_point = append_ok(client, carol, child["id"], "carol local fork point")
    carol_branch = fork(
        client, carol, child["id"], carol_point["message_id"], "Carol branch"
    )
    assert add_member(client, carol, carol_branch["id"], bob).status_code == 201
    basis = append_ok(client, bob, carol_branch["id"], "bob basis")
    proposal = submit(
        client, bob, carol_branch["id"], [item("x", "X", [basis["message_id"]])]
    )
    assert proposal.status_code == 201, proposal.text
    proposal_id = proposal.json()["proposal_id"]

    removed = client.delete(
        f"/conversations/{carol_branch['id']}/members/{bob['id']}",
        headers=auth(carol),
    )
    assert removed.status_code == 204
    assert approve(client, alice, proposal_id).status_code == 409
    assert (
        approve(client, alice, proposal_id).json()["error"]["code"]
        == "proposer_membership_lapsed"
    )
    assert board_map(client, alice, parent["id"]) == {}

    # Rejoining mints a new membership id and deliberately does not cure it.
    assert add_member(client, carol, carol_branch["id"], bob).status_code == 201
    response = approve(client, alice, proposal_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "proposer_membership_lapsed"
    status = client.get(f"/resolution-proposals/{proposal_id}", headers=auth(bob))
    assert status.json()["approvable"] is False
    assert "proposer_membership_lapsed" in status.json()["block_codes"]


def test_retracted_basis_redacts_pending_text_and_blocks_approval(
    client: TestClient, branch: dict
) -> None:
    alice = branch["alice"]
    bob = branch["bob"]
    parent_id = branch["parent"]["id"]
    child_id = branch["child"]["id"]
    basis = append_ok(client, bob, child_id, "withdrawable basis")
    proposal_id = submit(
        client, bob, child_id, [item("secret-topic", "Public wording", [basis["message_id"]])]
    ).json()["proposal_id"]

    retracted = client.post(
        f"/messages/{basis['message_id']}/retraction", headers=auth(bob)
    )
    assert retracted.status_code == 200

    review = client.get(f"/resolution-proposals/{proposal_id}", headers=auth(alice))
    assert review.status_code == 200
    body = review.json()
    assert body["approvable"] is False
    assert body["block_code"] == "basis_retracted"
    assert body["items"][0]["text_visible"] is False
    assert body["items"][0]["public_text"] is None
    assert "Public wording" not in review.text

    response = approve(client, alice, proposal_id)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "basis_retracted"
    assert board_map(client, alice, parent_id) == {}


def test_target_topic_conflict_blocks_but_unrelated_change_does_not(
    client: TestClient, branch: dict
) -> None:
    alice = branch["alice"]
    bob = branch["bob"]
    parent_id = branch["parent"]["id"]
    child_id = branch["child"]["id"]

    old_basis = append_ok(client, bob, child_id, "old target basis")
    second_basis = append_ok(client, bob, child_id, "stale target basis")
    winning_basis = append_ok(client, bob, child_id, "winning target basis")
    adjacent_basis = append_ok(client, bob, child_id, "adjacent basis")

    first = submit(client, bob, child_id, [item("target", "target v1", [old_basis["message_id"]])])
    first_id = first.json()["proposal_id"]
    assert approve(client, alice, first_id).status_code == 201

    stale = submit(
        client, bob, child_id, [item("target", "stale v2", [second_basis["message_id"]])]
    ).json()["proposal_id"]
    winner = submit(
        client, bob, child_id, [item("target", "winner v2", [winning_basis["message_id"]])]
    ).json()["proposal_id"]
    assert approve(client, alice, winner).status_code == 201
    response = approve(client, alice, stale)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "topic_updated_elsewhere"
    topics = board_map(client, alice, parent_id)
    assert topics["target"]["resolution"]["version"] == 2
    assert topics["target"]["resolution"]["public_text"] == "winner v2"

    # An adjacent topic moving after submission is not a conflict.
    plan_basis = append_ok(client, bob, child_id, "plan basis")
    plan_proposal = submit(
        client, bob, child_id, [item("plan", "plan text", [plan_basis["message_id"]])]
    ).json()["proposal_id"]
    adjacent_proposal = submit(
        client,
        bob,
        child_id,
        [item("adjacent", "adjacent text", [adjacent_basis["message_id"]])],
    ).json()["proposal_id"]
    assert approve(client, alice, adjacent_proposal).status_code == 201
    assert approve(client, alice, plan_proposal).status_code == 201
    assert board_map(client, alice, parent_id)["plan"]["status"] == "effective"


def test_effective_target_becoming_pending_blocks_stale_approval(
    client: TestClient, branch: dict
) -> None:
    alice = branch["alice"]
    bob = branch["bob"]
    parent_id = branch["parent"]["id"]
    child_id = branch["child"]["id"]

    shaky_basis = append_ok(client, bob, child_id, "shaky basis")
    replacement_basis = append_ok(client, bob, child_id, "replacement basis")
    current = submit(
        client, bob, child_id, [item("shaky", "shaky text", [shaky_basis["message_id"]])]
    ).json()["proposal_id"]
    assert approve(client, alice, current).status_code == 201

    replacement = submit(
        client,
        bob,
        child_id,
        [item("shaky", "would replace", [replacement_basis["message_id"]])],
    ).json()["proposal_id"]
    assert client.post(
        f"/messages/{shaky_basis['message_id']}/retraction", headers=auth(bob)
    ).status_code == 200
    response = approve(client, alice, replacement)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "topic_pending_reconsideration"
    topic = board_map(client, alice, parent_id)["shaky"]
    assert topic["status"] == "pending_reconsideration"
    assert topic["resolution"] is None


def test_external_dependency_change_and_post_apply_cascade_are_enforced(
    client: TestClient, branch: dict
) -> None:
    alice = branch["alice"]
    bob = branch["bob"]
    parent_id = branch["parent"]["id"]
    child_id = branch["child"]["id"]

    b_basis = append_ok(client, bob, child_id, "B basis")
    b2_basis = append_ok(client, bob, child_id, "B2 basis")
    a_basis = append_ok(client, bob, child_id, "A basis")
    x_basis = append_ok(client, bob, child_id, "X basis")

    b1 = submit(client, bob, child_id, [item("B", "B1", [b_basis["message_id"]])]).json()
    assert approve(client, alice, b1["proposal_id"]).status_code == 201
    a1 = submit(
        client,
        bob,
        child_id,
        [item("A", "A1 depends B", [a_basis["message_id"]], ["B"])],
    ).json()
    assert approve(client, alice, a1["proposal_id"]).status_code == 201

    # A normal stale external pin is blocked when B is replaced first.
    stale_x = submit(
        client,
        bob,
        child_id,
        [item("X", "X depends B", [x_basis["message_id"]], ["B"])],
    ).json()["proposal_id"]
    b2 = submit(client, bob, child_id, [item("B", "B2", [b2_basis["message_id"]])]).json()
    assert approve(client, alice, b2["proposal_id"]).status_code == 201
    response = approve(client, alice, stale_x)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "external_dependency_changed"

    # Rebuild the old-B/old-A situation and make a package that both replaces B
    # and tries to use old A as an external premise. Applying B would invalidate
    # A, so the external premise would not survive the whole package.
    b3_basis = append_ok(client, bob, child_id, "B3 basis")
    y_basis = append_ok(client, bob, child_id, "Y basis")
    b3 = submit(client, bob, child_id, [item("B", "B3", [b3_basis["message_id"]])]).json()
    # B is now at v2 from the previous replacement. Approve B3 and then A2 so
    # this scenario starts from a clean effective B/A pair again.
    assert approve(client, alice, b3["proposal_id"]).status_code == 201
    a2_basis = append_ok(client, bob, child_id, "A2 basis")
    a2 = submit(
        client,
        bob,
        child_id,
        [item("A", "A2 depends B3", [a2_basis["message_id"]], ["B"])],
    ).json()
    assert approve(client, alice, a2["proposal_id"]).status_code == 201

    b4_basis = append_ok(client, bob, child_id, "B4 basis")
    package = submit(
        client,
        bob,
        child_id,
        [
            item("B", "B4 replacement", [b4_basis["message_id"]]),
            item("Y", "Y uses old A externally", [y_basis["message_id"]], ["A"]),
        ],
    )
    assert package.status_code == 201, package.text
    package_id = package.json()["proposal_id"]
    response = approve(client, alice, package_id)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "external_premise_would_lapse"

    topics = board_map(client, alice, parent_id)
    assert topics["B"]["resolution"]["version"] == 3
    assert topics["A"]["status"] == "effective"
    assert "Y" not in topics


def test_retraction_and_replacement_cascade_pending_downstream(
    client: TestClient, branch: dict
) -> None:
    alice = branch["alice"]
    bob = branch["bob"]
    parent_id = branch["parent"]["id"]
    child_id = branch["child"]["id"]

    b_basis = append_ok(client, bob, child_id, "B evidence")
    a_basis = append_ok(client, bob, child_id, "A evidence")
    c_basis = append_ok(client, bob, child_id, "C evidence")
    d_basis = append_ok(client, bob, child_id, "D evidence")
    b2_basis = append_ok(client, bob, child_id, "B replacement evidence")

    for topic, text, basis, deps in [
        ("B", "B original public wording", b_basis, []),
        ("A", "A public wording", a_basis, ["B"]),
        ("C", "C public wording", c_basis, ["A"]),
        ("D", "D unrelated wording", d_basis, []),
    ]:
        proposal = submit(
            client, bob, child_id, [item(topic, text, [basis["message_id"]], deps)]
        )
        assert proposal.status_code == 201, proposal.text
        assert approve(client, alice, proposal.json()["proposal_id"]).status_code == 201

    # Replacing B moves A and C pending in the approval transaction and reports
    # the impact scope. D remains effective.
    replacement = submit(
        client, bob, child_id, [item("B", "B replacement wording", [b2_basis["message_id"]])]
    ).json()["proposal_id"]
    result = approve(client, alice, replacement)
    assert result.status_code == 201, result.text
    invalidated = {x["topic_key"]: x["pending_reason"] for x in result.json()["invalidated_topics"]}
    assert invalidated == {"A": "dependency_replaced", "C": "dependency_pending"}

    topics = board_map(client, alice, parent_id)
    assert topics["B"]["resolution"]["public_text"] == "B replacement wording"
    assert topics["A"]["status"] == "pending_reconsideration"
    assert topics["A"]["pending_reason"] == "dependency_replaced"
    assert topics["A"]["resolution"] is None
    assert topics["C"]["status"] == "pending_reconsideration"
    assert topics["C"]["resolution"] is None
    assert topics["D"]["status"] == "effective"
    assert topics["D"]["resolution"]["public_text"] == "D unrelated wording"
    board_text = client.get(
        f"/conversations/{parent_id}/resolution-board", headers=auth(alice)
    ).text
    assert "A public wording" not in board_text
    assert "C public wording" not in board_text

    # A same-text chat repost does not restore the invalidated chain.
    append_ok(client, bob, child_id, "B evidence")
    topics = board_map(client, alice, parent_id)
    assert topics["A"]["status"] == "pending_reconsideration"
    assert topics["C"]["status"] == "pending_reconsideration"

    # A later retraction of the new B basis starts evidence invalidation from B
    # (already effective replacement) but does not change A/C reasons back.
    retract = client.post(
        f"/messages/{b2_basis['message_id']}/retraction", headers=auth(bob)
    )
    assert retract.status_code == 200
    topics = board_map(client, alice, parent_id)
    assert topics["B"]["status"] == "pending_reconsideration"
    assert topics["B"]["pending_reason"] == "basis_retracted"
    assert topics["A"]["status"] == "pending_reconsideration"
    assert topics["D"]["status"] == "effective"


def test_coordinated_package_can_replace_both_dependency_and_dependent(
    client: TestClient, branch: dict
) -> None:
    alice = branch["alice"]
    bob = branch["bob"]
    parent_id = branch["parent"]["id"]
    child_id = branch["child"]["id"]

    b_basis = append_ok(client, bob, child_id, "B1 basis")
    a_basis = append_ok(client, bob, child_id, "A1 basis")
    c_basis = append_ok(client, bob, child_id, "C1 basis")
    b2_basis = append_ok(client, bob, child_id, "B2 basis")
    a2_basis = append_ok(client, bob, child_id, "A2 basis")

    b1 = submit(client, bob, child_id, [item("B", "B1", [b_basis["message_id"]])])
    assert approve(client, alice, b1.json()["proposal_id"]).status_code == 201
    a1 = submit(
        client, bob, child_id, [item("A", "A1", [a_basis["message_id"]], ["B"])]
    )
    assert approve(client, alice, a1.json()["proposal_id"]).status_code == 201
    c1 = submit(
        client, bob, child_id, [item("C", "C1", [c_basis["message_id"]], ["A"])]
    )
    assert approve(client, alice, c1.json()["proposal_id"]).status_code == 201

    # Input order deliberately puts A first even though A2 internally depends
    # on B2; the package must validate/apply as one DAG, not by array order.
    coordinated = submit(
        client,
        bob,
        child_id,
        [
            item("A", "A2 coordinated", [a2_basis["message_id"]], ["B"]),
            item("B", "B2 coordinated", [b2_basis["message_id"]]),
        ],
    )
    assert coordinated.status_code == 201, coordinated.text
    approved = approve(client, alice, coordinated.json()["proposal_id"])
    assert approved.status_code == 201, approved.text
    invalidated = {
        row["topic_key"]: row["pending_reason"]
        for row in approved.json()["invalidated_topics"]
    }
    assert invalidated == {"C": "dependency_replaced"}

    topics = board_map(client, alice, parent_id)
    assert topics["A"]["status"] == "effective"
    assert topics["B"]["status"] == "effective"
    assert topics["A"]["resolution"]["version"] == 2
    assert topics["B"]["resolution"]["version"] == 2
    dep = topics["A"]["resolution"]["dependencies"][0]
    assert dep["topic_key"] == "B"
    assert dep["version"] == 2
    assert topics["C"]["status"] == "pending_reconsideration"


def test_approved_resolution_survives_proposer_leaving_child(
    client: TestClient, branch: dict
) -> None:
    alice = branch["alice"]
    bob = branch["bob"]
    parent_id = branch["parent"]["id"]
    child_id = branch["child"]["id"]
    basis = append_ok(client, bob, child_id, "durable basis")
    proposal_id = submit(
        client, bob, child_id, [item("durable", "durable text", [basis["message_id"]])]
    ).json()["proposal_id"]
    assert approve(client, alice, proposal_id).status_code == 201

    removed = client.delete(
        f"/conversations/{child_id}/members/{bob['id']}", headers=auth(alice)
    )
    assert removed.status_code == 204
    topic = board_map(client, alice, parent_id)["durable"]
    assert topic["status"] == "effective"
    assert topic["resolution"]["public_text"] == "durable text"


def test_resolutions_and_approval_retry_survive_restart(
    client: TestClient, branch: dict, db_path: str
) -> None:
    alice = branch["alice"]
    bob = branch["bob"]
    parent_id = branch["parent"]["id"]
    child_id = branch["child"]["id"]
    basis = append_ok(client, bob, child_id, "restart basis")
    proposal_id = submit(
        client, bob, child_id, [item("restart", "survives restart", [basis["message_id"]])]
    ).json()["proposal_id"]
    first = approve(client, alice, proposal_id)
    assert first.status_code == 201
    approval = first.json()

    pending_basis = append_ok(client, bob, child_id, "pending across restart")
    pending_across_restart = submit(
        client,
        bob,
        child_id,
        [item("after-restart", "approved after restart", [pending_basis["message_id"]])],
    ).json()["proposal_id"]

    # The function-scoped client and a fresh service share the same SQLite file;
    # a brand-new app/service is the HTTP restart boundary.
    with TestClient(create_app(db_path)) as restarted:
        board = restarted.get(
            f"/conversations/{parent_id}/resolution-board", headers=auth(alice)
        )
        assert board.status_code == 200
        assert (
            board.json()["topics"][0]["resolution"]["public_text"]
            == "survives restart"
        )
        retry = restarted.post(
            f"/resolution-proposals/{proposal_id}/approve",
            json={},
            headers=auth(alice),
        )
        assert retry.status_code == 200
        assert retry.json()["approval_id"] == approval["approval_id"]
        assert retry.json()["already_approved"] is True
        status = restarted.get(
            f"/resolution-proposals/{proposal_id}", headers=auth(bob)
        )
        assert status.status_code == 200
        assert status.json()["status"] == "approved"

        pending_review = restarted.get(
            f"/resolution-proposals/{pending_across_restart}", headers=auth(alice)
        )
        assert pending_review.status_code == 200
        assert pending_review.json()["status"] == "pending"
        approve_pending = restarted.post(
            f"/resolution-proposals/{pending_across_restart}/approve",
            json={},
            headers=auth(alice),
        )
        assert approve_pending.status_code == 201, approve_pending.text
        assert (
            approve_pending.json()["resolutions"][0]["public_text"]
            == "approved after restart"
        )

    # Direct service initialization also performs additive schema upgrade safely.
    service = ConversationService(db_path)
    board_service = service.get_resolution_board(parent_id, alice["id"])
    assert board_service["topics"][0]["status"] == "effective"


def test_additive_schema_upgrade_preserves_discussions_and_reminders(
    tmp_path: Path,
) -> None:
    import sqlite3

    db_path = str(tmp_path / "upgrade.sqlite3")
    old = ConversationService(db_path)
    alice = old.create_user("Alice")["id"]
    bob = old.create_user("Bob")["id"]
    parent = old.create_conversation(alice, "Existing parent")["id"]
    old.add_member(parent, alice, bob)
    old.follow_conversation(parent, bob)
    message = old.append_message(
        parent,
        alice,
        "discussion that must survive",
        None,
        mention_user_ids=[bob],
    )[0]
    claim = old.claim_notifications(bob, "pre-upgrade-request", 10)
    assert claim["items"]

    # Simulate a pre-resolution database by removing only the new tables. The
    # old conversation/membership/notification tables and rows remain untouched.
    new_tables = [
        "resolution_board",
        "resolution_dependencies",
        "resolution_evidence",
        "resolutions",
        "resolution_proposal_dependencies",
        "resolution_proposal_evidence",
        "resolution_proposal_items",
        "resolution_proposals",
        "resolution_approvals",
    ]
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        for table in new_tables:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.commit()
    finally:
        conn.close()

    upgraded = ConversationService(db_path)
    messages = upgraded.list_messages(parent, alice, 50, None)["items"]
    assert any(item["message_id"] == message["message_id"] for item in messages)
    assert messages[0]["body"] == "discussion that must survive"

    replay = upgraded.claim_notifications(bob, "pre-upgrade-request", 10)
    assert replay["batch_id"] == claim["batch_id"]
    assert replay["items"][0]["message"]["body"] == "discussion that must survive"
    assert upgraded.get_resolution_board(parent, alice)["topics"] == []


def test_concurrent_approval_creates_exactly_one_package(tmp_path: Path) -> None:
    service = ConversationService(str(tmp_path / "approval-race.sqlite3"))
    alice = service.create_user("Alice")["id"]
    bob = service.create_user("Bob")["id"]
    parent = service.create_conversation(alice, "Parent")["id"]
    point = service.append_message(parent, alice, "point", None)[0]["message_id"]
    child = service.fork_conversation(parent, alice, point, "Child")["id"]
    service.add_member(child, alice, bob)
    basis = service.append_message(child, bob, "basis", None)[0]["message_id"]
    proposal, _ = service.create_resolution_proposal(
        child,
        bob,
        [{"topic_key": "race", "public_text": "Race text",
          "evidence_message_ids": [basis], "depends_on_topic_keys": []}],
    )
    proposal_id = proposal["proposal_id"]

    barrier = threading.Barrier(6)

    def approve_once(_index: int):
        barrier.wait()
        return service.approve_resolution_proposal(proposal_id, alice)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(approve_once, range(6)))

    created = [created_flag for _, created_flag in results if created_flag]
    assert created == [True]
    approval_ids = {result["approval_id"] for result, _ in results}
    assert len(approval_ids) == 1
    board = service.get_resolution_board(parent, alice)["topics"]
    assert len(board) == 1
    assert board[0]["resolution"]["version"] == 1
