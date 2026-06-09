"""Shared normalization helpers for indexing and search."""

import re
import unicodedata

_VOICING_PREFIXES = (
    "mixed choir:",
    "mixed choir",
    "solo part:",
    "soloists:",
    "children's choir:",
    "male choir:",
    "female choir:",
)


def normalize_voicing(s: str) -> str:
    """Compact uppercase form: strip prefixes, periods, spaces, commas.

    Examples:
        'S.S.A.A.T.T.B.B.'          → 'SSAATTBB'
        'Mixed choir: S.A.T.B.'     → 'SATB'
        'Mixed choir SSAATTBB and piano' → 'MIXEDCHOIRSSAATTBBANDPIANO'
        'SSAA,TTBB,AATB'            → 'SSAATTBBAATB'
        'TTBB'                      → 'TTBB'
    """
    if not s:
        return ""
    lower = s.lower()
    for prefix in _VOICING_PREFIXES:
        if lower.startswith(prefix):
            s = s[len(prefix):].strip(": ").strip()
            break
    s = s.replace(",", " ")
    return "".join(c for c in s if c.isalpha()).upper()


def normalize_title(s: str) -> str:
    """Lowercase, strip diacritics, collapse punctuation to single spaces.

    Examples:
        'Stämning'             → 'stamning'
        'Vårnatt (Lenznacht)'  → 'varnatt lenznacht'
        'Sicut cervus'         → 'sicut cervus'
        'Stemning'             → 'stemning'

    # TODO: v1.1 — handle Scandinavian phonetic equivalence
    #   (ä↔e, å↔aa) for cross-orthography title matching.
    #   E.g., 'Stämning' (modern Swedish) and 'Stemning' (older
    #   Danish/Norwegian spelling Peterson-Berger sometimes used)
    #   should match. Currently they don't — both normalize to their
    #   respective ASCII forms ('stamning' vs 'stemning').
    """
    if not s:
        return ""
    s = s.lower()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())
