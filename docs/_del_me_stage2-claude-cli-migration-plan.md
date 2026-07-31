> **SUPERSEDED — kept for reference only, not deleted.** Merged into
> `docs/cli-subprocess-plan.md`, reconciled against the current 3-parallel-implementations
> architecture (`src/direct_api/`, `src/agents/`, `src/claude_cli_subprocess/`). This doc
> assumed Stage 2 had a single implementation being replaced in place — under the current
> architecture, several of its instructions below (e.g. "remove `tool_*` from
> `func_tools_and_utils.py`", "remove `GENERATOR_MODEL`/`MAX_TURNS`") are **wrong** and must
> not be followed literally; see `docs/cli-subprocess-plan.md`'s "Corrections from the
> superseded doc" section. Everything else here (skill package facts, function designs,
> config settings, verification staging) was carried forward accurately.

# Plan: Move Stage 2 (note generation) from raw Anthropic API to `claude` CLI subscription billing, using the engineered study-notes skill package

## Context

Stage 1 (transcript/material routing, Haiku via the raw API) already ran for real and cost
more than expected — already addressed separately (escalation removed, no repeated LLM spend
on already-processed files). Stage 2 (the actual chapter-note generator) has **never once run
in `--live` mode** on this machine — every historical run has been the zero-token mock
preview. Before Stage 2 is ever billed for the first time, the user wants to avoid repeating
Stage 1's cost surprise by switching Stage 2's execution engine from metered per-token
Anthropic API calls to the `claude` CLI (Claude Code), billed against a Claude.ai
subscription instead.

Separately, the user keeps iteratively improving the actual study-notes skill and doesn't
want to keep hardcoding a path/copy of it into this project. Investigation revealed this
isn't a single prompt file question — there are three candidate artifacts, and the one the
user wants used, `templates/study-notes.skill`, turned out to be a full **engineered skill
package** (zip: `SKILL.md` + `tools/` + `lib/` + `schema/` + `example/` + `LESSONS.md`), not
a plain markdown prompt. Its own `SKILL.md` documents a **deterministic, data-driven
pipeline** specifically built to solve the token-cost/quality problem this plan is about: the
model authors two JSON files (`content.json`, `figures.json`) against strict schemas, then
runs five fixed commands (`tools/ingest.py`, `tools/validate.py`×2, `lib/figbuild.py`,
`tools/verify.py`, `lib/build.js`, `tools/qa.py`) that do the actual docx/diagram
construction deterministically in code — the model never hand-writes matplotlib or docx
calls. The skill's own text states the earlier freeform approach "burned ~150k tokens per
document and still shipped 13 layout defects." **This package, run via Claude Code, is the
token-efficiency answer — not prompt caching bolted onto the old hand-rolled loop.**

**Explicit scope, confirmed with the user:**
1. Only Stage 2 changes engines. Stage 1 (Haiku routing, raw API) is untouched.
2. The skill to use is the **package** at `templates/study-notes.skill` (or whatever the
   user drops there going forward as they keep enhancing it) — not the plain
   `templates/study-notes-skill.md`, and not the simpler `SKILL.md`-only skill currently
   installed at `~/.claude/skills/study-notes/` on this machine (confirmed different content,
   older, lacks the tools/lib pipeline).

## Confirmed environment facts

- `claude` CLI v2.1.197 is installed at `/usr/bin/claude`, already authenticated via
  OAuth/subscription (`~/.claude/.credentials.json` present).
- Headless/non-interactive invocation: `-p/--print` + `--output-format json` +
  `--permission-mode acceptEdits` + `--allowedTools` + `--add-dir` — no TTY/human required.
- **Billing precedence risk (highest-severity item in this plan):** a `claude` subprocess
  that inherits `ANTHROPIC_API_KEY` silently bills metered Console instead of the
  subscription — no error, no warning. Stage 1's `load_dotenv(~/.anthropic_env)` puts that
  key into `os.environ` of the same `main.py` process that later runs Stage 2. **Must
  explicitly scrub `ANTHROPIC_API_KEY` from the subprocess's `env=`.**
- `templates/study-notes.skill` (confirmed via `python3 -m zipfile`): a real skill package,
  79KB zipped, `SKILL.md` alone is 16.3KB (vs. 6.9KB for the plain template and 7.1KB for the
  simple installed skill). Contents: `tools/{ingest,validate,verify,qa,regress,pedagogy,
  baseline,invariants,_log}.py`, `lib/{build.js,figbuild.py,figlib.py,dochelp.js,
  package.json}`, `schema/{content,figures}.schema.json`, `example/{content,figures}.json`
  (a working reference), `quality/baseline.json` + `quality/reference_section/`, `LESSONS.md`.
  Frontmatter `name: study-notes` — same invocation name as the simpler installed skill, but
  materially different content.
- The skill's own text is explicit that **paths must never be hardcoded**: "All paths below
  are relative to this SKILL.md... Never hardcode an absolute skill path; the location
  changes every session" — the model resolves its own skill directory (`$S`) at runtime. This
  independently confirms the "don't hardcode the skill path" requirement was already a design
  goal of the skill itself, not something this project's wrapper code needs to solve by
  string manipulation.
- Confirmed external-binary dependencies (grepped `subprocess.run`/`execFileSync` calls in
  the package): `pdftotext`, `pdfinfo`, `pdftoppm` (poppler-utils), `soffice` (libreoffice),
  `node`+`npm install docx` once per working folder, `python3`. **All of these are already
  install.sh dependencies of this project** — no new system packages needed.
- The skill explicitly recommends **subagent delegation** for reading transcript page images
  ("Never read transcript page images in the main thread... Spawn a subagent") — this is a
  Claude-Code-native pattern (Task/subagent tool), reinforcing that this package was
  engineered for a real Claude Code execution environment, not the old hand-rolled loop and
  not a bare single-shot `claude -p` call with no subagent capability.
- `~/.claude/skills/study-notes/SKILL.md` (the currently-globally-installed skill) is a
  simpler, older, different document — confirmed NOT what the user wants used for this
  pipeline. Left alone by this plan; not touched or overwritten.
- `-r/--resume <session_id>` can resume an interrupted headless session via Claude Code's own
  transcript persistence, replacing the current hand-rolled full-message-history checkpoint
  with `{"session_id": ..., "attempts": N}`.
- Usage-window/rate-limit specifics under subscription billing are undocumented anywhere
  findable — treated as something to observe empirically in the pilot, not hardcoded.
- Confirmed via grep: Stage 1 doesn't use any `tool_*`/`AGENT_TOOLS` code — all of that is
  Stage-2-only and safe to delete.
- Confirmed via read: `main.py` only consumes `run_generate()`'s int exit code — no signature
  change needed there.

## Architecture: before → after

**Before:** `stage2_api.py` builds a system prompt from `templates/study-notes-skill.md`,
then runs a hand-rolled loop calling `client.messages.create(...)` up to `MAX_TURNS=200`
times against a custom `AGENT_TOOLS` schema, checkpointing full message history after every
turn.

**After:** For each chapter, sync the current `templates/*.skill` package into a stable local
skill directory, then make ONE `subprocess.run(["claude", "-p", ...])` call with
`ANTHROPIC_API_KEY` scrubbed, `cwd` set to a **local scratch working folder** (not the actual
Google-Drive chapter folder — the skill wants `npm install`/`node_modules`/figures/build
artifacts in "your own working folder," and building on synced Drive storage would be slow
and pollute it with build cruft, the same "zero-latency local execution" reasoning already
used elsewhere in this pipeline), `--add-dir` extended to the real chapter folder (for
reading transcripts/supporting material and writing the final `.docx`), and
`--allowedTools` covering the skill's actual dependencies (`Bash(python3 *)`,
`Bash(node *)`, `Bash(npm *)`, `Bash(pdftotext *)`, `Bash(pdfinfo *)`, `Bash(pdftoppm *)`,
`Bash(soffice *)`, `Read`, `Write`, `Edit`, `Glob`, `Grep`). The prompt is short: invoke
`/study-notes`, point at the chapter's transcripts/supporting folders, state the required
output path — the skill's own pipeline (ingest → author JSON → validate → figbuild → verify →
build → qa) does the rest deterministically. Success is still decided by
`expected_docx.exists()` after the call returns, never by trusting self-reported success.

### Skill package sync (new, required)

Claude Code needs the skill's own directory (containing `tools/`, `lib/`, `schema/`,
`example/`) physically present on disk to discover and run it — a bare prompt reference to
`/study-notes` isn't enough if only a plain `SKILL.md` exists at the discovered location.
Before invoking `claude -p`, a small sync step:
1. Finds the newest `templates/*.skill` file by mtime (supports the user dropping updated
   exports without any code change — satisfies "pick the latest at runtime").
2. Compares it (content hash, or just mtime) against what's currently unzipped at the target
   skill directory; re-extracts only if changed, so this is nearly free on unchanged runs.
3. Extracts it to a **stable local directory this project controls** — recommended:
   `config.LOCAL_RUNTIME_ROOT / ".claude" / "skills" / "study-notes"`, a sibling of the local
   scratch working folder — rather than the user's global `~/.claude/skills/study-notes/`.
   This avoids clobbering/conflicting with whatever the user has installed globally (which
   may be intentionally different, used for other purposes), and keeps the sync fully
   contained to this project.

**Open mechanic to verify in the pilot, not assumed:** whether Claude Code's project/local
skill discovery reliably finds `.claude/skills/study-notes/` by walking up from a `cwd` set
to a subdirectory of `LOCAL_RUNTIME_ROOT` (the way `git`/`CLAUDE.md` discovery walks up from
cwd), the same way global `~/.claude/skills/` discovery is known to work. If this doesn't
pan out cleanly, the fallback is syncing into `~/.claude/skills/study-notes/` directly
(global, simpler, but does overwrite/share state with whatever's installed there today) —
this exact question is the first thing to resolve in the pilot's manual smoke test (below),
before writing any of the wrapper code around it.

## File-by-file changes

### `src/stage2_api.py`
- **Keep unchanged:** `select_target_chapter()`, the MOCK-mode zero-cost contract, the
  `expected_docx.exists()` truth-check philosophy, `EXIT_OK`/`EXIT_RATE_LIMITED`/`EXIT_FATAL`
  + `write_retry_epoch()`, `MARKER`/`FAILMARK` semantics.
- **Remove:** `load_skill_prompt()`, `build_user_prompt()`, `AGENT_TOOLS`, `execute_tool()`,
  `_truncate_text_blocks()`, the manual per-turn loop, the `anthropic.Anthropic()` client.
- **Add:**
  - `sync_skill_package() -> Path` — finds newest `templates/*.skill`, extracts to the local
    skill directory if changed, returns that directory's path (for logging/verification, not
    for hardcoding into the prompt — the skill resolves itself).
  - `build_cli_prompt(target_dir, expected_docx) -> str` — short: `/study-notes`, the
    chapter's transcript/supporting folder paths, required output path, same scope framing
    as today (transcripts = spine/non-negotiable, supporting = enrichment-only). No tool-name
    references — the skill's own pipeline instructions take over from here.
  - `build_claude_env() -> dict` — `os.environ.copy()` with `ANTHROPIC_API_KEY` popped.
    Isolated as its own small, obviously-named, unit-tested function — the single
    highest-risk item in this migration, not an inline `.pop()` a future edit could drop.
  - `run_claude_cli(target_dir, prompt, resume_session_id=None) -> dict` — builds argv against
    a local scratch `cwd` with `--add-dir target_dir`, `--permission-mode acceptEdits`,
    `--allowedTools config.CLAUDE_ALLOWED_TOOLS`, optional `--model config.CLAUDE_MODEL`,
    optional `-r resume_session_id`; runs via `subprocess.run(..., env=build_claude_env(),
    cwd=local_scratch_dir, capture_output=True, text=True,
    timeout=config.CLAUDE_CLI_TIMEOUT_SECONDS)`; parses stdout JSON; returns a plain dict
    (`ok`, `result`, `session_id`, `total_cost_usd`, `usage`, `error`, `returncode`). Logs
    cost/usage/error prominently regardless of outcome. Never passes
    `--dangerously-skip-permissions`.
  - `classify_cli_result(result: dict) -> dict` — same `{retry, retry_epoch, reason}` shape as
    `classify_api_error()`, plugs into the same retry-epoch machinery. Pattern-matches
    `error` text for rate-limit-shaped / auth-shaped / unknown; defaults retryable epoch to
    `now + CLAUDE_RETRY_EPOCH_DEFAULT_SECONDS` (placeholder ~5hr) absent a better signal.
    Flagged as the piece most likely to need tuning once real error text is observed.
- **Rewrite `run_generate()`'s LIVE branch:** sync the skill package; load
  `{"session_id", "attempts"}` from the (much smaller) progress file if present; on resume,
  send a short continuation prompt instead of the full prompt (avoids re-sending large PDF
  context into a session Claude Code can already recall); call `run_claude_cli()`; run the
  truth-check; write MARKER/FAILMARK or persist progress + `write_retry_epoch()` exactly as
  today's retry path does.
- `config.MAX_TURNS` becomes irrelevant — replaced by `CLAUDE_CLI_TIMEOUT_SECONDS` bounding
  one subprocess call.

### `src/func_tools_and_utils.py`
Remove (confirmed dead outside Stage 2 via grep): `tool_read`, `tool_write`, `tool_edit`,
`tool_glob`, `tool_grep`, `tool_bash`, `ALLOWED_BASH_COMMANDS`, `tool_convert_to_png`,
`tool_view_image`, `tool_view_pdf_page`, `_text_block`, `_image_block_from_bytes`,
`_IMAGE_MEDIA_TYPES`. Keep everything else (`TokenTracker`, `classify_api_error`,
`write_retry_epoch`, `sha256`, `is_ignorable`, `load_state`/`save_state`, exit constants).
Add a small `log_cli_usage_summary(logger, result)` helper for Stage 2's differently-shaped
usage dict.

### `config/settings.py`
- Add: `CLAUDE_CLI_PATH` (default `"claude"`), `CLAUDE_MODEL` (default unset; when set,
  passed as `--model` — keeps explicit quality control available), `CLAUDE_ALLOWED_TOOLS`
  (default `"Bash(python3 *),Bash(node *),Bash(npm *),Bash(pdftotext *),Bash(pdfinfo *),
  Bash(pdftoppm *),Bash(soffice *),Read,Write,Edit,Glob,Grep"` — matching the package's
  confirmed actual dependencies), `CLAUDE_PERMISSION_MODE` (`"acceptEdits"`),
  `CLAUDE_CLI_TIMEOUT_SECONDS` (default 3600, placeholder pending pilot timing),
  `CLAUDE_RETRY_EPOCH_DEFAULT_SECONDS` (`5*3600` placeholder), `CLAUDE_SKILL_SOURCE_GLOB`
  (default `templates/*.skill`), `CLAUDE_SKILL_INSTALL_DIR` (default
  `LOCAL_RUNTIME_ROOT / ".claude" / "skills" / "study-notes"`), `CLAUDE_WORKSPACE_ROOT`
  (default `LOCAL_RUNTIME_ROOT / "generation-workspace"` — per-chapter scratch subfolders
  live here).
- Remove: `GENERATOR_MODEL` (superseded by `CLAUDE_MODEL`), `MAX_TURNS` (superseded by
  `CLAUDE_CLI_TIMEOUT_SECONDS`).
- Unchanged: `MAX_ATTEMPTS`, `ROUTER_MODEL`, all marker/dir constants, `CHAPTER_PROGRESS_DIR`,
  `RETRY_EPOCH_FILE`, `LOCAL_RUNTIME_ROOT`.

### `templates/` disposition
- `templates/study-notes.skill` — **this is now the canonical source**, read by
  `sync_skill_package()`. Not modified by this project; the user keeps dropping updated
  exports here.
- `templates/study-notes-skill.md` — **remove**. Stale, references dead tool names, no
  longer read by anything once `load_skill_prompt()` is deleted. Grep the repo once more for
  its path before deleting (README's directory tree references it).
- `templates/study-notes-skill-improvement-brief.md` — read its content before deciding;
  likely harmless historical notes, fine to keep as long as nothing wires it in as a runtime
  input.

### `src/main.py`
No signature/call-site change needed. Extend `run_doctor()`'s diagnostics: `claude` resolves
on PATH, `~/.claude/.credentials.json` exists, `templates/*.skill` is found and its mtime,
and clarify the existing `ANTHROPIC_API_KEY` check is Stage-1-only and deliberately scrubbed
before Stage 2's subprocess call.

### `tests/test_utils.py`
- `test_select_target_chapter_skips_dotfolders`, `test_select_target_chapter_skips_setup_and_review_folders`:
  unchanged.
- `test_run_generate_mock_mode_makes_zero_api_calls`: rewrite — `fgn.client` no longer
  exists; patch `run_claude_cli` to raise if ever called; keep the same
  `EXIT_OK`/no-docx/no-marker assertions.
- New tests: `test_build_claude_env_pops_anthropic_api_key` (highest priority),
  `test_build_claude_env_noop_when_key_absent`, `test_sync_skill_package_picks_newest_and_skips_unchanged`,
  `test_classify_cli_result_*` battery (placeholder fixtures until real CLI error text is
  observed), and optionally `test_run_generate_live_mode_resume_uses_short_prompt`.
- `tests/test_main_cli.py`: no changes.

### Documentation (`readme.md`, `docs/architecture.md`)
Update the now-false "no `claude` CLI / Claude Code subscription login required" claim, the
Mock Mode / Visual QA tools / Resume Capability bullets, the directory-tree skill-file entry,
and `docs/architecture.md`'s Stage 3 invocation-tree diagram — all describe the old
custom-tool-loop design. Add a short section describing the skill package sync mechanism and
the local-scratch-workspace-vs-Drive-chapter-folder split.

## Verification / staged pilot rollout

Deliberately staged so nothing touches the unattended nightly job until confidence is
established — first time Stage 2 will ever spend anything for real.

1. **Resolve the skill-discovery mechanic first, manually, outside any project code.**
   Extract `templates/study-notes.skill` by hand into a candidate local directory, `cd` into
   a scratch subfolder under it, and run `claude -p "/study-notes ..." --output-format json`
   against one real, small, already-assembled chapter (via `--add-dir`) with
   `env -u ANTHROPIC_API_KEY`. Confirm the skill actually loads (check the output/behavior
   matches the package's pipeline, not a generic response) before writing any wrapper code
   around it. This determines whether project-local discovery works or the fallback
   (global `~/.claude/skills/`) is needed.
2. **Static checks, zero `claude` invocations:** full test suite passes;
   `--run-generate-no-llm` manually run and log confirms readiness checks (claude on PATH,
   skill package found) with no subprocess/network call; `--doctor` reports sensibly.
3. **Manual single-chapter run through this project's own code**, `python3 src/main.py
   --run-generate` at a terminal, watching the log live. Confirm the scrubbed-env subprocess
   call fires correctly, the truth-check gates the success marker, cost/usage are logged, and
   — critically, out-of-band via Claude.ai's usage view since this can't be verified from
   this project's own logs alone — that the run billed against the subscription.
4. **Deliberately induce a resumable failure** (e.g. temporarily lower
   `CLAUDE_CLI_TIMEOUT_SECONDS`) to confirm session-ID resume works end to end with the short
   continuation prompt, not a full re-send.
5. **Capture real rate-limit/usage-window error text** whenever naturally observed, feed it
   back into `classify_cli_result()`'s patterns and the test fixtures.
6. **Only after several sanity-checked manual runs**, flip
   `scripts/wsl-study-notes-processor.sh`'s nightly invocation from `--run-generate-no-llm` to
   `--run-generate` — a separate, deliberately reviewable one-line change. No other
   wrapper-script changes needed: the exit-code-42/retry-epoch-wake mechanism is already
   engine-agnostic (confirmed by reading both `wsl-study-notes-processor.sh` and
   `win-environment-setup.ps1`).

## Open items carried into implementation (not blocking, flagged for pilot-time resolution)
- Skill discovery mechanic (project-local `.claude/skills/` vs. global) — resolve first, per
  pilot step 1.
- Exact `-r`/`--resume` flag spelling — confirm against `claude -p --help`.
- Real usage-window length/caps under subscription billing — undocumented; observe
  empirically.
- `CLAUDE_CLI_TIMEOUT_SECONDS` default (3600s) — revisit once a real chapter's generation
  time is measured.
- `templates/study-notes-skill-improvement-brief.md` content — read before deciding
  keep/remove (likely inert historical notes).

## Status
No implementation has started. This document is a persisted copy for the user's own
reference/tracking inside the repo — this is a documentation save only, not a green light for
any of the code/config changes described above.
