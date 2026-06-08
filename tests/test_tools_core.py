"""
Sanity tests for the three core retrieval tools.

Runnable directly:  python tests/test_tools_core.py
Not a unittest suite — output is intended for human inspection.
"""

import json
import sys
from pathlib import Path

# Allow imports from src/ regardless of working directory
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tools.search import search_local_index
from tools.details import get_work_details
from tools.score import fetch_score


def _dump(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


print("=== Test 1: search by voicing SSAATTBB ===")
_dump(search_local_index(voicing="SSAATTBB", limit=5))

print()
print("=== Test 2: search Palestrina Renaissance Sacred ===")
_dump(search_local_index(
    composer="Palestrina",
    period="Renaissance",
    genre_main="Sacred",
    limit=5,
))

print()
print("=== Test 3: get_work_details for Vårnatt ===")
results = search_local_index(composer="Stenhammar", query="Vårnatt", limit=1)
if results:
    _dump(get_work_details(results[0]["work_id"]))
else:
    print("(no results for Vårnatt search)")

print()
print("=== Test 4: fetch_score for Vårnatt ===")
if results:
    _dump(fetch_score(results[0]["work_id"]))
else:
    print("(no results for Vårnatt search)")

print()
print("=== Test 5: fetch_score for an Alfvén work (in copyright, no free score) ===")
alfven_results = search_local_index(composer="Alfvén", has_free_score=False, limit=1)
if alfven_results:
    _dump(fetch_score(alfven_results[0]["work_id"]))
else:
    print("(no Alfvén results without free score)")
