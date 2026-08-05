"""
skill_retro.py -- the autonomous skill-retrospective/self-improvement
machinery, shared BY IMPORT (not by copy) between
src/claude_cli_subprocess/stage2_cli.py and src/agents/stage2_graph.py.
Originally written once in stage2_cli.py (PR #5, "web-enrichment") and
then duplicated-with-drift into stage2_graph.py when Stage 2 was ported to
the agents/LangGraph architecture; moved here so there is exactly one copy
for both implementations to call, instead of two that quietly diverge over
time (the two copies had already started drifting on default values,
comments, and one dropped code path before being unified here).

Every function below is implementation-agnostic: they operate purely on
paths (a rundir containing .study-notes/run.jsonl, a skill directory, a
list of candidate dicts) and have no idea whether the chapter that produced
those findings was generated via the `claude` CLI, LangGraph, or the
direct Anthropic SDK path.

TRUTH-CHECK, NOT SELF-REPORTED SUCCESS (a rule these functions repeatedly
lean on): never trust a model's own claim that it did the right thing --
re-derive it from ground truth instead. Here that means running the skill's
own tools/retro.py against the real run log rather than asking the model
what it found, and independently re-running tools/regress.py after a fix
session rather than trusting its "done". Same principle stated in
src/claude_cli_subprocess/stage2_cli.py's module docstring, where the CLI
Stage 2 generator applies it to whether a chapter actually produced a .docx.

apply_retro_fixes() is the one exception worth calling out: it still needs
an actual `claude -p` subprocess call (via
claude_cli_subprocess.stage2_cli.run_claude_cli) to do the editing, because
applying a fix needs a general Read/Edit/Bash coding agent over a scratch
copy of the skill -- something only the `claude` CLI provides out of the
box. It imports run_claude_cli/sync_skill_package LOCALLY, inside the
function body, rather than at module level: stage2_cli.py itself imports
FROM this module (to re-export these names for its own callers/tests), so
a top-level import in the other direction would be a circular import.
Deferring the import to call time breaks the cycle with no behavior change
-- by the time apply_retro_fixes() actually runs, stage2_cli has always
already finished loading.
"""

import json
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import settings as config
from src.func_tools_and_utils import logger


def capture_retro_findings(target_dir: Path, workspace: Path, resolved_skill_dir: Path = None) -> dict:
    """Run the skill's own tools/retro.py directly (--json --rundir
    <workspace>/.study-notes) rather than relying on the model to have
    invoked it, or scraping its text output from a transcript -- TRUTH-CHECK,
    NOT SELF-REPORTED SUCCESS. Writes target_dir/config.RETRO_FINDINGS when
    retro.py reports one or more candidate skill improvements, so they
    survive past the unattended run instead of evaporating.

    THIS EXISTS BECAUSE: SKILL.md's own retrospective workflow says to
    "present this list, get approval" before touching the skill -- but
    both implementations' own prompts separately instruct "fully unattended
    -- do not pause for confirmation." Confirmed live (claude_cli_subprocess
    path): the model correctly resolved that conflict by NOT pausing, but
    that meant retro.py's real findings (candidate skill improvements,
    backed by real repeated-failure counts) never reached a human at all,
    just a one-line dismissal buried in the model's own final summary.
    Neither instruction is wrong -- unattended runs genuinely can't hold an
    approval conversation -- so the fix is capturing the findings durably
    for a human to review LATER, not making the run stop and wait.

    workspace is wherever THIS chapter's .study-notes/run.jsonl actually
    lives -- a local scratch workspace for both implementations (see
    claude_cli_subprocess.stage2_cli._chapter_workspace and
    src.agents.tools._skill_run_env), not target_dir itself. resolved_skill_dir
    lets a caller point at a skill install location other than the default
    config.CLAUDE_SKILL_INSTALL_DIR (agents' own extraction under
    .skill-runtime/, via src.agents.prompts.ensure_skill_extracted(), is not
    that default location).

    Returns {"ok": bool, "candidates": list, "log_records": int}. ok=False
    (candidates=[]) on any failure -- retro.py exits non-zero when no run
    log exists yet (e.g. DEV_TOKEN_SAVER_MODE, which never touches the real
    skill scripts) -- best-effort, never something a chapter's
    success/failure depends on.
    """
    try:
        rundir = workspace / ".study-notes"
        base_skill_dir = resolved_skill_dir if resolved_skill_dir else config.CLAUDE_SKILL_INSTALL_DIR
        retro_py = base_skill_dir / "tools" / "retro.py"
        result = subprocess.run(
            [sys.executable, str(retro_py), "--rundir", str(rundir), "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return {"ok": False, "candidates": [], "log_records": 0}
        report = json.loads(result.stdout)
        candidates = report.get("candidates") or []
        log_records = report.get("log_records", 0)
        if candidates:
            lines = [
                f"Skill-improvement candidates from tools/retro.py ({log_records} log records this run).",
                "Captured automatically -- NOT applied. This run was unattended, so per",
                "this run's own prompt it correctly did not pause to ask; per SKILL.md's",
                "retrospective workflow, nothing here should be applied to the shared",
                "skill without a human reviewing it first (run tools/regress.py before accepting",
                "any change, then add it to LESSONS.md -- see SKILL.md's own \"After delivery\" section).",
                "",
            ]
            for i, c in enumerate(candidates, 1):
                lines.append(f"{i}. {c.get('issue')} (observed {c.get('observed')}x)")
                lines.append(f"   {c.get('change')}")
                lines.append("")
            (target_dir / config.RETRO_FINDINGS).write_text("\n".join(lines), encoding="utf-8")
        return {"ok": True, "candidates": candidates, "log_records": log_records}
    except Exception:
        return {"ok": False, "candidates": [], "log_records": 0}


def repackage_skill_dir(skill_dir: Path, out_skill_file: Path) -> None:
    """Zip skill_dir's contents back into out_skill_file (overwriting it),
    the inverse of sync_skill_package()'s extractall(). Used only by
    apply_retro_fixes(), after regress.py has independently confirmed a
    self-improvement session's edits are safe to keep."""
    with zipfile.ZipFile(out_skill_file, "w") as zf:
        for path in sorted(skill_dir.rglob("*")):
            rel_parts = path.relative_to(skill_dir).parts
            if any(p in {".git", "__pycache__", "node_modules"} for p in rel_parts):
                continue
            arcname = path.relative_to(skill_dir).as_posix()
            if path.is_dir():
                zf.writestr(arcname + "/", b"")
            else:
                zf.write(path, arcname, compress_type=zipfile.ZIP_DEFLATED)


def apply_retro_fixes(candidates: list, source_workspace: Path) -> dict:
    """Autonomously apply tools/retro.py's candidate skill improvements --
    opt-in (config.ENABLE_AUTO_SKILL_IMPROVEMENT), a SEPARATE claude -p
    session from chapter generation itself, scoped ONLY to editing a scratch
    copy of the skill and validating via tools/regress.py.

    Explicitly authorized to skip human approval, unlike SKILL.md's own
    interactive-session "present this list, get approval" retrospective
    text (that text still describes the right workflow for a human doing
    skill development directly -- this function exists for the unattended
    pipeline, which has no one to ask): git tracks
    templates/study-notes.skill, so a bad change is always recoverable.
    tools/regress.py is still the accept/reject gate, and it is
    INDEPENDENTLY RE-RUN by this function after the session claims success,
    never just trusted -- TRUTH-CHECK, NOT SELF-REPORTED SUCCESS applies
    here too, arguably more so: this is the one code path in the whole
    pipeline that can rewrite the pipeline's own behavior for every future
    chapter, regardless of which implementation generated the findings.

    source_workspace is the chapter workspace whose .study-notes/run.jsonl
    produced `candidates` -- granted read access (via --add-dir) so the
    fixing session can look at the RAW logged instances behind each
    aggregated candidate. issue/count/suggested-change alone isn't enough
    to tell a genuine defect from a false positive: the SLASH_OK fix this
    function is modeled on needed exactly that raw evidence (2 of 3 flagged
    instances turned out to be false positives, not the genuine "make the
    frac rule more prominent" fix the aggregated candidate alone suggested).

    Returns {"applied": bool, "reason": str}.
    """
    # Deferred import -- see this module's docstring for why (breaks a
    # circular import with claude_cli_subprocess.stage2_cli, which imports
    # THIS module to re-export these functions for its own callers/tests).
    from src.claude_cli_subprocess.stage2_cli import run_claude_cli, sync_skill_package

    workspace = config.AUTO_SKILL_IMPROVEMENT_WORKSPACE
    workspace.mkdir(parents=True, exist_ok=True)
    skill_copy = workspace / "skill_copy"

    try:
        source_skill = sorted(
            config.LOCAL_RUNTIME_ROOT.glob(config.CLAUDE_SKILL_SOURCE_GLOB),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        if not source_skill:
            return {"applied": False, "reason": "no templates/*.skill source found"}
        # Always start from the CURRENT accepted skill, discarding any stale
        # copy from a previous attempt -- this function must never build on
        # top of a change that was itself never validated.
        if skill_copy.exists():
            shutil.rmtree(skill_copy)
        sync_skill_package(skill_copy)

        candidate_lines = "\n".join(
            f"{i}. {c.get('issue')} (observed {c.get('observed')}x): {c.get('change')}"
            for i, c in enumerate(candidates, 1)
        )
        prompt = f"""Fully unattended -- do not pause for confirmation and do not ask any questions.

tools/retro.py found these candidate skill improvements from a real chapter generation run:

{candidate_lines}

Skill copy to edit (a scratch copy -- never edit anything outside this path):
  {skill_copy}

Raw evidence for these candidates (the actual logged tool events, not just the
aggregated summary above) is at:
  {source_workspace / ".study-notes" / "run.jsonl"}

For EACH candidate: read the raw evidence behind it before deciding on a fix -- an
aggregated "observed Nx" count can hide a mix of genuine defects and false positives
that need different fixes (or no fix at all). Make the smallest change that addresses
what the evidence actually shows. Skip a candidate rather than guess if the evidence
doesn't clearly support one fix.

Before finishing, from cwd {workspace} (node_modules must be installed here first --
`npm install --silent --no-audit --no-fund docx@9.7.1` if not already present), run:
  python3 {skill_copy}/tools/regress.py
It must exit 0 (PASS). If it doesn't, revert whichever change caused the failure and
either try a smaller fix or skip that candidate -- a change that fails regress.py must
never be left in place, regardless of how reasonable it seemed.

For every change you keep, add a one-line entry to {skill_copy}/LESSONS.md following
its existing table format (see the file for the convention).

Report at the end: which candidates you addressed, which you skipped and why, and
confirm regress.py's final pass/fail."""

        start_time = time.time()
        logger.info(f"Invoking claude CLI for autonomous skill improvement "
                    f"({len(candidates)} candidate(s), cwd={workspace}) ...")
        result = run_claude_cli(
            target_dir=source_workspace / ".study-notes",
            prompt=prompt,
            workspace_override=workspace,
            timeout_seconds=config.AUTO_SKILL_IMPROVEMENT_TIMEOUT_SECONDS
        )
        if not result["ok"]:
            return {"applied": False, "reason": result.get("error", "cli call failed")}

        # Check if the session actually made any changes to the skill dir
        modified = any(p.stat().st_mtime > start_time for p in skill_copy.rglob("*") if p.is_file())
        if not modified:
            return {"applied": False, "reason": "session exited without modifying the skill"}

        # TRUTH-CHECK: re-run regress.py OURSELVES against skill_copy,
        # regardless of what the session's own JSON envelope or final text
        # claims -- see this function's docstring.
        try:
            # Ensure docx is installed in the workspace, as regress.py relies on it and the fixing session might have skipped or failed it
            if not (workspace / "node_modules" / "docx").exists():
                subprocess.run(["npm", "install", "docx"], cwd=str(workspace), capture_output=True)

            regress = subprocess.run(
                [sys.executable, str(skill_copy / "tools" / "regress.py")],
                cwd=str(workspace), capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            return {"applied": False, "reason": f"regress.py could not be run: {e}"}

        if regress.returncode != 0:
            logger.warning(f"Autonomous skill improvement attempt did NOT pass "
                            f"regress.py (exit {regress.returncode}); discarding, "
                            f"templates/study-notes.skill unchanged.")
            return {"applied": False, "reason": "regress.py failed after the session's edits",
                    "regress_output": regress.stdout[-2000:]}

        repackage_skill_dir(skill_copy, source_skill[0])
        try:
            sync_skill_package()
            sync_skill_package(config.CLAUDE_SKILL_GLOBAL_INSTALL_DIR)
            logger.info(f"Autonomous skill improvement applied and verified via regress.py "
                        f"(claude CLI returncode={result['returncode']}); "
                        f"templates/study-notes.skill updated and re-synced.")
            return {"applied": True, "reason": "regress.py passed", "session_result": result["raw_stdout"][-2000:]}
        except Exception as e:
            logger.warning(f"Skill was updated in templates/ but sync failed: {e}")
            return {"applied": True, "reason": f"applied but sync incomplete, check both install dirs manually: {e}", "session_result": result["raw_stdout"][-2000:]}
    except Exception as e:
        logger.exception(f"Unexpected error during autonomous skill improvement: {e}")
        return {"applied": False, "reason": f"unexpected error: {e}"}
