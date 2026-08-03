# Migration to a Multi-Agent Architecture

## Why this document exists

Stage 2 (note generation) currently runs as **one Sonnet model in a single multi-turn loop** (`src/stage2_api.py::run_generate`, up to `MAX_TURNS=200`), handed a flat list of 10 generic tools (`AGENT_TOOLS`) — including a generic `tool_bash` that allowlists `python3/node/soffice/mkdir/ls/cat/cp/mv`. The model itself decides, turn by turn, whether to ingest source PDFs, author `content.json`/`figures.json`, render figures, compile the `.docx`, or run QA scripts — all by shelling out through that one bash tool inside one growing conversation.

**Premise correction:** there is no literal `claude` CLI subprocess anywhere in this repo (`requirements.txt` only lists `anthropic`, `pypdf`, `python-docx`, `python-dotenv`, `openpyxl`). What this document rejects is the pattern above — one generalist agent improvising its own tool sequencing via a bash escape hatch — and replaces it with a true multi-agent graph: an explicit orchestrator plus specialized agents, each with a narrow, typed toolset instead of a bash shell.

Two additional findings, confirmed by reading the code directly, that this migration must account for:

- **Bug:** `load_skill_prompt()` (`src/stage2_api.py:65`) reads `templates/study-notes-skill.md`. That file **does not exist** — `templates/` contains only `study-notes.skill` (a 79 KB ZIP archive) and `_archived_/`. Stage 2 today silently falls back to a minimal hardcoded prompt and never loads the real `SKILL.md`, tool scripts, schemas, or worked example packaged inside the ZIP. **Decision (approved 2026-07-31): this fix lands as a separate prerequisite patch, before any `src/agents/` work begins** — see "Prerequisite Patch" below. Everything in this document assumes that fix is already in place.
- `python-docx` (`import docx as _docx` in `stage1_api.py`, used at line ~132) is genuinely in use — for reading `.docx` source materials during Stage 1 triage/extraction. It has nothing to do with final document compilation (that's `lib/build.js` + the Node `docx` npm package) and should not be removed.

---

## Prerequisite Patch (lands first, separate from this migration)

Fix `load_skill_prompt()` in `src/stage2_api.py` to unzip `templates/study-notes.skill` and read `SKILL.md` from inside the archive, instead of reading the nonexistent flat `templates/study-notes-skill.md`. Preserve the existing fallback-to-minimal-prompt behavior for the genuinely-missing-file case (defense in depth), but the primary path must resolve to the real ZIP contents. This is a small, independent, easily-verified change — ship and verify it (confirm the real `SKILL.md` text now reaches the system prompt in a `--live` run) before starting the `src/agents/` work, since `src/agents/prompts.py` is written assuming correct ZIP-loading already exists.

The `templates/study-notes.skill` ZIP already contains non-trivial, validated logic that must be **wrapped as tools, not rewritten**:

```
SKILL.md, LESSONS.md
lib/build.js         — content.json + figures → .docx (Node `docx` package)
lib/dochelp.js        — OOXML rendering helpers (callouts, tables, stacked-fraction math)
lib/figbuild.py       — figures.json → PNG (matplotlib + geometry solver)
lib/figlib.py         — geometry primitives + auto-label collision detection
schema/content.schema.json, schema/figures.schema.json
tools/ingest.py       — PDF → text or autocropped PNG pages, SHA256-cached
tools/validate.py     — JSON Schema structural gate
tools/verify.py       — independently recomputes every numeric answer
tools/qa.py           — OOXML integrity, math presence, page-break safety, figure cross-check
tools/baseline.py     — quality regression vs quality/baseline.json
tools/invariants.py   — document skeleton enforcement (required sections, question count, coverage)
tools/pedagogy.py     — teaching-quality gates (analogy density, recall prompts, figure-text linkage)
tools/regress.py, tools/retro.py, tools/_log.py — offline/CI-adjacent, not part of the live agent loop
example/{content,figures}.json, quality/baseline.json, quality/reference_section/{content,figures}.json
```

All of these are deterministic Python/Node scripts with structured JSON/exit-code output — none of them involve an LLM today.

---

## Proposed Changes

### Rollout wrapper: all new work lands in new files; legacy implementation stays untouched

**Decision (2026-07-31):** none of the new multi-agent logic modifies `src/stage2_api.py` or `src/stage1_api.py` in place. Both stay **byte-for-byte unchanged** — including `AGENT_TOOLS`, `execute_tool()`, `_truncate_text_blocks()`, `run_router()`, and `llm_route()` — so the current monolithic-loop behavior remains fully intact and callable at all times. Every new component described below (`src/agents/*`) is net-new code.

A new dispatcher, `src/agents/dispatch.py`, exposes two functions with the exact same signatures as today's entry points, and chooses at call time which implementation actually runs:

```python
def generate_notes(target_dir: Path, live_mode: bool, verbose: bool) -> int:
    """Same signature/return as stage2_api.run_generate(). Dispatches to
    the legacy loop or the new graph based on config.STAGE2_IMPL."""
    if config.STAGE2_IMPL == "graph":
        from src.agents.stage2_graph import run_stage2_chapter
        return run_stage2_chapter(target_dir, live_mode)
    from src.stage2_api import run_generate
    return run_generate(target_dir, live_mode, verbose)

def route_file(path: Path, buckets: list[str], prior: dict | None) -> tuple[list[dict], bool, str]:
    """Same signature/return as stage1_api.llm_route(). Dispatches to
    the legacy router or the new Stage 1 graph based on config.STAGE1_IMPL."""
    if config.STAGE1_IMPL == "graph":
        from src.agents.stage1_graph import route_one_file
        return route_one_file(path, buckets, prior)
    from src.stage1_api import llm_route
    return llm_route(path, buckets, prior)
```

`config.STAGE1_IMPL` / `config.STAGE2_IMPL` (env-var driven, see settings changes below) default to `"legacy"`, so nothing changes behaviorally until explicitly flipped to `"graph"` per stage. The **only** touch to any existing file is a one-line import swap in `main.py` (import `generate_notes`/`route_file` from `src.agents.dispatch` instead of importing `run_generate`/`llm_route` directly) — that's wiring, not a logic change, and is easy to revert. This also gives the Verification Plan a clean way to A/B the two implementations on the same chapter by flipping one env var (see Verification Plan, step 3).

### Stage 1 — Triage/Extraction swarm

Implemented entirely in new files (`src/agents/stage1_graph.py`), reachable via the dispatcher above; `src/stage1_api.py`'s existing `run_router()`/`llm_route()` are left untouched as the `"legacy"` path. This is a light-touch change: Stage 1 is already one cheap, tool-forced Haiku call per file, so the new path becomes a small LangGraph graph rather than a fleet of independent agents.

- **`extract_node`** (deterministic, no LLM) — wraps the existing `extract_content()` snippet extraction (PDF/`.docx`/text) verbatim.
- **`triage_node`** (Haiku, `TRIAGE_MODEL`) — the same tool-forced call as today, using the same `ROUTER_TOOL` / `route_file` schema. No change to the classification logic, just formalized as a graph node.
- **`reconcile_node`** (deterministic) — wraps today's `_parse_matches()` plus the disagreement/park-for-review branching that currently lives inline in `run_assemble()`'s Stage A/B loops. This makes "confident disagreement → park for review" and "router unavailable → filename fallback" (`limited=True`) explicit conditional edges instead of duplicated nested `if` statements.

File-system side effects (`safe_copy`, `park_for_review`, `write_sources`, `_prev/` archival) stay in `stage1_api.py`'s existing outer loop, untouched. The graph itself only returns a routing *decision* (matches + confidence + disagreement flag) with the same shape as today's `llm_route()` return value, so `dispatch.route_file()` is a thin pass-through (decision approved 2026-07-31, see "Resolved Decisions" below).

```python
def route_one_file(path: Path, buckets: list[str], prior: dict | None) -> tuple[list[dict], bool, str]:
    """Drop-in replacement for llm_route(); same return shape:
    (matches, limited, model_used)."""
```

### Stage 2 — Orchestrator

New module `src/agents/stage2_graph.py`, reachable via `src/agents/dispatch.py` when `STAGE2_IMPL="graph"`. `src/stage2_api.py`'s existing monolithic loop is left untouched as the `"legacy"` path. A LangGraph `StateGraph[Stage2State]` implements the new path.

```python
class Stage2State(TypedDict):
    chapter_dir: str
    transcripts: list[str]
    supporting: list[str]
    content_json: dict | None
    figures_json: dict | None
    qa_report: dict | None
    qa_pass_count: int
    turn_count: int
    attempt_count: int
    messages: dict[str, list]   # keyed by node name — Author/QA/Figure keep separate histories
    status: Literal["running", "done", "failed_retryable", "failed_fatal"]
```

Graph shape:

```
ingest → author → figure → compiler → qa
                                        │
                       ┌────────────────┼─────────────────────┐
                  qa_passed        qa_failed_fixable    qa_failed_unfixable_or_maxed
                       │                 │                     │
                      END          back to author            END (failure)
```

```python
graph = StateGraph(Stage2State)
graph.add_node("ingest", ingest_node)
graph.add_node("author", author_node)
graph.add_node("figure", figure_node)
graph.add_node("compiler", compiler_node)
graph.add_node("qa", qa_node)
graph.add_edge("ingest", "author")
graph.add_edge("author", "figure")
graph.add_edge("figure", "compiler")
graph.add_edge("compiler", "qa")
graph.add_conditional_edges("qa", route_after_qa, {
    "pass": END,
    "retry": "author",
    "fail": END,
})
compiled = graph.compile(checkpointer=checkpointer)

def run_stage2_chapter(chapter_dir: str, live_mode: bool, resume: bool = True) -> int:
    """Drives the compiled graph for one chapter, returns the same exit codes
    (0 / 42 / 1) that run_generate() returns today."""
```

**Checkpointer (decision, approved 2026-07-31):** LangGraph's default `SqliteSaver`, pointed at `state/graph-checkpoints/<chapter>.sqlite`, is the source of truth for resume — this is what `run_stage2_chapter(resume=True)` reads on restart after a crash or rate-limit pause. In addition, `make_agent_node`'s wrapper (in `src/agents/base.py`) writes a plain human-readable JSON dump of the current `Stage2State` to `state/progress/<chapter>_debug.json` after every node completes — mirroring today's hand-readable progress-file workflow. This debug file is dev/inspection-only (`cat` it to see where a run is); it is never read back on resume, which always goes through the Sqlite checkpointer.

`route_after_qa` bounds the QA↔Author loop by `qa_pass_count`, capped at `config.QA_MAX_RETRY_LOOPS` (default 5) — this makes today's implicit "loop until turn 200 or the model stops calling tools" safety valve an explicit, targeted retry cap on the one edge that actually needs it, instead of a flat cap on raw API turns across the entire run.

The Orchestrator **is** the graph plus this thin driver function — there is no separate "Orchestrator LLM agent" call. Sequencing decisions that today live implicitly inside the model's own turn-by-turn tool choices are replaced by the graph's edges.

### Stage 2 — Author agent (Sonnet)

The one agent that keeps free-form generation. Owns authoring `content.json`/`figures.json` from transcripts and supporting material. Scoped tools only — no generic bash, no arbitrary-path edit:

```python
def tool_read_source(path: str) -> str:
    """Read one transcript/supporting text file. Path-scoped to chapter_dir + subdirs."""

def tool_view_source_page(path: str, page: int) -> ImageBlock:
    """Render one PDF page of a source doc as an image for visual reading of
    scanned/handwritten material. Wraps tool_view_pdf_page (func_tools_and_utils.py)."""

def tool_write_content_json(content: dict) -> dict:
    """Validate `content` against schema/content.schema.json via tools/validate.py
    (subprocess-wrapped) before writing to <chapter_dir>/content.json.
    Returns {ok: bool, errors: list[str]} — forces schema conformance at write
    time instead of discovering it later in the QA node."""

def tool_write_figures_json(figures: dict) -> dict:
    """Same pattern against schema/figures.schema.json."""

def tool_read_qa_feedback() -> dict:
    """On a retry loop (QA routed back to Author), returns the prior qa_report's
    structured findings — not the whole graph history."""
```

Author's system prompt is `SKILL.md` (unzipped from `templates/study-notes.skill`) plus `example/content.json` / `example/figures.json`, loaded once via `src/agents/prompts.py` and marked for prompt caching (see Cost Mitigation below).

### Stage 2 — Figure agent (Haiku)

Wraps `lib/figbuild.py` as its one real tool, plus a narrow self-check surface:

```python
def tool_figbuild(figures_json_path: str, chapter_dir: str) -> dict:
    """Wraps lib/figbuild.py unchanged: figures.json -> rendered PNGs.
    subprocess.run(["python3", "figbuild.py", figures_json_path, "--out", chapter_dir]).
    Returns {ok: bool, rendered: list[str], errors: list[str]}."""

def tool_view_figure(png_path: str) -> ImageBlock:
    """Reuses tool_view_image, scoped to the chapter's own figure output dir."""
```

Loop: `figbuild → view each rendered figure → if visibly broken, report a structured note back to Author via the graph's failure edge`. Figure never edits `figures.json` itself and never retries `figbuild.py` internally — **decision (approved 2026-07-31): every figure problem always bounces back to Author** to edit `figures.json`'s source description, keeping "who writes content" and "who renders it" cleanly separated and keeping Figure's own reasoning limited to "did the render succeed, yes/no."

### Stage 2 — QA agent

**One aggregate node, not five separate agents.** `validate.py`, `verify.py`, `invariants.py`, `pedagogy.py`, `baseline.py`, `qa.py` are all deterministic scripts with structured pass/fail output — splitting them into five LangGraph nodes with five separate LLM calls would multiply Haiku calls for zero reasoning benefit.

```python
def tool_run_structural_gates(content_json_path: str, figures_json_path: str) -> dict:
    """Runs validate.py (schema), verify.py (numeric answer recomputation),
    invariants.py (skeleton / question-count / coverage) as subprocesses.
    All three always run (no fail-fast) so Author sees every failure at once.
    Returns {schema_ok, verify_ok, invariants_ok, failures: [...]}."""

def tool_run_quality_gates(content_json_path: str, chapter_dir: str) -> dict:
    """Runs pedagogy.py (analogy density, recall prompts, worked-example rigor,
    figure-text linkage) and baseline.py (regression vs quality/baseline.json).
    Returns {pedagogy_ok, baseline_ok, deltas: {...}}."""

def tool_run_document_qa(docx_path: str, content_json_path: str, do_pdf_check: bool = False) -> dict:
    """Runs qa.py (OOXML integrity, math/fraction presence, page-break safety,
    figure cross-check, optional --pdf layout checks via soffice+pdftotext+pdftoppm).
    Returns {ok, issues: [...], pdf_checked: bool}."""
```

**Decision (approved 2026-07-31): no LLM call in the QA node at all** — pure Python aggregation (`ok = all(...)`) producing one structured `qa_report`, at zero marginal API cost. The `qa_report` is passed back to Author as raw JSON with light Python-side triage (blocking/highest-severity gate failures surfaced first, full detail attached) rather than a natural-language summary — QA stays fully non-agentic end to end.

### Stage 2 — Compiler agent (Haiku, diagnosis-only)

Wraps `lib/build.js`. The most mechanical of the five agents — genuinely closer to "no LLM" than an agent:

```python
def tool_compile_docx(content_json_path: str, figures_json_path: str, chapter_dir: str) -> dict:
    """Wraps lib/build.js: subprocess.run(["node", "build.js", content_json_path,
    "--figs", figs_dir, "-o", docx_path]).
    Returns {ok: bool, docx_path: str | None, stderr: str | None}."""
```

Since schema-valid JSON should essentially never fail the deterministic builder, Compiler's only reasoning job is: if `build.js` does fail, translate its Node stack trace into a plain-English diagnosis for Author, rather than routing a raw stderr blob back into the graph.

### Telemetry &amp; State Tracing

Two complementary layers, both optional/opt-in beyond the local debug dump already decided above:

1. **Local debug hook (already decided, no new dependency).** As described under "Stage 2 — Orchestrator," `make_agent_node`'s wrapper in `src/agents/base.py` dumps the current `Stage2State` to `state/progress/<chapter>_debug.json` (plain JSON, human-readable) after every node executes — regardless of whether LangSmith tracing is enabled. This is the default, always-on tracing mechanism and requires no external service or API key.
2. **LangSmith (optional, off by default).** LangGraph integrates with LangSmith out of the box: setting `LANGCHAIN_TRACING_V2=true` and `LANGSMITH_API_KEY=<key>` in the environment is enough to get a visual trace of every graph transition, tool call, latency, and token count per node in the LangSmith UI, with zero code changes required beyond the env vars. This is genuinely useful for debugging the QA↔Author retry loop and comparing legacy-vs-graph runs side by side (Verification Plan step 4). **Flag before enabling:** LangSmith is a third-party SaaS — turning tracing on sends full prompts/tool payloads (which include chapter transcript content) to LangChain's cloud. Keep this **off by default** (unset `LANGCHAIN_TRACING_V2`, the LangGraph default), and only opt in per-run for a specific debugging session, on non-sensitive chapters, with awareness that content leaves the local machine.

### Shared tools module

`src/agents/tools.py` — the formal skill-script → Agent Tool mapping:

| Skill script | Tool function | Used by |
|---|---|---|
| `tools/ingest.py` | `tool_ingest(pdf_path, chapter_dir) -> dict` | `ingest` node (pre-Author) |
| `lib/figbuild.py` | `tool_figbuild(figures_json_path, chapter_dir) -> dict` | Figure |
| `lib/build.js` | `tool_compile_docx(content_json_path, figures_json_path, chapter_dir) -> dict` | Compiler |
| `tools/validate.py`, `tools/verify.py`, `tools/invariants.py` | `tool_run_structural_gates(...)` | QA |
| `tools/pedagogy.py`, `tools/baseline.py` | `tool_run_quality_gates(...)` | QA |
| `tools/qa.py` | `tool_run_document_qa(...)` | QA |
| `tools/retro.py`, `tools/regress.py`, `tools/_log.py` | not wrapped — offline/CI-adjacent, reused directly for run logging, not exposed to any agent |

All wrappers are `subprocess.run(...)` calls around the existing unzipped scripts — never reimplemented. `tool_read_source`, `tool_view_source_page`, `tool_write_content_json`, `tool_write_figures_json`, `tool_view_figure` also live here, all path-scoped to `chapter_dir` (reusing the traversal-safety pattern already present in `tool_view_pdf_page`/`tool_convert_to_png`). No node gets a generic bash escape hatch.

`src/agents/base.py` — `Stage2State`, a `make_agent_node(model, tools, system_prompt_fn)` factory shared by Author/Figure/Compiler (so each node isn't hand-rolled), and checkpointer wiring.

`src/agents/prompts.py` — loads `SKILL.md` from the ZIP (relying on the Prerequisite Patch above already being in place), assembles each agent's system prompt slice around a shared cacheable prefix. In `DEV_TOKEN_SAVER_MODE` (see below) this loader is bypassed entirely in favor of a 1-sentence dummy prompt.

### config/settings.py changes

```python
AUTHOR_MODEL   = os.environ.get("STUDY_NOTES_AUTHOR_MODEL", "claude-sonnet-5")
FIGURE_MODEL   = os.environ.get("STUDY_NOTES_FIGURE_MODEL", "claude-haiku-4-5")
COMPILER_MODEL = os.environ.get("STUDY_NOTES_COMPILER_MODEL", "claude-haiku-4-5") # error-diagnosis only
TRIAGE_MODEL   = os.environ.get("ASSEMBLE_ROUTER_MODEL", "claude-haiku-4-5-20251001")  # renamed alias, kept for compat
QA_MAX_RETRY_LOOPS  = int(os.environ.get("STUDY_NOTES_QA_MAX_RETRIES", "5"))
GRAPH_CHECKPOINT_DIR = STATE_DIR / "graph-checkpoints"
GENERATOR_MODEL = AUTHOR_MODEL   # back-compat alias

# Rollout wrapper (src/agents/dispatch.py reads these; default = fully legacy, zero behavior change)
STAGE1_IMPL = os.environ.get("STUDY_NOTES_STAGE1_IMPL", "legacy")   # "legacy" | "graph"
STAGE2_IMPL = os.environ.get("STUDY_NOTES_STAGE2_IMPL", "legacy")   # "legacy" | "graph"

# Cost-safe dev/test toggle (see "Cost-Safe Dev/Test Mode" below)
DEV_TOKEN_SAVER_MODE = os.environ.get("DEV_TOKEN_SAVER_MODE", "0") == "1"
```

Note: `QA_MODEL` is intentionally not added — per the QA decision above, QA runs no LLM call at all.

### requirements.txt changes

```
langgraph>=0.2.0
```

**Decision (approved 2026-07-31): `langchain-anthropic` is not added.** Each LangGraph node calls the raw `anthropic` SDK directly (`client.messages.create(...)`), keeping `classify_api_error()` and `TokenTracker` working unchanged, since both are written against raw SDK exception types and `response.usage`.

### File tree

**Create (all net-new; nothing below touches existing implementation files):**
- `src/agents/__init__.py`
- `src/agents/dispatch.py` — the legacy/graph rollout wrapper described above
- `src/agents/base.py`
- `src/agents/tools.py`
- `src/agents/stage2_graph.py`
- `src/agents/stage1_graph.py`
- `src/agents/prompts.py`
- `tests/test_stage2_graph.py`
- `tests/test_stage1_graph.py`
- `tests/test_agents_tools.py`
- `tests/test_dispatch.py` — asserts `dispatch.generate_notes`/`dispatch.route_file` call the correct implementation for each value of `STAGE1_IMPL`/`STAGE2_IMPL`

**Unchanged (by design — this is the point of the rollout wrapper):**
- `src/stage2_api.py` — including `AGENT_TOOLS`, `execute_tool()`, `_truncate_text_blocks()`, the mock-mode branch, and the live-mode turn loop. Remains fully callable as the `"legacy"` path.
- `src/stage1_api.py` — including `run_router()`, `llm_route()`, `ROUTER_TOOL`, and the Stage A/B loop structure (`park_for_review`, `write_sources`, `safe_copy`). Remains fully callable as the `"legacy"` path.
- `src/func_tools_and_utils.py` — `tool_convert_to_png`, `tool_view_image`, `tool_view_pdf_page`, `_text_block`, `classify_api_error`, `TokenTracker` are all reused as-is (imported, not modified) by the new scoped tool wrappers in `src/agents/tools.py`.

**Modify (additive only — config/wiring, not logic):**
- `config/settings.py` — additions listed above.
- `requirements.txt` — addition listed above.
- `main.py` — one-line import swap: import `generate_notes`/`route_file` from `src.agents.dispatch` instead of importing `run_generate`/`llm_route` directly from their original modules. This is the only change to any pre-existing file in the whole migration.
- `docs/architecture.md`, `docs/skill-integration-plan.md`, `docs/stage2-claude-cli-migration-plan.md` — reconcile with this design; they currently describe the old single-loop architecture as the only architecture.

**Delete:** nothing, anywhere. The legacy implementation is kept indefinitely as the fallback path (or until a future decision is made to retire it once the graph path has proven itself in production).

---

## Cost-Safe Dev/Test Mode: `DEV_TOKEN_SAVER_MODE`

To let the full graph control-flow be exercised in real `--live` mode — real API calls, real conditional edges, real checkpoint/resume — without spending real generation-scale tokens, add a dedicated toggle that sits between mock mode (zero API calls, no graph invocation at all) and full production `--live` mode (real skill prompt, real models, real PDFs).

**Toggle:** `DEV_TOKEN_SAVER_MODE=1` environment variable, read into `config.DEV_TOKEN_SAVER_MODE` (see settings changes above). When set, every LangGraph node in `src/agents/` checks this flag and short-circuits to cheap behavior; no code path needs a separate CLI flag, though `main.py` may optionally also accept `--dev-token-saver` as a convenience alias that sets the same env var before importing `config`.

When `DEV_TOKEN_SAVER_MODE` is on, all five behaviors below apply together (it is an all-or-nothing dev toggle, not five independent flags):

1. **Force cheap models.** `src/agents/base.py`'s node factory overrides every agent's configured model to the cheapest one already defined in this project — `config.FIGURE_MODEL` (`claude-haiku-4-5`) — for Author, Figure, Compiler, and Triage alike, regardless of their normal per-agent model config. (Note: the project standardizes on `claude-haiku-4-5` already, used today for `FIGURE_MODEL`/`TRIAGE_MODEL` — this toggle reuses that same id rather than introducing a different/older Haiku version.)
2. **Swap prompts.** `src/agents/prompts.py` skips loading `SKILL.md` from the ZIP entirely and returns a fixed one-sentence dummy system prompt (e.g. `"You are a test agent; call at most one tool, then stop."`) for every agent. No ZIP read, no prompt-cache setup.
3. **Cap output.** Every `client.messages.create(...)` call made by any node passes a small hardcoded `max_tokens` (e.g. `50`) instead of the agent's normal limit, bounding worst-case runaway generation to a trivial cost per call.
4. **Truncate inputs.** The `ingest` node / `tool_ingest` intercepts real transcript/supporting PDFs and, instead of running real `pdftotext`/`pdftoppm` extraction, returns a small fixed dummy string (e.g. `"Sample transcript content for testing."`) as the "extracted" content for every source file.
5. **Purpose.** With all four of the above active, a full `ingest → author → figure → compiler → qa` pass (including the QA↔Author conditional retry edge, the `SqliteSaver` checkpoint, and the debug-dump file) can be run end-to-end against the real Anthropic API for a few cents at most, validating graph wiring and control flow before any real, expensive `--live` run. See Verification Plan, step 3.

**How to actually run this smoke test so it lands in `logs/study-notes-pipeline.log`:** invoking `python3 src/main.py` directly does NOT write to that log file (the Python logger is stdout-only by design — see `src/func_tools_and_utils.py::setup_logger`'s docstring; only `scripts/wsl-study-notes-processor.sh`'s `exec >>"$LOG_FILE" 2>&1` redirect populates it). Run the smoke test through the wrapper instead:

```
DEV_TOKEN_SAVER_MODE=1 STUDY_NOTES_STAGE2_IMPL=graph bash scripts/wsl-study-notes-processor.sh --stage2-mode llm-token-saver
```

`--stage2-mode llm-token-saver` must be passed explicitly -- the wrapper has no implicit
no-args nightly fallback of its own, so invoking it with no args at all would leave Stage 2
at `main.py`'s own `off` default and never touch the graph at all.

This exercises the exact same code path as the real nightly automation (mount check, dummy-mode gate, `main.py` invocation) with the dev-token-saver flags applied, and the run's full output ends up appended to `logs/study-notes-pipeline.log` like any other pipeline run.

This mode is dev/test-only and must never be enabled for a real nightly or supervised production run — it produces a placeholder `.docx`, not usable study notes.

---

## Verification Plan (local pilot rollout)

1. **Unit tests first, no network.** `tests/test_stage1_graph.py`, `test_stage2_graph.py`, `test_agents_tools.py` mock `client.messages.create` (or the LangChain equivalent) to return canned tool-forced responses, and mock every `subprocess.run` in `src/agents/tools.py` to assert argv construction and JSON/exit-code parsing in isolation. Existing `test_classify_and_rename.py`, `test_main_cli.py`, `test_utils.py` must keep passing unmodified.
2. **Mock-mode graph wiring, zero API calls.** Run `python3 src/main.py --run-assemble-no-llm --dry-run --run-generate-no-llm` with `STUDY_NOTES_STAGE1_IMPL=graph STUDY_NOTES_STAGE2_IMPL=graph` — the exact command the WSL "dummy mode" gate already uses — and confirm both stages short-circuit identically to today (no graph/LLM invocation at all in mock mode; the mock-mode branch in `run_generate()` is preserved verbatim, untouched) and log "0 tokens consumed" exactly as today.
3. **`DEV_TOKEN_SAVER_MODE` smoke test.** With `STAGE2_IMPL=graph` and `DEV_TOKEN_SAVER_MODE=1`, run one chapter in `--live` mode. This exercises the *entire* graph control flow — `ingest → author → figure → compiler → qa`, the conditional QA↔Author retry edge, checkpoint/resume, and the debug-dump file — with real (but trivial and capped) API calls, at near-zero cost. This is the cheapest way to validate wiring before spending real tokens, and sits between step 2 (zero API calls) and step 4 (full real cost). See "Cost-Safe Dev/Test Mode" below.
4. **Legacy-vs-graph A/B on one real chapter, `--live`, regression vs. last known-good.** Because the dispatcher can invoke either implementation via one env var, run the *same* chapter twice — once with `STAGE2_IMPL=legacy` (today's monolithic loop) and once with `STAGE2_IMPL=graph` (`DEV_TOKEN_SAVER_MODE=0`, real skill prompt, real models) — and diff the two `.docx` outputs directly. Before that: feed the skill's own `example/content.json`/`figures.json` straight into Figure→Compiler→QA (bypassing Author) to validate that half of the graph before spending any Sonnet tokens. Confirm word count, figure count, all QA gate pass/fail status, and turn/attempt/token counts (via `TokenTracker`) are in the same ballpark between the two runs.
5. **Resume-from-crash test.** Kill the process mid-graph (e.g. during the Figure node) and confirm the LangGraph `SqliteSaver` checkpointer resumes from the last completed node rather than restarting Author from scratch, and that `state/progress/<chapter>_debug.json` reflects the state at the point of the crash.
6. **Exit-code and marker-file contract test.** Force each of the three exit paths (retryable, fatal, success) with `STAGE2_IMPL=graph` and confirm `_hold`, `_notes_done`, `_notes_FAILED.txt`, and `state/retry-epoch.txt` are written in the same locations with the same content shape as the `legacy` path, and that `main.py`'s exit code (0 / 42 / 1) is unchanged — so `scripts/win-environment-setup.ps1`'s exit-code branching needs zero changes.
7. **Re-enable nightly automation last.** Only after steps 1–6 pass, let `scripts/wsl-study-notes-processor.sh`'s existing nightly invocation run unmodified (still mock-mode for generation, zero cost, and still `STAGE1_IMPL=legacy`/`STAGE2_IMPL=legacy` by default) for a few nights. Then flip one env var at a time — first `STAGE1_IMPL=graph` alone, then `STAGE2_IMPL=graph` for one supervised `--live` chapter — before trusting unattended `graph`-path runs. Because the legacy path is never modified or removed, flipping either env var back to `legacy` is an instant rollback at any point.

---

## User Review Required — all items resolved 2026-07-31

### 1. Framework selection: LangGraph vs. CrewAI — **Approved: LangGraph**

LangGraph's `StateGraph` plus built-in checkpointing map directly onto this codebase's existing resume-from-`state/progress/<chapter>.json` design — a checkpoint is a strict superset of what's manually done today (dumping `{turn, messages, attempts}` to JSON after every turn). CrewAI's role-based "Crew" abstraction is built around independent agents collaborating via a manager/hierarchical process, which fits open-ended delegation problems; here the Author→Figure→Compiler→QA sequence is a **fixed pipeline with one conditional loop-back edge**, which is exactly LangGraph's `add_conditional_edges` pattern. LangGraph also lets each node use a different model (Sonnet for Author, Haiku elsewhere) with zero framework friction, whereas CrewAI's agent-role model is more naturally single-model-per-crew.

This is a new dependency in a project with zero framework dependencies today beyond the raw `anthropic` SDK, and changes the debugging story from "read a flat JSON progress file by hand" to LangGraph's checkpoint model — mitigated by the `SqliteSaver` + human-readable debug-dump design above, and by the fact that the legacy path remains fully available via the rollout wrapper.

### 2. API cost mitigation — **Approved: both levers**

- **Prompt caching for `SKILL.md`.** Mark Author's system prompt block with `cache_control: {"type": "ephemeral"}`. **Approved approach: shared cache prefix** — every agent (Author, Figure, Compiler) that needs any part of `SKILL.md` gets the *entire* cached document as a shared prefix, maximizing cross-agent cache hits, accepting that Figure/Compiler see some irrelevant sections in exchange for guaranteed cache reuse.
- **LLM-free QA + Haiku elsewhere.** Approved: QA runs no LLM call at all (pure Python aggregation, $0 marginal cost); Figure and Compiler(diagnosis-only) and Triage use Haiku; only Author uses Sonnet.

---

## Resolved Decisions (formerly Open Questions) — 2026-07-31

1. **Stage 1 side effects:** `reconcile_node` returns a decision only (matches + confidence + disagreement flag). `park_for_review`, `safe_copy`, and `write_sources` stay in `stage1_api.py`'s existing outer loop — untouched, per the rollout-wrapper decision above.
2. **QA→Author retry payload:** raw structured JSON, with light Python-side triage (highest-severity/blocking gate failures surfaced first, full detail attached) — no natural-language synthesis, no LLM call in QA.
3. **Figure self-retry:** none. Every figure problem always bounces back to Author to edit `figures.json`'s source description; Figure's own reasoning never goes beyond "did the render succeed, yes/no."
4. **Checkpointer backend:** LangGraph's default `SqliteSaver` (`state/graph-checkpoints/<chapter>.sqlite`) is the resume source of truth, **plus** a debug-dump step: `make_agent_node`'s wrapper writes the current `Stage2State` as plain JSON to `state/progress/<chapter>_debug.json` after every node, preserving the "`cat` the state file to debug" workflow as an inspection aid (never read back on resume).
5. **LangChain vs. raw SDK:** `langgraph` alone; every node calls the raw `anthropic` SDK directly. `langchain-anthropic` is not added as a dependency.
6. **`load_skill_prompt()` bug fix:** lands as a separate prerequisite patch (see "Prerequisite Patch" section near the top of this document), landed and verified before any `src/agents/` work begins — not bundled into this migration's own file changes.
