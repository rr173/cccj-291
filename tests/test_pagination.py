"""Pagination over inherited + local history, and cursor authorisation."""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import (
    add_member,
    append_ok,
    auth,
    create_conversation,
    fork,
    read_all,
    remove_member,
)


def page(
    client: TestClient,
    user: dict,
    conversation_id: str,
    *,
    limit: int,
    cursor: str | None = None,
):
    params: dict = {"limit": limit}
    if cursor is not None:
        params["cursor"] = cursor
    return client.get(
        f"/conversations/{conversation_id}/messages",
        headers=auth(user),
        params=params,
    )


def build_branch(client: TestClient, alice: dict, bob: dict) -> dict:
    root = create_conversation(client, alice, "Long root")
    inherited = [append_ok(client, alice, root["id"], f"root {i}") for i in range(5)]
    child = fork(client, alice, root["id"], inherited[-1]["message_id"], "Child")
    assert add_member(client, alice, child["id"], bob).status_code == 201
    local = [append_ok(client, bob, child["id"], f"child {i}") for i in range(4)]
    return {"root": root, "child": child, "inherited": inherited, "local": local}


def test_pagination_covers_inherited_then_local_without_gaps(
    client: TestClient, alice: dict, bob: dict
) -> None:
    data = build_branch(client, alice, bob)
    child = data["child"]
    expected = [m["message_id"] for m in data["inherited"] + data["local"]]

    for limit in (1, 2, 3, 4, 9):
        seen: list[str] = []
        positions: list[int] = []
        cursor = None
        pages = 0
        while True:
            response = page(client, bob, child["id"], limit=limit, cursor=cursor)
            assert response.status_code == 200
            body = response.json()
            seen.extend(item["message_id"] for item in body["items"])
            positions.extend(item["position"] for item in body["items"])
            pages += 1
            if not body["has_more"]:
                assert body["next_cursor"] is None
                break
            assert body["next_cursor"]
            cursor = body["next_cursor"]
            assert pages < 50
        assert seen == expected, f"limit={limit}"
        assert positions == list(range(1, 10)), f"limit={limit}"
        assert len(set(seen)) == len(seen)

    sources = [i["source"] for i in read_all(client, bob, child["id"], limit=2)]
    assert sources == ["inherited"] * 5 + ["local"] * 4


def test_each_page_has_one_retraction_state(
    client: TestClient, alice: dict, bob: dict
) -> None:
    data = build_branch(client, alice, bob)
    first_page = page(client, bob, data["child"]["id"], limit=2).json()
    assert len(first_page["items"]) == 2
    # Retract the message that would appear on the *next* page.
    target = data["inherited"][3]["message_id"]
    assert (
        client.post(
            f"/messages/{target}/retraction", headers=auth(alice)
        ).status_code
        == 200
    )

    # The already-issued cursor still works; the page it produces is internally
    # consistent (all rows read from one snapshot after the retraction).
    second = page(
        client, bob, data["child"]["id"], limit=2, cursor=first_page["next_cursor"]
    )
    assert second.status_code == 200
    items = second.json()["items"]
    assert [i["position"] for i in items] == [3, 4]
    placeholder = next(i for i in items if i["message_id"] == target)
    assert placeholder["retracted"] is True
    assert placeholder["body"] is None
    # No other row in this page disagrees about its own retraction state.
    assert all(i["body"] is None for i in items if i["retracted"])

    # Full re-read: same 9 messages, exactly one placeholder.
    everything = read_all(client, bob, data["child"]["id"], limit=3)
    assert len(everything) == 9
    assert sum(1 for i in everything if i["retracted"]) == 1


def test_cursor_is_bound_to_user_and_conversation(
    client: TestClient, alice: dict, bob: dict, carol: dict
) -> None:
    data = build_branch(client, alice, bob)
    child_id = data["child"]["id"]

    issued = page(client, bob, child_id, limit=2).json()["next_cursor"]
    assert issued

    # Carol is not a member.
    assert page(client, carol, child_id, limit=2, cursor=issued).status_code == 403

    # Alice is a member but did not receive the cursor.
    assert page(client, alice, child_id, limit=2, cursor=issued).status_code == 400

    # Tampering invalidates the signature.
    tampered = issued[:-2] + ("AA" if not issued.endswith("AA") else "BB")
    bad = page(client, bob, child_id, limit=2, cursor=tampered)
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] in {"invalid_cursor"}

    # A cursor for messages cannot be used against the search endpoint.
    search = client.get(
        f"/conversations/{child_id}/search",
        headers=auth(bob),
        params={"q": "child", "cursor": issued},
    )
    assert search.status_code == 400


def test_old_cursor_dies_when_membership_is_removed_and_re_added(
    client: TestClient, alice: dict, bob: dict
) -> None:
    data = build_branch(client, alice, bob)
    child_id = data["child"]["id"]
    cursor = page(client, bob, child_id, limit=2).json()["next_cursor"]

    assert remove_member(client, alice, child_id, bob).status_code == 204
    denied = page(client, bob, child_id, limit=2, cursor=cursor)
    assert denied.status_code == 403

    assert add_member(client, alice, child_id, bob).status_code == 201
    replay = page(client, bob, child_id, limit=2, cursor=cursor)
    assert replay.status_code == 400
    assert replay.json()["error"]["code"] == "invalid_cursor"

    # A fresh read works and restarts from the beginning.
    fresh = page(client, bob, child_id, limit=2)
    assert fresh.status_code == 200
    assert [i["position"] for i in fresh.json()["items"]] == [1, 2]


def test_search_pages_only_the_branch_and_is_case_insensitive(
    client: TestClient, alice: dict, bob: dict
) -> None:
    root = create_conversation(client, alice, "Root")
    keep = append_ok(client, alice, root["id"], "Deploy the BLUE release")
    append_ok(client, alice, root["id"], "deploy the green release")
    child = fork(client, alice, root["id"], keep["message_id"], "Only blue")
    parent_late = append_ok(client, alice, root["id"], "blue but after the fork")
    assert add_member(client, alice, root["id"], bob).status_code == 201
    local = append_ok(client, bob, root["id"], "Blue from bob")

    root_hits = client.get(
        f"/conversations/{root['id']}/search",
        headers=auth(alice),
        params={"q": "blue"},
    ).json()
    assert [i["message_id"] for i in root_hits["items"]] == [
        keep["message_id"],
        parent_late["message_id"],
        local["message_id"],
    ]

    # The child only knows its inherited prefix; later parent messages and
    # parent-local messages are outside its view.
    child_hits = client.get(
        f"/conversations/{child['id']}/search",
        headers=auth(alice),
        params={"q": "BLUE"},
    ).json()
    assert [i["message_id"] for i in child_hits["items"]] == [keep["message_id"]]

    # Multi-term AND matching.
    both = client.get(
        f"/conversations/{root['id']}/search",
        headers=auth(alice),
        params={"q": "deploy blue"},
    ).json()
    assert [i["message_id"] for i in both["items"]] == [keep["message_id"]]

    # Paged search: one result per page across the full branch.
    pages: list[str] = []
    cursor = None
    while True:
        params = {"q": "blue", "limit": 1}
        if cursor:
            params["cursor"] = cursor
        body = client.get(
            f"/conversations/{root['id']}/search",
            headers=auth(alice),
            params=params,
        ).json()
        pages.extend(i["message_id"] for i in body["items"])
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
    assert pages == [
        keep["message_id"],
        parent_late["message_id"],
        local["message_id"],
    ]

    # Operators are literal, not SQL.
    literal = client.get(
        f"/conversations/{root['id']}/search",
        headers=auth(alice),
        params={"q": "%' OR 1=1 --"},
    )
    assert literal.status_code == 200
    assert literal.json()["items"] == []
