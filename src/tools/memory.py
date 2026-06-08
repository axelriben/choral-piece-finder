"""
remember / recall — user-preference memory tools for the choral-piece-finder agent.

Backed by data/user_memory.json.  Persists structured preferences (key/value)
and free-form facts across sessions.  Both tools are safe to call in a
single-threaded agent loop; no file locking is used.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_MEMORY_PATH = Path(__file__).parent.parent.parent / "data" / "user_memory.json"

REMEMBER_TOOL_SPEC = {
    "name": "remember",
    "description": (
        "Stores a fact or preference about the user for use across sessions. "
        "Use when the user shares persistent information (their choir, their "
        "voicing preferences, their typical repertoire interests, an upcoming "
        "concert theme)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {
                "type": "string",
                "enum": ["preference", "fact"],
                "description": (
                    "Preferences are structured (key/value); "
                    "facts are free-form sentences."
                ),
            },
            "key": {
                "type": "string",
                "description": (
                    "For preferences only: the preference key "
                    "(e.g., 'default_voicing'). Ignored for facts."
                ),
            },
            "value": {
                "type": "string",
                "description": "The preference value or the fact text.",
            },
        },
        "required": ["kind", "value"],
    },
}

RECALL_TOOL_SPEC = {
    "name": "recall",
    "description": (
        "Returns what the agent knows about the user from previous sessions. "
        "Should be called at the start of a conversation to personalize "
        "subsequent answers. Returns preferences (structured) and facts (free-form)."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

# Expose a combined TOOL_SPEC for tools that iterate over a single spec per module.
# Callers that need both specs individually should import REMEMBER_TOOL_SPEC /
# RECALL_TOOL_SPEC directly.
TOOL_SPEC = REMEMBER_TOOL_SPEC


_DEFAULTS: dict = {
    "preferences": {},
    "facts": [],
    "last_updated": None,
}


def _load() -> dict:
    """Load memory file, returning defaults if absent or malformed."""
    if not _MEMORY_PATH.exists():
        return {**_DEFAULTS, "preferences": {}, "facts": []}
    try:
        data = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
        data.setdefault("preferences", {})
        data.setdefault("facts", [])
        return data
    except (ValueError, OSError):
        return {**_DEFAULTS, "preferences": {}, "facts": []}


def _save(data: dict) -> None:
    """Write memory to disk atomically (write to tmp, rename)."""
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    _MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _MEMORY_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(_MEMORY_PATH)


def remember(kind: str, value: str, key: str | None = None) -> dict:
    """Store a preference or fact in the persistent memory file.

    For *kind* = 'preference', *key* must be provided; the entry is stored
    as ``preferences[key] = value``, overwriting any previous value.
    For *kind* = 'fact', the *value* string is appended to the facts list;
    exact duplicates are silently ignored.

    Returns {"stored": True, "kind": kind, ...} on success.
    """
    data = _load()

    if kind == "preference":
        if not key:
            return {"stored": False, "error": "key is required for kind='preference'"}
        data["preferences"][key] = value
        _save(data)
        return {"stored": True, "kind": "preference", "key": key, "value": value}

    if kind == "fact":
        if value not in data["facts"]:
            data["facts"].append(value)
            _save(data)
            return {"stored": True, "kind": "fact", "value": value}
        return {"stored": True, "kind": "fact", "value": value, "note": "already known"}

    return {"stored": False, "error": f"unknown kind: {kind!r}"}


def recall() -> dict:
    """Return the full memory contents (preferences + facts).

    Returns a dict with ``preferences`` (dict), ``facts`` (list of strings),
    and ``last_updated`` (ISO timestamp or null).  Safe to call even if no
    memory has been written yet.
    """
    return _load()
