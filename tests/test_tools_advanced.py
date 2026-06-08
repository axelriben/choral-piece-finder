"""
Sanity tests for the four advanced tools.

Runnable directly:  python tests/test_tools_advanced.py

Tests that require network access (analyze_score_features) will attempt
the download; failures are reported without aborting the rest of the suite.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tools.search import search_local_index
from tools.details import get_work_details
from tools.analyze import analyze_score_features
from tools.verify import cross_source_verify
from tools.disambiguate import disambiguate_homonyms
from tools.memory import remember, recall


def _dump(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Test A: analyze_score_features on a Palestrina work with MusicXML
# ---------------------------------------------------------------------------
print("=== Test A: analyze a Palestrina work with MusicXML ===")
results = search_local_index(composer="Palestrina", limit=20)
target = None
for r in results:
    details = get_work_details(r["work_id"])
    for mf in details.get("media_files", []):
        if mf["format"] in ("MusicXML", "mxl", "xml"):
            target = r["work_id"]
            break
    if target:
        break

if target:
    print(f"(using work_id: {target})")
    _dump(analyze_score_features(target))
else:
    print("No suitable test target found (no MusicXML files in Palestrina set).")

# ---------------------------------------------------------------------------
# Test B: cross_source_verify on Vårnatt (single-source case)
# ---------------------------------------------------------------------------
print()
print("=== Test B: cross_source_verify on Vårnatt ===")
results = search_local_index(composer="Stenhammar", query="Vårnatt", limit=1)
if results:
    _dump(cross_source_verify(results[0]["work_id"]))
else:
    print("(no results for Vårnatt search)")

# ---------------------------------------------------------------------------
# Test C: disambiguate Peterson-Berger Stämning (the homonymy case)
# ---------------------------------------------------------------------------
print()
print("=== Test C: disambiguate Peterson-Berger Stämning ===")
_dump(disambiguate_homonyms(composer="Peterson-Berger", title="Stämning"))

# ---------------------------------------------------------------------------
# Test D: memory — remember + recall
# ---------------------------------------------------------------------------
print()
print("=== Test D: memory ===")
_dump(remember(kind="fact", value="User sings in an SSAATTBB choir in Uppsala."))
_dump(remember(kind="preference", key="default_language", value="Swedish"))
_dump(recall())
