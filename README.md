# Choral Piece Finder

An AI agent that helps choir singers and conductors find pieces that suit their ensemble's specific requirements, including voicing, language, period, length, and availability of a free score. The agent searches a local index of about 1,350 works by five composers (Palestrina, plus four Swedish Romantics), drawn from two sources, and reports honestly when the right edition exists but isn't free or hasn't been digitized.


## What this is

This project arose out of a natural need to find appropriate choir pieces to sing with a local ensemble. The aim is to facilitate the search of appropriate repertoire by combining data across sources and, if possible, to provide links to scores and sound files. The agent should also honestly report what is available and what isn't, due for example to a composer's catalog still being under copyright.

Version 1.0 covers five composers (Palestrina, plus the Swedish Romantics Stenhammar, Peterson-Berger, Lindberg, and Alfvén) across two sources (the Choral Public Domain Library and Swedish Musical Heritage), for a total of about 1,350 works.

The project will continue to be expanded to include more composers and features.


## What's novel

This isn't just a database wrapper. The agent addresses three IR problems specific to choral score discovery:

1. **Cross-source metadata reconciliation.** Different catalogs encode the same work differently — for example, CPDL labels Stenhammar's Vårnatt as SATB, while SMH correctly identifies it as SSAATTBB with piano. The schema records per-field provenance so such discrepancies can be surfaced; in v1 the two corpora don't overlap, so this is architecturally supported rather than fully exercised.

2. **Coverage-aware tiered retrieval.** The agent honestly reports whether a score is freely downloadable, catalog-only because of copyright, or unknown. Lindberg's works just entered the Swedish public domain in 2026, so coverage is actively evolving.

3. **Orphaned-media discovery.** Some score PDFs exist on Swedish Musical Heritage's servers but aren't linked from any catalog page. The agent discovers them by enumerating media IDs and surfacing them alongside the catalog-linked editions.


## Quick start

Tested on macOS with Python 3.11. Should work on Linux and Windows with minor adjustments.

**Prerequisites:** Python 3.10 or later, a Berget.AI API key.

```bash
# Clone and enter the project
git clone https://github.com/axelriben/choral-piece-finder.git
cd choral-piece-finder

# Set up a virtual environment
python3 -m venv venv
source venv/bin/activate   # macOS/Linux
# or: venv\Scripts\activate  # Windows

# Install dependencies
pip install -r requirements.txt

# Configure your Berget API key
cp .env.example .env
# Edit .env and paste your Berget API key

# Run the agent
python src/agent.py
```

The agent starts a REPL. Type a question, get an answer. Type `quit` to exit.

The pre-built index (`data/index.db`) is committed to the repository, so the agent works immediately on a fresh clone without re-crawling anything.

**Optional:** if you want to re-crawl sources or analyze scores not in the local cache, install the Playwright browser:

```bash
playwright install chromium
```


## Example queries

The agent works in English and Swedish.

**"Find me Stenhammar's Vårnatt"**
Returns both editions in the database, including four additional editions discovered by enumerating SMH's media IDs beyond what the catalog page links to.

**"What's the vocal range of Palestrina's Sicut cervus?"**
Downloads the MusicXML edition, parses it with music21, and reports per-voice ranges. Includes a caveat that music21's interpretation of original-clef Renaissance editions can shift the absolute octave registration.

**"Find me a sacred piece suitable for 5 singers"**
Returns pieces matching the genre and number of voices.

**"Help me find some secular pieces suitable for a male choir"**
Returns pieces matching genre and specific voices.

**"Remember that I sing in an SSAATTBB choir" / "Given that, what choir pieces do you recommend?"**
Stores the preference, then recalls it on the next turn to filter recommendations to the user's voicing.

The agent draws all answers from the local index. When tools return empty results, the agent reports this honestly rather than filling in from general knowledge.


## Architecture

The system runs in two phases.

**Offline indexing** (one-time). Crawlers fetch metadata from CPDL (via the MediaWiki API, behind a Cloudflare bypass) and SMH (HTML scraping). A separate probe enumerates SMH media IDs to discover downloadable files that aren't linked from any catalog page. The results merge into a unified SQLite database at `data/index.db` with per-field provenance tracking.

**Online query.** The agent runs an OpenAI-compatible tool-calling loop against Llama 3.3 70B Instruct, hosted by Berget.AI. The LLM dispatches calls to seven local tools that query the SQLite index, fetch score URLs, run music21 score analysis, assist with title disambiguation, and read/write a small persistent memory file.

```
[Offline]
  CPDL ────► crawl_cpdl.py ────► parse_cpdl.py ─┐
                                                  ├─► build_index.py ──► data/index.db
  SMH ────► crawl_smh.py ───────────────────────┤
            probe_smh_media.py ─────────────────┘
            match_orphaned_media.py

[Online]
  user ──► agent.py ──► Berget (Llama 3.3 70B)
              │
              └──► dispatcher.py ──► tools/* ──► data/index.db
                                                 data/user_memory.json  (runtime)
                                                 data/score_cache/      (runtime)
```

A more detailed treatment of the design is in [`DESIGN.md`](DESIGN.md).


## Project structure

```
choral-piece-finder/
├── README.md
├── DESIGN.md                       full design document
├── MUSIC21_NOTES.md                known music21 limitations and verification results
├── requirements.txt
├── .env.example                    template for the Berget API key
├── data/
│   ├── index.db                    unified SQLite index (≈1,350 works; committed)
│   ├── cpdl_palestrina*.json       raw and parsed Palestrina crawl data
│   ├── smh_*.json                  per-composer SMH crawl data
│   ├── smh_media_probe.json        media-ID enumeration results
│   ├── smh_orphaned_media.json     orphan-classification output
│   ├── user_memory.json            user preferences — created at runtime, gitignored
│   └── score_cache/                cached MusicXML downloads — gitignored
├── prompts/                        Claude Code prompts used during development
├── tests/
│   ├── test_normalize_args.py
│   ├── test_tools_core.py
│   ├── test_tools_advanced.py
│   └── test_tools.py
└── src/
    ├── agent.py                    main entry point — CLI REPL
    ├── dispatcher.py               tool registry and dispatch
    ├── db.py                       SQLite connection helper
    ├── utils.py                    title and voicing normalization
    ├── cpdl_session.py             Playwright + Cloudflare bypass session
    ├── crawl_cpdl.py               CPDL crawler (MediaWiki API + wikitext parser)
    ├── parse_cpdl.py               wikitext template parsing
    ├── crawl_smh.py                SMH HTML crawler
    ├── probe_smh_media.py          SMH media-ID probe (orphan discovery)
    ├── match_orphaned_media.py     classify probed media against crawled records
    ├── backfill_media_notes.py     one-off: parse filenames for edition notes
    ├── build_index.py              merge JSON crawl data into the unified SQLite index
    ├── prompts/
    │   └── system.txt              the agent's system prompt
    └── tools/
        ├── search.py               search_local_index
        ├── details.py              get_work_details
        ├── score.py                fetch_score (with edition-quality ranking)
        ├── analyze.py              analyze_score_features (music21)
        ├── verify.py               cross_source_verify
        ├── disambiguate.py         disambiguate_homonyms
        └── memory.py               remember / recall
```


## Limitations

This is v1. A few known limitations, honestly documented.

**Homonymy and cross-orthography disambiguation.** The `disambiguate_homonyms` tool retrieves structured candidates for same-title queries, but the natural-language summarization layer occasionally introduces errors when consolidating multiple records. Cross-orthography matching — for example *Stämning* (modern Swedish) ↔ *Stemning* (older Danish/Norwegian spelling that Peterson-Berger also used) — is not yet implemented. Both are planned for v1.1.

**Music21 octave-displacement on Renaissance editions.** The `analyze_score_features` tool uses music21 to extract per-voice ranges from MusicXML, but verification against original CPDL editions revealed that music21 shifts the absolute octave registration by one octave for editions using treble-8vb clefs (common in modern CPDL Renaissance editions). The interval span (range in semitones) is reliable; absolute pitches require verification against the source edition. Documented in [`MUSIC21_NOTES.md`](MUSIC21_NOTES.md). Modern editions in standard clefs are unaffected.

**Cross-source metadata reconciliation is architecturally supported, not exercised.** The schema records per-field provenance, but v1's two corpora (CPDL covers Palestrina; SMH covers the Swedish composers) don't overlap, so cross-source comparison can't fire on real data. Future expansion to IMSLP would activate this.

**Multi-fetch sequencing.** Asking the agent for "the score links for all three" pieces sometimes results in only the first piece being fetched, with the agent announcing intent to fetch the others but ending the turn before doing so. This is a known weakness of Llama 3.3's tool-calling and can be addressed by either prompt engineering or by changing `fetch_score` to accept a list of `work_id`s. Planned for v1.1.


## Future work

In rough priority order:

- IMSLP integration (much broader European repertoire, especially 19th-century)
- Cross-orthography title matching (*Stämning* ↔ *Stemning*, *Vaarnatt* ↔ *Vårnatt*)
- Per-voice difficulty estimation (range span, chromaticism, rhythmic density)
- Better music21 clef handling for Renaissance editions
- Web UI option (currently CLI only)
- Expanded composer coverage (likely starting with more Nordic composers via SMH)


## Acknowledgments

This project was built for an Information Retrieval lab at Uppsala University. [Berget.AI](https://berget.ai) provided free credits for LLM access (model: Llama 3.3 70B Instruct). Data is drawn from the [Choral Public Domain Library](https://www.cpdl.org) and [Swedish Musical Heritage](https://www.swedishmusicalheritage.com), both of which are publicly accessible. Development used Claude Code and Claude.ai as engineering assistants; prompts used during development are committed in `prompts/`.
