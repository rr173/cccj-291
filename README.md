# Branched conversations service

A small multi-user conversation service built with **Python + FastAPI + SQLite**.
It supports "fork the discussion from here", per-conversation membership, and
author-driven retraction that takes effect in every branch that inherited the
message. Everything is HTTP; there is no chat UI and no push provider.

Members can **follow** a conversation and be **mentioned** by an author; both
produce **offline notifications** that devices pick up from a personal inbox
with short-lived, leased claim batches — no external push involved.

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
| Follow instance | `follows`, unique per (conversation, user); deleted on unfollow/removal |
| Notification | `notifications` — one per (message, recipient), `seq` in commit order |
| Why a notification exists | `notification_reasons` — points at a `follow_id` or `membership_id` |
| Claim batch (the credential) | `claim_batches`, unique per (recipient, request_id), with `expires_at` |
| Batch contents | `claim_items` — plus the confirmation marker |
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

### Offline notifications

Any member may **follow** a conversation, and an author may **mention** current
members when appending (`mention_user_ids`; non-members are rejected with
`400 mention_not_member`). A new original message creates one notification per
recipient — followers at that moment plus mentioned members, never the author,
and never two rows for the same (message, recipient). Only the append path
creates notifications: fork inheritance, joining later, and starting to follow
never backfill, and a deduplicated append retry neither adds notifications nor
changes the original recipients.

Each notification keeps its **trigger reasons**. A reason is live only while
its source still exists:

* a *follow* reason dies when that follow instance ends (unfollow, or losing
  membership) — re-following mints a new instance and revives nothing;
* a *mention* reason dies when the membership it was minted from is removed —
  re-inviting mints a new membership and revives nothing;
* retracting the message invalidates all of its notifications at once.

A notification is returnable and confirmable only while at least one reason is
live. Once the last reason lapses, even an already-claimed, unconfirmed
notification can no longer be read or confirmed.

Devices pick up work with **claim batches**: `POST /notifications/claim` with a
per-user unique `request_id` takes the oldest claimable notifications (origin
submission order, skipping items a live lease holds elsewhere) and leases them
to the batch for **30 seconds**. The response carries the conversation, the
original message with its body, and the batch credential (`batch_id`). Within
the lease, `POST /notifications/confirm` confirms items one by one. After
expiry the items become claimable by a *new* `request_id` — which is how a
second device takes over from one that went offline — and the old credential
can no longer confirm them. Retrying a claim with the same `request_id`
replays the original batch filtered to what is still valid, unconfirmed and
still owned by it: no new items, no lease renewal, empty stays empty.
Confirming is idempotent; a replayed confirm changes nothing and returns no
body. Batches, leases and confirmations are plain SQLite rows, so they survive
restarts.


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
| `POST` | `/conversations/{id}/messages` | `{body, client_token?, mention_user_ids?}`; `201` created / `200` deduplicated retry |
| `GET` | `/conversations/{id}/messages` | `limit` (1–100), `cursor`; inherited + local, in order |
| `GET` | `/conversations/{id}/messages/{message_id}` | one message through this branch's view |
| `GET` | `/conversations/{id}/search` | `q`, `limit`, `cursor`; case-insensitive, all terms (AND), retracted excluded; cursor requires same `q` |
| `POST` | `/messages/{message_id}/retraction` | author only; idempotent |
| `GET` | `/conversations/{id}/events` | `after_seq`, `limit`; commit-ordered audit for this branch |

### Follows

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/conversations/{id}/follow` | members only; `201` new / `200` already following |
| `DELETE` | `/conversations/{id}/follow` | `204`, idempotent; ends this follow instance's reasons |
| `GET` | `/conversations/{id}/follow` | `{following, followed_at}` for the caller |

### Notification inbox

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/notifications/claim` | `{request_id, limit?}` → leased batch `{batch_id, expires_at, items[]}`; same `request_id` replays the original batch |
| `POST` | `/notifications/confirm` | `{batch_id, notification_id}` → confirms one item; replay returns no body |

Claim responses contain, per item, the `notification_id`, the origin
`conversation_id`, the original `message` (with body), the still-live
`reasons`, and `created_at`. Confirm errors are `404 batch_not_found` /
`404 notification_not_in_batch` / `403 not_batch_owner` /
`409 claim_expired` / `409 notification_inactive`.


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

* `tests/` — 49 tests covering two-level forks, cross-branch retraction,
  idempotent retries, cursor invalidation, snapshot-consistent paging, race
  outcomes, restart persistence, storage-level immutability, and the
  notification inbox: dual-source reason invalidation (unfollow vs. member
  removal vs. retraction), two devices racing for one inbox, lease expiry and
  re-claim with a fresh credential, and batch/confirmation survival across
  restarts.
* `scripts/demo.py` — starts a real uvicorn server, drives it over HTTP as
  Alice/Bob/Carol/Dave, exercises two fork levels, parent/child divergence,
  cross-branch retraction and old cursors, the follow → mention → claim →
  confirm inbox flow, then **kills and restarts the server against the same
  SQLite file** and re-verifies lineage, retraction, a pre-restart cursor and
  the pre-restart claim batch. It prints `N checks passed` and exits non-zero
  on failure.
