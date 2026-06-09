# music21 octave-displacement limitation in analyze_score_features

## What the tool does

`analyze_score_features` (in `src/tools/analyze.py`) downloads a MusicXML
score for a given work, caches it locally, and uses the
[music21](https://web.mit.edu/music21/) library to compute per-part pitch
ranges, an estimated key, time signatures, measure count, and a rough
duration estimate. The tool is intended to fill in metadata that the CPDL
and SMH catalogs don't provide, particularly per-voice vocal ranges.

## Verification performed

We verified two Palestrina CPDL editions:

**Accepit Iesus calicem a 6** — Pothárn Imre edition (CPDL #56519,
posted 2020-01-03). The MusicXML is a multi-file MXL archive (one
`.musicxml` file per part) generated from a LilyPond source.

**Sicut cervus** — a second independently-tested edition.

Both show the same uniform octave-too-high displacement across all voices.
The table below documents the Accepit Iesus calicem results:

| Part    | music21 reported | Actual (score) | Error            |
|---------|-----------------|----------------|------------------|
| Cantus  | G4–G5           | G3–G4          | +1 octave        |
| Altus   | G3–C5           | G2–C4          | +1 octave        |
| Sextus  | C4–C5           | C3–C4          | +1 octave        |
| Tenor   | F3–A5           | F2–A3          | +1 octave (lower portion) +2 octaves (upper portion) |
| Quintus | G3–G5           | G2–G3          | +1 octave (lower portion) +2 octaves (upper portion) |
| Bassus  | C3–B5           | C2–D3          | +1 octave (lower notes) +3 octaves (top note) |

**The interval span (range in semitones) is correct for every voice in every
edition tested.** Only the absolute octave registration is affected.

## Three categories of error

**1. Uniform octave displacement across all Renaissance editions tested.**
Every voice in both the Accepit Iesus calicem and Sicut cervus editions is
reported one octave too high. The likely cause: modern CPDL editions
commonly use treble-8vb clefs (a treble clef with a small 8 below) for
upper voices and the tenor to improve readability for modern singers. Music21's
interpretation of these clefs appears to be inconsistent — it does not
reliably apply the implied one-octave-down transposition when computing
absolute pitch. This is a known limitation in music21's handling of
`<clef-octave-change>` markers in MusicXML.

**2. Partial additional displacement on 8vb treble-clef parts.** The Tenor
(P4) and Quintus (P5) parts carry a
`<clef-octave-change>-1</clef-octave-change>` marker, indicating that the
clef should be displayed with an 8 below (tenor clef). Music21 appears to
apply this correction inconsistently: the lower portion of each part is
shifted up by the expected single octave, while the upper portion appears
to receive an additional octave shift, suggesting the octave-change is
being applied to some notes but not others during parsing.

**3. Extreme Bassus top-note error.** The Bassus part's highest note is
reported as B5 (MIDI 83), over two octaves above the actual D3. The raw
XML for the Bassus part contains 221 notes in an F-clef with no
octave-change markers; D3 is a plausible bass-voice top note, while B5 is
not. We were unable to identify the source of the B5 in the XML within the
time budgeted for investigation. The most probable explanations are a
chord-tone mis-attribution (music21 counting a pitch from an adjacent
chord layer) or a single stray note in the LilyPond-generated file.

## Why these errors occur

Renaissance polyphony presents several compounding challenges for MusicXML
parsers: treble-8vb clefs used for all upper voices and the tenor (a modern
editorial convention for readability), optional octave-transposition markers
with no standardised convention for whether pitch data represents written or
sounding pitch, and dense six-to-eight voice textures where chord-tone
attribution to the correct part is non-trivial. music21 is a general-purpose
library designed for a broad range of Western art music; it handles these
edge cases less robustly than a tool written specifically for Renaissance
vocal polyphony.

## Scope of the limitation

The uniform octave-too-high displacement has been confirmed across all
Renaissance choral polyphony editions tested (Accepit Iesus calicem and
Sicut cervus, both from CPDL). Both are multi-voice Renaissance works
encoded with treble-8vb clefs for upper voices.

Solo song editions and instrumental editions are not affected by this
bug — the issue is specific to Renaissance choral polyphony where
treble-8vb clefs are used for upper voices. Modern choral editions in
standard clefs (G, C, and F without octave-change markers) also produce
correct results. SMH works have no MusicXML at all and are therefore
unaffected.

## Why we are not addressing this in v1

Correct absolute-pitch recovery from octave-transposing-clef MusicXML would
require either detecting and reversing the encoder's octave convention at
parse time (heuristic, fragile) or parsing the full score-layout context to
infer clef intent (substantial engineering). Neither approach is
straightforward. More importantly, the IR contributions of this project —
cross-source metadata reconciliation, orphaned-media discovery, and
confidence-tiered retrieval — do not depend on correct pitch-range data.
The analyze tool adds value by providing structural information (measure
count, time signature, key estimate, range *span*) even when absolute
octave registration is wrong.

## Path for future work

1. Characterise the error patterns across a broader sample of CPDL editions
   to determine whether the uniform +1 octave shift is consistent (which
   would allow a simple post-hoc correction for Renaissance works).
2. Consider a Music21-independent MXL parser that explicitly resolves
   written-vs-sounding pitch and octave-transposition-clef semantics before
   passing notes to the range-computation logic.
3. Report the inconsistent `<clef-octave-change>` handling (item 2 above)
   upstream to the music21 project with a minimal reproducing example.
