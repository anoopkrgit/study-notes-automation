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

# On native Windows, `python3` is NOT the interpreter the pipeline runs under -- it
# resolves to a PATH/Store alias (measured: 3.14, with no test deps) while the runner is
# installed under 3.12. Use the launcher explicitly, or the suite fails with
# "No module named pytest" and nothing is actually verified:
py -3.12 -m pytest tests/ -q

# Environment health check (tools on PATH, API key present, dirs exist) — no pipeline run
python3 src/main.py --doctor

# Cheap end-to-end smoke test of both stages' wiring (real API/CLI calls, near-zero cost,
# dummy content) — the fastest way to confirm nothing is broken after a change
python3 src/main.py --stage1-mode llm-token-saver --stage2-mode llm-token-saver --verbose
# or: bash run_test.sh (same idea, against the real target-root chapter folders, but
# pins --stage2-impl graph rather than exercising all three)

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

No linter/formatter is configured in this repo. `pyproject.toml` carries packaging (see
"Packaging as a dependency" below) plus one `[tool.pytest.ini_options]` key, `testpaths =
["tests"]`; there's no separate `pytest.ini`. `testpaths` is a safety gate, not style: a
bare `pytest` recurses from rootdir, so any top-level `test_*.py` gets collected — a scratch
file named `test_api.py` (a hand-run Anthropic repro, since deleted) was collectable, and
its calls were all module-level, so collection alone would have fired real billable API
requests. Keep scratch scripts out of the root, and don't remove `testpaths`.

**This repo must work on native Windows AND Linux/WSL — verify on both before calling a
change done** (see "Cross-platform" below for the defect classes that keep recurring). WSL is
the second platform, reached via `wsl.exe -e bash -lc "cd /mnt/c/... && ..."`. Its system
Python has no project deps, so build the venv from `requirements.txt` — **not** a hand-picked
subset:

```bash
python3 -m venv /tmp/v && /tmp/v/bin/pip install -r requirements.txt pytest
/tmp/v/bin/python -m pytest tests/ -q     # expect the same count as Windows
```

A partially-provisioned venv produces ~20 failures that look like real Linux bugs and are
not (`langgraph`, `ddgs`, `pypdf` missing → `ModuleNotFoundError`, plus `AttributeError`
cascades from the same cause). Both platforms are expected to be **fully green and equal**;
if they differ, suspect provisioning before code. `pytest` is the only test-time extra —
everything the suite and the skill need at runtime is declared in `requirements.txt`.

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
web-enrichment / skill-resolution-fix commit history for the exact unzip/edit/rezip
approach (`repackage_skill_dir()` in `src/common/skill_retro.py` for the programmatic version).
After re-zipping, diff the entry list against `git show HEAD:templates/study-notes.skill` to
confirm nothing was dropped. The skill's own contract worth knowing before reading its
`SKILL.md`: content fixes go through `build.js --patch` (a few hundred tokens) or by editing
one `content.d/<part>.json` — never a throwaway script that rewrites `content.json`, which is
what `_run-diagnostics.txt` exists to detect.
`.claude/skills/` and `.skill-runtime/` in this repo are gitignored *local extraction
caches* of that zip (`sync_skill_package()` in `stage2_cli.py`), not sources of truth.

`sync_skill_package()` extracts to **two** locations every run, not one:
`config.CLAUDE_SKILL_INSTALL_DIR` (project-local) and `config.CLAUDE_SKILL_GLOBAL_INSTALL_DIR`
(`~/.claude/skills/study-notes`, user-level). This is deliberate, not redundant: project-local
skill discovery from `run_claude_cli()`'s nested `state/workspace/<chapter>` cwd was
confirmed unreliable on a real live run (root-caused to this repo's `.claude/` being
gitignored) — `/study-notes` silently fuzzy-resolved to an unrelated stale global skill
instead of failing loudly. `verify_resolved_skill()` is the truth-check for this: it reads
the session transcript's own `"Base directory for this skill: ..."` line after every run and
treats a resolution outside {project-local, global} as FATAL. See `docs/cli-subprocess-plan.md`'s
"Resolved" section for the full incident.

### Everything the `subprocess` impl knows about a run comes from the session transcript

Four functions in `stage2_cli.py` derive ground truth by reading Claude Code's own local
session JSONL (`~/.claude/projects/<mangled-cwd>/*.jsonl`) rather than trusting the model's
self-report — the same "TRUTH-CHECK, NOT SELF-REPORTED SUCCESS" rule as the `expected_docx.exists()`
gate: `_heartbeat_summary()` (progress line), `write_web_sources_manifest()` (web audit),
`write_run_diagnostics()` (efficiency telemetry), and `verify_resolved_skill()` (FATAL
wrong-skill check). All four route through one lookup, `_latest_session_transcript()`.

**That lookup's path mangling must match Claude Code's real on-disk convention exactly**:
every path separator *and* the Windows drive colon map to `-`, so `C:\Users\x` →
`C--Users-x` (note the double dash). It previously dropped the colon, so on native Windows
the lookup returned `None` on every call and all four features silently no-opped — a run
would log `starting up` for hours, produce no manifests, and leave the FATAL skill check
unable to fire. When touching this, pin expectations to literal real directory names
(`test_session_transcript_dir_matches_real_claude_code_layout`); a test that recomputes the
expectation with the same transform under test agrees with the bug and catches nothing.

After a successful run, `capture_retro_findings()` runs the skill's own `tools/retro.py --json`
directly (not relying on the model having invoked it) and writes `_retro-findings.txt` into the
chapter folder if there are candidate skill improvements. `apply_retro_fixes()` (opt-in via
`config.ENABLE_AUTO_SKILL_IMPROVEMENT`, off by default) can act on those automatically: a
separate, bounded `claude -p` session edits a scratch copy of the skill, and `tools/regress.py`
is independently re-run as the accept/reject gate — a failing `regress.py` discards the attempt
entirely; `templates/study-notes.skill` is only ever overwritten after an independently-confirmed
pass. Both functions (plus `repackage_skill_dir()`) live in `src/common/skill_retro.py` and are
shared by BOTH the `subprocess` and `graph` Stage 2 implementations — one copy, not two.
`capture_retro_findings()` also takes `extra_candidates`, so findings the skill's run log
cannot see (it only records what the skill's *tools* rejected, never how the model went about
fixing them) still land in the one artifact a human reviews — `write_run_diagnostics()` feeds
it the ad-hoc-scratch-script count that way. A non-zero `retro.py` exit is now logged loudly
rather than swallowed; a silent swallow is how a Windows-only crash in `retro.py` left the
retrospective a permanent no-op on that platform without ever surfacing an error.

`DEV_TOKEN_SAVER_MODE` (set via `--stageN-mode llm-token-saver`) is a cost-safe smoke-test
toggle honored by all three implementations: cheap model, dummy prompt/content, capped
output, no escalation. It's how CI-cost-free wiring checks work — never enable it for a
real production run.

## State & idempotency

- `state/assemble-state.json` — SHA-256 content hashes of every file Stage 1 has already
  routed; re-runs only process genuinely new files. Don't hand-edit casually.
  **Untracked** — the whole of `state/` is gitignored, and `githooks/pre-commit` blocks
  it structurally. It was tracked once "for progress preservation" and, because each
  entry stores the ABSOLUTE source path of a routed file, that published 155 real paths
  (a student's name, their coaching programme, and a dated record of which lecture
  happened when) to a public repo. It is pure runtime state: `load_state()` returns
  `{"processed": {}}` when the file is absent, so a fresh clone just re-routes from
  scratch. Back it up outside git if you want it preserved; do not re-add it.
- `state/retry-epoch.txt` — written on a retryable API/CLI error with the actual
  reset time (read from response headers when available); the nightly wrapper schedules a
  Windows wake task against it.
- `state/progress/<chapter>.json` — Stage 2's cross-run resume state (conversation
  history for `legacy`/`graph`, `claude` session id for `subprocess`).
- `state/workspace/<chapter>/` — per-chapter scratch dir for `subprocess`'s `run_claude_cli()`
  cwd (moved here from a top-level dir in a recent commit — if you see references to a
  top-level `generation-workspace/`, they're stale).
- `state/graph-checkpoints/` — LangGraph's own SQLite checkpointer for the `graph` impl.
- Per-chapter marker files: `_hold` (not ready yet), `_notes_done` (success), `_notes_FAILED.txt`
  (non-retryable failure, needs a human), `_sources.txt`/`_web-sources.txt` (attribution
  manifests — the `subprocess` variant is built from Claude Code's own session transcript,
  not the model's self-report; see `write_web_sources_manifest()` in `stage2_cli.py`, and the
  `graph` variant of the same name in `stage2_graph.py`), `_retro-findings.txt` (candidate
  skill improvements from that chapter's `tools/retro.py` run — see `capture_retro_findings()`/
  `apply_retro_fixes()` in `src/common/skill_retro.py`, shared by both implementations), and
  `_run-diagnostics.txt` (per-run efficiency telemetry — ad-hoc scratch `*.py` written vs
  `build.js --patch` invocations; written only when there is something to report).
  `_web-sources.txt` is written on EVERY successful `subprocess` run, including when
  enrichment was off: "no file" previously meant three different things at once (disabled /
  enabled-but-unused / transcript unreadable), and that ambiguity is what let a run's
  "web research was not used, the transcripts were sufficient" be believed when the tools
  had in fact never been granted.
- Source files are read-only: Stage 1 only ever *copies* into chapter folders, never
  moves/deletes from the incoming source directories.

## Configuration

`config/settings.py` is the single source of truth for paths, models, and every tunable —
almost everything is `os.environ.get(...)`-backed with a sane default, so behavior changes
via env vars, not code edits. Notable groups: base paths/models (top of file), the
`STAGE1_IMPL`/`STAGE2_IMPL`/`DEV_TOKEN_SAVER_MODE` rollout switches, the `CLAUDE_*` block
(subprocess-implementation CLI flags/timeouts, including `CLAUDE_SKILL_INSTALL_DIR` +
`CLAUDE_SKILL_GLOBAL_INSTALL_DIR`, both kept in sync every run), the `ENABLE_WEB_ENRICHMENT`
(**now defaults ON**; it shipped off pending validation and so was never exercised — the flag
is still the kill switch, but the outcome is recorded in `_web-sources.txt` either way)/
`WEB_SEARCH_ALLOWED_DOMAINS`/`MAX_WEB_SEARCHES_PER_CHAPTER` block (bounded web search — see
`docs/web-enrichment-plan.md`; originally `subprocess`-only, now wired into all three Stage 2
implementations — `subprocess` via Claude Code's own `WebSearch`/`WebFetch` tools, `graph`
via `tool_web_search`/`tool_web_fetch` in `src/agents/tools.py`, and `direct_api` via the
same tools ported in per PR #7/#8), and
`ENABLE_AUTO_SKILL_IMPROVEMENT`/`AUTO_SKILL_IMPROVEMENT_WORKSPACE` (opt-in autonomous
application of `retro.py` candidates, `regress.py`-gated — off by default).

### Source/target paths are placeholders on purpose

`DEFAULT_TRANSCRIPT_SRC`/`DEFAULT_COLLECTED_SRC`/`DEFAULT_TARGET_ROOT` fall back to
**generic placeholders** (`.../Education/Student/...`) that are not expected to resolve to
anything on disk. This repo is public; a real path here leaks the student's name, their
coaching programme and the Drive layout, which is exactly what it used to do.

Real paths arrive as the `TRANSCRIPT_SRC`/`COLLECTED_SRC`/`TARGET_ROOT` env vars, exported
by the runner (`run-claude-agent`'s `paths_config.py`, which prompts once and saves to
`paths.json`) **before** it imports this module. There is deliberately no second mechanism —
no `.env`, no local config file in this repo — because a path settable in two places is a
path that will eventually disagree with itself.

The consequence to know: invoking `src/main.py` directly, without those vars exported, is
a **silent no-op** — Stage 1 finds zero files under a placeholder folder and exits 0 having
done nothing. It does not error. (`run_test.sh` is the exception; its `find` fails loudly
under `set -euo pipefail`.) If you add a unified entry point, make it the thing that
guarantees those vars are set.

### Personal data must not re-enter this repo

`githooks/pre-commit` (enable per clone with `git config core.hooksPath githooks`) blocks
two things: anything staged under `state/`, and any staged content or filename matching a
regex in `.pii-patterns`. That patterns file is **gitignored** — it holds the real names,
so committing it would publish exactly what it guards; `.pii-patterns.example` is the
tracked template. The hook is plain `sh` + `grep` so it behaves identically under Git Bash
and WSL, and `.gitattributes` pins `githooks/*` to `eol=lf` because `core.autocrlf=true`
would otherwise check it out with CRLF and silently disable it.

Note this only guards *future* commits. The pre-existing git history still contains the
personal data; removing it there is a separate history-rewrite decision.

### What the `claude` CLI subprocess is granted, and why it isn't grantable in the prompt

`CLAUDE_ALLOWED_TOOLS` is a per-verb allowlist, not a blanket `Bash` grant. Two constraints
interact and are easy to get wrong:

- **The runs are unattended, so every denial is a guaranteed-wasted turn** — there is nobody
  to approve. One live run burned ~68 turns purely on `"this command requires approval"`.
  The list therefore includes the read-only/scratch verbs the skill's own pipeline actually
  uses (`ls/grep/cat/wc/mkdir/rm/env/cd`) alongside the interpreters.
- **`export` is deliberately NOT granted.** `SKILL.md` step 1 needs `NODE_PATH` (so
  `build.js` resolves `docx`) and Windows needs `PYTHONIOENCODING=utf-8`; both are set
  process-side in `build_claude_env()` (`src/claude_cli_subprocess/common.py`) so nothing has
  to be exported. Adding `Bash(export *)` instead would be the wrong fix.

`run_claude_cli()` also passes `--add-dir` for both skill install dirs, so the model's reads
of `$S/lib/figlib.py` etc. aren't refused as outside the working directory.

## Cross-platform (Windows + Linux/WSL)

Platform defects in this repo are a recurring, high-cost class — they fail *silently* and
look like model misbehaviour. `docs/stage2-token-burn-postmortem.md` is the case study.
Already handled correctly (don't "fix" these): `skill_package.py`'s `fcntl`/`msvcrt` lock
branch, `func_tools_and_utils.py`'s `_WINDOWS_BUILTIN_COMMANDS` (services `ls/cat/cp/mv/mkdir`
in Python on Windows, where they have no executable), and `settings.py`'s `G:/` vs `/mnt/g`
root. The patterns to apply in new code:

- **Never hardcode `python3`** to run a helper script — use `sys.executable`. On Windows
  `python3` is a PATH/Store alias pointing at a *different* interpreter than the one running
  the pipeline, so provisioned deps aren't there and it surfaces as an unrelated
  `ModuleNotFoundError`. (`agents/tools.py` had 11 such call sites.)
- **Never pass a bare `npm`** to `subprocess` — resolve with `shutil.which()` first. npm ships
  only as `npm.cmd`/`npm.ps1` on Windows and `CreateProcess` only auto-appends `.exe`, so a
  bare name raises `FileNotFoundError`. `node`/`soffice`/`pdftoppm` are real `.exe`s and are fine.
- **Always pass `encoding="utf-8"`** to `open()`/`write_text()` on anything textual. Windows
  defaults to cp1252 and dies on the chapter content's real symbols (₁, ∝, °). Note the run
  log is written by *both* Python (`json.dumps`, ASCII-escaped) and `build.js` (Node's
  `JSON.stringify`, raw UTF-8), so readers must assume UTF-8.
- **Don't compare file mtimes against `time.time()`** to detect "did this change?" — different
  sources, different resolutions. Snapshot `(st_mtime_ns, st_size)` before and after instead.
- `Path.home()` reads `USERPROFILE` on Windows, `HOME` on POSIX — tests that monkeypatch only
  `HOME` silently no-op on Windows and assert nothing (see `_set_fake_home` in the stage2 tests).

## Packaging as a dependency

This repo is transitioning to also being pip-installable, so a sibling project
(`run-claude-agent`) can depend on it via
`git+https://github.com/anoopkrgit/study-notes-automation.git@develop` instead of a git
clone. `pyproject.toml`'s `[tool.setuptools]` block keeps `config/`, `src/` (+ subpackages),
and `templates/` installed as siblings under one root — preserving the exact nesting that
`config/settings.py`'s `LOCAL_RUNTIME_ROOT` (computed as "two directories above my own
`__file__`") and the skill-zip glob logic assume, confirmed empirically against a real wheel
build/install. `config/` has no `__init__.py` (matches the git-clone layout — it's an
implicit namespace package, never imported as `config.settings`); a consumer needs to put
the installed `config/` dir itself onto `sys.path` before `import settings` will resolve,
same as `src/main.py` already does for a git clone (see `run-claude-agent`'s `bootstrap.py`
for the installed-package equivalent). `config/settings.py`'s `LOCAL_RUNTIME_ROOT` can also
be overridden via the `STUDY_NOTES_RUNTIME_ROOT` env var, so a pip-installed consumer can
point state/logs outside site-packages.

If you change how `LOCAL_RUNTIME_ROOT` (or any other path derived from `__file__`) is
computed, re-check this packaging story — it was verified against one specific layout, not
derived from a general principle.

### `requirements.txt` is a BUILD INPUT — never delete it

Two install paths are live and **neither reads the other's file**, which is why both
`requirements.txt` and `pyproject.toml` exist. It is not duplication to clean up:

- **Git clone + nightly scheduler** — `install.sh` (repo ROOT, paired with `install.bat`;
  it is *not* in `scripts/`) does `pip install -r requirements.txt` into `~/.global_venv`,
  which `scripts/wsl-study-notes-processor.sh` then runs the pipeline from. This path never
  builds the repo as a package and never reads `pyproject.toml`.
- **`pip install git+...@develop`** (how `run-claude-agent` consumes this repo) — reads
  `pyproject.toml`, which declares `dynamic = ["dependencies"]` and pulls the list from
  `requirements.txt` via `[tool.setuptools.dynamic]`. pip never reads `requirements.txt` on
  its own; that indirection is what makes it happen.

So the dependency list is maintained in exactly ONE place (`requirements.txt`) and consumed
by both. The consequence: `requirements.txt` must stay tracked and must stay valid
requirements syntax — pins and `#` comments only, no `-r`/`-e`/`--index-url` lines, which
setuptools rejects there. Deleting it, untracking it, or "folding it into pyproject" breaks
the pip-install path at BUILD time, where this repo's own test suite cannot see it. Verified
by reconstructing a clone from the git index and running a real `pip install` from it: all
12 deps resolved and the `config`/`src`/`templates` sibling layout held in site-packages.

## Docs worth reading before larger changes

- `docs/architecture.md` — the full invocation tree, Windows→WSL→Python→retry/sleep flow.
- `docs/migration-to-agents.md` / `docs/agent_migration_walkthrough.md` — why the `graph`
  implementation exists and how it was verified against `legacy`.
- `docs/web-enrichment-plan.md` — bounded web search design, originally written for the
  `subprocess` Stage 2 generator (whose guardrail mechanism — `WebFetch(domain:...)`
  permission rules, `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` — is CLI-specific). Since
  written, web enrichment + retro-candidate synthesis have been ported into `direct_api`
  and `graph` too (PR #7/#8); the doc predates that and describes only the original
  `subprocess` design.
- `docs/setup-guide.md` — Windows Task Scheduler + WSL environment setup (not needed for
  code changes, only for standing up the nightly automation itself).
- `docs/stage2-token-burn-postmortem.md` — forensic breakdown of a run that consumed two
  full 5-hour subscription windows. Read before optimising anything cost-related: it
  quantifies where the turns actually went (permission friction and correction rounds, not
  context size), records the cross-platform defects found alongside, and lists changes that
  were **considered and rejected**, with reasons — check it before re-proposing one.

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
