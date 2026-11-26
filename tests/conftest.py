"""Shared fixtures: an isolated database and a test client per test."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.main import create_app  # noqa: E402


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "conversations.sqlite3")


@pytest.fixture()
def client(db_path: str) -> TestClient:
    with TestClient(create_app(db_path)) as test_client:
        yield test_client


@pytest.fixture()
def alice(client: TestClient) -> dict:
    response = client.post("/users", json={"display_name": "Alice"})
    assert response.status_code == 201
    return response.json()


@pytest.fixture()
def bob(client: TestClient) -> dict:
    response = client.post("/users", json={"display_name": "Bob"})
    assert response.status_code == 201
    return response.json()


@pytest.fixture()
def carol(client: TestClient) -> dict:
    response = client.post("/users", json={"display_name": "Carol"})
    assert response.status_code == 201
    return response.json()


def auth(user: dict) -> dict[str, str]:
    return {"X-User-Id": user["id"]}


def create_conversation(client: TestClient, user: dict, title: str) -> dict:
    response = client.post(
        "/conversations", json={"title": title}, headers=auth(user)
    )
    assert response.status_code == 201, response.text
    return response.json()


def append(
    client: TestClient,
    user: dict,
    conversation_id: str,
    body: str,
    client_token: str | None = None,
):
    payload = {"body": body}
    if client_token is not None:
        payload["client_token"] = client_token
    return client.post(
        f"/conversations/{conversation_id}/messages",
        json=payload,
        headers=auth(user),
    )


def append_ok(
    client: TestClient,
    user: dict,
    conversation_id: str,
    body: str,
    client_token: str | None = None,
) -> dict:
    response = append(client, user, conversation_id, body, client_token)
    assert response.status_code in (200, 201), response.text
    return response.json()


def fork(
    client: TestClient,
    user: dict,
    conversation_id: str,
    message_id: str,
    title: str | None = None,
) -> dict:
    payload = {"parent_message_id": message_id}
    if title is not None:
        payload["title"] = title
    response = client.post(
        f"/conversations/{conversation_id}/fork",
        json=payload,
        headers=auth(user),
    )
    assert response.status_code == 201, response.text
    return response.json()


def read_all(
    client: TestClient, user: dict, conversation_id: str, limit: int = 100
) -> list[dict]:
    items: list[dict] = []
    cursor: str | None = None
    for _ in range(1000):
        params: dict = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = client.get(
            f"/conversations/{conversation_id}/messages",
            headers=auth(user),
            params=params,
        )
        assert response.status_code == 200, response.text
        page = response.json()
        items.extend(page["items"])
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
        assert cursor
    else:  # pragma: no cover - guards against an infinite cursor loop
        raise AssertionError("pagination did not terminate")
    return items


def add_member(client: TestClient, actor: dict, conversation_id: str, user: dict):
    return client.post(
        f"/conversations/{conversation_id}/members",
        json={"user_id": user["id"]},
        headers=auth(actor),
    )


def remove_member(client: TestClient, actor: dict, conversation_id: str, user: dict):
    return client.delete(
        f"/conversations/{conversation_id}/members/{user['id']}",
        headers=auth(actor),
    )
