"""
Server-Sent Events stream for a live session.

Emits one JSON object per event. The dashboard consumes via EventSource.
Supported event types:
  - state      (governance state transition)
  - sample     (resource sample tick)
  - recommend  (new recommendation)
  - ledger     (raw ledger entry)
  - done       (session ended)
"""

import asyncio
import json

from fastapi import APIRouter, HTTPException
from sse_starlette.sse import EventSourceResponse

from backend.services.session_manager import session_manager

router = APIRouter(prefix="/sessions", tags=["stream"])


def _classify(entry: dict) -> str:
    t = entry.get("event_type", "")
    if t == "state_transition":
        return "state"
    if t == "resource_sample":
        return "sample"
    if t == "recommendation_issued":
        return "recommend"
    return "ledger"


@router.get("/{session_id}/stream")
async def stream(session_id: str):
    rt = session_manager.get(session_id)
    if not rt:
        from backend.evidence.archive import read_entries
        archived = read_entries(session_id)
        if archived is None:
            raise HTTPException(404, "session not found")

        async def replay():
            for entry in archived:
                yield {"event": _classify(entry), "data": json.dumps(entry, separators=(",", ":"))}
            yield {"event": "done", "data": json.dumps({"session_id": session_id, "archived": True})}
        return EventSourceResponse(replay())

    async def event_generator():
        sent_hashes: set[str] = set()
        # First, replay everything already in the ledger so late-joining
        # clients see the full history.
        for entry in rt.ledger.read_all():
            h = entry.get("entry_hash", "")
            if h in sent_hashes:
                continue
            sent_hashes.add(h)
            yield {
                "event": _classify(entry),
                "data": json.dumps(entry, separators=(",", ":")),
            }

        # Then tail the ledger until the session stops.
        while not rt.closed:
            for entry in rt.ledger.read_all():
                h = entry.get("entry_hash", "")
                if h in sent_hashes:
                    continue
                sent_hashes.add(h)
                yield {
                    "event": _classify(entry),
                    "data": json.dumps(entry, separators=(",", ":")),
                }
            await asyncio.sleep(0.4)

        # Final drain
        for entry in rt.ledger.read_all():
            h = entry.get("entry_hash", "")
            if h in sent_hashes:
                continue
            sent_hashes.add(h)
            yield {
                "event": _classify(entry),
                "data": json.dumps(entry, separators=(",", ":")),
            }
        yield {"event": "done", "data": json.dumps({"session_id": session_id})}

    return EventSourceResponse(event_generator())