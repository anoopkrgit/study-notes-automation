# Multi-Agent Migration — Implementation Walkthrough

## Summary

Implemented the full multi-agent architecture described in [migration-to-agents.md](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/docs/migration-to-agents.md). All new code is **net-new files** — zero existing implementation files were modified (except additive config/requirements changes).

---

## Files Created (11 new files)

### `src/agents/` — Core Agent Framework

| File | Size | Purpose |
|------|------|---------|
| [\_\_init\_\_.py](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/src/agents/__init__.py) | 256B | Package docstring with rollout wrapper instructions |
| [base.py](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/src/agents/base.py) | 7.0KB | `Stage1State`, `Stage2State` TypedDicts, `make_agent_node()` factory (raw Anthropic SDK tool loop), `get_checkpointer()` (SqliteSaver) |
| [prompts.py](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/src/agents/prompts.py) | 4.0KB | ZIP-based SKILL.md loading, prompt caching with `cache_control: ephemeral`, DEV mode dummy prompts |
| [tools.py](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/src/agents/tools.py) | 18KB | All 13 tool wrappers (subprocess-based), Anthropic tool schemas (`AUTHOR_TOOLS`, `FIGURE_TOOLS`, `COMPILER_TOOLS`), `TOOL_REGISTRY`, path traversal safety |
| [stage1_graph.py](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/src/agents/stage1_graph.py) | 7.1KB | Stage 1 LangGraph: `extract_node` → `triage_node` → `reconcile_node`, `route_one_file()` drop-in |
| [stage2_graph.py](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/src/agents/stage2_graph.py) | 7.8KB | Stage 2 LangGraph: `ingest` → `author` → `figure` → `compiler` → `qa` with conditional QA↔Author retry, `run_stage2_chapter()` driver |
| [dispatch.py](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/src/agents/dispatch.py) | 1.5KB | Rollout wrapper: `generate_notes()` / `route_file()` dispatch to legacy or graph based on env vars |

### `tests/` — Test Suite (4 new files)

| File | Size | Tests |
|------|------|-------|
| [test_dispatch.py](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/tests/test_dispatch.py) | 2.1KB | Legacy/graph dispatch routing for both stages |
| [test_agents_tools.py](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/tests/test_agents_tools.py) | 3.7KB | Path traversal safety, DEV mode dummies, TOOL_REGISTRY completeness |
| [test_stage1_graph.py](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/tests/test_stage1_graph.py) | 4.2KB | Extract/triage/reconcile nodes, route_one_file return shape |
| [test_stage2_graph.py](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/tests/test_stage2_graph.py) | 4.2KB | Ingest/QA nodes, route_after_qa logic (pass/retry/fail), mock mode, graph structure |

---

## Files Modified (2 files, additive only)

| File | Change |
|------|--------|
| [settings.py](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/config/settings.py) | Added: `AUTHOR_MODEL`, `FIGURE_MODEL`, `COMPILER_MODEL`, `TRIAGE_MODEL`, `QA_MAX_RETRY_LOOPS`, `GRAPH_CHECKPOINT_DIR`, `STAGE1_IMPL`, `STAGE2_IMPL`, `DEV_TOKEN_SAVER_MODE` |
| [requirements.txt](file:///c:/06-PROJECTS/trial/study-notes-automation-redesigned/requirements.txt) | Added: `langgraph>=0.2.0` |

---

## Files NOT Modified (by design)

These remain byte-for-byte unchanged per the migration plan's rollout wrapper decision:

- `src/func_generate_notes.py` — legacy monolithic loop intact
- `src/func_assemble_chapters.py` — legacy router intact
- `src/func_tools_and_utils.py` — `classify_api_error`, `TokenTracker`, all tools reused as-is
- `src/main.py` — import swap NOT yet applied (see Next Steps)
- `tests/test_classify_and_rename.py`, `tests/test_main_cli.py`, `tests/test_utils.py` — untouched

---

## Key Design Decisions Implemented

1. **Raw Anthropic SDK inside LangGraph nodes** — `langchain-anthropic` NOT added; `classify_api_error()` and `TokenTracker` work unchanged.
2. **QA node is LLM-free** — pure Python aggregation of deterministic script results, $0 marginal API cost.
3. **Prompt caching** — `SKILL.md` marked with `cache_control: {"type": "ephemeral"}` as shared prefix across all agents.
4. **DEV_TOKEN_SAVER_MODE** — forces Haiku, dummy prompts, `max_tokens=50`, and dummy inputs across all nodes.
5. **No figure self-retry** — all figure problems bounce back to Author via the graph's conditional edge.

## Code Review Fixes Applied

During review of subagent output, I caught and fixed 3 issues:
- `qa_node` was checking for a nonexistent `'pass'` key — fixed to use actual gate keys (`schema_ok`, `verify_ok`, `invariants_ok`, `pedagogy_ok`, `baseline_ok`, `ok`)
- `route_after_qa` was mutating state directly — fixed to be a pure router function
- `triage_node` was passing a dict to `TokenTracker.record()` — fixed to pass the raw `response.usage` object

---

## Next Steps (for you)

> [!IMPORTANT]
> **One remaining wiring change.** The plan calls for a one-line import swap in `main.py` to route through the dispatcher. This was intentionally deferred since the plan says *"the only touch to any existing file"* — you should apply it manually when you're ready to test:
> ```diff
> -from src.func_generate_notes import run_generate
> +from src.agents.dispatch import generate_notes as run_generate
> ```

### 3. State Management & Missing Status Bug Fix
- **The Issue:** Even though the QA node evaluated all gates correctly as True in mock mode, the overarching StateGraph retained `status = 'running'` instead of setting it to `done`. This caused `run_stage2_chapter` to fall through to the failure condition, writing an empty `_notes_FAILED.txt` instead of the success marker.
- **The Fix:** Modified `qa_node` in `stage2_graph.py` to correctly evaluate `qa_report['all_passed']` and update the graph state with `{'status': 'done' if qa_report['all_passed'] else 'running'}`.

### 4. Full Pipeline Verification
- Executed the full pipeline: Stage 1 (Legacy logic via `--run-assemble-no-llm`) + Stage 2 (`DEV_TOKEN_SAVER_MODE=1 STUDY_NOTES_STAGE2_IMPL=graph`).
- Cleared the `/checkpoints` Sqlite directory and previous `_notes_done` and `_notes_FAILED.txt` markers before execution.
- Successfully verified that the graph processes the real target chapters from `000-Education/...`, passes all mock QA gates, cleanly exits with `EXIT_OK (0)`, and correctly touches the `_notes_done` marker file for `Physics-Ch2-Work-Energy-Theorem`.

### Verification Plan (from the migration doc)
1. **Unit tests** — `pytest tests/test_dispatch.py tests/test_agents_tools.py tests/test_stage1_graph.py tests/test_stage2_graph.py`
2. **Mock mode** — `STUDY_NOTES_STAGE2_IMPL=graph python3 src/main.py --run-generate-no-llm` (should short-circuit identically to legacy)
3. **DEV_TOKEN_SAVER_MODE smoke test** — `STUDY_NOTES_STAGE2_IMPL=graph DEV_TOKEN_SAVER_MODE=1 python3 src/main.py --run-generate --live` (real API calls, trivial cost)
4. **Legacy-vs-graph A/B** — run the same chapter with `STAGE2_IMPL=legacy` then `STAGE2_IMPL=graph`, diff outputs
5. **Resume test** — kill mid-graph, restart, confirm SqliteSaver resumes from last node
6. **Exit code contract** — confirm markers (`_notes_done`, `_notes_FAILED.txt`) and exit codes (0/42/1) match legacy
