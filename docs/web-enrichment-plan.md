# Plan: Bounded Web Enrichment for Stage 2 (Note Generator)

Status: **Implemented for the `claude_cli_subprocess` generator
(2026-08-04).** `direct_api`/`agents` are unaffected by this plan -- see
"Which implementation this plan targets" below.

## Goal

Transcripts + supporting materials should continue to define the chapter's
*scope* (non-negotiable, unchanged). On top of that, let the generator do a
small, tightly bounded amount of its own web research to enrich content
quality (better real-world examples, clearer analogies, extra practice
problems) -- without letting it "go wild" (uncontrolled searching, low-quality
sources, or scope creep beyond what the transcripts actually teach).

## Which implementation this plan targets

The repo grew a third Stage 2 implementation (`src/claude_cli_subprocess/`)
after this plan was first drafted, alongside the original `src/direct_api/`
(plain Anthropic SDK, `client.messages.create`) and `src/agents/`. The
original draft of this plan assumed the SDK path -- it named
`src/stage2_api.py` and a request-level `tools` list, neither of which exist
in the CLI-subprocess module. **This revision targets
`src/claude_cli_subprocess/stage2_cli.py` specifically**, since that's the
version actually implemented (2026-08-04). The core guardrail *mechanism* is
different enough between the two that the sections below should not be
read as applying to `direct_api`/`agents` without re-deriving them there.

## Core approach

`claude_cli_subprocess` delegates the whole generation pipeline to a real
Claude Code session running the packaged `study-notes` skill (see
`stage2_cli.py`'s own module docstring) -- there is no request-level `tools`
list this code controls directly, only the CLI's own built-in `WebSearch` /
`WebFetch` tools, granted or withheld via `--allowedTools`. That changes both
guardrails from single config fields on an API request into two *separate*
CLI-native mechanisms, discovered by reading Claude Code's own permission
docs (`docs/en/tools-reference.md`, `docs/en/permissions.md`) rather than
assumed by analogy with the SDK's server-side web search tool:

- **Domain allow-list**: `WebSearch` permission rules in Claude Code take
  no specifier -- it's `allow`/`deny` on the whole tool, not per-domain.
  (The tool's own input schema *has* an `allowed_domains` field, but that's
  something the model chooses to pass per search call, not something this
  code can force from outside.) The actual hard, CLI-enforced boundary is on
  `WebFetch`, whose permission rules DO support domain scoping --
  `WebFetch(domain:example.com)`, matched by hostname, `*` wildcards
  supported. So the enforced guardrail here is: grant bare `WebSearch`
  (prompted to pass `allowed_domains` matching our list, as a courtesy, not
  as the boundary) plus `WebFetch(domain:...)` for *only* the 5 approved
  domains below and no bare `WebFetch` -- so even if a search surfaces a
  result outside the list, there is no tool that can actually fetch it.
- **Search budget**: no per-request field exists for the CLI. Claude Code
  enforces a session-wide cap via the `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION`
  environment variable (default 200 as of Claude Code 2.1.212+; accepts any
  positive whole number). Since `run_claude_cli()` makes exactly one `claude`
  session per chapter (resumed via `-r` across retries, not restarted), a
  session-level cap maps directly onto "per chapter" the way the original
  plan intended -- set low, it becomes our per-chapter search budget.

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

**Search cap:** 3 per chapter generation, via
`CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` (see "Core approach" above).
Enough for one lookup per major concept the transcript covers, without
letting a single run spiral in latency/cost.

This starting config is expected to be adjusted after reviewing the audit
trail from the first few real (`--live`) runs -- it's a one-line change either
way, not a redesign.

*Open verification item:* whether this env var can be set *below* its
200-call default (docs confirm it "can be raised", not explicitly that it
can be lowered) was not empirically confirmed before implementation --
consistent with this module's existing practice of shipping defensively
around unverified CLI details (see `run_claude_cli()`'s docstring on the
`--output-format json` envelope shape, or the `-r/--resume` flag spelling).
Confirm against a real `--live` run's actual search count once the first
pilot chapters are generated.

## Guardrails, layered

1. **Domain allow-list** -- hard boundary on source quality, enforced via
   `WebFetch(domain:...)` scoping (see "Core approach" above).
2. **Search budget** -- hard boundary on cost/scope creep, enforced via
   `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` (see "Core approach" above).
3. **Scope boundary explicit for web content too** -- `direct_api`'s
   `build_user_prompt()` already has a "must NOT expand scope beyond what the
   transcripts cover" rule for `supporting/` material, but the
   `study-notes` skill (`SKILL.md`, the CLI implementation's actual
   instruction set) did not have an equivalent line -- its one mention of
   the web ("Research the web for depth, where depth means more explanation
   ... and a wider variety of problem types") was already present before this
   plan, granted no tool to act on it, and didn't say anything about scope.
   Added a line to `SKILL.md`'s "Sources, in priority order" paragraph
   making the same rule explicit for web content, and naming the 5 allowed
   domains + the search cap there so the model has the actual numbers, not
   just "some limit exists".
4. **Mandatory attribution, built from ground truth, not self-report** --
   consistent with this module's existing "TRUTH-CHECK, NOT SELF-REPORTED
   SUCCESS" philosophy (`stage2_cli.py` module docstring; `expected_docx.exists()`
   gating `config.MARKER` rather than trusting the CLI's own JSON envelope),
   `_web-sources.txt` is NOT written by prompting the model to self-report
   what it searched/fetched. `stage2_cli.py` already reads Claude Code's own
   local session transcript JSONL for the heartbeat log
   (`_heartbeat_summary()`); `write_web_sources_manifest()` reuses that same
   file, this time scanning *every* line for `WebSearch`/`WebFetch`
   `tool_use` blocks, so the manifest reflects tool calls Claude Code
   actually made, not what the model claims it made.
5. **Graceful degradation** -- if search fails or hits its cap mid-run,
   generation must continue using only transcripts/supporting material.
   Never block the whole chapter on a failed search, consistent with how the
   pipeline already treats other API hiccups as non-fatal. No extra code
   needed for this: `WebSearch`/`WebFetch` are just two more tools among the
   ones already granted via `--allowedTools`; a failed call is the model's
   problem to route around within the same session, not something
   `run_claude_cli()` has visibility into or needs to special-case.
6. **Low-risk rollout path** -- `ENABLE_WEB_ENRICHMENT` (new config flag,
   default OFF) gates whether `WebSearch`/`WebFetch(domain:...)` are ever
   added to `--allowedTools` at all. Since `run_stage2_chapter()` only calls
   `run_claude_cli()` when `live_mode=True` (the nightly unattended run uses
   `--run-generate-no-llm`, i.e. mock mode, which returns before building any
   tool list), turning this flag on inherently only affects the manual
   `--run-generate --live` path -- no separate manual/nightly branch needed
   in code for this specifically.

## What was implemented (`claude_cli_subprocess`, 2026-08-04)

- `config/settings.py`: `ENABLE_WEB_ENRICHMENT` (bool, env `ENABLE_WEB_ENRICHMENT`,
  default off), `WEB_SEARCH_ALLOWED_DOMAINS` (the 5-domain list above),
  `MAX_WEB_SEARCHES_PER_CHAPTER` (int, env `MAX_WEB_SEARCHES_PER_CHAPTER`,
  default 3).
- `src/claude_cli_subprocess/common.py`: `build_claude_env()` takes an
  optional `extra_env` dict, merged in after the existing
  `ANTHROPIC_API_KEY` pop -- used to set
  `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` only when web enrichment is on
  (kept general/reusable rather than a one-off web-search-only parameter).
- `src/claude_cli_subprocess/stage2_cli.py`:
  - `run_claude_cli()`: when `config.ENABLE_WEB_ENRICHMENT` and not
    `dev_mode`, appends `WebSearch` plus one `WebFetch(domain:...)` entry per
    allowed domain to the `--allowedTools` list, and passes
    `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` via `build_claude_env()`'s new
    `extra_env`. Left untouched (and covered by the existing
    `test_real_run_gets_full_tool_list_not_empty` test, which asserts
    `--allowedTools` equals `config.CLAUDE_ALLOWED_TOOLS` exactly) when the
    flag is off, since the default is off.
  - New `write_web_sources_manifest(target_dir, workspace)`: scans the
    session's local JSONL transcript (same file `_heartbeat_summary()`
    reads) for `WebSearch`/`WebFetch` tool_use blocks, extracts
    query/url/allowed_domains, and writes `_web-sources.txt` into
    `target_dir` (next to `_sources.txt`). Called from `run_stage2_chapter()`
    right after the existing `expected_docx.exists()` success check, only
    when `config.ENABLE_WEB_ENRICHMENT` is on. Best-effort/silent-on-failure,
    same as `_heartbeat_summary()` -- an unreadable transcript degrades to
    "no manifest written", never a failed chapter.
- `templates/study-notes.skill` (the packaged skill zip -- `SKILL.md` inside
  it is the actual source of truth read by the CLI at runtime; there is no
  separate tracked source directory, so this was unzip -> edit -> re-zip in
  place): extended the "Sources, in priority order" paragraph with the
  allowed-domain list, the search cap, the explicit anti-scope-creep rule for
  web content, and an instruction to note in the delivery summary when web
  search was used (for a human skimming the log; the actual audit trail is
  `_web-sources.txt`, built from ground truth per guardrail 4, not from this
  prose).

## Open items for next session

- Still not empirically confirmed (2026-08-05 update): the first genuinely
  successful live `--live` run (Physics-Ch5-Optics, after fixing an
  unrelated skill-resolution bug that invalidated every earlier attempt --
  see `docs/cli-subprocess-plan.md`'s "Resolved" section) had
  `ENABLE_WEB_ENRICHMENT=1` and the tools available, but the model judged it
  didn't need outside sources for that chapter and never called `WebSearch`/
  `WebFetch` at all (no `_web-sources.txt` was written, correctly -- see
  `write_web_sources_manifest()`, which only writes when a search/fetch
  actually happened). That's valid behavior per this plan's own "discretionary,
  not mandatory" design, but it means the domain-list/cap questions below are
  still open -- a chapter that never searches can't exercise them.
- Confirm the domain list/cap still look right after seeing a real
  `_web-sources.txt` audit trail from a handful of chapters.
- Decide whether `ENABLE_WEB_ENRICHMENT` should default on or stay opt-in
  once validated.
- Confirm `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` actually lowers the cap
  below its default in practice (see the "Open verification item" under
  "Decided configuration" above) -- not yet confirmed against a real `--live`
  run.
- No `direct_api`/`agents` equivalent exists yet. If either of those
  implementations is still in active use, this plan would need a second,
  separate pass for the SDK-level `tools`/`allowed_domains`/`max_uses`
  mechanism the original draft described -- that mechanism is real for those
  two, just not for `claude_cli_subprocess`.

---

# Plan: Local Retrieval-Augmented Generation (RAG) Over Supporting Materials

Status: **Decided, not yet implemented; untouched by the 2026-08-04 session
that implemented the web-enrichment plan above.** This is the actual RAG
component of the project -- unlike the web-enrichment plan above, this is
real embedding based semantic retrieval, and it requires no internet access
at all. File paths below have been updated to match the current repo layout
(`src/direct_api/`, added after this plan's first draft) but the design
itself has not been re-evaluated against `claude_cli_subprocess` or `agents`
-- both postdate this plan too, and unlike the web-enrichment plan's tool-list
mechanism, it's not yet clear whether this plan's touch points (a Python hook
after Stage 1 files a supporting file; a Python hook in Stage 2's context
assembly) even have an equivalent to hook into on the CLI-subprocess path,
where Stage 2 is a single opaque `claude -p` call rather than Python-driven
turn loop. Re-scope this plan against whichever implementation is live before
picking it up.

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
   existing `pypdf.PdfReader` already imported in `src/direct_api/stage1_api.py`)
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
- Hook into `src/direct_api/stage1_api.py`: after a supporting file is
  copied, chunk + embed + update the index for that file (hash-gated, once
  only).
- Hook into `src/direct_api/stage2_api.py` (`run_generate`): replace the
  current "glob and read every file in `supporting/`" step with a call into
  the new retrieval function, keeping the existing file listing only as a
  fallback/manifest for logging.
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
