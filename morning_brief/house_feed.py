"""The House episodes as podcast-feed entries, shared by the owner's feed and the housemates' feeds."""
from __future__ import annotations

import json

from . import house, house_script


def episodes(conn) -> list[dict]:
    """Newest first, in the shape podcast.build_house_feed expects."""
    out = []
    for row in house.list_episodes(conn):
        script = json.loads(row["script_json"])
        out.append({"date": row["date"], "title": script["title"], "created_at": row["created_at"],
                    "updated_at": row["updated_at"], "audio_bytes": row["audio_bytes"],
                    "duration_s": row["duration_s"], "description": house_script.describe(script)})
    return out
