# Wire the packaged `study-notes.skill` into the API-based generator

## Context

`stage2_api.py` (Stage 2, the unattended nightly generator that talks
directly to the Anthropic API — no Claude Code CLI) currently uses
`templates/study-notes-skill.md`, a ~3.5KB plain-text prompt. It works, but per
`templates/study-notes-skill-improvement-brief.md`, it forces the model to
re-derive every mechanical decision (page layout, fraction rendering, figure
geometry, docx-building code) from scratch on every run: ~158k tokens, ~40
minutes, 45 turns, 13 hand-fixed layout defects in the first production run,
and no enforced quality bar.

`templates/study-notes.skill` is a newer, already-proven replacement — it's a
zip-packaged Agent Skill (SKILL.md + LESSONS.md + `tools/*.py` + `lib/*.js`
+`lib/*.py` + JSON schemas + a measured quality baseline) that has already been
used successfully *interactively* through Claude Code (its "spawn a subagent
for image-heavy board pages" instruction is written for Claude Code's native
Task tool). It replaces "the LLM writes rendering code" with "the LLM writes
`content.json`/`figures.json`; prebuilt scripts validate, verify, render, and
QA them," with fail-fast gates and quality thresholds measured from an
approved reference document.

The goal: port this same skill package to run under `stage2_api.py`'s
much more restrictive, custom agentic loop — no persistent shell, no
subagent-spawning tool, a fixed `tool_bash` allowlist, `shell=False` (so shell
globs and `export` don't work) — without giving that unattended loop any new
unsafe capability (e.g. `npm` stays out of the model's hands).

Decision made during planning: build a lightweight nested sub-call tool for
image-heavy board pages (rather than skipping isolation), since the whole
point of the new skill is avoiding the token/context blowup that direct image
viewing caused in the old flow.

## Approach

### 1. Unpack the skill package once, cached (`src/stage2_api.py`)

New helper `ensure_skill_unpacked() -> Path`:
- Hash `config.STUDY_NOTES_SKILL_PACKAGE` (the `.skill` zip) with the existing
  `sha256()` from `func_tools_and_utils.py`.
- Compare against a marker file in `config.SKILL_CACHE_DIR`; if unchanged and
  `SKILL.md` already exists there, skip extraction.
- Otherwise extract via `zipfile.ZipFile` into a temp dir and atomically swap
  it into place — same tmp-write-then-`.replace()` pattern already used by
  `save_state()` in `func_tools_and_utils.py`.
- Returns the absolute extracted root (contains `SKILL.md`, `tools/`, `lib/`,
  `schema/`, `example/`, `quality/`, `LESSONS.md`).

### 2. Provision the pinned `docx` npm dependency ourselves (never via the model)

New helper `ensure_node_deps(skill_root: Path) -> Path`:
- Idempotently `npm install` (plain `subprocess.run`, this is *our* setup
  code, not gated by `tool_bash`) into `config.NODE_DEPS_DIR`, using the
  skill's own `lib/package.json` as the manifest (so the pinned `docx`
  version always tracks whatever the skill ships, not a duplicated constant
  in our settings).
- Skip if `node_modules/docx` already present at the right version.
- Returns `config.NODE_DEPS_DIR`, to be injected as `NODE_PATH`.

We deliberately do **not** add `npm` to `ALLOWED_BASH_COMMANDS` — that would
let an unattended overnight agent run arbitrary installs. Provisioning stays
entirely on our side.

### 3. Swap the system prompt, with a rollback toggle (`src/stage2_api.py`, `config/settings.py`)

- Add `config.USE_PACKAGED_SKILL` (env-var gated, default on).
- Rewrite `load_skill_prompt()`: when on, call `ensure_skill_unpacked()` and
  read `SKILL.md` from the extracted root; keep the existing minimal
  fallback text on any failure. When off, fall back to today's
  `templates/study-notes-skill.md` unchanged — a one-env-var rollback with no
  code revert if the packaged skill misbehaves on its first live overnight
  run.

### 4. Bridge our folder convention into the skill's expectations (`build_user_prompt`)

SKILL.md doesn't know about our `transcripts/`/`supporting/` chapter-folder
convention (`config.TRANSCRIPTS_DIR`/`config.SUP_DIR`), and it can't know
about our sandbox's limits. `build_user_prompt(...)` needs to keep its
existing "top-level transcripts are spine, `supporting/` is extra material,
ignore meta files" explanation, and additionally tell the model:
- The absolute extracted skill root path, to substitute for every `$S` /
  "the skill directory" reference in SKILL.md's example commands.
- There is **no persistent shell**: each `tool_bash` call is an independent
  subprocess with no memory of a previous call's `export`/variable
  assignment — never attempt `export NODE_PATH=...` or `S=<path>` as a
  command; always use the full absolute path inline instead of `$S`.
- `NODE_PATH` is already configured automatically (see below), so `node
  <skill>/lib/build.js` resolves `docx` with no setup step needed.

### 5. Make `tool_bash` actually run SKILL.md's example commands (`src/func_tools_and_utils.py`)

Two small, safe additions to `tool_bash()`:
- **Glob expansion**: SKILL.md's own examples use shell globs (e.g.
  `sources/*.pdf`). Since `tool_bash` uses `shlex.split()` +
  `subprocess.run(shell=False)`, `*` is never expanded — the literal string
  would be passed to argparse. Pre-expand any argument containing
  `*`/`?`/`[` via Python's `glob.glob()` (relative to `cwd`, sorted; leave
  the token as-is if nothing matches) before building argv. No shell
  involved, so this doesn't reopen the injection risk the existing
  metacharacter check guards against.
- **Automatic `NODE_PATH`**: when `args[0] == "node"`, inject
  `NODE_PATH=<config.NODE_DEPS_DIR>` into the subprocess environment. This
  is what makes point 4's "no persistent shell" note actually work — the
  model never needs to set the env var itself.
- No change to `ALLOWED_BASH_COMMANDS` — `python3`, `node`, `soffice` already
  cover every script the skill invokes (`tools/*.py` via `python3`,
  `lib/build.js` via `node`; `pdftotext`/`pdfinfo`/`pdftoppm` are called
  *internally* by `tools/ingest.py`'s own subprocess calls, never directly
  by the model, so they don't need to be in the model-facing allowlist).

### 6. Add the subagent-isolation tool (`src/stage2_api.py`)

New `tool_spawn_subagent(prompt: str, image_paths: list[str], tracker: TokenTracker) -> str`:
- Builds ONE user turn: the given `prompt` text plus an image content block
  per path in `image_paths` (PNG files — the natural interface, since
  `tools/ingest.py` already produces calibrated/autocropped PNGs at
  `sources/<name>_pages/*.png`; reuse `_image_block_from_bytes` /
  `_IMAGE_MEDIA_TYPES` from `func_tools_and_utils.py`).
- Makes exactly one `client.messages.create()` call — no tools, text+image
  in, text out. This is a single-shot "transcribe what's on these pages"
  task; the outer SKILL.md-driven loop is what validates the resulting JSON
  later via `tools/validate.py`, so the subagent doesn't need its own
  retry/validation logic.
- Records usage on the **same** `tracker` the main loop already threads
  through Stage 2 (mirrors how Stage 1's router calls already feed a shared
  `TokenTracker`), so the token-usage summary log stays accurate.
- Returns only the response's text. Critically, the image bytes/base64 used
  to build that one call are never appended to the main loop's `messages`
  list — only this returned string is, which is the entire point (keeps the
  main conversation's context from ballooning the way the old flow's did on
  the 21MB scanned-board PDF already sitting in the Gaseous State chapter).
- Add its schema to `AGENT_TOOLS`, and thread `tracker` into `execute_tool()`
  (small signature change: `execute_tool(name, args)` →
  `execute_tool(name, args, tracker)`, updating the one call site in the
  turn loop).

### 7. Fail fast on setup, log the resolved paths

In the `--live` branch, call `ensure_skill_unpacked()` /
`ensure_node_deps()` once before entering the turn loop. On failure, write
`FAILMARK` and return `EXIT_FATAL` immediately — consistent with this
codebase's existing "fail loud, don't guess" philosophy (see
`load_state()`'s lock-handling docstring). Log the resolved skill root and
node-deps path at INFO level.

### 8. Update `--doctor` (`src/main.py`)

Replace the current `npm ls -g docx` global-package check with checks
against the new pinned local install
(`config.NODE_DEPS_DIR/node_modules/docx`, version cross-checked against the
skill's `lib/package.json`), plus a check that
`config.STUDY_NOTES_SKILL_PACKAGE` exists and is a valid zip containing
`SKILL.md` — so `--doctor` stays an accurate pre-flight signal for the new
dependency chain.

### Files touched
- `config/settings.py` — new constants: `STUDY_NOTES_SKILL_PACKAGE`,
  `SKILL_CACHE_DIR`, `NODE_DEPS_DIR`, `USE_PACKAGED_SKILL`.
- `src/stage2_api.py` — `ensure_skill_unpacked()`,
  `ensure_node_deps()`, rewritten `load_skill_prompt()` and
  `build_user_prompt()`, new `tool_spawn_subagent()` + `AGENT_TOOLS` entry,
  `execute_tool()` signature change, setup calls + logging in the `--live`
  branch.
- `src/func_tools_and_utils.py` — `tool_bash()` glob expansion + automatic
  `NODE_PATH` injection for `node` calls.
- `src/main.py` — `run_doctor()` checks for the pinned local `docx` install
  and skill-package validity.
- `templates/study-notes-skill.md` — unchanged; kept only as the
  `USE_PACKAGED_SKILL=0` rollback prompt.

## Verification

- **Unit tests** (pytest, following `tests/test_utils.py` /
  `tests/test_main_cli.py`'s existing style):
  - `ensure_skill_unpacked()`: a tiny fake zip in `tmp_path` extracts on
    first call; an unchanged zip does *not* re-extract on a second call;
    changing the zip's contents triggers re-extraction.
  - `tool_bash` glob expansion: a command like `ls *.pdf` against a tmp dir
    with real files expands correctly; shell-metacharacter rejection still
    fires for `;`/`|`/etc. regardless of the new glob step.
  - `tool_bash` `NODE_PATH` injection: a `node` call against a tiny script
    that prints `process.env.NODE_PATH` confirms the injected value.
  - `tool_spawn_subagent`: mock the Anthropic client's `messages.create` to
    return a canned response; assert the returned string matches, `tracker`
    recorded the right usage, and no image payload leaks into any structure
    the main loop's `messages` list would see.
  - Re-run the full `pytest tests/` suite — must stay green.
- **No-token smoke test**: call `ensure_skill_unpacked()` /
  `ensure_node_deps()` directly and confirm `node <extracted>/lib/build.js
  --help` (or similar no-op) runs without a `Cannot find module 'docx'`
  error. Run `python3 src/main.py --doctor` and confirm the new checks
  report correctly.
- **Real acceptance run** (spends tokens — run manually/supervised, per this
  codebase's existing convention for `--live`): `python3 src/main.py
  --run-generate` against one already-ready chapter (state file shows
  `Chemistry-Ch1-Gaseous-State` already assembled), watching the log to
  confirm the model follows SKILL.md's ingest → validate → figbuild → verify
  → build → qa sequence, that `tool_spawn_subagent` is invoked for the large
  scanned-board transcript without inflating the main loop's token count,
  and that a valid `.docx` + `_notes_done` marker land at the expected path.
