#!/usr/bin/env python3
"""End-to-end multi-user demo against a real HTTP server, with a restart.

Runs the FastAPI app with uvicorn on a free localhost port, exercises it over
plain HTTP as several distinct users, kills the server process, starts a new
one against the same SQLite file, and verifies that lineage, retraction and
cursors survived. No chat UI and no external push provider are involved.

Usage:
    python scripts/demo.py            # uses .venv's uvicorn if available
    python scripts/demo.py --keep-db  # keep the temporary database

The script is self-contained: it creates a temporary database unless
CONVERSATIONS_DB is set.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ---------------------------------------------------------------------------
# Tiny HTTP client (standard library only, so the script has no extra deps)
# ---------------------------------------------------------------------------


class HttpError(Exception):
    def __init__(self, status: int, payload: Any) -> None:
        super().__init__(f"HTTP {status}: {payload!r}")
        self.status = status
        self.payload = payload


class Api:
    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def request(
        self,
        method: str,
        path: str,
        *,
        user: str | None = None,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if user is not None:
            headers["X-User-Id"] = user
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                raw = response.read()
                if not raw:
                    return None
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw) if raw else None
            except json.JSONDecodeError:
                payload = raw.decode("utf-8", "replace")
            raise HttpError(exc.code, payload) from exc

    # Convenience wrappers --------------------------------------------------

    def create_user(self, name: str) -> dict[str, Any]:
        return self.request("POST", "/users", body={"display_name": name})

    def create_conversation(self, user: str, title: str) -> dict[str, Any]:
        return self.request(
            "POST", "/conversations", user=user, body={"title": title}
        )

    def append(
        self,
        user: str,
        conversation_id: str,
        body: str,
        client_token: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"body": body}
        if client_token is not None:
            payload["client_token"] = client_token
        return self.request(
            "POST",
            f"/conversations/{conversation_id}/messages",
            user=user,
            body=payload,
        )

    def fork(
        self,
        user: str,
        conversation_id: str,
        message_id: str,
        title: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            f"/conversations/{conversation_id}/fork",
            user=user,
            body={"parent_message_id": message_id, "title": title},
        )

    def messages(
        self,
        user: str,
        conversation_id: str,
        *,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        query: dict[str, Any] = {"limit": limit}
        if cursor:
            query["cursor"] = cursor
        return self.request(
            "GET",
            f"/conversations/{conversation_id}/messages",
            user=user,
            query=query,
        )

    def all_messages(self, user: str, conversation_id: str, limit: int = 2) -> list:
        items: list[dict[str, Any]] = []
        cursor = None
        while True:
            page = self.messages(user, conversation_id, limit=limit, cursor=cursor)
            items.extend(page["items"])
            if not page["has_more"]:
                return items
            cursor = page["next_cursor"]
            assert cursor is not None

    def search(self, user: str, conversation_id: str, q: str) -> dict[str, Any]:
        return self.request(
            "GET",
            f"/conversations/{conversation_id}/search",
            user=user,
            query={"q": q},
        )

    def add_member(self, user: str, conversation_id: str, member: str) -> Any:
        return self.request(
            "POST",
            f"/conversations/{conversation_id}/members",
            user=user,
            body={"user_id": member},
        )

    def remove_member(self, user: str, conversation_id: str, member: str) -> Any:
        return self.request(
            "DELETE",
            f"/conversations/{conversation_id}/members/{member}",
            user=user,
        )

    def retract(self, user: str, message_id: str) -> dict[str, Any]:
        return self.request("POST", f"/messages/{message_id}/retraction", user=user)


# ---------------------------------------------------------------------------
# Server lifecycle
# ---------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass
class Server:
    process: subprocess.Popen
    port: int
    db_path: str

    @property
    def api(self) -> Api:
        return Api(f"http://127.0.0.1:{self.port}")


def python_executable() -> str:
    venv_python = os.path.join(ROOT, ".venv", "bin", "python")
    return venv_python if os.path.exists(venv_python) else sys.executable


def start_server(db_path: str, log_path: str) -> Server:
    port = free_port()
    env = dict(os.environ)
    env["CONVERSATIONS_DB"] = db_path
    env["PYTHONPATH"] = ROOT + os.pathsep + env.get("PYTHONPATH", "")
    log = open(log_path, "ab")
    process = subprocess.Popen(
        [
            python_executable(),
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=log,
    )
    server = Server(process=process, port=port, db_path=db_path)
    deadline = time.time() + 30
    while time.time() < deadline:
        if process.poll() is not None:
            log.close()
            with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                raise RuntimeError(
                    "server exited during startup:\n" + handle.read()[-4000:]
                )
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=1
            ) as response:
                if response.status == 200:
                    log.close()
                    return server
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.1)
    process.kill()
    log.close()
    raise RuntimeError("server did not become healthy within 30 seconds")


def stop_server(server: Server) -> None:
    if server.process.poll() is None:
        server.process.terminate()
        try:
            server.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.process.kill()
            server.process.wait(timeout=15)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------


CHECKS: list[str] = []
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        CHECKS.append(label)
        print(f"  PASS  {label}")
    else:
        FAILURES.append(label)
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def ids(items: list[dict[str, Any]]) -> list[str]:
    return [item["message_id"] for item in items]


def expect_error(label: str, fn, status: int, code: str | None = None) -> None:
    try:
        fn()
    except HttpError as exc:
        ok = exc.status == status
        if code is not None:
            payload = exc.payload or {}
            ok = ok and payload.get("error", {}).get("code") == code
        check(label, ok, f"got HTTP {exc.status} {exc.payload!r}")
    else:
        check(label, False, "expected an error but the call succeeded")


def banner(title: str) -> None:
    print(f"\n=== {title} ===")


def seed(api: Api) -> dict[str, Any]:
    """Create users, a root conversation, two fork levels, and content."""
    banner("Users and root conversation")
    alice = api.create_user("Alice")
    bob = api.create_user("Bob")
    carol = api.create_user("Carol")
    dave = api.create_user("Dave")
    print(f"  alice={alice['id']}\n  bob={bob['id']}\n  carol={carol['id']}\n  dave={dave['id']}")

    root = api.create_conversation(alice["id"], "Launch plan")
    root_id = root["id"]
    check("creator is the sole owner", [m["role"] for m in root["members"]] == ["owner"])

    r1 = api.append(alice["id"], root_id, "We should launch in May. SECRET: code 1234")
    r2 = api.append(alice["id"], root_id, "Marketing prepares the announcement.")
    r3 = api.append(
        alice["id"], root_id, "Engineering needs two more weeks.", "tok-r3"
    )
    check("root has three local messages", [m["position"] for m in api.all_messages(alice["id"], root_id)] == [1, 2, 3])

    check(
        "retry with same client_token is deduplicated",
        api.append(
            alice["id"], root_id, "Engineering needs two more weeks.", "tok-r3"
        )["message_id"]
        == r3["message_id"],
    )
    check(
        "retry did not create an extra message",
        len(api.all_messages(alice["id"], root_id)) == 3,
    )

    banner("Invite Bob to the root (membership is per conversation)")
    expect_error(
        "uninvited user cannot read",
        lambda: api.messages(bob["id"], root_id),
        403,
        "not_a_member",
    )
    api.add_member(alice["id"], root_id, bob["id"])
    check("invited Bob can read", len(api.all_messages(bob["id"], root_id)) == 3)

    return {
        "alice": alice["id"],
        "bob": bob["id"],
        "carol": carol["id"],
        "dave": dave["id"],
        "root": root_id,
        "r1": r1,
        "r2": r2,
        "r3": r3,
    }


def build_forks(api: Api, s: dict[str, Any]) -> dict[str, Any]:
    banner("Fork from an existing message (level 1)")
    # Fork from r2: the child inherits r1,r2 and nothing after.
    child = api.fork(s["alice"], s["root"], s["r2"]["message_id"], "Marketing branch")
    child_id = child["id"]
    check("child inherits exactly the prefix", ids(api.all_messages(s["alice"], child_id)) == [s["r1"]["message_id"], s["r2"]["message_id"]])
    check("child fork point recorded", child["fork_point"]["message_id"] == s["r2"]["message_id"])
    check("child starts with only the forker", [m["user"]["id"] for m in child["members"]] == [s["alice"]])

    expect_error(
        "Carol cannot read the child yet",
        lambda: api.messages(s["carol"], child_id),
        403,
        "not_a_member",
    )
    api.add_member(s["alice"], child_id, s["carol"])
    check("Carol can read child history she lawfully inherits", len(api.all_messages(s["carol"], child_id)) == 2)
    expect_error(
        "Carol still cannot read the parent",
        lambda: api.messages(s["carol"], s["root"]),
        403,
        "not_a_member",
    )

    banner("Parent keeps talking; child does not see it")
    parent_late = api.append(s["bob"], s["root"], "Bob: the press release is drafted.")
    check("child still has only its two inherited messages", ids(api.all_messages(s["carol"], child_id)) == [s["r1"]["message_id"], s["r2"]["message_id"]])

    banner("Child appends locally; parent does not see it")
    c1 = api.append(s["carol"], child_id, "Carol: I will handle the press list.")
    c2 = api.append(s["carol"], child_id, "Carol: press list is ready.")
    parent_ids = ids(api.all_messages(s["alice"], s["root"]))
    check("child messages do not flow back to parent", c1["message_id"] not in parent_ids and c2["message_id"] not in parent_ids)
    check("child positions are inherited prefix + local", [m["position"] for m in api.all_messages(s["carol"], child_id)] == [1, 2, 3, 4])
    check(
        "inherited messages keep original id and author",
        api.all_messages(s["carol"], child_id)[0]["message_id"] == s["r1"]["message_id"]
        and api.all_messages(s["carol"], child_id)[0]["author"]["id"] == s["alice"],
    )

    banner("Fork from the fork (level 2), by a non-author member")
    # Carol forks the child at c1 (a child-local message).
    grandchild = api.fork(s["carol"], child_id, c1["message_id"], "Press branch")
    grandchild_id = grandchild["id"]
    check("grandchild inherits 3 messages", ids(api.all_messages(s["carol"], grandchild_id)) == [
        s["r1"]["message_id"],
        s["r2"]["message_id"],
        c1["message_id"],
    ])
    check("grandchild fork provenance points at the child", grandchild["parent_conversation_id"] == child_id)
    check(
        "grandchild sees original message ids, not copies",
        api.all_messages(s["carol"], grandchild_id)[0]["message_id"] == s["r1"]["message_id"],
    )

    api.add_member(s["carol"], grandchild_id, s["dave"])
    check("Dave can read the grandchild's inherited history", len(api.all_messages(s["dave"], grandchild_id)) == 3)
    expect_error(
        "Dave cannot read the intermediate child",
        lambda: api.messages(s["dave"], child_id),
        403,
        "not_a_member",
    )
    expect_error(
        "Dave cannot read the root",
        lambda: api.messages(s["dave"], s["root"]),
        403,
        "not_a_member",
    )
    g1 = api.append(s["dave"], grandchild_id, "Dave: schedule the embargo.")
    check("grandchild local append", g1["position"] == 4)
    check("child does not see grandchild append", g1["message_id"] not in ids(api.all_messages(s["carol"], child_id)))

    return {
        "child": child_id,
        "grandchild": grandchild_id,
        "parent_late": parent_late,
        "c1": c1,
        "c2": c2,
        "g1": g1,
    }


def old_cursor_scenario(api: Api, s: dict[str, Any], f: dict[str, Any]) -> dict[str, Any]:
    banner("Pagination and old cursors")
    # Paginate the grandchild one message at a time: inherited + local, no gaps.
    all_items: list[dict[str, Any]] = []
    cursor = None
    pages = 0
    while True:
        page = api.messages(s["dave"], f["grandchild"], limit=1, cursor=cursor)
        all_items.extend(page["items"])
        pages += 1
        if not page["has_more"]:
            break
        cursor = page["next_cursor"]
    check("paged read has no gaps or duplicates", ids(all_items) == [s["r1"]["message_id"], s["r2"]["message_id"], f["c1"]["message_id"], f["g1"]["message_id"]])
    check("paged read uses one message per page", pages == 4)

    # Issue a cursor for Dave and then remove him.
    first_page = api.messages(s["dave"], f["grandchild"], limit=1)
    stale_cursor = first_page["next_cursor"]
    api.remove_member(s["carol"], f["grandchild"], s["dave"])
    expect_error(
        "removed member is denied",
        lambda: api.messages(s["dave"], f["grandchild"], cursor=stale_cursor),
        403,
        "not_a_member",
    )
    api.add_member(s["carol"], f["grandchild"], s["dave"])
    expect_error(
        "re-added member cannot replay old cursor",
        lambda: api.messages(s["dave"], f["grandchild"], cursor=stale_cursor),
        400,
        "invalid_cursor",
    )

    # Cursor is bound to its user.
    alice_page = api.messages(s["alice"], f["child"], limit=1)
    expect_error(
        "cursor issued to one member cannot be used by another member",
        lambda: api.messages(s["carol"], f["child"], cursor=alice_page["next_cursor"]),
        400,
        "invalid_cursor",
    )

    # Tampered cursor is rejected.
    tampered = alice_page["next_cursor"][:-2] + ("AA" if not alice_page["next_cursor"].endswith("AA") else "BB")
    expect_error(
        "tampered cursor is rejected",
        lambda: api.messages(s["alice"], f["child"], cursor=tampered),
        400,
        "invalid_cursor",
    )
    return {"stale_cursor": stale_cursor}


def retraction_scenario(api: Api, s: dict[str, Any], f: dict[str, Any]) -> dict[str, Any]:
    banner("Cross-branch retraction")
    hits = api.search(s["dave"], f["grandchild"], "SECRET")
    check("retracted text is searchable before retraction", [i["message_id"] for i in hits["items"]] == [s["r1"]["message_id"]])

    detail = api.request(
        "GET",
        f"/conversations/{f['grandchild']}/messages/{s['r1']['message_id']}",
        user=s["dave"],
    )
    check(
        "single-message read reports the original origin, not the branch",
        detail["origin"] == {"conversation_id": s["root"], "position": 1}
        and detail["source"] == "inherited",
    )

    # A branch whose fork point is the very message that will be retracted.
    pivot = api.fork(s["dave"], f["grandchild"], s["r1"]["message_id"], "Pivot branch")
    check(
        "pivot branch inherits just the later-to-be-retracted message",
        ids(api.all_messages(s["dave"], pivot["id"])) == [s["r1"]["message_id"]],
    )

    expect_error(
        "non-author cannot retract",
        lambda: api.retract(s["bob"], s["r1"]["message_id"]),
        403,
        "not_message_author",
    )
    result = api.retract(s["alice"], s["r1"]["message_id"])
    check("retraction reports origin", result["origin"] == {"conversation_id": s["root"], "position": 1})
    check(
        "retraction spans every branch that inherited it",
        result["affected_conversations"] == 4,
    )
    check(
        "retraction reports descendant fork points",
        {d["conversation_id"] for d in result["forked_at_retracted_point"]}
        == {pivot["id"]},
    )

    for label, user, conv in (
        ("root", s["alice"], s["root"]),
        ("child", s["carol"], f["child"]),
        ("grandchild", s["dave"], f["grandchild"]),
    ):
        items = api.all_messages(user, conv)
        target = next((i for i in items if i["message_id"] == s["r1"]["message_id"]), None)
        check(f"{label} keeps the retracted placeholder", target is not None and target["retracted"] and target["body"] is None)
        check(f"{label} keeps the placeholder's author", target is not None and target["author"]["id"] == s["alice"])
        check(f"{label} keeps positions unchanged", [i["position"] for i in items] == list(range(1, len(items) + 1)))
        check(f"{label} search cannot find the text", api.search(user, conv, "SECRET")["items"] == [])

    # New forks on either side cannot recover it.
    fresh = api.fork(s["dave"], f["grandchild"], f["g1"]["message_id"], "Post-retraction branch")
    fresh_items = api.all_messages(s["dave"], fresh["id"])
    placeholder = next(i for i in fresh_items if i["message_id"] == s["r1"]["message_id"])
    check("a new fork inherits only the placeholder", placeholder["retracted"] and placeholder["body"] is None)
    check("a new fork cannot recover text via search", api.search(s["dave"], fresh["id"], "code 1234")["items"] == [])

    again = api.retract(s["alice"], s["r1"]["message_id"])
    check("retraction is idempotent", again["already_retracted"] is True and again["retracted_at"] == result["retracted_at"])

    # Child-side audit log explains the retraction without needing parent access.
    child_events = api.request(
        "GET", f"/conversations/{f['child']}/events", user=s["carol"]
    )
    retraction_events = [e for e in child_events if e["kind"] == "message.retracted"]
    check(
        "retraction is audited inside the child branch",
        any(e["message_id"] == s["r1"]["message_id"] for e in retraction_events),
    )

    return {"fresh": fresh["id"], "retracted_at": result["retracted_at"]}


def verify_after_restart(api: Api, state: dict[str, Any]) -> None:
    banner("After restart: lineage, membership, retraction, cursors")
    s, f = state["seed"], state["forks"]

    child_items = api.all_messages(s["carol"], f["child"])
    check("child lineage survives restart", ids(child_items) == [s["r1"]["message_id"], s["r2"]["message_id"], f["c1"]["message_id"], f["c2"]["message_id"]])
    check("fork provenance survives restart", api.request("GET", f"/conversations/{f['child']}", user=s["alice"])["parent_conversation_id"] == s["root"])
    check("retracted body is still gone", child_items[0]["retracted"] and child_items[0]["body"] is None)
    check("retracted text is still unsearchable", api.search(s["carol"], f["child"], "SECRET")["items"] == [])
    check("grandchild lineage survives restart", ids(api.all_messages(s["dave"], f["grandchild"]))[:3] == [s["r1"]["message_id"], s["r2"]["message_id"], f["c1"]["message_id"]])
    check("post-retraction branch survives restart", len(api.all_messages(s["dave"], state["retraction"]["fresh"])) == 4)
    check("new appends still work after restart", api.append(s["carol"], f["child"], "after restart")["position"] == 5)

    # A cursor minted before the restart still verifies and continues correctly.
    pre = state["pre_restart_cursor"]
    page = api.messages(s["carol"], f["child"], limit=1, cursor=pre)
    check("pre-restart cursor still verifies", [i["position"] for i in page["items"]] == [2])
    check("pre-restart cursor continues the same sequence", page["items"][0]["message_id"] == s["r2"]["message_id"])

    # Removal still invalidates an old cursor after restart.
    api.remove_member(s["alice"], f["child"], s["carol"])
    expect_error(
        "removal after restart invalidates old cursor",
        lambda: api.messages(s["carol"], f["child"], cursor=pre),
        403,
        "not_a_member",
    )
    api.add_member(s["alice"], f["child"], s["carol"])
    expect_error(
        "re-invite after restart cannot replay the old cursor",
        lambda: api.messages(s["carol"], f["child"], cursor=pre),
        400,
        "invalid_cursor",
    )

    # Authorship of inherited messages is still the original user.
    author = api.all_messages(s["carol"], f["child"])[0]["author"]
    check("original author preserved after restart", author["id"] == s["alice"] and author["display_name"] == "Alice")
    check("origin triple is stable", api.all_messages(s["carol"], f["child"])[0]["origin"] == {"conversation_id": s["root"], "position": 1})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-db", action="store_true", help="keep the temp DB")
    args = parser.parse_args()

    tempdir = tempfile.mkdtemp(prefix="conversations-demo-")
    db_path = os.environ.get("CONVERSATIONS_DB", os.path.join(tempdir, "demo.sqlite3"))
    log_path = os.path.join(tempdir, "server.log")
    print(f"database: {db_path}")
    print(f"server log: {log_path}")

    server = start_server(db_path, log_path)
    print(f"server: http://127.0.0.1:{server.port} (pid {server.process.pid})")
    state: dict[str, Any] = {}
    try:
        api = server.api
        print(f"\nhealth: {api.request('GET', '/health')}")
        state["seed"] = seed(api)
        state["forks"] = build_forks(api, state["seed"])
        state["pre_restart_cursor"] = api.messages(
            state["seed"]["carol"], state["forks"]["child"], limit=1
        )["next_cursor"]
        state["cursors"] = old_cursor_scenario(api, state["seed"], state["forks"])
        state["retraction"] = retraction_scenario(
            api, state["seed"], state["forks"]
        )
        # A long-lived cursor issued before the retraction, used after restart
        # to prove the cursor key and membership binding both persisted.
        state["pre_restart_cursor"] = api.messages(
            state["seed"]["carol"], state["forks"]["child"], limit=1
        )["next_cursor"]
    finally:
        banner("Restarting the server against the same database")
        stop_server(server)
        print(f"  stopped pid; exit code {server.process.returncode}")
        server = start_server(db_path, log_path)
        print(f"  started new process pid {server.process.pid} on port {server.port}")

    try:
        verify_after_restart(server.api, state)
    finally:
        stop_server(server)

    banner("Summary")
    print(f"  {len(CHECKS)} checks passed, {len(FAILURES)} failed")
    if FAILURES:
        for failure in FAILURES:
            print(f"    - {failure}")
        return 1
    if args.keep_db:
        print(f"  kept database at {db_path}")
    else:
        print(f"  temporary files under {tempdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
