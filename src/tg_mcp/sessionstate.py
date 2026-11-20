"""Keeps Telegram's update position across restarts, so missed events are caught up.

StringSession stores only the login key. This adds the update counters and the minimum
of cached entities that catching up needs: no names, usernames or phone numbers.
"""

from datetime import UTC, datetime
from typing import Any

from telethon import types, utils
from telethon.sessions import StringSession


def capture(session: StringSession) -> dict[str, Any]:
    states = {}
    for entity_id, state in session.get_update_states():
        states[str(entity_id)] = [state.pts, state.qts, int(state.date.timestamp()), state.seq]

    channel_ids = {int(entity_id) for entity_id in states if entity_id != "0"}
    wanted = {0} | {utils.get_peer_id(types.PeerChannel(channel_id)) for channel_id in channel_ids}
    self_row = next((row for row in session._entities if row[0] == 0), None)
    if self_row is not None:
        wanted.add(self_row[1])  # Telethon stores its own user id as the hash of row 0.
    entities = [[row[0], row[1]] for row in session._entities if row[0] in wanted]
    return {"states": states, "entities": entities}


def restore(session_string: str, saved: dict[str, Any] | None) -> StringSession:
    session = StringSession(session_string)
    if not saved:
        return session
    for entity_id, (pts, qts, date, seq) in saved.get("states", {}).items():
        session.set_update_state(
            int(entity_id), types.updates.State(pts, qts, datetime.fromtimestamp(date, UTC), seq, 0)
        )
    for entity_id, access_hash in saved.get("entities", []):
        session._entities.add((entity_id, access_hash, None, None, None))
    return session
