# Branched conversations service

A small multi-user conversation service built with **Python + FastAPI + SQLite**.
It supports "fork the discussion from here", per-conversation membership, and
author-driven retraction that takes effect in every branch that inherited the
message. Everything is HTTP; there is no chat UI and no push provider.

Members can **follow** a conversation and be **mentioned** by an author; both
produce **offline notifications** that devices pick up from a personal inbox
with short-lived, leased claim batches — no external push involved.

Child members can also turn a supported branch discussion into an atomic
**resolution package** for its direct parent. The parent owner reviews only the
proposed topics, public wording and dependencies, then approves the whole
package at once. A parent **resolution board** shows the current resolution for
each topic and moves topics plus downstream dependents to reconsideration when
evidence or pinned dependency versions lapse.

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
| Desktop replica change log | `sync_changes` — one row per user-visible change, fanned out to each observing membership instance |
| Resolution proposal/package | `resolution_proposals`, `resolution_proposal_items`, evidence and dependency pins |
| Passed resolution versions | `resolutions`, `resolution_evidence`, `resolution_dependencies` |
| Current parent board pointer | `resolution_board` — effective or `pending_reconsideration` per `(parent, topic)` |
| Idempotent approval result | `resolution_approvals`, including the stored original response snapshot |
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

### Resolution boards and cross-branch approval

A branch can propose formal wording to its **direct parent** without forwarding
or exposing the branch chat. A proposal is an all-or-nothing package:

* each item identifies a `topic_key` and contains `public_text`;
* each item cites one or more evidence messages; every cited message must be a
  **local original of the child conversation**, not an inherited message, and
  must be unretracted at submission;
* `depends_on_topic_keys` may name other topics. A topic present in the same
  package is an **internal** dependency and is pinned to the new package
  version. Any other dependency is **external** and is pinned to the resolution
  version effective in the parent when the proposal is submitted;
* duplicate topics, self/cyclic dependencies, invalid evidence and missing
  effective external premises are rejected at submission.

The proposal stores the proposer's exact child `membership_id`. Removing the
proposer deletes that row; being invited back mints a new id, so a
leave-and-rejoin proposer cannot cure a stale proposal.

Review is deliberately narrow. The parent owner sees topics, public wording and
dependency pins, but the API never serializes evidence message ids or bodies.
If a pending proposal's evidence is retracted, its `public_text` becomes `null`
and `text_visible` becomes `false`; approval then fails with `409 basis_retracted`.

Approval is owner-only and atomic. Under one `BEGIN IMMEDIATE` transaction the
service rechecks:

1. the submission-time child membership id still exists;
2. every evidence message is still unretracted;
3. each package topic is still at its submission base and, if it was effective
   at submission, has not become `pending_reconsideration`;
4. every external dependency is still the exact effective version pinned at
   submission;
5. applying the package would not cascade-invalidate one of those external
   premises. This catches the “old A depends on old B, but this package would
   replace B while relying on old A” case with
   `external_premise_would_lapse`.

Any failed check rolls the whole package back. Unrelated topics do not block.
The first successful approval returns `201`; retrying the same approval returns
`200` with the same `approval_id`, resolution ids and stored result snapshot
(`already_approved: true`) and writes no new resolution versions. Concurrent
approvals are serialized by SQLite; exactly one creates the package.

The board is scoped to one parent conversation. Each topic has at most one
current pointer. An effective topic contains the latest passed version and its
exact dependency pins. When evidence is retracted, that topic becomes
`pending_reconsideration`; when a pinned dependency version is replaced or
becomes pending, direct and transitive dependents become pending too. Pending
topics serialize `resolution: null`, so neither the old wording nor earlier
versions are echoed. Unrelated topics stay unchanged. Posting the same chat
text again creates a new message but never automatically restores an old
resolution. Approval itself survives the proposer later leaving the child.

All proposal, approval and board tables are additive. Opening an old database
runs `CREATE TABLE/INDEX IF NOT EXISTS`; existing conversations, messages,
memberships and notification rows are not rebuilt or deleted.

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


### Desktop replica sync

A desktop client keeps a local copy of the chats of one account. It does not
re-download history on every launch. Instead it starts from an empty copy once
and thereafter pulls only changes. The server keeps no per-device state: every
user-visible change is appended to `sync_changes` when it commits, fanned out to
each membership instance that can see it, and a pull is an indexed walk of that
account's rows. The incremental path never diffs or scans the message table
(only a one-time history load for a conversation a device just received, bounded
by its grant's `history_max_position`).

Capture is inside the same write transaction as the change itself:

| Change | Sync row(s) |
| --- | --- |
| membership created (create, fork, invite) | `membership.granted` with the branch's current `max_position` |
| message appended | `message.appended` for every **current member instance** of that conversation |
| message retracted | `message.retracted` for every membership of **every branch** that references the message — the author's own branches are irrelevant |
| membership removed | `membership.revoked` for the removed instance |

`POST /sync/pull` takes `{device_id, cursor?, limit?}` and returns one batch:

```json
{
  "items": [
    {"type": "conversation.granted", "conversation_id": "cvs_…", "membership_id": "mem_…",
     "history_max_position": 7, "conversation": {…}, "seq": 41},
    {"type": "message", "conversation_id": "cvs_…", "membership_id": "mem_…",
     "position": 1, "message": {…same shape as GET messages…}, "seq": null},
    {"type": "conversation.cleared", "conversation_id": "cvs_…", "membership_id": "mem_…",
     "reason": "membership_revoked", "seq": 45}
  ],
  "has_more": true,
  "caught_up": false,
  "next_cursor": "…signed, bound to account AND device…",
  "progress": {"after_seq": 41, "loading_conversation_id": "cvs_…"}
}
```

Item semantics:

* **`conversation.granted`** — the identity `membership_id` may read this
  conversation. Its messages follow as `message` items for positions
  `1..history_max_position`, possibly over several batches. On a *new*
  `membership_id`, discard any local copy loaded under an older one and reload.
* **`message`** — a message at its branch position, identical to the message
  API: same original id, author, origin, `source`, and a retracted message is a
  placeholder (`body: null`). Upsert by `(conversation_id, position)`. The body
  is read in the same snapshot as the rest of the batch.
* **`conversation.cleared`** — delete this conversation's local copy, but only
  if it was loaded under this `membership_id`. Other conversations are
  untouched, even when they hold the same original message. A late clear for an
  old identity cannot delete a newer identity's copy.

Client protocol:

1. Start with no cursor and an empty replica.
2. `POST /sync/pull`. Apply the whole `items` list and *then* save
   `next_cursor`, in one local transaction (or build the next state and swap it
   in). If applying fails, keep the old cursor.
3. Repeat while `has_more` is true. `caught_up == true` (equivalently
   `has_more == false`) means the replica now reflects every committed change.
4. On the next launch, send the saved cursor; a caught-up replica usually gets
   an empty batch immediately.

Guarantees that make retries safe:

* **Same response, same snapshot.** One batch read comes from one SQLite
  snapshot; every `message` body in it is readable by the account at that
  instant, and retraction state is already applied.
* **Retry from the old cursor.** Progress is a position in the append-only log,
  not a watermark on rows. A lost response is retried from the last *applied*
  cursor: that sequence range is recomputed against a fresh snapshot, so no
  change is skipped and a message retracted meanwhile arrives as a placeholder,
  never with its old text.
* **Stale rows are never sent.** An append/retraction row whose membership
  instance has since been removed is skipped by the server (progress still
  advances), so unreadable bodies are not re-sent.
* **Identity cannot leak across a re-invite.** A re-invite mints a new
  `membership_id`; its grant and history are separate. A half-finished walk
  under the old identity is abandoned (the revocation clears the old copy) and
  the new identity reloads the branch from its own watermark.
* **Per-device progress.** The cursor is HMAC-signed and bound to the account
  *and* `device_id`; another account or device gets `400 invalid_cursor` and
  cannot advance or reuse it. Pulling on one device never moves another's
  progress.
* **Convergence.** With writes stopped, consecutive pulls advance to empty
  batches in finitely many requests. Each response is bounded even if most
  scanned rows are stale or a grant's history page is short.

Databases created before this feature have no sync log; the first pull by a
user synthesizes grants for their existing memberships at the current
watermark, so the change log stays additive (`CREATE TABLE/INDEX IF NOT
EXISTS`). Notifications, resolution boards and member rosters are not part of
the replica and are not synced.



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

### Resolutions and cross-branch approval

All routes use the same `X-User-Id` authentication. Parent board reads require
current membership in the parent; review queues and approval require the
parent's current owner. The proposer may read their own proposal even if they
are not a parent member.

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/conversations/{child_id}/resolution-proposals` | A current child member submits an atomic package to the child's direct parent. Optional `client_token` gives `201`/`200` idempotent submission. |
| `POST` | `/conversations/{parent_id}/resolution-proposals` | Same operation when the body contains `child_conversation_id`; verifies that the body child is a direct child of the path parent. |
| `POST` | `/resolution-proposals` | Compatibility form requiring `child_conversation_id`; optional `parent_conversation_id` is checked as the direct parent. |
| `GET` | `/conversations/{parent_id}/resolution-proposals?status=pending` | Parent owner only; lists incoming/all proposals. Ordinary parent members receive `403 owner_required`. |
| `GET` | `/resolution-proposals/{proposal_id}` | Proposer or direct-parent owner. Pending items with retracted evidence have `public_text: null`. The same detail/approval operations also exist under `/conversations/{parent_id}/resolution-proposals/{proposal_id}`. |
| `POST` | `/resolution-proposals/{proposal_id}/approve` | Direct-parent owner only; atomically applies the package. First success is `201`, replay is `200` with the original approval result. `/approval` and `/proposals/{id}/approve` are aliases. |
| `GET` | `/conversations/{parent_id}/resolution-board` | Parent members only. Returns `{conversation_id, topics[]}`; `/resolutions` is an alias and also repeats the rows as `resolutions`/`items`. |
| `GET` | `/conversations/{parent_id}/resolution-board/{topic_key}` | Parent members only; reads one board topic. `/conversations/{parent_id}/resolutions/{topic_key}` is an alias. |

Request body:

```json
{
  "client_token": "optional-retry-token",
  "items": [
    {
      "topic_key": "refunds",
      "public_text": "Refunds are allowed for 30 days.",
      "evidence_message_ids": ["msg_child_local_one", "msg_child_local_two"],
      "depends_on_topic_keys": ["safety"]
    },
    {
      "topic_key": "safety",
      "public_text": "Escalate harmful refund requests.",
      "evidence_message_ids": ["msg_child_local_three"],
      "depends_on_topic_keys": ["charter"]
    }
  ]
}
```

In that example `safety` is an internal package dependency, while `charter` is
external and must already be effective in the parent. The approval response
contains `approval_id`, stable `resolution_ids`, versioned `resolutions[]`, and
`invalidated_topics[]` describing the impact radius. The board represents a
pending topic as:

```json
{
  "topic_key": "refunds",
  "status": "pending_reconsideration",
  "resolution": null,
  "pending_reason": "basis_retracted",
  "invalidated_at": "2026-11-26T07:50:00.000000Z"
}
```

`pending_reason` is one of `basis_retracted`, `dependency_replaced`, or
`dependency_pending`. Approval conflicts use `409` with codes such as
`proposer_membership_lapsed`, `basis_retracted`, `topic_updated_elsewhere`,
`topic_pending_reconsideration`, `external_dependency_changed`,
`external_dependency_lapsed`, or `external_premise_would_lapse`.

### Messages

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/conversations/{id}/messages` | `{body, client_token?, mention_user_ids?}`; `201` created / `200` deduplicated retry |
| `GET` | `/conversations/{id}/messages` | `limit` (1–100), `cursor`; inherited + local, in order |
| `GET` | `/conversations/{id}/messages/{message_id}` | one message through this branch's view |
| `GET` | `/conversations/{id}/search` | `q`, `limit`, `cursor`; case-insensitive, all terms (AND), retracted excluded; cursor requires same `q` |
| `POST` | `/messages/{message_id}/retraction` | author only; idempotent |
| `GET` | `/conversations/{id}/events` | `after_seq`, `limit`; commit-ordered audit for this branch |

### Replica sync

| Method | Path | Notes |
| --- | --- | --- |
| `POST` | `/sync/pull` | `{device_id, cursor?, limit?}` → one batch with the next change items; see [Desktop replica sync](#desktop-replica-sync) |

`limit` is 1–500 (default 100) and bounds the number of items in the response.
The cursor is opaque and bound to the account and `device_id`; omitting it
starts from an empty replica. Errors: `400 invalid_cursor` for a cursor from
another account/devic

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

* `tests/` — 81 tests covering two-level forks, cross-branch retraction,
  idempotent retries, cursor invalidation, snapshot-consistent paging, race
  outcomes, restart persistence, storage-level immutability, the
  notification inbox, cross-branch resolution packages (atomic approval,
  parent/proposer authorization, evidence privacy and redaction, submission
  pins, stale/external conflicts, leave-and-rejoin membership identity,
  idempotent approval replay, and retraction/replacement invalidation chains),
  and desktop replica sync (bootstrap, paged history with concurrent writes,
  retraction across branches, identity-scoped clearing, device-bound progress,
  lost-response retry, convergence and restart).
* `tests/test_resolutions.py` and `tests/test_sync.py` can be run alone with
  `.venv/bin/python -m pytest tests/test_sync.py -q`.
* `scripts/verify_resolutions.py` — self-contained HTTP-level narrative check
  using FastAPI `TestClient` against a temporary SQLite file. It
  creates users and branches, approves a package with internal and external
  dependencies, checks review privacy and access boundaries, retracts evidence,
  prints the affected board radius, redacts a pending proposal, restarts a
  second app against the same SQLite file, and replays the approval. Run:

  ```bash
  .venv/bin/python scripts/verify_resolutions.py
  ```

  It prints `N checks passed` and exits non-zero on failure.
* `scripts/verify_sync.py` — self-contained HTTP-level check of the desktop
  replica. It drives `scripts/replica.py` (which applies batches exactly as a
  client would) from empty through paged bootstrap, interrupted walks with
  concurrent appends/retractions/membership changes, a lost response retried
  from old progress, cross-branch retraction, offline remove/re-invite,
  conversation-scoped clearing, per-device cursor binding, convergence to empty
  batches, and a service restart. Every scenario compares the reconstructed
  copy with `GET /conversations/{id}/messages`. Run:

  ```bash
  python3 scripts/verify_sync.py
  ```

  It prints `72 checks passed` and exits non-zero on failure.
* `scripts/demo.py` — starts a real uvicorn server, drives it over HTTP as
  Alice/Bob/Carol/Dave, exercises two fork levels, parent/child divergence,
  cross-branch retraction and old cursors, the follow → mention → claim →
  confirm inbox flow, then **kills and restarts the server against the same
  SQLite file** and re-verifies lineage, retraction, a pre-restart cursor and
  the pre-restart claim batch. It prints `N checks passed` and exits non-zero
  on failure.
