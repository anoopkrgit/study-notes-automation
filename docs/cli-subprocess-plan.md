# claude CLI subprocess implementation plan — both stages

Supersedes `docs/_del_me_stage2-claude-cli-migration-plan.md` (kept, renamed, not deleted —
see note at its top). That doc was written under an older architecture (Stage 2 has one
implementation, replaced in place) that predates the current decision below. Its concrete
technical content (skill package facts, function designs, config settings, verification
staging) is carried forward here, reconciled against the current architecture; a few of its
instructions were flatly wrong under that architecture and are explicitly corrected, not
silently dropped — see "Corrections from the superseded doc" below.

## Context

The pipeline has two stages (Stage 1 "assemble/route files into chapter folders", Stage 2
"generate chapter notes"), each dispatched through `src/agents/dispatch.py` to a selectable
implementation via `--stage1-impl`/`--stage2-impl`. Today: `legacy` (direct Anthropic SDK
calls) and `graph` (LangGraph multi-agent). This plan adds a third, parallel implementation —
`subprocess` — that invokes the `claude` CLI as a subprocess, billed against a Claude
subscription instead of a metered API key. **All three implementations are co-equal and
permanent, not a migration that retires the other two.** `direct_api/stage2_api.py`
(Stage 2's SDK implementation) is not touched, rewritten, or stripped by this plan — it stays
fully intact and independently selectable via `--stage2-impl legacy`.

Separately, Stage 2's original motivation (from the superseded doc) still applies to the new
`claude_cli_subprocess/stage2_cli.py` once built: Stage 2 has never run in `--live` mode for real on this
machine, and the user wants to avoid a Stage-1-style cost surprise by using CLI/subscription
billing rather than metered per-token API calls for its first real run. The engineered skill
package (`templates/study-notes.skill` — `SKILL.md` + `tools/` + `lib/` + `schema/` +
`example/` + `LESSONS.md`, not a plain prompt file) is the reason: its deterministic
JSON-authoring pipeline (`content.json`/`figures.json` → fixed build scripts) is what makes
CLI/subscription execution both cheaper and higher-quality than the old hand-rolled loop.

## Decisions (folder names + scope)

1. **`src/direct_api/`** — the SDK-based implementation (was "legacy"). Not `api_calls`:
   avoids ambiguity since the graph implementation also calls the API.
2. **`src/agents/`** — unchanged, already correctly organized (LangGraph).
3. **`src/claude_cli_subprocess/`** — the new subprocess-based implementation. Not `subprocess`: avoids
   shadowing Python's stdlib `subprocess` module, which this package's own code needs to
   `import subprocess` to shell out to the `claude` binary.
4. **CLI enum values stay `legacy`/`graph`/`subprocess`** — unchanged even though folders are
   `direct_api`/`agents`/`claude_cli_subprocess`. Preserves already-published `docs/setup-guide.md`
   Task-Scheduler `-PipelineArgs` commands; mirrors the existing precedent that `"graph"`
   already doesn't match the `agents` folder name today.
5. **`dispatch.py` stays inside `src/agents/`** — not relocated, even though it now switches
   between three co-equal siblings. Scope-minimization call, purely cosmetic either way.
6. **Stage 1 subprocess: fully built now**, ported from the working reference prototype
   `src/assemble_chapters_subtask.py`. **Stage 2 subprocess: fully built now**, built from the detailed
   design in this doc (see `docs/claude-cli-subprocess-stage2-implementation-plan.md` for the executed implementation).
7. **`--stage2-impl subprocess` is now a real, fully working choice.** The `parser.error()`
   fail-fast in `main.py` and the `NotImplementedError` stub in
   `src/claude_cli_subprocess/stage2_cli.py` (both defense-in-depth against the module not existing
   yet) have been removed now that a real implementation is behind them — see
   `docs/claude-cli-subprocess-stage2-implementation-plan.md`.
8. **`src/assemble_chapters_subtask.py` gets deleted** once its logic is ported into
   `src/claude_cli_subprocess/stage1_cli.py` and tests pass — confirm before deleting, don't do it silently.

## What moves, what stays

| File | Action |
|---|---|
| `src/stage1_api.py` | → `src/direct_api/stage1_api.py` |
| `src/stage2_api.py` | → `src/direct_api/stage2_api.py` (content unchanged — see "Corrections" below) |
| `src/func_tools_and_utils.py` | **stays in `src/`** — shared by `direct_api/`, `agents/`, and `claude_cli_subprocess/` alike (logger, sha256, is_ignorable, load_state/save_state, classify_api_error, TokenTracker, write_retry_epoch, EXIT_* constants, tool_* implementations) |
| `src/func_classify_and_rename.py` | **stays in `src/`** — used by `direct_api/stage1_api.py` today AND by the new `claude_cli_subprocess/stage1_cli.py` |
| `src/agents/*` | unchanged |
| `src/assemble_chapters_subtask.py` | ported into `src/claude_cli_subprocess/stage1_cli.py`, then deleted (pending confirmation) |

New: `src/direct_api/__init__.py`, `src/claude_cli_subprocess/__init__.py`, `src/claude_cli_subprocess/common.py`,
`src/claude_cli_subprocess/stage1_cli.py`, `src/claude_cli_subprocess/stage2_cli.py`.

## Import edits required by the move

- `src/main.py`: `from src.stage1_api import run_assemble` →
  `from src.direct_api.stage1_api import run_assemble`
- `src/agents/dispatch.py` (lazy import inside `generate_notes()`, "legacy" branch):
  `from src.stage2_api import run_generate` →
  `from src.direct_api.stage2_api import run_generate`
- `src/agents/dispatch.py` (lazy import inside `route_file()`, "legacy" branch):
  `from src.stage1_api import llm_route` →
  `from src.direct_api.stage1_api import llm_route`
- `src/agents/stage2_graph.py` (lazy import inside `run_stage2_chapter()`):
  `from src.stage2_api import select_target_chapter` →
  `from src.direct_api.stage2_api import select_target_chapter` — read this file's
  exact current line before editing, not independently re-verified in the last research pass.
- Everything else (imports of `func_tools_and_utils`/`func_classify_and_rename` inside the
  moved files, and `agents/tools.py`, `stage1_graph.py`, `stage2_graph.py`'s own imports of
  those two shared modules) is **unchanged** — those two files aren't moving.

Test files needing import-path edits: `tests/test_utils.py` (`import src.stage2_api
as fgn` / `from src.stage2_api import select_target_chapter, run_generate` / `import
src.stage1_api as fac` → `src.direct_api.stage2_api` /
`src.direct_api.stage1_api`); `tests/test_dispatch.py` (mock `patch()` string
targets `'src.stage2_api.run_generate'` and `'src.stage1_api.llm_route'`
→ `'src.direct_api.stage2_api.run_generate'` /
`'src.direct_api.stage1_api.llm_route'`). `tests/test_classify_and_rename.py` and
`tests/test_stage2_graph.py` are unaffected.

---

## Stage 1: `src/claude_cli_subprocess/` — build now

### `common.py`

The one thing both stages need: `build_claude_env()` returns `os.environ.copy()` with
`ANTHROPIC_API_KEY` popped, safe to pass as `subprocess.run(..., env=build_claude_env())`.

**Billing precedence risk — the single highest-severity item in this whole plan:** a `claude`
subprocess that inherits `ANTHROPIC_API_KEY` silently bills metered Console instead of the
subscription — no error, no warning. `src/direct_api/stage1_api.py` (imported by
the shared `run_assemble()` orchestration regardless of which `--stage1-impl` is active) calls
`load_dotenv()` at import time, populating `ANTHROPIC_API_KEY` into `os.environ` — so by the
time ANY subprocess-mode call happens (Stage 1 today, Stage 2 once built), that key WILL be
present in the parent process's environment and WILL leak into the `claude` subprocess unless
explicitly popped. `build_claude_env()` must be isolated as its own small, obviously-named,
unit-tested function — not an inline `.pop()` a future edit could accidentally drop.

Also port `is_usage_limit(text)` verbatim from `src/assemble_chapters_subtask.py` lines
264-282: regex for `reached\s*\|\s*\d{9,13}` (a Unix-timestamp-suffixed rate-limit message),
plus substring checks for "usage limit"/"rate limit"/"5-hour"/"limit reached"/"too many
requests"/"quota exceeded".

Do **not** add a generalized `invoke_claude_cli_subprocess()` primitive yet — Stage 1's flags
(`--json-schema`, `--allowedTools Read,Bash`, `--dangerously-skip-permissions`) and Stage 2's
eventual flags (`--add-dir`, `--permission-mode acceptEdits`, `-r <session_id>`, no
skip-permissions at all) are different enough that a shared wrapper now would be premature
abstraction.

### `stage1_cli.py`

Ported from `src/assemble_chapters_subtask.py` lines 345-529 (`ROUTER_SCHEMA`, `run_router()`,
`_parse_matches()`, `llm_route()`), with these deltas:
- Rename the public entrypoint `llm_route` → `route_file(path, buckets, prior=None,
  tracker=None) -> (matches, limited, model_used)` — matches
  `direct_api.stage1_api.llm_route()`'s and `agents.stage1_graph.route_one_file()`'s
  exact 3-tuple contract. `tracker` accepted for signature parity but unused (CLI subscription
  billing isn't a per-call token count the way `TokenTracker` expects) — document why in a
  docstring, don't silently drop the param.
- Pull `ROUTER_MODEL`, `CONF_MIN` from `config` instead of the reference file's local
  constants. Add `config.ESCALATE_MODEL` (new — see Config section). Note: `ROUTER_MODEL`'s
  *effective* default becomes `"claude-haiku-4-5-20251001"` (the repo's existing pinned
  value) rather than the reference file's `"claude-haiku-4-5"` literal — same env var
  (`ASSEMBLE_ROUTER_MODEL`), different fallback. Almost certainly desired, flagged as a silent
  behavior delta worth knowing about.
- Add `env=build_claude_env()` to the `subprocess.run(...)` call — the critical fix; the
  reference file's `run_router()` has no `env=` kwarg at all.
- Replace `cwd=str(TARGET_ROOT)` (doesn't exist in this repo) with
  `cwd=str(config.LOCAL_RUNTIME_ROOT)` — not load-bearing for correctness since the router
  reads via an absolute `path` argument in the prompt, just needs a stable valid cwd.
- Import `is_usage_limit` from `.common` instead of defining it locally.

### `stage2_cli.py` — stub only, for now

```python
def run_stage2_chapter(target_dir=None, live_mode=False) -> int:
    raise NotImplementedError(
        "Stage 2 subprocess (`claude` CLI) implementation is not yet built. "
        "See the 'Stage 2: full design' section of docs/cli-subprocess-plan.md. "
        "Use --stage2-impl legacy or --stage2-impl graph instead."
    )
```

---

## Stage 2: design (built — see docs/claude-cli-subprocess-stage2-implementation-plan.md for the executed implementation)

Everything below is carried forward from the superseded doc, reconciled to the "new parallel
`src/claude_cli_subprocess/stage2_cli.py` module" architecture instead of "rewrite
`direct_api/stage2_api.py` in place." **Corrections from the superseded doc are
called out explicitly** — see the callout box at the end of this section.

### Confirmed environment facts

- `claude` CLI v2.1.197 installed at `/usr/bin/claude`, already authenticated via
  OAuth/subscription (`~/.claude/.credentials.json` present).
- Headless/non-interactive invocation: `-p/--print` + `--output-format json` +
  `--permission-mode acceptEdits` + `--allowedTools` + `--add-dir` — no TTY/human required.
- `templates/study-notes.skill` (confirmed via `python3 -m zipfile`): a real skill package,
  79KB zipped, `SKILL.md` alone 16.3KB. Contents:
  `tools/{ingest,validate,verify,qa,regress,pedagogy,baseline,invariants,_log}.py`,
  `lib/{build.js,figbuild.py,figlib.py,dochelp.js,package.json}`,
  `schema/{content,figures}.schema.json`, `example/{content,figures}.json` (a working
  reference), `quality/baseline.json` + `quality/reference_section/`, `LESSONS.md`.
  Frontmatter `name: study-notes` — same invocation name as a simpler, older,
  globally-installed skill at `~/.claude/skills/study-notes/` (confirmed different content;
  NOT what should be used; left alone, not touched or overwritten by this plan).
- The skill's own text is explicit paths must never be hardcoded: "Never hardcode an absolute
  skill path; the location changes every session" — Claude Code resolves its own skill
  directory (`$S`) at runtime.
- Confirmed external-binary dependencies (grepped `subprocess.run`/`execFileSync` in the
  package): `pdftotext`, `pdfinfo`, `pdftoppm` (poppler-utils), `soffice` (libreoffice),
  `node`+`npm install docx` once per working folder, `python3` — all already `install.sh`
  dependencies of this project, no new system packages needed.
- The skill recommends subagent delegation for reading transcript page images ("Never read
  transcript page images in the main thread... Spawn a subagent") — a Claude-Code-native
  pattern, reinforcing this package was engineered for real Claude Code execution, not a bare
  single-shot `claude -p` call with no subagent capability.
- `-r/--resume <session_id>` can resume an interrupted headless session via Claude Code's own
  transcript persistence, replacing a hand-rolled full-message-history checkpoint with
  `{"session_id": ..., "attempts": N}`.
- Usage-window/rate-limit specifics under subscription billing are undocumented anywhere
  findable — treat as something to observe empirically in the pilot, not hardcode.

### Architecture (once built)

For each chapter, sync the current `templates/*.skill` package into a stable local skill
directory, then make ONE `subprocess.run(["claude", "-p", ...])` call with
`env=build_claude_env()` (shared with Stage 1 — see `common.py` above), `cwd` set to a
**local scratch working folder** (not the actual Google-Drive chapter folder — the skill
wants `npm install`/`node_modules`/figures/build artifacts in "your own working folder," and
building on synced Drive storage would be slow and pollute it with build cruft),
`--add-dir` extended to the real chapter folder (for reading transcripts/supporting material
and writing the final `.docx`), and `--allowedTools` covering the skill's actual dependencies:
`Bash(python3 *), Bash(node *), Bash(npm *), Bash(pdftotext *), Bash(pdfinfo *),
Bash(pdftoppm *), Bash(soffice *), Read, Write, Edit, Glob, Grep`. The prompt is short: invoke
`/study-notes`, point at the chapter's transcripts/supporting folders, state the required
output path — the skill's own pipeline (ingest → author JSON → validate → figbuild → verify →
build → qa) does the rest deterministically. Success is decided by `expected_docx.exists()`
after the call returns, never by trusting self-reported success.

**Skill package sync (new, required):** Claude Code needs the skill's own directory
(`tools/`, `lib/`, `schema/`, `example/`) physically present on disk. Before invoking
`claude -p`:
1. Find the newest `templates/*.skill` file by mtime (supports dropping updated exports
   without a code change).
2. Compare against what's currently unzipped at the target skill directory (content hash or
   mtime); re-extract only if changed.
3. Extract to a stable local directory this project controls — recommended:
   `config.LOCAL_RUNTIME_ROOT / ".claude" / "skills" / "study-notes"` — rather than the user's
   global `~/.claude/skills/study-notes/`, to avoid clobbering whatever's installed there for
   other purposes.

**Open mechanic to verify in the pilot, not assumed:** whether Claude Code's project/local
skill discovery reliably finds `.claude/skills/study-notes/` by walking up from a `cwd` set to
a subdirectory of `LOCAL_RUNTIME_ROOT`. If not, fallback is syncing into
`~/.claude/skills/study-notes/` directly (global, simpler, but shares state with whatever's
installed there today). Resolve this first in the pilot's manual smoke test, before writing
wrapper code around it.

### `src/claude_cli_subprocess/stage2_cli.py` — functions to build

- `sync_skill_package() -> Path` — finds newest `templates/*.skill`, extracts to the local
  skill directory if changed, returns that directory's path (for logging, not for hardcoding
  into the prompt).
- `build_cli_prompt(target_dir, expected_docx) -> str` — short: `/study-notes`, the chapter's
  transcript/supporting folder paths, required output path, same scope framing as
  `direct_api.stage2_api` today (transcripts = spine/non-negotiable, supporting =
  enrichment-only). No tool-name references — the skill's own pipeline instructions take over.
- `build_claude_env() -> dict` — **reuse `src/claude_cli_subprocess/common.py::build_claude_env()`**,
  don't redefine it here (see Corrections below).
- `run_claude_cli_subprocess(target_dir, prompt, resume_session_id=None) -> dict` — builds argv against a
  local scratch `cwd` with `--add-dir target_dir`, `--permission-mode acceptEdits`,
  `--allowedTools config.CLAUDE_ALLOWED_TOOLS`, optional `--model config.CLAUDE_MODEL`,
  optional `-r resume_session_id`; runs via `subprocess.run(..., env=build_claude_env(),
  cwd=local_scratch_dir, capture_output=True, text=True,
  timeout=config.CLAUDE_CLI_TIMEOUT_SECONDS)`; parses stdout JSON; returns a plain dict (`ok`,
  `result`, `session_id`, `total_cost_usd`, `usage`, `error`, `returncode`). Logs
  cost/usage/error prominently regardless of outcome. Never passes
  `--dangerously-skip-permissions` (unlike Stage 1's router, which is read-only).
- `classify_cli_result(result: dict) -> dict` — same `{retry, retry_epoch, reason}` shape as
  `classify_api_error()`, plugs into the same retry-epoch machinery. Pattern-matches `error`
  text for rate-limit-shaped/auth-shaped/unknown; defaults retryable epoch to
  `now + CLAUDE_RETRY_EPOCH_DEFAULT_SECONDS` (placeholder ~5hr) absent a better signal. Most
  likely piece to need tuning once real error text is observed.
- `run_stage2_chapter(target_dir, live_mode) -> int` (the module's actual dispatch
  entrypoint, replacing today's `NotImplementedError` stub): sync the skill package; load
  `{"session_id", "attempts"}` from a progress file if present; on resume, send a short
  continuation prompt instead of the full prompt; call `run_claude_cli_subprocess()`; run the
  truth-check; write `MARKER`/`FAILMARK` or persist progress + `write_retry_epoch()` — same
  contract shape as `direct_api.stage2_api.run_generate()` and
  `agents.stage2_graph.run_stage2_chapter()`.

### Config additions for Stage 2 (add when built — additive, nothing removed)

```python
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "")  # unset by default; when set, passed as --model
CLAUDE_ALLOWED_TOOLS = os.environ.get("CLAUDE_ALLOWED_TOOLS",
    "Bash(python3 *),Bash(node *),Bash(npm *),Bash(pdftotext *),Bash(pdfinfo *),"
    "Bash(pdftoppm *),Bash(soffice *),Read,Write,Edit,Glob,Grep")
CLAUDE_PERMISSION_MODE = os.environ.get("CLAUDE_PERMISSION_MODE", "acceptEdits")
CLAUDE_CLI_TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_CLI_TIMEOUT_SECONDS", "3600"))  # placeholder, revisit after pilot timing
CLAUDE_RETRY_EPOCH_DEFAULT_SECONDS = int(os.environ.get("CLAUDE_RETRY_EPOCH_DEFAULT_SECONDS", str(5 * 3600)))  # placeholder
CLAUDE_SKILL_SOURCE_GLOB = os.environ.get("CLAUDE_SKILL_SOURCE_GLOB", "templates/*.skill")
CLAUDE_SKILL_INSTALL_DIR = LOCAL_RUNTIME_ROOT / ".claude" / "skills" / "study-notes"
CLAUDE_WORKSPACE_ROOT = LOCAL_RUNTIME_ROOT / "generation-workspace"  # per-chapter scratch subfolders
```

`src/func_tools_and_utils.py` addition (when built): a small `log_cli_usage_summary(logger,
result)` helper for Stage 2's differently-shaped usage dict — additive, doesn't touch any
existing function in that file.

### `templates/` disposition (when Stage 2 is built)

- `templates/study-notes.skill` — canonical source, read by `sync_skill_package()`. Not
  modified by this project.
- `templates/study-notes-skill.md` — candidate for removal once nothing reads it anymore.
  Grep the repo once more before deleting (readme's directory tree references it, and
  `direct_api/stage2_api.py` — the still-live legacy implementation — may still read
  it via `load_skill_prompt()`; **do not remove this until confirming `direct_api` doesn't
  depend on it**, since that implementation is not being touched by this plan).
- `templates/study-notes-skill-improvement-brief.md` — read before deciding; likely harmless
  historical notes.

### Verification / staged pilot rollout (when Stage 2 is built)

Deliberately staged so nothing touches the unattended nightly job until confidence is
established.
1. **Resolve the skill-discovery mechanic first, manually, outside any project code.** Extract
   `templates/study-notes.skill` by hand, `cd` into a scratch subfolder, run `claude -p
   "/study-notes ..." --output-format json` against one real, small, already-assembled
   chapter (via `--add-dir`) with `env -u ANTHROPIC_API_KEY`. Confirm the skill actually loads
   before writing any wrapper code.
2. **Static checks, zero `claude` invocations:** full test suite passes; `--stage2-mode no-llm
   --stage2-impl subprocess` (once the stub is replaced) confirms readiness checks (claude on
   PATH, skill package found) with no subprocess/network call; `--doctor` reports sensibly.
3. **Manual single-chapter run**, `python3 src/main.py --stage2-mode llm-full --stage2-impl subprocess`
   at a terminal, watching the log live. Confirm the scrubbed-env subprocess call fires
   correctly, the truth-check gates the success marker, cost/usage are logged, and —
   critically, out-of-band via Claude.ai's usage view — that the run billed against the
   subscription.
4. **Deliberately induce a resumable failure** (e.g. temporarily lower
   `CLAUDE_CLI_TIMEOUT_SECONDS`) to confirm session-ID resume works end to end.
5. **Capture real rate-limit/usage-window error text** whenever naturally observed, feed back
   into `classify_cli_result()`'s patterns and test fixtures.
6. **Only after several sanity-checked manual runs**, consider switching nightly automation to
   `--stage2-impl subprocess` via the Task-Scheduler mode-switching mechanism already
   documented in `docs/setup-guide.md`'s "Switching the Nightly Pipeline Mode" section — no
   wrapper-script changes needed beyond what that section already covers.

### Corrections from the superseded doc

The original `stage2-claude-cli-migration-plan.md` assumed Stage 2 had one implementation
being replaced in place. Under the current 3-parallel-implementations architecture, these
specific instructions from that doc **do not apply** and must not be followed literally:

- ~~"Remove `tool_read`, `tool_write`, `tool_edit`, `tool_glob`, `tool_grep`, `tool_bash`,
  `tool_convert_to_png`, `tool_view_image`, `tool_view_pdf_page`, ... from
  `func_tools_and_utils.py`"~~ — **wrong now.** `src/agents/tools.py`,
  `src/agents/stage1_graph.py`, and `src/agents/stage2_graph.py` (the graph implementation)
  import exactly those functions. They stay. Nothing is removed from
  `func_tools_and_utils.py`.
- ~~"Remove `GENERATOR_MODEL` (superseded by `CLAUDE_MODEL`), `MAX_TURNS` (superseded by
  `CLAUDE_CLI_TIMEOUT_SECONDS`)"~~ — **wrong now.** `direct_api/stage2_api.py` (the
  legacy implementation) still uses both and stays fully intact. `CLAUDE_MODEL` and
  `CLAUDE_CLI_TIMEOUT_SECONDS` are purely additive new settings for `claude_cli_subprocess/stage2_cli.py`.
- ~~"Rewrite `run_generate()`'s LIVE branch [in `stage2_api.py`]"~~ — **wrong now.**
  `direct_api/stage2_api.py` is not touched by this plan at all. All of this logic
  goes into the new `src/claude_cli_subprocess/stage2_cli.py::run_stage2_chapter()` instead.
- ~~`tests/test_utils.py`: "rewrite `test_run_generate_mock_mode_makes_zero_api_calls`...
  `fgn.client` no longer exists"~~ — **wrong now**, `fgn` (`direct_api.stage2_api`)
  is unchanged, `fgn.client` still exists. New tests for the subprocess path belong in a new
  `tests/test_claude_cli_subprocess_stage2.py` once that module is built, not as edits to
  `tests/test_utils.py`.
- `config/settings.py`: the old doc's proposed `CLAUDE_CLI_PATH` setting is **unified into
  `CLAUDE_BIN`** (already introduced for Stage 1 below) — one setting for the `claude` binary
  path, shared by both stages, not two separately-named settings for the same thing.

Everything else from the original doc (skill package facts, function designs, the pilot
verification staging, the config settings not listed above) carries forward unchanged.

---

## Config additions for Stage 1 (add now)

Append after the existing "Multi-Agent Architecture settings" block in `config/settings.py`:

```python
# ---------------------------------------------------------------------------
# claude CLI settings (src/claude_cli_subprocess/) -- the "subprocess" implementation.
# Invokes the `claude` binary as a subprocess (Claude-subscription billing)
# rather than calling the Anthropic SDK directly. Defaults work with a bare
# `claude` on PATH -- zero config needed to try it. CLAUDE_BIN is shared by
# both Stage 1 (built now) and Stage 2 (scaffolded; see cli-subprocess-plan.md).
# ---------------------------------------------------------------------------
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
# Reuses the same ASSEMBLE_ROUTER_MODEL env var as ROUTER_MODEL above -- one
# router-model setting shared across whichever --stage1-impl is selected.
ESCALATE_MODEL = os.environ.get("ASSEMBLE_ESCALATE_MODEL", "claude-sonnet-5")
```

No naming collisions confirmed by reading the full current file. `CONF_MIN` (existing) is
reused as-is by `stage1_cli.py`. Stage 2's settings (`CLAUDE_MODEL`, `CLAUDE_ALLOWED_TOOLS`, etc.,
listed above) get added when Stage 2 is actually built, not now.

## `src/main.py` / `src/agents/dispatch.py` diffs

- `--stage1-impl` choices: `["legacy", "graph"]` → `["legacy", "graph", "subprocess"]`.
- `--stage2-impl` choices: `["legacy", "graph"]` → `["legacy", "graph", "subprocess"]`, plus in
  `main()`, immediately after `args = parser.parse_args()`:
  ```python
  if args.stage2_impl == "subprocess":
      parser.error(
          "--stage2-impl subprocess is not yet implemented -- see the 'Stage 2: full "
          "design' section of docs/cli-subprocess-plan.md. Use --stage2-impl legacy or "
          "--stage2-impl graph."
      )
  ```
  matches the existing `parser.error(...)` precedent already used in `resolve_stage()`.
- `dispatch.py::route_file()`: add an `elif impl == 'subprocess':` branch before the `else:`
  (legacy) branch:
  ```python
  elif impl == 'subprocess':
      logger.info('Stage 1: using SUBPROCESS implementation (claude CLI)')
      from src.claude_cli_subprocess.stage1_cli import route_file as cli_route_file
      return cli_route_file(path, buckets, prior, tracker)
  ```
- `dispatch.py::generate_notes()`: add a matching `elif impl == 'subprocess':` branch calling
  `src.claude_cli_subprocess.stage2_cli.run_stage2_chapter(target_dir, live_mode)` — keeps the
  `NotImplementedError` message defined in exactly one place.
- Both `dispatch.py` "legacy" branches also update import paths to `src.direct_api.func_*` per
  the move above.

## New tests (build now, for Stage 1)

`tests/test_dispatch.py`: add `test_route_file_subprocess_mode` (mocks
`src.claude_cli_subprocess.stage1_cli.route_file`) and `test_generate_notes_subprocess_mode_raises` (asserts
`NotImplementedError`), following the existing `patch('src.agents.dispatch.config')` pattern.

New `tests/test_claude_cli_subprocess_stage1.py`: mock `subprocess.run` (never call the real `claude`
binary) — JSON envelope parsing, usage-limit detection, launch-failure handling
(`FileNotFoundError`), confidence-based escalation to `ESCALATE_MODEL`, and critically
`build_claude_env()` actually popping `ANTHROPIC_API_KEY`.

(Stage 2 tests — `test_build_claude_env_pops_anthropic_api_key`,
`test_sync_skill_package_picks_newest_and_skips_unchanged`, `test_classify_cli_result_*`,
`test_run_generate_live_mode_resume_uses_short_prompt` — get written when `claude_cli_subprocess/stage2_cli.py`
is actually built, in a new `tests/test_claude_cli_subprocess_stage2.py`, not as edits to
`tests/test_utils.py`.)

## Documentation updates

Not exhaustive line-editing — after the code move, run
`grep -rn "stage1_api\|stage2_api" --include="*.md" .` and update path
references only (not narrative content) in the ~6 files with hits: `readme.md`,
`docs/agent_migration_walkthrough.md`, `docs/migration-to-agents.md`,
`docs/skill-integration-plan.md`, `docs/web-enrichment-plan.md`, and this file's own
references. Re-run the same grep after editing to confirm zero stale hits remain.
`docs/setup-guide.md`'s Task-Scheduler examples need **no changes** (CLI enum values aren't
changing). When Stage 2 is eventually built: update `readme.md`/`docs/architecture.md`'s
now-outdated "no `claude` CLI login required" framing and Stage-3 invocation-tree description
— but only for the `subprocess` path; the `legacy`/`graph` paths' descriptions stay accurate
as-is since those implementations are untouched.

## Verification (Stage 1, now)

```bash
cd /path/to/project/root

# 1. Full existing suite — confirms the move broke nothing
pytest tests/ -q

# 2. New tests specifically
pytest tests/test_claude_cli_subprocess_stage1.py tests/test_dispatch.py -q -k "subprocess or claude_cli_subprocess"

# 3. Import smoke test for every moved/new module
python3 -c "
import sys; from pathlib import Path
ROOT = Path('.').resolve()
sys.path.insert(0, str(ROOT / 'config')); sys.path.insert(0, str(ROOT))
import src.direct_api.stage1_api, src.direct_api.stage2_api
import src.claude_cli_subprocess.common, src.claude_cli_subprocess.stage1_cli, src.claude_cli_subprocess.stage2_cli
import src.agents.dispatch
print('all imports OK')
"

# 4. CLI surface + fail-fast check
python3 src/main.py --help | grep -A3 "stage1-impl\|stage2-impl"
python3 src/main.py --stage2-mode llm-full --stage2-impl subprocess; echo "exit code: $?"  # expect parser.error, nonzero, no work started

# 5. Doc-reference grep, before and after edits
grep -rn "stage1_api\|stage2_api" --include="*.md" .
```

Manual/live step (not automatable, needs `claude` CLI installed + authenticated): run one real
Stage 1 routing call via `--stage1-impl subprocess` against a single test file, and confirm
`ANTHROPIC_API_KEY` did not leak into the subprocess — most certain method is temporarily
pointing `CLAUDE_BIN` at a tiny wrapper script that dumps `env` to a file before exec'ing the
real `claude` binary, then grepping that dump for `ANTHROPIC_API_KEY` (expect no match).

## Streamlined cost-safety mode across all three implementations (implemented)

`config.DEV_TOKEN_SAVER_MODE` is set per-stage via `--stage1-mode`/`--stage2-mode
{off,no-llm,llm-token-saver,llm-full}` (see `src/main.py::resolve_stage_mode()`) — there is no
longer a standalone `--dev-token-saver` flag. Passing `llm-token-saver` for a stage produces a
near-zero-cost real call that still exercises that stage's full wiring, using whatever
cost-control mechanism its active `--stageN-impl` actually has available. Because the setting
is per-stage rather than global, the two stages can independently be `llm-full` and
`llm-token-saver` in the same invocation — e.g. a real Stage 1 routing pass alongside a cheap
Stage 2 wiring smoke test.

| Implementation | Cheap model | Dummy prompt (skips real content) | Capped output | Skipped escalation |
|---|---|---|---|---|
| `direct_api` (`src/direct_api/`) | Stage 1 already uses cheap `ROUTER_MODEL`; Stage 2 switches to `FIGURE_MODEL` | Stage 1 skips `extract_content()` (real PDF pages); Stage 2 skips `build_user_prompt()`/`load_skill_prompt()` | `max_tokens` capped (150 for Stage 1's forced tool call, 50 for Stage 2) | n/a (Stage 1 has no escalation step) |
| `agents` (`src/agents/`) | forces `FIGURE_MODEL` for every node | `prompts.py` swaps in a one-line dummy instruction | `max_tokens=50` | n/a |
| `claude_cli_subprocess` (`src/claude_cli_subprocess/`) | n/a (CLI has no cheap/expensive model split at this layer beyond `ROUTER_MODEL`/`ESCALATE_MODEL`) | `stage1_cli.py` skips the real routing prompt (never points the CLI at a real file) | `--max-budget-usd`/`--effort low` (no SDK `max_tokens` to cap) | escalation to `ESCALATE_MODEL` skipped entirely |

New settings: `config.DEV_TOKEN_SAVER_MAX_BUDGET_USD` (default `"0.02"`), `config.DEV_TOKEN_SAVER_EFFORT`
(default `"low"`) — subprocess-specific, since the CLI has no `max_tokens` equivalent to cap
directly. When Stage 2 subprocess (`src/claude_cli_subprocess/stage2_cli.py`) is eventually built,
it should follow the same pattern: dummy `/study-notes` prompt that doesn't point at real
transcripts, plus these same two flags, rather than inventing a new mechanism.

## Open items carried into implementation

- Skill discovery mechanic (project-local `.claude/skills/` vs. global) — resolve first, per
  Stage 2 pilot step 1, when Stage 2 is built.
- Exact `-r`/`--resume` flag spelling — confirm against `claude -p --help` at that time.
- Real usage-window length/caps under subscription billing — undocumented; observe
  empirically.
- `CLAUDE_CLI_TIMEOUT_SECONDS` default (3600s) — revisit once a real chapter's generation time
  is measured.
- `templates/study-notes-skill-improvement-brief.md` content — read before deciding
  keep/remove.
- `src/agents/stage2_graph.py` line ~315's exact current text — confirm before editing its
  import path.
