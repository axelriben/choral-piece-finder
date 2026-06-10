# Task: CPDL crawler for a single composer

## Context
I'm building an AI agent for choral score discovery as part of a university IR
lab. Full design is in DESIGN.md (please read it). This task is the first piece
of the indexing pipeline described in section 7 of that document.

## Goal
Write a Python script at src/crawl_cpdl.py that takes a composer name on the
command line and produces a JSON file at data/cpdl_<composer_slug>.json
containing all of that composer's works as parsed from the Choral Public Domain
Library (CPDL).

## Specific requirements

1. Use the CPDL MediaWiki API at https://www.cpdl.org/wiki/api.php (not HTML
   scraping). Set a descriptive User-Agent header like
   "choral-piece-finder/0.1 (university IR lab; <my email>)".

2. The script takes a CPDL category name as input — e.g. "Palestrina,_Giovanni_Pierluigi_da_compositions"
   — and uses the categorymembers endpoint to list every work page in that
   category. For each work page, fetch its wikitext content and parse out the
   structured fields.

3. Fields to extract per work (best-effort; leave null if absent):
   - title
   - composer (as listed)
   - voicing (e.g., "SATB", "SSAATTBB")
   - number_of_voices (e.g., "4vv")
   - language(s) of text
   - genre (sacred/secular, sub-genre if listed)
   - year of composition (if listed)
   - text author / source
   - first line of text (incipit)
   - score file URLs and formats (PDF, MusicXML, MIDI, etc.) — capture
     all of them with their format
   - CPDL page URL
   - raw wikitext (keep this for debugging; can drop in v2)

4. Rate-limit: at most one request per second to CPDL. Use time.sleep.

5. Handle errors gracefully — if a work page fails to parse, log a warning
   and continue with the next one. Do not crash the whole crawl on one
   bad page.

6. Output: a single JSON file at data/cpdl_<composer_slug>.json containing
   a list of work records. The slug should be the composer's last name in
   lowercase (e.g., "palestrina").

7. Include a __main__ block so the script is runnable as
   `python src/crawl_cpdl.py "Palestrina,_Giovanni_Pierluigi_da_compositions"`.

8. Add a short docstring at the top explaining what the script does and how
   to run it.

## What I do NOT want

- No database writing yet — JSON output only, so I can inspect it.
- No music21 analysis yet — that's a later step.
- No fuzzy matching across sources — that's a later step.
- No deduplication beyond what CPDL itself provides.
- No CLI flags beyond the positional composer-category argument.

## Test plan

I'll run the script against Palestrina first. The output should contain
at least 100 works (Palestrina is well-represented on CPDL). I'll inspect
the JSON manually before we move on.

## Project context

Repo structure:
ir_lab4/
├── src/
│   ├── crawl_cpdl.py       <- this file
│   └── ...
├── data/                    <- output JSON goes here
├── requirements.txt         <- add any new dependencies here
└── DESIGN.md                <- read this for context