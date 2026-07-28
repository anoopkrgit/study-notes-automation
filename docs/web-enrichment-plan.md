# Plan: Bounded Web Enrichment for Stage 2 (Note Generator)

Status: **Decided, not yet implemented.** Recorded so it can be picked up in a
later session without re-deriving the reasoning.

## Goal

Transcripts + supporting materials should continue to define the chapter's
*scope* (non-negotiable, unchanged). On top of that, let the generator do a
small, tightly bounded amount of its own web research to enrich content
quality (better real-world examples, clearer analogies, extra practice
problems) -- without letting it "go wild" (uncontrolled searching, low-quality
sources, or scope creep beyond what the transcripts actually teach).

## Core approach

Use Claude's native **server-side web search tool**, not a custom
scraper/fetcher. Two of its config fields double as hard guardrails enforced
by the API itself (not just prompt instructions the model could drift away
from):

- `allowed_domains` -- the model literally cannot fetch outside this list.
- `max_uses` -- a hard cap on number of searches per request.

## Decided configuration

**Allowed domains (5):**

| Domain | Why |
|---|---|
| `ncert.nic.in` | The actual textbook publisher -- ground truth for what's in-syllabus. |
| `hyperphysics.phy-astr.gsu.edu` | University-maintained, no ads, rigorously accurate physics reference. |
| `chem.libretexts.org` | Open, textbook-grade chemistry content, no ad-driven SEO padding. |
| `khanacademy.org` | Non-commercial, pedagogy-first explanations and worked examples. |
| `byjus.com` | Matches the exam-pattern style already present in the source material (JEE Main revision notes, foundation-batch content) -- the one commercial ed-tech site included, deliberately. |

Deliberately excluded: Vedantu, Toppr, Embibe, Wikipedia -- kept the list tight
rather than broad. Can be added later if the audit trail (see below) shows
real gaps.

**Search cap:** 3 per chapter generation (`max_uses`). Enough for one lookup
per major concept the transcript covers, without letting a single run spiral
in latency/cost.

This starting config is expected to be adjusted after reviewing the audit
trail from the first few real (`--live`) runs -- it's a one-line change either
way, not a redesign.

## Guardrails, layered

1. **Domain allow-list** -- hard boundary on source quality (see above).
2. **Search budget** -- hard boundary on cost/scope creep (see above).
3. **Scope boundary unchanged** -- the existing prompt rule that supporting
   material "must NOT expand scope beyond what the transcripts cover" extends
   verbatim to web content: search is for *enriching* a topic the transcript
   already teaches, never for introducing a subtopic the transcript never
   covered (even if commonly taught alongside that chapter elsewhere).
4. **Mandatory attribution** -- every web-derived fact/example must be
   traceable. Extend the existing `_sources.txt` manifest convention with a
   new `_web-sources.txt`, written after generation, listing every query
   issued and every URL actually used.
5. **Graceful degradation** -- if search fails or hits its cap mid-run,
   generation must continue using only transcripts/supporting material.
   Never block the whole chapter on a failed search, consistent with how the
   pipeline already treats other API hiccups as non-fatal.
6. **Low-risk rollout path** -- the nightly unattended run currently does
   `--run-generate-no-llm` (zero-token dry preview only); real generation only
   happens when manually run with `--run-generate --live` while watching the
   log. Web search should only be active in that manual path initially, so
   several chapters can be spot-checked for citation quality before ever
   trusting it in the unattended nightly job.

## What implementation would actually touch (when picked up)

- New config constants in `config/settings.py`: `WEB_SEARCH_ALLOWED_DOMAINS`,
  `MAX_WEB_SEARCHES_PER_CHAPTER`, an `ENABLE_WEB_ENRICHMENT` flag (opt-in, off
  until validated).
- Add the server-side web-search tool to the `tools` list in the live-mode API
  call in `src/func_generate_notes.py` (`run_generate`, around the
  `client.messages.create(...)` call), alongside the existing custom
  `AGENT_TOOLS`.
- Extend the turn-loop's message-reconstruction in `run_generate` (currently
  only preserves `text`/`tool_use` content blocks when rebuilding `messages`)
  to also preserve the server tool's search/result blocks -- otherwise that
  context silently drops on the next turn.
- Extend `build_user_prompt()` with the enrichment-vs-scope rule above.
- Write `_web-sources.txt` next to `_sources.txt` after a successful
  generation.

## Open items for next session

- Confirm the domain list/cap still look right after seeing a real
  `_web-sources.txt` audit trail from a handful of chapters.
- Decide whether `ENABLE_WEB_ENRICHMENT` should default on or stay opt-in
  once validated.

---

# Plan: Local Retrieval-Augmented Generation (RAG) Over Supporting Materials

Status: **Decided, not yet implemented.** This is the actual RAG component of
the project -- unlike the web-enrichment plan above, this is real embedding
based semantic retrieval, and it requires no internet access at all.

## Why this exists

Today, Stage 2 does not retrieve anything: it reads the *entire* `supporting/`
folder that Stage 1 already filed for that one chapter, in full. That's
context-scoped generation, not RAG (see reasoning captured in-session: a true
RAG system retrieves relevant chunks from a corpus via semantic similarity at
generation time, rather than reading a folder someone already pre-sorted into
place).

This plan replaces "read everything already filed here" with real retrieval:
chunk and embed the supporting-materials corpus, and at generation time pull
only the top-k chunks most relevant to what the transcript actually covers.

## Key design decision: scope of the retrieval corpus

Two options, and the choice determines whether this is a meaningful upgrade
or just an efficiency tweak:

- **Narrow scope** -- retrieve only within the one chapter's already-filed
  `supporting/` folder. Low added value: mostly just bounds token cost via
  chunking instead of full-document reads; doesn't add anything Stage 1's
  per-file classifier didn't already decide.
- **Whole-corpus scope (recommended)** -- build one persistent index over
  *every* supporting/collected document ever seen, across all chapters and
  subjects. At generation time, retrieve top-k chunks relevant to the current
  transcript regardless of which chapter folder a document was originally
  filed under. This is where the real value is: a chunk that Stage 1's
  per-file router filed under a different (but related) chapter, or a chunk
  from a document that was never confidently routable at all, can still
  surface here if it's actually relevant -- because relevance is now judged
  per-chunk against the transcript's own content, not per-file against a
  fixed taxonomy. Subject-level pre-filtering (e.g. only search within the
  same subject) keeps this from pulling in cross-subject noise.

Recommendation: whole-corpus scope, subject-filtered.

## Architecture

1. **Chunking** -- extract text from each supporting PDF/docx (reusing the
   existing `pypdf.PdfReader` already imported in `func_assemble_chapters.py`)
   into page- or paragraph-sized chunks with slight overlap.
2. **Embedding model** -- a small local/offline model (e.g. a
   `sentence-transformers` model), not an API call. This keeps retrieval at
   zero marginal token/API cost and keeps material fully local/private,
   consistent with this pipeline's existing "assembly may spend LLM tokens,
   generation must be deliberate" cost philosophy.
3. **Vector store** -- given the corpus size (a school year's worth of PDFs,
   not millions of documents), a flat local file is enough: cosine similarity
   over a numpy array persisted as e.g. `state/embeddings/index.npz`, plus a
   `state/embeddings/chunks.json` sidecar holding chunk text and metadata
   (source file, subject, chapter, page). No vector DB dependency needed at
   this scale.
4. **Incremental indexing** -- chunk + embed a supporting file once, the same
   way Stage 1 already dedupes via SHA-256 content hash in
   `assemble-state.json`; re-runs never re-embed unchanged files.
5. **Retrieval at generation time** -- derive query text from the chapter's
   own spine transcripts (representative snippets/subtopics), embed those,
   and cosine-similarity top-k search (e.g. k=8) against the index, filtered
   to the same subject. Inject only those top-k chunks into the generation
   prompt, each tagged with its source file + page, instead of dumping full
   supporting documents.

## Guardrails (same philosophy as the web-enrichment plan)

- **Scope invariant preserved** -- retrieved chunks may only enrich topics the
  transcript already covers; the transcript remains the sole scope-definer.
- **Attribution** -- write a `_retrieved-sources.txt` manifest per chapter
  (mirroring `_sources.txt` / the planned `_web-sources.txt`) listing every
  chunk actually used: source file, page, similarity score.
- **Cost bound** -- fixed top-k cap keeps prompt size flat no matter how large
  the historical corpus grows.
- **Graceful degradation** -- if the embedding dependency or index is
  unavailable, fall back to today's behavior (read the chapter's `supporting/`
  folder in full) rather than failing the run.

## What implementation would actually touch (when picked up)

- New module `src/func_retrieval.py`: chunking, embedding, index build/update,
  top-k query.
- New config constants in `config/settings.py`: `EMBEDDING_MODEL_NAME`,
  `RETRIEVAL_TOP_K`, `RETRIEVAL_INDEX_DIR`, `CHUNK_SIZE_CHARS` /
  `CHUNK_OVERLAP_CHARS`.
- New dependency in `requirements.txt` for the local embedding model
  (flagged as an open decision -- weighs install size against simplicity).
- Hook into `func_assemble_chapters.py`: after a supporting file is copied,
  chunk + embed + update the index for that file (hash-gated, once only).
- Hook into `func_generate_notes.py` (`run_generate`): replace the current
  "glob and read every file in `supporting/`" step with a call into the new
  retrieval function, keeping the existing file listing only as a fallback/
  manifest for logging.
- Update `build_user_prompt()` to present retrieved chunks (with attribution)
  instead of full supporting-file dumps.
- Tests: chunking correctness, deterministic top-k ranking (mocked embedding
  calls), and graceful fallback when the index is empty/missing.

## Rollout

Same low-risk validation approach as the web-enrichment plan: run manually on
a handful of chapters first, compare retrieved-chunk relevance against
today's full-dump baseline, before making retrieval the default path in
generation.

## Open items for next session

- Confirm whole-corpus vs. narrow-scope retrieval is still the right call.
- Pick the actual local embedding model/library and confirm the added
  dependency is acceptable.
- Decide default chunk granularity (page-level vs. paragraph-level).
