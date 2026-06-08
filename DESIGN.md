# Design — Choral Piece Finder

## 1. Problem and scope
This AI agent is meant to help choral conductors or singers find choral pieces that are relevant for them by various criteria, including voicing, era, genre, key, etc. Queries can be e.g. "show me sacred pieces for mixed choir with at most 6 voices under 5 minutes with a free score" The first version will be limited to 5 composers and 2 databases, but this will be expanded in the future. The project arose from a natural need to find pieces that fit an ensemble of a particular size, skill level and setting.

## 2. Sources
The sources used in the initial version are Choral Public Domain Library (CPDL) (https://www1.cpdl.org/wiki/index.php/Main_Page) and Swedish Musical Heritage (SMH) (https://www.swedishmusicalheritage.com). CPDL is a community-maintained library of public-domain choral scores with broad European coverage and frequent MusicXML availability, useful for both retrieval and computed-feature analysis. SMH provides authoritative editorial metadata for Swedish composers, often with downloadable scholarly PDF editions, complementing CPDL's coverage gaps for Nordic Romantic repertoire.

## 3. The four IR problems this addresses
3.1 Cross-source metadata reconciliation. Different sources use different conventions and have different metadata quality. For example, CPDL labels Stenhammar's Vårnatt as SATB, while SMH correctly identifies it as SSAATTBB with piano. Both labels are defensible (the piece's texture is mostly four-part with divisi) but a user needs to know that four singers are not enough to represent all divisi.

3.2 Coverage-aware retrieval with tiered confidence. There are at least 4 different confidence tiers that must be taken into account: (1) a full record with elaborate and authoritative metadata and a downloadable PDF score. (2) rich and trustworthy metadata that can be used authoritatively, but the score cannot be directly provided. (3) sparse metadata, no download: Only the coarse fields (Year, Work category, Duration, Text). Information about voicing unavailable. (4) Pieces only visible as entries on a composer's page but without a page of their own, composers with incomplete lists of works, or composers who are absent altogether.

3.3 Orphaned-media discovery beyond the source's own navigation. The score for a piece might be available from a source's database but cannot be found by navigating the interface of the database; it can be revealed as a separate downloadable file by enumerating media IDs directly, even though no work page links to it.

3.4 Homonymy + work containment disambiguation: disambiguating works with the same name by the same composer, e.g., Peterson-Berger's J.P. Jacobsen vs Sigrid Elmblad Stämning. The Elmblad Stämning is available as a standalone entry on Peterson-Berger's composer page, while the Jacobsen is inside the entry "8 songs for mixed choir".

## 3.5 Cloudflare session helper (src/cpdl_session.py)

CPDL is protected by Cloudflare's managed challenge. `CPDLSession` (in
`src/cpdl_session.py`) encapsulates the bypass: Playwright launches real Chrome
once to acquire a `cf_clearance` cookie, which is then injected into a
`curl_cffi` session configured with Chrome TLS impersonation. Cookie age is
tracked and the Playwright step re-runs automatically when cookies approach
expiry (default 20 min), so long crawls and on-demand score downloads both work
without manual intervention. Both `crawl_cpdl.py` and `tools/analyze.py` use it
via `get_cpdl_session()`.

## 4. Architecture overview
The system will have two phases: an offline indexing phase and an online query phase. The offline indexing phase includes crawling CPDL and SMH and parsing, normalizing and storing locally. In the online query phase, the user asking a question like in the example in section 1. Then the agent uses tools to query the local index and return an answer.

The LLM is Llama 3.3 70B Instruct, hosted by Berget.AI, accessed via the OpenAI-compatible API. The LLM does not directly access the sources at query time — it operates only through tools you've defined that hit the local index.
Tool calling is the mechanism by which the agent retrieves context and performs actions. Each tool addresses one or more of the IR problems from Section 3.


[Offline]
  CPDL ──┐
         ├──► crawler ──► normalize ──► SQLite index
  SMH ───┘                                  │
                                            ▼
[Online]                              [local index]
                                            ▲
  user ──► CLI ──► agent loop ◄──► Berget (Llama 3.3 70B)
                       │
                       └──► tools ──► local index / SMH / music21

## 5. Data model
Each work in the local index is represented as a record with the following fields. The design reflects the IR problems identified in section 3: voicings is a list rather than a single value because pieces can have multiple valid scorings (e.g., Åhlén's Sommarpsalm exists as both SATB and SSAATBB); duration_min_sec and duration_max_sec are a range because strophic works have length that depends on verse count; field_provenance and field_confidence enable cross-source reconciliation and tiered-confidence retrieval by tracking, per field, which source the value came from and how reliable it is judged to be.

work_id              -- internal stable ID
sources              -- list of {source_name, source_url, source_id}
composer_normalized  -- e.g., "Stenhammar, Wilhelm"
composer_dates       -- (birth_year, death_year)
title_primary        -- main title
title_alternates     -- other titles / translations
incipit              -- first line of text, for disambiguation
text_author          -- librettist / poet
text_language        -- ISO code: "sv", "la", etc.
year_composition     -- year or range
period               -- "Renaissance" | "Late Romantic" | etc.
genre                -- "sacred motet" | "secular partsong" | etc.
voicings             -- list of voicing strings
duration_min_sec     -- integer or null
duration_max_sec     -- integer or null
key                  -- string, may include modulations
has_free_score       -- bool
score_urls           -- list of {format, url, source}
parent_work_id       -- if movement of a cycle
constituent_work_ids -- if parent of others
field_provenance     -- per-field: source it came from
field_confidence     -- per-field: "high" | "medium" | "low"

## 6. Tools
The agent has seven tools, each addressing one or more IR problems from section 3.

6.1 search_local_index — Queries the local SQLite index. Takes structured filters (composer, voicing, language, period, duration range, has_free_score) and an optional free-text query over title and incipit. Returns work records with per-field provenance. The primary retrieval tool; most queries begin here.

6.2 get_work_details — Given a work ID, returns the full record including all available metadata across sources, with provenance and confidence per field. Used after a search narrows to a candidate of interest.

6.3 fetch_score — Given a work ID, returns the URL of the score file (PDF or MusicXML) if freely available, or returns publisher and edition information if not. Implements honest coverage reporting.

6.4 analyze_score_features — Given a MusicXML URL, parses with music21 and returns computed features: vocal range per voice, total duration estimate, key signature, time signature. Addresses the gap where CPDL metadata lacks per-voice range information.

6.5 cross_source_verify — Given a work ID, checks whether the same work exists in both CPDL and SMH and returns any discrepancies between sources (e.g., differing voicings, dates, or genre tags). Directly addresses IR problem 3.1.

6.6 disambiguate_homonyms — Given a composer and title with multiple candidate matches, returns each candidate with its incipit, text author, key, and parent work (if applicable) to enable disambiguation. Addresses IR problem 3.4.

6.7 remember / recall — Read/write tools backed by a JSON memory file persisting user preferences across sessions (e.g., the user's choir voicing, skill level, language preference, repertoire interests).


## 7. Indexing pipeline
Before the agent is usable, a one-time crawler script populates the local SQLite index. For each of the five composers, the crawler:

1. Fetches the composer's CPDL category page via the MediaWiki API and iterates through linked work pages, parsing structured fields (voicing, language, genre, year, available score URLs).

2. Fetches the composer's SMH page and iterates through linked work pages, parsing structured fields (work category, instrumentation, year, duration, score downloads).

3. Probes SMH's downloadMedia.php endpoint by sequential media ID to discover PDFs that exist but are not linked from any work page (addressing IR problem 3.3).

4. Where MusicXML is available from CPDL, downloads and parses with music21 to compute per-voice ranges, then attaches the computed features to the record.

5. Merges records across CPDL and SMH by fuzzy matching on composer-normalized + title + incipit, preserving per-field provenance and noting discrepancies for cross_source_verify to surface later.

The result is a SQLite database the agent queries through search_local_index. The crawler runs in minutes for five composers and produces an inspectable JSON dump for debugging.

## 8. Agent loop
The agent operates a standard tool-calling loop. A system prompt establishes its role as a choral-music research assistant aware of the four IR problems; conversation history is held in memory for the session; tool calls are dispatched to local Python functions and their results appended as role: "tool" messages. The loop terminates when the model returns a final text response, or after a safety limit of 10 iterations.

loop (max 10 iterations):
  call Berget with (system_prompt, conversation, tools)
  if response.tool_calls:
    for each tool_call:
      result = dispatch_tool(tool_call.name, tool_call.arguments)
      append {role: "tool", tool_call_id, content: result} to conversation
    continue
  else:
    return response.content to user

Errors inside tools are returned as tool results (not raised as exceptions), so the model can react and try a different approach rather than crashing the loop.

## 9. OpenClaw-inspired pieces
9.1 Memory files (used). A JSON file at data/user_memory.json stores user preferences (e.g., choir voicing, skill level, language preference). The remember and recall tools write and read this file. The agent recalls memory at the start of each session and uses it to tailor recommendations.

9.2 Skills (used loosely). Each tool is backed by a small, single-purpose Python module (CPDL access, SMH access, music21 analysis, cross-source matching). This modular separation parallels OpenClaw's skill abstraction without adopting its full machinery.

9.3 Compaction and heartbeat (not used in v1). Compaction is unnecessary because demo sessions are short (under 10 turns) and stay well within context window limits. Heartbeat is unnecessary because no long-running background processes exist; the crawler runs once, not continuously. Both are noted as possible future extensions if the system grows.

## 10. Out of scope (for v1)
The first version will have no IMSLP integration, no semantic / embedding-based search, no web UI (CLI only), no comprehensive CPDL crawl (5 composers only), no support for in-copyright pieces beyond metadata, no automatic difficulty estimation.

## 11. Open questions

What does CPDL's MediaWiki API return in practice for our five composers? The schema in section 5 may need adjustment once the first crawl is complete.

Will fuzzy matching of titles across CPDL and SMH be reliable enough without embeddings, or will semantic matching be needed for the cross-source merge step?

How sequential is SMH's media ID space, and what's a sensible upper bound for the orphaned-PDF probe?

How well does Llama 3.3 70B handle Swedish-language queries, titles, and incipits?

How should the agent surface coverage gaps to the user without being overly verbose ("this piece exists but is in copyright, available from Gehrmans") on every result?


