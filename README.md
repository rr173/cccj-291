# Branched conversations service

A small multi-user conversation service built with **Python + FastAPI + SQLite**.
It supports "fork the discussion from here", per-conversation membership, and
author-driven retraction that takes effect in every branch that inherited the
message. Everything is HTTP; there is no chat UI and no push provider.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# run the service
.venv/bin/python -m uvicorn app.main:app --port 8000
# interactive docs at http://127.0.0.1:8000/docs

# end-to-end multi-user demo incl. a real server restart
.venv/bin/python scripts/demo.py

# test suite
.venv/bin/python -m pytest -q
```

The database file defaults to `data/conversations.sqlite3`; override with the
`CONVERSATIONS_DB` environment variable.

Identity is a plain `X-User-Id` header (register a user with `POST /users`).
There is no password/token provider — this is a local, reproducible service.

## Model

| Concept | Storage |
| --- | --- |
| User | `users` |
| Conversation | `conversations`, including `parent_conversation_id`, `fork_point_message_id`, `fork_point_position`, `inherited_message_count` |
| Message (original, stored once) | `messages` — id, origin conversation + position, author, body, retraction marker |
| A conversation's ordered view | `conversation_messages (conversation_id, position, message_id)` — references only |
| Explicit membership | `memberships`, unique per (conversation, user) |
| Commit-ordered audit | `events`, auto-increment `seq` |

A fork copies **references**, never content. That single decision gives the
required semantics:

* inherited messages keep the **same original message id and author**, with their
  position in the new branch and an `origin` object pointing back at the
  conversation and position where the message was actually authored;
* content added to the parent after the fork is not in the child, and child
  appends never flow back;
* retraction flips one canonical row, so no descendant (including a
  great-grandchild) can hold a private copy to leak; the ordered placeholder
  remains at its position in every branch;
* retracted text is `NULL` in storage and filtered from reads/search. The
  database itself rejects resurrecting a body, deleting a message, or changing
  its author/origin (`messages_*` triggers), and writes use
  `secure_delete` + a WAL checkpoint so the text does not linger.

### Membership

Membership is strictly per conversation. A child conversation starts with only
the user who forked it; parent members get no automatic access. A child member
may see the inherited prefix without belonging to any ancestor. Only the owner
(role `owner`, assigned to the forker/creator) may invite or remove members, and
the owner cannot be removed.

### Ordering and races

Every write runs in its own connection inside `BEGIN IMMEDIATE`, so sequence
allocation, fork materialisation, append and retraction are serialised by
SQLite. Whatever interleaving clients produce, the outcome corresponds to a
serial commit order, which is recorded in `events.seq`. Reads use a deferred
transaction, so one page is read from one snapshot: a page never mixes
pre- and post-retraction states.

`client_token` on append is idempotent per (origin conversation, author) and is
unique in the schema, so retries — even concurrent ones — return the original
message instead of creating a new one.

### Pagination and cursors

Cursors are HMAC-signed opaque tokens. The signing key is persisted in the
database, so cursors keep verifying across process restarts. Each cursor is
bound to endpoint kind, conversation, user, the specific **membership id** and,
for search, the query. Removing a member deletes that membership row:

* while removed, reads are rejected with `403 not_a_member`;
* after re-invitation a *new* membership id is issued, so replaying the old
  cursor yields `400 invalid_cursor`.

Pagination walks the branch's combined inherited + local positions in ascending
order, so there are no gaps or duplicates.

## HTTP API

All conversation routes require `X-User-Id`. Errors are
`{"error": {"code": ..., "message": ...}}`.

### Users and conversations

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/users` | `{display_name}` → user |
| `GET` | `/users/{id}` | |
| `POST` | `/conversations` | `{title}` → conversation; caller becomes sole owner |
| `GET` | `/conversations` | conversations the caller currently belongs to |
| `GET` | `/conversations/{id}` | metadata, fork provenance, members |
| `POST` | `/conversations/{id}/fork` | `{parent_message_id, title?}` → child conversation |

Forking requires the fork point to be currently **visible** in the parent
(inherited or local). A retracted message is still visible as a placeholder, so
forking at it is allowed and inherits only the placeholder. Forking at a message
the conversation does not contain, or that came after its fork point, is
rejected with `400 fork_point_not_visible`.

### Members

| Method | Path | Notes |
| --- | --- | --- |
| `GET` | `/conversations/{id}/members` | members of this conversation only |
| `POST` | `/conversations/{id}/members` | `{user_id}`, owner only; `201` new / `200` existing |
| `DELETE` | `/conversations/{id}/members/{user_id}` | owner only; `204`; invalidates that user's cursors |

### Messages

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/conversations/{id}/messages` | `{body, client_token?}`; `201` created / `200` deduplicated retry |
| `GET` | `/conversations/{id}/messages` | `limit` (1–100), `cursor`; inherited + local, in order |
| `GET` | `/conversations/{id}/messages/{message_id}` | one message through this branch's view |
| `GET` | `/conversations/{id}/search` | `q`, `limit`, `cursor`; case-insensitive, all terms (AND), retracted excluded; cursor requires same `q` |
| `POST` | `/messages/{message_id}/retraction` | author only; idempotent |
| `GET` | `/conversations/{id}/events` | `after_seq`, `limit`; commit-ordered audit for this branch |

A message object looks like:

```json
{
  "message_id": "msg_...",
  "position": 2,
  "source": "inherited",
  "author": {"id": "usr_...", "display_name": "Alice"},
  "origin": {"conversation_id": "cvs_root", "position": 2},
  "body": null,
  "retracted": true,
  "retracted_at": "2026-11-26T03:25:19.622168Z",
  "created_at": "2026-11-26T03:25:19.547696Z"
}
```

`position` is the message's place in the conversation you are reading;
`origin` is the immutable triple of the original utterance. `source` tells
inherited from local.

`POST /messages/{id}/retraction` returns `affected_conversations` and
`forked_at_retracted_point` (branches whose fork point is that message). It is
idempotent; the second call reports `already_retracted: true` and the original
timestamp.

## Tests and demo

* `tests/` — 27 tests covering two-level forks, cross-branch retraction,
  idempotent retries, cursor invalidation, snapshot-consistent paging, race
  outcomes, restart persistence, and storage-level immutability.
* `scripts/demo.py` — starts a real uvicorn server, drives it over HTTP as
  Alice/Bob/Carol/Dave, exercises two fork levels, parent/child divergence,
  cross-branch retraction and old cursors, then **kills and restarts the server
  against the same SQLite file** and re-verifies lineage, retraction, and a
  pre-restart cursor. It prints `N checks passed` and exits non-zero on failure.
