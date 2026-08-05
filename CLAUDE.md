# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

An unattended pipeline that turns raw 9th-grade PCM (Physics/Chemistry/Maths) lecture
transcripts + reference material into print-ready `.docx` study notes. Two stages:

1. **Stage 1 (Assembler)** — classifies incoming files by subject/chapter (LLM router,
   cheap model first, escalates on low confidence) and files them into per-chapter folders.
2. **Stage 2 (Generator)** — picks the next ready chapter and generates the full `.docx`
   (diagrams, worked examples, practice problems, cheat sheet).

Runs nightly via Windows Task Scheduler waking the machine at 3 AM, driving a WSL/Python
engine — see `docs/architecture.md` for the full invocation tree (PowerShell → WSL bash →
`main.py` → per-stage implementation → retry/sleep handling). `docs/setup-guide.md` covers
the Windows+WSL setup itself.

## Commands

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run one test file / one test
python3 -m pytest tests/test_claude_cli_subprocess_stage2.py -v
python3 -m pytest tests/test_claude_cli_subprocess_stage2.py::test_web_enrichment_enabled_adds_scoped_tools_and_search_cap -v

# Environment health check (tools on PATH, API key present, dirs exist) — no pipeline run
python3 src/main.py --doctor

# Cheap end-to-end smoke test of both stages' wiring (real API/CLI calls, near-zero cost,
# dummy content) — the fastest way to confirm nothing is broken after a change
python3 src/main.py --stage1-mode llm-token-saver --stage2-mode llm-token-saver --verbose
# or: bash run_test.sh (same thing, against the real target-root chapter folders)

# Stage 1 only, no LLM (deterministic filename routing, zero tokens)
python3 src/main.py --stage1-mode no-llm

# Stage 2 dry preview (zero tokens, no .docx produced)
python3 src/main.py --stage2-mode no-llm

# Real full-cost run of one stage (spends tokens / CLI budget)
python3 src/main.py --stage1-mode llm-full
python3 src/main.py --stage2-mode llm-full

# Stage 2 against one specific chapter folder (bypasses auto-selection/_hold)
python3 src/main.py --stage2-mode llm-full --target-dir "<AI-Chapter-Notes>/<Chapter-Folder>"

# Choose which of the 3 parallel implementations runs each stage (see Architecture below)
python3 src/main.py --stage1-mode llm-full --stage1-impl subprocess
python3 src/main.py --stage2-mode llm-full --stage2-impl graph
```

No linter/formatter is configured in this repo. There's no `pyproject.toml`/`pytest.ini`;
pytest runs off plain file/function discovery in `tests/`.

## Architecture: three parallel implementations per stage

The core thing to understand before touching Stage 1 or Stage 2 code: **each stage has
three independent, side-by-side implementations**, chosen per-run via `--stage1-impl`/
`--stage2-impl {legacy,graph,subprocess}` (default `legacy` for both). `src/agents/dispatch.py`
(`generate_notes()`, `route_file()`) is the *only* place that decides which one runs —
`main.py` and everything downstream never import a specific implementation directly.

| impl | Stage 1 module | Stage 2 module | How it talks to Claude |
|---|---|---|---|
| `legacy` (default) | `src/direct_api/stage1_api.py` (`llm_route`) | `src/direct_api/stage2_api.py` (`run_generate`) | Direct Anthropic SDK, single call / monolithic tool-call loop |
| `graph` | `src/agents/stage1_graph.py` (`route_one_file`) | `src/agents/stage2_graph.py` (`run_stage2_chapter`) | LangGraph flowchart of small specialized agents (see `src/agents/__init__.py` for the full "what is an agent/node/edge here" explainer — read that before touching this folder), still direct SDK calls underneath |
| `subprocess` | `src/claude_cli_subprocess/stage1_cli.py` (`route_file`) | `src/claude_cli_subprocess/stage2_cli.py` (`run_stage2_chapter`) | Shells out to the `claude` CLI (`subprocess.Popen(["claude", "-p", ...])`), billed against a Claude subscription instead of the metered API key |

Why three: the old `legacy` code is kept completely untouched as a risk-free rollback;
`graph` and `subprocess` were both built alongside it without modifying it. Flipping
`--stage1-impl`/`--stage2-impl` back to `legacy` is never a code change, just a flag.
`src/func_tools_and_utils.py` and `src/func_classify_and_rename.py` (chapter taxonomy —
the one place that knows "lecture 5 of Physics is Chapter 4") are shared by all three.

For `subprocess`, the actual multi-step generation pipeline (ingest → author JSON →
validate → figures → verify → build → QA) is NOT Python code — it's delegated to the
packaged Claude Code skill at `templates/study-notes.skill`. That file is a **tracked
zip**, not a directory; there is no separate unpacked source tree in git. To edit its
`SKILL.md` (or anything else inside), unzip it, edit, and re-zip in place — see the
web-enrichment commit history for the exact unzip/edit/rezip approach. `.claude/skills/`
and `.skill-runtime/` in this repo are gitignored *local extraction caches* of that zip
(`sync_skill_package()` in `stage2_cli.py`), not sources of truth.

`DEV_TOKEN_SAVER_MODE` (set via `--stageN-mode llm-token-saver`) is a cost-safe smoke-test
toggle honored by all three implementations: cheap model, dummy prompt/content, capped
output, no escalation. It's how CI-cost-free wiring checks work — never enable it for a
real production run.

## State & idempotency

- `state/assemble-state.json` — SHA-256 content hashes of every file Stage 1 has already
  routed; re-runs only process genuinely new files. Don't hand-edit casually.
- `state/retry-epoch.txt` — written on a retryable API/CLI error with the actual
  reset time (read from response headers when available); the nightly wrapper schedules a
  Windows wake task against it.
- `state/progress/<chapter>.json` — Stage 2's cross-run resume state (conversation
  history for `legacy`/`graph`, `claude` session id for `subprocess`).
- Per-chapter marker files: `_hold` (not ready yet), `_notes_done` (success), `_notes_FAILED.txt`
  (non-retryable failure, needs a human), `_sources.txt`/`_web-sources.txt` (attribution
  manifests — the latter is built from Claude Code's own session transcript, not the
  model's self-report; see `write_web_sources_manifest()` in `stage2_cli.py`).
- Source files are read-only: Stage 1 only ever *copies* into chapter folders, never
  moves/deletes from the incoming source directories.

## Configuration

`config/settings.py` is the single source of truth for paths, models, and every tunable —
almost everything is `os.environ.get(...)`-backed with a sane default, so behavior changes
via env vars, not code edits. Notable groups: base paths/models (top of file), the
`STAGE1_IMPL`/`STAGE2_IMPL`/`DEV_TOKEN_SAVER_MODE` rollout switches, the `CLAUDE_*` block
(subprocess-implementation CLI flags/timeouts), and the `ENABLE_WEB_ENRICHMENT`/
`WEB_SEARCH_ALLOWED_DOMAINS`/`MAX_WEB_SEARCHES_PER_CHAPTER` block (bounded web search for
the `subprocess` Stage 2 generator only — see `docs/web-enrichment-plan.md`).

## Docs worth reading before larger changes

- `docs/architecture.md` — the full invocation tree, Windows→WSL→Python→retry/sleep flow.
- `docs/migration-to-agents.md` / `docs/agent_migration_walkthrough.md` — why the `graph`
  implementation exists and how it was verified against `legacy`.
- `docs/web-enrichment-plan.md` — bounded web search design for the `subprocess` Stage 2
  generator, including why its guardrail mechanism (`WebFetch(domain:...)` permission
  rules, `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION`) is CLI-specific and doesn't carry over
  to `direct_api`/`agents` as-is.
- `docs/setup-guide.md` — Windows Task Scheduler + WSL environment setup (not needed for
  code changes, only for standing up the nightly automation itself).

**Stale docs, don't trust for current facts:** `readme.md`'s "Repository Structure" section
still shows the pre-split layout (`src/stage1_api.py`/`src/stage2_api.py` directly, no
`agents/`/`direct_api/`/`claude_cli_subprocess/`) — trust `src/agents/dispatch.py` and this
file over it for module paths. `src/claude_cli_subprocess/__init__.py`'s docstring still
says Stage 2 is "scaffolded only -- raises NotImplementedError"; it's fully implemented.

## Keeping this file current

This file is a maintained snapshot, not something regenerated automatically — it will
drift as the code changes. After making a change that shifts something documented above
(a new implementation/config block, a changed CLI flag, a moved module), or after noticing
this file already disagrees with the code, ask the user whether to re-run `/init` to
refresh it, rather than silently leaving it stale or silently rewriting it unasked.
