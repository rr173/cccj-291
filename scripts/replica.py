"""A reference desktop replica: applies server batches atomically.

This is deliberately dependant on nothing but the JSON the server returns. It
is used by the verifier and by tests to prove that the batches alone are enough
to reconstruct the same view the normal message API serves.

Local state per conversation::

    {
        "membership_id": "mem_...",     # identity the copy was loaded under
        "granted_seq": 42,              # the grant item's seq
        "history_max_position": 5,      # grant watermark
        "next_position": 6,             # next history position still to load
        "messages": {position: message_json},
        "cleared": bool,
    }

``next_position > history_max_position`` means the initial load is complete.
The device also stores the server ``next_cursor`` and applies it only after the
whole batch has been written, which is what makes a retry safe.
"""

from __future__ import annotations

import copy
from typing import Any


class Replica:
    def __init__(self) -> None:
        self.conversations: dict[str, dict[str, Any]] = {}
        self.cursor: str | None = None
        self.applied_batches = 0
        self.applied_items = 0
        self.last_cleared: list[dict[str, Any]] = []

    # -- application -----------------------------------------------------

    def apply_batch(self, batch: dict[str, Any]) -> None:
        """Apply one server response, or nothing at all.

        Build the whole next state copy first so a caller can treat this as a
        single transaction; the real client wraps it in one SQLite/transaction.
        """
        next_conversations = copy.deepcopy(self.conversations)
        cleared: list[dict[str, Any]] = []

        for item in batch["items"]:
            kind = item["type"]
            cid = item["conversation_id"]
            if kind == "conversation.granted":
                existing = next_conversations.get(cid)
                if (
                    existing is None
                    or existing["membership_id"] != item["membership_id"]
                ):
                    # A grant under a new identity starts from nothing: any
                    # copy loaded under the old identity is discarded and the
                    # history is loaded again at the new watermark.
                    next_conversations[cid] = {
                        "membership_id": item["membership_id"],
                        "granted_seq": item["seq"],
                        "conversation": item["conversation"],
                        "history_max_position": item["history_max_position"],
                        "next_position": 1,
                        "messages": {},
                    }
                continue

            if kind == "conversation.cleared":
                target = next_conversations.get(cid)
                if target is not None and target["membership_id"] == item[
                    "membership_id"
                ]:
                    del next_conversations[cid]
                    cleared.append(item)
                continue

            if kind == "message":
                target = next_conversations.get(cid)
                if target is None or target["membership_id"] != item[
                    "membership_id"
                ]:
                    # Message from an identity whose grant we no longer hold:
                    # never let stale data overwrite the current copy.
                    continue
                target["messages"][item["position"]] = item["message"]
                if item["position"] >= target["next_position"]:
                    target["next_position"] = item["position"] + 1
                continue

            raise AssertionError(f"unknown sync item type {kind!r}")

        # Commit only when every item applied.
        self.conversations = next_conversations
        self.cursor = batch["next_cursor"]
        self.applied_batches += 1
        self.applied_items += len(batch["items"])
        if cleared:
            self.last_cleared = cleared

    # -- reading ---------------------------------------------------------

    def messages(self, conversation_id: str) -> list[dict[str, Any]]:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            return []
        return [
            conversation["messages"][position]
            for position in sorted(conversation["messages"])
        ]

    def fully_loaded(self, conversation_id: str) -> bool:
        conversation = self.conversations.get(conversation_id)
        if conversation is None:
            return False
        return (
            conversation["next_position"]
            > conversation["history_max_position"]
        )

    def conversation_ids(self) -> set[str]:
        return set(self.conversations)


def drive(
    client,
    user: dict,
    device_id: str,
    replica: Replica,
    *,
    limit: int | None = None,
    max_batches: int = 200,
) -> int:
    """Pull until caught up, applying every batch. Returns batch count."""
    batches = 0
    while True:
        payload: dict[str, Any] = {"device_id": device_id}
        if replica.cursor is not None:
            payload["cursor"] = replica.cursor
        if limit is not None:
            payload["limit"] = limit
        response = client.post(
            "/sync/pull", json=payload, headers={"X-User-Id": user["id"]}
        )
        assert response.status_code == 200, response.text
        batch = response.json()
        replica.apply_batch(batch)
        batches += 1
        if not batch["has_more"]:
            assert batch["caught_up"] is True
            return batches
        assert batches < max_batches, "sync did not converge"
