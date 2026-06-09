"""
Tool registry and dispatch for the choral-piece-finder agent.

All seven tools are registered here with their TOOL_SPEC and callable.
The rest of the codebase (agent.py) only imports from this module, keeping
tool discovery and error handling in one place.
"""

import json
import logging
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from tools.search import search_local_index, TOOL_SPEC as SEARCH_SPEC
from tools.details import get_work_details, TOOL_SPEC as DETAILS_SPEC
from tools.score import fetch_score, TOOL_SPEC as SCORE_SPEC
from tools.analyze import analyze_score_features, TOOL_SPEC as ANALYZE_SPEC
from tools.verify import cross_source_verify, TOOL_SPEC as VERIFY_SPEC
from tools.disambiguate import disambiguate_homonyms, TOOL_SPEC as DISAMBIGUATE_SPEC
from tools.memory import (
    remember,
    recall,
    REMEMBER_TOOL_SPEC as TOOL_SPEC_REMEMBER,
    RECALL_TOOL_SPEC as TOOL_SPEC_RECALL,
)

log = logging.getLogger(__name__)


# Filter-boolean parameters where False means "default behavior" (same as
# omitting the parameter).  The LLM often fills these with False as a
# placeholder; stripping them prevents inadvertently inverting the filter.
_FILTER_BOOLEANS_OMIT_IF_FALSE = frozenset({
    "has_free_score",
    "include_non_choral",
})


def _normalize_args(args: dict) -> dict:
    """Cleans up common LLM tool-argument quirks before the tool is called.

    1. Strip empty-string and 'null'/'None' placeholder values.
    2. Detect strings containing literal \\uXXXX escape sequences and
       decode them to actual Unicode characters.
    3. Coerce string 'true'/'false' to booleans.
    4. Coerce numeric strings to integers.
    5. Strip filter-boolean parameters whose value is False — False is the
       default for these switches and the LLM often passes it as a
       placeholder, which would incorrectly invert the filter.
    """
    out = {}
    for key, value in args.items():
        if value in (None, "", "null", "None"):
            continue
        if isinstance(value, str) and "\\u" in value:
            try:
                value = value.encode("utf-8").decode("unicode_escape")
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass
        if isinstance(value, str) and value.lower() in ("true", "false"):
            value = value.lower() == "true"
        if isinstance(value, str) and value.lstrip("-").isdigit():
            try:
                value = int(value)
            except ValueError:
                pass
        if key in _FILTER_BOOLEANS_OMIT_IF_FALSE and value is False:
            continue
        out[key] = value
    return out


# ---------------------------------------------------------------------------
# Registry: tool_name → (callable, spec_dict)
# ---------------------------------------------------------------------------

REGISTRY: dict[str, tuple] = {
    "search_local_index":     (search_local_index,     SEARCH_SPEC),
    "get_work_details":       (get_work_details,        DETAILS_SPEC),
    "fetch_score":            (fetch_score,             SCORE_SPEC),
    "analyze_score_features": (analyze_score_features,  ANALYZE_SPEC),
    "cross_source_verify":    (cross_source_verify,     VERIFY_SPEC),
    "disambiguate_homonyms":  (disambiguate_homonyms,   DISAMBIGUATE_SPEC),
    "remember":               (remember,                TOOL_SPEC_REMEMBER),
    "recall":                 (recall,                  TOOL_SPEC_RECALL),
}


def all_tool_specs() -> list[dict]:
    """Return all tool specs in OpenAI function-calling format."""
    return [
        {"type": "function", "function": spec}
        for _, spec in REGISTRY.values()
    ]


def dispatch(tool_name: str, arguments: dict) -> dict:
    """Look up *tool_name*, call it with **arguments, return result.

    On any exception, returns {"error": "<ExceptionClass>: <message>"}
    so the LLM can react rather than the agent loop crashing.
    """
    entry = REGISTRY.get(tool_name)
    if entry is None:
        log.warning("dispatch: unknown tool %r", tool_name)
        return {"error": f"UnknownTool: '{tool_name}' is not registered"}

    fn, _ = entry
    arguments = _normalize_args(arguments)
    arg_summary = json.dumps(arguments, ensure_ascii=False)[:120]
    log.info("→ %s(%s)", tool_name, arg_summary)

    t0 = time.monotonic()
    try:
        result = fn(**arguments)
        elapsed = time.monotonic() - t0
        size = len(json.dumps(result, ensure_ascii=False, default=str))
        log.info("← %s OK  %.2fs  %d bytes", tool_name, elapsed, size)
        return result
    except Exception as exc:
        elapsed = time.monotonic() - t0
        log.warning("← %s ERROR  %.2fs  %s: %s", tool_name, elapsed, type(exc).__name__, exc)
        return {"error": f"{type(exc).__name__}: {exc}"}
