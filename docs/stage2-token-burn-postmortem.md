# Stage 2 token-burn postmortem — Chemistry-Ch1-Gaseous-State

Forensic analysis of one `subprocess` Stage 2 run that consumed **two full 5-hour
subscription windows** across two attempts while producing a document that passed every
quality gate. Written 2026-08-08 from the run's own artifacts, not from self-report.

**Command:**

```bash
study-notes-runner --stage1-mode off --stage2-mode llm-full --target-dir "<AI-Chapter-Notes>/Chemistry-Ch1-Gaseous-State"
```

**Session:** `4e519a01-be97-4730-a804-53be4b0194c9` (1329 lines, 3.5 MB, plus 6 subagent
transcripts). Model: `claude-sonnet-5` (pinned, `config/settings.py`). Outcome: success —
79 pages, `_notes_done` placed, `qa_report.json` all-passed.

## Headline numbers

| Metric | Value |
|---|---|
| Wall clock | 06:47:49 → 10:31:53 (with a 2h36m dead gap) |
| API turns (main thread) | **622** |
| API turns (6 subagents) | **328** |
| Cache-read tokens (main) | 218,394,164 |
| Cache-creation tokens (main) | 6,895,183 |
| Output tokens (main) | 805,359 |
| Cache-read tokens (subagents) | 17,621,809 |
| Context growth (main) | 35k → **577k** |
| Tool-call errors (main) | 47 |
| Tool-call errors (subagents) | 50 |

For comparison, the skill's own `LESSONS.md` describes a normal run as **~45 turns,
context peaking ~150k, ~40 minutes**. This run was ~10× the turns and ~4× the context
ceiling.

## Timeline — two attempts, two different failure modes

| Time | Event |
|---|---|
| 06:47:49 | Session starts, `/study-notes` skill invoked |
| 06:48–06:57 | Setup, source inspection, planning (~143 turns) |
| 06:58–07:05 | **6 subagents dispatched in parallel** for section drafting |
| 07:05 | Window 1 exhausted. Session goes dark. |
| *(2h36m gap)* | |
| 09:41:21 | `"Continue from where you left off"` — attempt 2 resumes via `-r` |
| 09:41–10:31 | ~478 turns of gate-fixing, figure building, correction rounds |
| 10:31:53 | Delivered |

**Window 1 was killed by the subagent fan-out, not by context growth.** The main thread
had only reached ~143 turns in 18 minutes. The six subagents consumed 17.6M cache-read
tokens in roughly six minutes of wall time — one of them (Boyle's/Charles's) 7.85M alone.

**Window 2 was a 478-turn gate-fixing grind** — where the friction analysis below applies.

## Error taxonomy — ~70% is model-independent friction

Of 97 total tool errors across main thread and subagents:

| Cause | Count | Nature |
|---|---|---|
| Permission / sandbox denials | ~68 | Config. `"This command requires approval"`, `"cd + output redirection requires approval"`, `"blocked outside working directories"` |
| Windows cp1252 encoding | ~6 | Config. `UnicodeEncodeError` / `UnicodeDecodeError` |
| Genuine quality-gate failures | ~20 | **Productive** — the quality system working as designed |

### The root cause worth naming

`SKILL.md` step 1 **mandates** `export NODE_PATH="$PWD/node_modules"`. The runner's
`CLAUDE_ALLOWED_TOOLS` ([config/settings.py:164](../config/settings.py)) does not permit
`export`. The transcript shows the exact cascade:

```
09:56:48  export NODE_PATH=...        → DENIED
09:56:57  node build.js               → Error: Cannot find module 'docx'
```

Identical story for `export PYTHONIOENCODING=utf-8` (denied 06:53:20), followed by three
separate `UnicodeEncodeError` crashes.

**The skill's documented pipeline is in direct conflict with the runner's tool allowlist.**

## Subagent analysis — turn-driven, not context-driven

| Subagent | Prompt chars | Turns | Errors | Cache-read |
|---|---|---|---|---|
| Boyle's / Charles's laws | 7,991 | **113** | **19** | 7,852,784 |
| Dalton's law | 6,964 | 51 | 9 | 2,551,923 |
| Front matter + math toolkit | 8,670 | 55 | 5 | 2,488,226 |
| Gay-Lussac / Avogadro / ideal gas | 9,491 | 39 | 5 | 1,872,511 |
| Back matter (practice, cheat sheet) | 10,489 | 38 | 6 | 1,592,233 |
| KTG + molecular speeds | 13,011 | 32 | 6 | 1,264,132 |

Prompts were **small** (7–13k chars) and average context per turn was modest (40–70k).
The 17.6M total is `328 turns × ~53k avg` — **the driver is turn count, not payload size.**
Error count tracks turn count almost linearly.

### Input preprocessing was not the problem

`sources/transcript.json` (68 KB) is dated **Aug 7 00:36** — six hours before this session
started. Ingest and transcription had already been done by the earlier failed attempt and
cached. Only **one** of the six subagents even read it.

A Stage 2a/2b split (preprocess separately, then generate) would have saved nothing here,
because that preprocessing had already happened.

### The seam that would actually help

The subagents were not only drafting prose. Their file reads give them away — `_common.py`,
`figlib.py`, `run_fig.py`, `f01_three_states.py` — plus writes of figure scripts and
`PowerShell` invocations. **All six independently re-derived how figlib works and
independently hit the same permission wall.**

The productive split is **author-content vs. build-figures**: let subagents emit content
JSON and figure *specs* only, then run one deterministic `figbuild` pass.

## The `--patch` finding

`build.js` ships a `--patch` flag ([lib/build.js:29,61](../templates/study-notes.skill))
specifically so, per `LESSONS.md`, *"a revision costs a few hundred tokens, not ~28,000."*

**It was never invoked.** Root cause: `--patch` appears nowhere in `SKILL.md`'s Pipeline
block — only in `build.js --help` and a rationale list in `LESSONS.md`. A model following
`SKILL.md` literally would never learn it exists.

Instead the session wrote **16 ad-hoc Python scripts** to patch content by hand:

```
_add_crossref.py      _add_periods.py        _categorize_missing.py
_check_sentences.py   _debug_run.py          _dump_fx.py
_dump_ktg.py          _fix_slashes.py        _insert_figs.py
_insert_figs2.py      _invariants_text_check.py  _list_slashes.py
_missing_topics.py    _patch_crossref.py     _run_build.py
_run_py.py
```

This is the exact anti-pattern `SKILL.md` opens by warning against — the approach that
*"burned ~150k tokens per document and still shipped 13 layout defects."*

## The retro that never ran

`tools/retro.py` **crashes on Windows**, and the failure is completely silent:

1. [`retro.py:74`](../templates/study-notes.skill) — `for line in open(p):` with no
   `encoding=`, so Python defaults to cp1252 → `UnicodeDecodeError: byte 0x81`.
2. [`skill_retro.py:96`](../src/common/skill_retro.py) — `if result.returncode != 0:
   return {"ok": False, "candidates": []}` swallows it.
3. No `_retro-findings.txt` written; the `ACTION NEEDED` log line only fires when
   candidates exist.

**The retro has never produced a finding on this machine.** Third bug in the same
Windows-encoding family as the `doctor` docx check and the Stage 2 heartbeat.

### Findings recovered by re-running with `encoding='utf-8'`

Correction rounds: `build rejected 2`, `invariants failed 5`, `baseline failed 4`,
`figbuild failed 3`, `qa failed 2`, `verify failed 1`. No figure failed more than once.

| # | Candidate | Observed |
|---|---|---|
| 1 | **inline-slash rejections** | **32×** |
| 2 | too few practice questions | 9× |
| 3 | transcript coverage gaps | 5× |
| 4 | missing required sections | 1× |

Finding #1 is precisely what the model complained about in its own closing summary —
"10 of 171 transcript topics can't be matched verbatim because their wording requires a
literal `/`". The self-improvement loop correctly identified the run's largest friction
source, and a missing `encoding='utf-8'` deleted it.

## Web enrichment: never enabled

`ENABLE_WEB_ENRICHMENT` defaults to `"0"` ([config/settings.py:198](../config/settings.py))
and is not set in the runner, User env, or Machine env. `WebSearch`/`WebFetch` were never
granted — zero calls in the main thread or any subagent.

The model's closing line — *"Web research was not used; the transcripts were sufficient
for this chapter's scope"* — is a rationalization of a constraint, not a judgment call.

The gap is that three different states produce identical output (no file, no log line):

| State | Meaning | Today |
|---|---|---|
| Disabled | tools never granted ← **this run** | silent |
| Enabled, 0 used | model chose not to | silent |
| Enabled, N used | full audit trail | `_web-sources.txt` |

## Model choice: would Opus 5 have helped?

Verified pricing: **Opus 5 $5/$25** per MTok vs **Sonnet 5 $3/$15** — ~1.67× at list, far
closer than the historical Opus/Sonnet gap. (This run is subscription-billed, and the
5-hour window's model weighting is not documented here; the API ratio is a proxy only.)

- **Permission denials (~70% of waste):** barely helped. A stronger model might generalize
  after 1–2 denials rather than 6, but it cannot know the allowlist. Config fixes remove
  100% at zero cost.
- **Encoding errors:** no help. Environmental.
- **Gate failures:** **genuine help.** 32× inline-slash is instruction adherence, exactly
  where Opus 5 leads. Opus 5 also self-verifies without being told, cutting correction rounds.
- **Counter-effect:** Opus 5 is documented to reach for subagents *more* readily than Opus
  4.8 (the migration guidance recommends an explicit delegation cap) and to write longer
  responses and files. Window 1 died from subagent fan-out — Opus 5 would likely have made
  that spike **larger**.

**Conclusion: not the first lever.** Fix the model-independent friction, re-measure, then
reconsider. A cheaper intermediate step: `CLAUDE_EFFORT` is unset
([config/settings.py:160](../config/settings.py)), so runs use the CLI default; `xhigh` is
the documented best setting for coding/agentic work on Sonnet 5. If Opus 5 is adopted
later, a subagent cap becomes necessary — not to save window totals, but because Opus 5
spawns more of them.

## Cross-platform defects found while implementing the fixes

Audited native Windows vs Linux/WSL after the remediation above. Four real defects, all
now fixed; the run's "starting up" heartbeat was the visible symptom of the first.

### 1. Session-transcript lookup never matched on Windows (one character)

`_latest_session_transcript()` mangled the workspace path with `.replace(":", "")`,
**dropping** the drive colon. Claude Code maps the colon to `-` like every other
separator, so real directories carry a **double** dash after the drive letter:

```
real      C--Users-parallel-AppData-...-Chemistry-Ch1-Gaseous-State
computed  C-Users-parallel-AppData-...-Chemistry-Ch1-Gaseous-State
```

The lookup therefore returned `None` on **every call on native Windows**, silently
disabling all four of its callers:

| Caller | Consequence |
|---|---|
| `_heartbeat_summary()` | logged `starting up` for the entire run — no progress ever shown |
| `write_web_sources_manifest()` | web audit trail never produced |
| `write_run_diagnostics()` | (new) would never have worked |
| `verify_resolved_skill()` | **the FATAL wrong-skill check could never fire** |

That last row is the serious one: per this repo's own history that check exists because
`/study-notes` once fuzzy-resolved to a stale global skill. On Windows it has been a no-op.

The bug survived because the test helper *mirrored* the production transform instead of
pinning the real on-disk layout — helper and lookup agreed with each other and both
disagreed with the filesystem. `test_session_transcript_dir_matches_real_claude_code_layout`
now pins literal directory names copied from a live machine.

### 2. Bare `npm` raises `FileNotFoundError` on Windows

`skill_retro.py` called `subprocess.run(["npm", "install", "docx"], ...)`. On Windows npm
ships only as `npm.cmd`/`npm.ps1`; `subprocess` with `shell=False` uses `CreateProcess`,
which only auto-appends `.exe`. `main.py`'s `--doctor` already documented and fixed this
via `shutil.which()`; this call site was missed. Verified empirically.

### 3. Hardcoded `python3` ran the wrong interpreter (11 call sites)

`src/agents/tools.py` invoked the skill's Python tools as `python3`. On Windows that
resolves to whatever the PATH/Store alias points at — measured here as 3.14, while the
pipeline runs under 3.12 — so skill scripts execute under a different interpreter than the
one whose dependencies were provisioned. Now `sys.executable`, matching `skill_retro.py`
and `--doctor`. No behavior change on POSIX.

### 4. mtime-vs-wall-clock race in `apply_retro_fixes()`

"Did the session change anything?" compared `st_mtime` against `time.time()`. Different
sources, different resolutions — a fast edit could read as no-change and discard a valid
improvement (and made its test flaky ~1 run in 3). Now a before/after `(mtime_ns, size)`
fingerprint with no clock in it.

### Already correct — checked, no action

`skill_package.py` (flock/msvcrt branch), `func_tools_and_utils.py`
(`_WINDOWS_BUILTIN_COMMANDS` servicing `ls/cat/cp/mv/mkdir` in Python on Windows),
`settings.py` (`G:/` vs `/mnt/g`), and `main.py`'s npm probe were all already
platform-branched correctly.

### Open, not a code fix: `jsonschema` missing on Windows

The skill's `tools/validate.py` imports `jsonschema`, which is installed under **neither**
Windows interpreter on this machine. The live run hit exactly this
(`ModuleNotFoundError: No module named 'jsonschema'`, 06:50:37) and the model hand-worked
around it — part of why the run was expensive. Provisioning gap, not code:

```bash
py -3.12 -m pip install jsonschema
```

## Agreed remediation

Approved 2026-08-08, implementation pending.

| Area | Change |
|---|---|
| Web logging | Default `ENABLE_WEB_ENRICHMENT` to `"1"`; log the grant at invocation; always write `_web-sources.txt` across all three states with a quantified one-liner (searches / fetches / domains / chars ingested) |
| `--patch` | Add it to `SKILL.md`'s Pipeline block, plus a rule: content fixes go via `--patch` or `content.d/<part>.json`, never a script that rewrites `content.json` |
| Diagnostics | New `_run-diagnostics.txt` counting ad-hoc `*.py` writes at workspace root outside `fig_scripts/` vs `--patch` invocations; feed into `retro.py` |
| Friction | Set `NODE_PATH` + `PYTHONIOENCODING=utf-8` in `build_claude_env()`; widen `CLAUDE_ALLOWED_TOOLS` with `ls/grep/cat/wc/mkdir/rm/env/cd`; `--add-dir` the resolved skill directory |
| Retro | Fix `retro.py`'s `open(p)` to pass `encoding='utf-8'`; make `capture_retro_findings()` log a warning with stderr on non-zero exit instead of returning empty silently |

**Explicitly rejected:**

- **Per-chapter `--max-budget-usd` ceiling.** Would convert silent window exhaustion into a
  loud failure, but doesn't make runs cheaper.
- **Capping subagent fan-out.** Concurrency caps don't reduce total tokens against a window
  that caps totals — spreading the same burn over more wall time only helps a burst limit,
  which was never established as the constraint. The real target, if revisited, is
  per-subagent context size.
- **Replacing `-r` resume with artifact-based resume.** Initially recommended and withdrawn:
  the saving was overstated (~2×, not "a fraction"), and window 1 was killed by subagent
  fan-out 18 minutes in, not by resume context growth.

None of the approved changes touch `baseline.py`, `verify.py`, `qa.py`, or `invariants.py`.
Per `LESSONS.md`, any change that fails `baseline.py` is rejected regardless of what it saves.
