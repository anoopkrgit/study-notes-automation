import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import json
import subprocess
import time
import zipfile
import pytest
from unittest.mock import patch, MagicMock, call

import settings as config
from src.claude_cli_subprocess.common import build_claude_env
from src.func_tools_and_utils import EXIT_OK, EXIT_RATE_LIMITED, EXIT_FATAL

from src.claude_cli_subprocess.stage2_cli import (
    run_stage2_chapter, run_claude_cli, classify_cli_result, sync_skill_package,
    write_web_sources_manifest, verify_resolved_skill, capture_retro_findings,
    apply_retro_fixes, _repackage_skill_dir, write_run_diagnostics
)


def _mock_popen(returncode=0, stdout="", stderr=""):
    """Build a MagicMock standing in for subprocess.Popen(...)'s return
    value, matching what run_claude_cli()'s Popen + communicate() polling
    loop actually reads: proc.communicate(timeout=...) -> (stdout, stderr),
    and proc.returncode once communicate() has returned (real subprocess.Popen
    only populates .returncode after the process has actually exited, same
    as here -- communicate() returning without raising is what the code
    treats as "the process is done")."""
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (stdout, stderr)
    mock_proc.returncode = returncode
    return mock_proc

@pytest.fixture
def mock_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CLAUDE_WORKSPACE_ROOT", tmp_path / "workspace")
    monkeypatch.setattr(config, "CHAPTER_PROGRESS_DIR", tmp_path / "progress")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "RETRY_EPOCH_FILE", tmp_path / "state" / "retry-epoch.txt")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path

def test_run_stage2_chapter_mock_mode_makes_zero_subprocess_calls(mock_dirs):
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)
    with patch('subprocess.Popen') as mock_popen:
        result = run_stage2_chapter(target_dir=target, live_mode=False)
        assert result == EXIT_OK
        mock_popen.assert_not_called()

def test_run_stage2_chapter_rejects_nonexistent_target_dir(mock_dirs):
    """A bad/typo'd --target-dir must fail fast (EXIT_FATAL), not silently
    report success as if it were just an empty-but-real chapter folder."""
    with patch('subprocess.Popen') as mock_popen:
        result = run_stage2_chapter(target_dir=mock_dirs / "does-not-exist", live_mode=False)
        assert result == EXIT_FATAL
        mock_popen.assert_not_called()

def test_run_stage2_chapter_dev_token_saver_uses_dummy_prompt_and_flags(monkeypatch, mock_dirs):
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", True)
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"ok": true, "result": "done"}', stderr="")

        # We need expected_docx to exist to get EXIT_OK and not EXIT_FATAL
        (target / "chapter1.docx").touch()

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            with patch('src.claude_cli_subprocess.stage2_cli.capture_retro_findings',
                       return_value={"ok": False, "candidates": [], "log_records": 0}):
                run_stage2_chapter(target_dir=target, live_mode=True)

        mock_popen.assert_called_once()
        args, kwargs = mock_popen.call_args
        cmd = args[0]
        prompt = cmd[cmd.index("-p") + 1]

        # Verify dummy prompt doesn't contain real target dir or the literal
        # "/study-notes" substring (confirmed live: including that string
        # anywhere risked the CLI parsing it as a real skill invocation).
        assert "cost-safe smoke-test" in prompt
        assert str(target) not in prompt
        assert "/study-notes" not in prompt

        # Verify NO tools are granted in dev mode -- confirmed live that
        # granting the full production tool list let a single dev-mode call
        # spend $0.145 (reported input/output_tokens both 0) before hitting
        # --max-budget-usd, so the budget cap alone isn't sufficient.
        assert cmd[cmd.index("--allowedTools") + 1] == ""

        # Verify budget and effort flags (belt-and-suspenders alongside
        # the empty --allowedTools above)
        assert "--max-budget-usd" in cmd
        assert cmd[cmd.index("--max-budget-usd") + 1] == str(config.DEV_TOKEN_SAVER_MAX_BUDGET_USD)
        assert "--effort" in cmd
        assert cmd[cmd.index("--effort") + 1] == config.DEV_TOKEN_SAVER_EFFORT

        # Verify env scrubbed
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]

def test_success_marker_requires_docx_to_actually_exist(mock_dirs):
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"ok": true, "result": "done"}', stderr="")

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            result = run_stage2_chapter(target_dir=target, live_mode=True)

        assert result == EXIT_FATAL
        assert (target / config.FAILMARK).exists()
        assert not (target / config.MARKER).exists()

def test_success_marker_written_when_docx_exists(mock_dirs):
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"ok": true, "result": "done"}', stderr="")

        (target / "chapter1.docx").touch()

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            result = run_stage2_chapter(target_dir=target, live_mode=True)

        assert result == EXIT_OK
        assert (target / config.MARKER).exists()

def test_real_run_gets_full_tool_list_not_empty(mock_dirs):
    """Companion to the dev-token-saver test: a REAL (non-dev-mode) run
    must still get the full config.CLAUDE_ALLOWED_TOOLS list -- guards
    against DEV_TOKEN_SAVER_MODE's empty --allowedTools fix accidentally
    also starving a real production run of tool access."""
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"result": "done"}', stderr="")

        (target / "chapter1.docx").touch()

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            with patch('src.claude_cli_subprocess.stage2_cli.capture_retro_findings',
                       return_value={"ok": False, "candidates": [], "log_records": 0}):
                run_stage2_chapter(target_dir=target, live_mode=True)

        args, kwargs = mock_popen.call_args
        cmd = args[0]
        granted = cmd[cmd.index("--allowedTools") + 1]
        # Every tool in the configured list must be granted. NOT an equality
        # check: config.ENABLE_WEB_ENRICHMENT now defaults on, so a real run
        # legitimately APPENDS WebSearch/WebFetch(domain:...) to this list --
        # asserted separately by the web-enrichment tests below. This test's job
        # is only "a real run isn't starved of tools", which is a superset check.
        granted_tokens = set(granted.split(","))
        for tool in config.CLAUDE_ALLOWED_TOOLS.split(","):
            assert tool in granted_tokens, f"real run lost tool grant: {tool}"
        assert "--max-budget-usd" not in cmd

def test_nonzero_unparseable_returncode_is_retryable(mock_dirs):
    """stdout is NOT valid JSON (unparseable envelope), so run_claude_cli()
    must fall back to the combined stdout+stderr text -- and that fallback
    text ("launch failure: something") must genuinely flow through to
    classify_cli_result() and match its "launch failure:" pattern, not
    just happen to pass because an unset MagicMock().stderr is truthy."""
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=1, stdout="launch failure: something", stderr="")

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            result = run_stage2_chapter(target_dir=target, live_mode=True)

        assert result == EXIT_RATE_LIMITED
        assert config.RETRY_EPOCH_FILE.exists()

def test_timeout_is_retryable(monkeypatch, mock_dirs):
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    # Shrink the timeout/heartbeat knobs so the poll loop's wall-clock
    # deadline (real time.monotonic(), unaffected by mocking) is reached in
    # a fraction of a second rather than actually waiting out the real
    # CLAUDE_CLI_TIMEOUT_SECONDS default.
    monkeypatch.setattr(config, "CLAUDE_CLI_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(config, "CLAUDE_CLI_HEARTBEAT_SECONDS", 0.01)

    with patch('subprocess.Popen') as mock_popen:
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=0.01)
        mock_popen.return_value = mock_proc

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            result = run_stage2_chapter(target_dir=target, live_mode=True)

        assert result == EXIT_RATE_LIMITED
        assert config.RETRY_EPOCH_FILE.exists()
        # The timeout path must actually try to kill the still-running
        # process before giving up.
        mock_proc.kill.assert_called_once()

def test_usage_limit_error_is_retryable(mock_dirs):
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(
            returncode=1, stdout='{"is_error": true, "result": "rate limit reached | 123"}', stderr="")

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            result = run_stage2_chapter(target_dir=target, live_mode=True)

        assert result == EXIT_RATE_LIMITED
        assert config.RETRY_EPOCH_FILE.exists()

def test_unrecognized_error_is_fatal_not_retryable(mock_dirs):
    """Judgment call: an unrecognized/unhandled error shape defaults to FATAL,
    not transient, because Stage 2 is high-stakes."""
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(
            returncode=1, stdout='{"is_error": true, "result": "Some entirely new error text"}', stderr="")

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            result = run_stage2_chapter(target_dir=target, live_mode=True)

        assert result == EXIT_FATAL
        assert (target / config.FAILMARK).exists()

def test_unexpected_exception_degrades_to_fatal_not_a_crash(mock_dirs):
    """Any failure outside run_claude_cli()'s own try/except (e.g. a
    corrupted skill package, a permissions error) must produce a clean
    FAILMARK + EXIT_FATAL, matching the same top-level resilience pattern
    direct_api.stage2_api.run_generate() and
    agents.stage2_graph.run_stage2_chapter() both use -- never an uncaught
    traceback that kills the whole nightly process."""
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package',
               side_effect=RuntimeError("corrupted skill zip")):
        result = run_stage2_chapter(target_dir=target, live_mode=True)

    assert result == EXIT_FATAL
    assert (target / config.FAILMARK).exists()
    assert "corrupted skill zip" in (target / config.FAILMARK).read_text()

def test_heartbeat_logged_while_call_is_still_running(monkeypatch, mock_dirs, caplog):
    """The whole point of switching run_claude_cli() from a single blocking
    subprocess.run() to a Popen + polling communicate() loop: a "still
    working" line must actually get logged while the call is in progress,
    not just silence until it finishes."""
    import logging
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(config, "CLAUDE_CLI_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(config, "CLAUDE_CLI_HEARTBEAT_SECONDS", 0.05)

    with patch('subprocess.Popen') as mock_popen:
        mock_proc = MagicMock()
        # Times out a couple of times (simulating "still running"), then
        # succeeds on the third poll.
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="claude", timeout=0.05),
            subprocess.TimeoutExpired(cmd="claude", timeout=0.05),
            ('{"ok": true, "result": "done"}', ""),
        ]
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        (target / "chapter1.docx").touch()

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            with caplog.at_level(logging.INFO):
                result = run_stage2_chapter(target_dir=target, live_mode=True)

        assert result == EXIT_OK
        assert any("still working" in r.message for r in caplog.records)

def test_sync_skill_package_only_reextracts_when_source_changed(monkeypatch, tmp_path):
    skill_dir = tmp_path / "skill_source"
    skill_dir.mkdir()
    skill_zip = skill_dir / "test.skill"

    with zipfile.ZipFile(skill_zip, "w") as zf:
        zf.writestr("test.txt", "v1")

    install_dir = tmp_path / "install"
    monkeypatch.setattr(config, "CLAUDE_SKILL_SOURCE_GLOB", "skill_source/*.skill")
    monkeypatch.setattr(config, "LOCAL_RUNTIME_ROOT", tmp_path) # glob uses this
    monkeypatch.setattr(config, "CLAUDE_SKILL_INSTALL_DIR", install_dir)

    # First sync
    sync_skill_package()
    assert (install_dir / "test.txt").read_text() == "v1"
    marker = install_dir / ".synced_from"
    assert marker.exists()

    # Change content of the installed file manually
    (install_dir / "test.txt").write_text("modified")

    # Second sync (unchanged zip mtime) -> should skip extract
    sync_skill_package()
    assert (install_dir / "test.txt").read_text() == "modified" # wasn't overwritten

    # Modify zip file to bump mtime
    time.sleep(0.1)
    with zipfile.ZipFile(skill_zip, "w") as zf:
        zf.writestr("test.txt", "v2")

    # Third sync -> should re-extract
    sync_skill_package()
    assert (install_dir / "test.txt").read_text() == "v2"

def test_sync_skill_package_explicit_install_dir_is_independent_of_project_local(monkeypatch, tmp_path):
    """sync_skill_package(install_dir=...) must sync to exactly that
    directory and leave config.CLAUDE_SKILL_INSTALL_DIR (the project-local
    default) completely untouched -- run_stage2_chapter() relies on the two
    calls being independent, one per target, not accidentally aliased."""
    skill_dir = tmp_path / "skill_source"
    skill_dir.mkdir()
    skill_zip = skill_dir / "test.skill"
    with zipfile.ZipFile(skill_zip, "w") as zf:
        zf.writestr("test.txt", "v1")

    project_local = tmp_path / "project_local_install"
    global_install = tmp_path / "global_install"
    monkeypatch.setattr(config, "CLAUDE_SKILL_SOURCE_GLOB", "skill_source/*.skill")
    monkeypatch.setattr(config, "LOCAL_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(config, "CLAUDE_SKILL_INSTALL_DIR", project_local)

    sync_skill_package(global_install)

    assert (global_install / "test.txt").read_text() == "v1"
    assert not project_local.exists()  # the default target was never touched

def test_run_stage2_chapter_syncs_skill_to_both_project_local_and_global(mock_dirs):
    """run_stage2_chapter() must sync the skill to BOTH locations every
    live run -- project-local (unchanged default call) and global
    (config.CLAUDE_SKILL_GLOBAL_INSTALL_DIR) -- so neither can silently go
    stale relative to templates/study-notes.skill. This is the fix for the
    live bug where /study-notes fuzzy-resolved to a stale global skill
    because only the project-local copy was being kept fresh."""
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"ok": true, "result": "done"}', stderr="")
        (target / "chapter1.docx").touch()

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package') as mock_sync:
            run_stage2_chapter(target_dir=target, live_mode=True)

        assert mock_sync.call_count == 2
        mock_sync.assert_has_calls([call(), call(config.CLAUDE_SKILL_GLOBAL_INSTALL_DIR)])


# ---------------------------------------------------------------------------
# Bounded web enrichment (docs/web-enrichment-plan.md)
# ---------------------------------------------------------------------------

def _set_fake_home(monkeypatch, home_dir: Path) -> None:
    """Point Path.home() at a temp dir on BOTH platforms.

    Path.home() reads USERPROFILE on Windows and HOME on POSIX. These tests
    originally set only HOME, which silently no-ops on Windows: Path.home()
    kept returning the REAL home, _latest_session_transcript() found no
    transcript there, and every transcript-reading test fell through the
    "returns None" branch and asserted nothing. They passed on POSIX and
    failed on Windows for a reason that had nothing to do with the code
    under test."""
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))


def _write_fake_transcript(home_dir: Path, workspace: Path, content_blocks):
    """Build a fake Claude Code session transcript JSONL at the same path
    write_web_sources_manifest() (and _heartbeat_summary()) derive from
    `workspace` -- one line, one message, whose content is exactly the
    given list of blocks (tool_use / text).

    The folder-name mangling must match Claude Code's REAL on-disk convention
    (every separator AND the drive colon -> "-"), which is pinned independently
    by test_session_transcript_dir_matches_real_claude_code_layout below. Do
    not "simplify" this to mirror whatever production currently does -- mirroring
    is how the missing-colon bug stayed invisible: the helper and the lookup
    agreed with each other and both disagreed with the filesystem."""
    resolved = str(workspace.resolve().absolute())
    mangled = resolved.replace("\\", "-").replace("/", "-").replace(":", "-")
    project_dir = home_dir / ".claude" / "projects" / mangled
    project_dir.mkdir(parents=True, exist_ok=True)
    transcript = project_dir / "session.jsonl"
    transcript.write_text(json.dumps({"message": {"content": content_blocks}}) + "\n", encoding="utf-8")
    return transcript


def test_write_web_sources_manifest_built_from_transcript_not_self_report(monkeypatch, tmp_path):
    """_web-sources.txt must reflect actual WebSearch/WebFetch tool_use
    blocks recorded in Claude Code's own session transcript -- the same
    'truth-check, not self-report' pattern stage2_cli.py already uses for
    the .docx success marker, not something the model merely claims it did
    in its text reply."""
    home_dir = tmp_path / "home"
    _set_fake_home(monkeypatch, home_dir)

    target = tmp_path / "chapter1"
    target.mkdir()
    workspace = tmp_path / "workspace" / "chapter1"
    workspace.mkdir(parents=True)

    _write_fake_transcript(home_dir, workspace, [
        {"type": "tool_use", "name": "WebSearch",
         "input": {"query": "refraction real world examples",
                   "allowed_domains": ["hyperphysics.phy-astr.gsu.edu"]}},
        {"type": "tool_use", "name": "WebFetch",
         "input": {"url": "https://hyperphysics.phy-astr.gsu.edu/hbase/geoopt/refr.html"}},
        {"type": "text", "text": "some unrelated assistant text, not a tool call"},
    ])

    write_web_sources_manifest(target, workspace)

    manifest = target / config.WEB_SOURCES
    assert manifest.exists()
    content = manifest.read_text()
    assert "refraction real world examples" in content
    assert "hyperphysics.phy-astr.gsu.edu/hbase/geoopt/refr.html" in content

def test_write_web_sources_manifest_records_enabled_but_unused(monkeypatch, tmp_path):
    """Tools granted but never used is its OWN recorded outcome, distinct from
    "tools were never granted". These two used to be indistinguishable (both
    wrote nothing at all), which is how a live run's "web research was not
    used -- the transcripts were sufficient" was believed when the truth was
    that WebSearch/WebFetch had never been granted."""
    home_dir = tmp_path / "home"
    _set_fake_home(monkeypatch, home_dir)

    target = tmp_path / "chapter1"
    target.mkdir()
    workspace = tmp_path / "workspace" / "chapter1"
    workspace.mkdir(parents=True)

    _write_fake_transcript(home_dir, workspace, [{"type": "text", "text": "no web tools used this run"}])

    write_web_sources_manifest(target, workspace, enabled=True)

    manifest = target / config.WEB_SOURCES
    assert manifest.exists(), "enabled-but-unused must still be recorded, not silent"
    content = manifest.read_text(encoding="utf-8")
    assert "Web enrichment: ON" in content
    assert "0 search(es)" in content
    assert "0 fetch(es)" in content

def test_write_web_sources_manifest_records_disabled(monkeypatch, tmp_path):
    """The third state: enrichment off, so the tools were never granted at all.
    Must say so explicitly rather than looking identical to "granted but
    unused"."""
    home_dir = tmp_path / "home"
    _set_fake_home(monkeypatch, home_dir)

    target = tmp_path / "chapter1"
    target.mkdir()
    workspace = tmp_path / "workspace" / "chapter1"
    workspace.mkdir(parents=True)
    _write_fake_transcript(home_dir, workspace, [{"type": "text", "text": "nothing"}])

    write_web_sources_manifest(target, workspace, enabled=False)

    content = (target / config.WEB_SOURCES).read_text(encoding="utf-8")
    assert "Web enrichment: OFF" in content
    assert "were NOT granted" in content

def test_write_web_sources_manifest_never_raises_when_transcript_missing(monkeypatch, tmp_path):
    """Best-effort: a missing transcript must never raise or block the
    pipeline, same as _heartbeat_summary()'s own fallback. It now still writes
    a manifest (silence is what caused the original misdiagnosis), but flags
    the counts as non-authoritative rather than reporting a confident zero."""
    home_dir = tmp_path / "home"
    _set_fake_home(monkeypatch, home_dir)
    target = tmp_path / "chapter1"
    target.mkdir()
    write_web_sources_manifest(target, tmp_path / "workspace" / "no-such-chapter", enabled=True)
    content = (target / config.WEB_SOURCES).read_text(encoding="utf-8")
    assert "not authoritative" in content

def test_web_enrichment_opt_out_grants_no_tools_no_env_var(monkeypatch, mock_dirs):
    """The OPT-OUT path. config.ENABLE_WEB_ENRICHMENT now defaults to True, but
    an operator who explicitly sets it to 0 must get no WebSearch/WebFetch grant
    and no CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION -- i.e. the kill switch
    still genuinely kills it."""
    monkeypatch.setattr(config, "ENABLE_WEB_ENRICHMENT", False)
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"ok": true, "result": "done"}', stderr="")
        (target / "chapter1.docx").touch()

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            with patch('src.claude_cli_subprocess.stage2_cli.capture_retro_findings',
                       return_value={"ok": False, "candidates": [], "log_records": 0}):
                run_stage2_chapter(target_dir=target, live_mode=True)

        args, kwargs = mock_popen.call_args
        allowed_tools = args[0][args[0].index("--allowedTools") + 1]
        assert "WebSearch" not in allowed_tools
        assert "CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION" not in kwargs["env"]

def test_session_transcript_dir_matches_real_claude_code_layout(monkeypatch, tmp_path):
    """GROUND TRUTH, not a mirror of our own transform.

    Both strings below were copied from real directories under
    ~/.claude/projects on a live machine. Claude Code maps every path
    separator AND the drive colon to "-", so a Windows path yields a DOUBLE
    dash after the drive letter ("C--Users-..."). Dropping the colon instead
    ("C-Users-...") makes _latest_session_transcript() return None on every
    call, which silently disables the heartbeat, the web-sources manifest, the
    run diagnostics, and the FATAL wrong-skill check -- all without an error.

    This test is deliberately literal: if it is ever "fixed" by recomputing the
    expectation with the same code under test, it stops testing anything."""
    from src.claude_cli_subprocess.stage2_cli import _latest_session_transcript

    home = tmp_path / "home"
    _set_fake_home(monkeypatch, home)

    cases = [
        (r"C:\Users\parallel\AppData\Roaming\Python\Python312\site-packages\state\workspace\Chemistry-Ch1-Gaseous-State",
         "C--Users-parallel-AppData-Roaming-Python-Python312-site-packages-state-workspace-Chemistry-Ch1-Gaseous-State"),
        (r"C:\06-PROJECTS\trial\study-notes-automation-redesigned",
         "C--06-PROJECTS-trial-study-notes-automation-redesigned"),
        ("/mnt/c/06-PROJECTS/trial/study-notes-automation-redesigned",
         "-mnt-c-06-PROJECTS-trial-study-notes-automation-redesigned"),
    ]

    for raw_path, expected_dir in cases:
        mangled = (raw_path.replace("\\", "-").replace("/", "-").replace(":", "-"))
        assert mangled == expected_dir, (
            f"path mangling drifted from Claude Code's real layout for {raw_path}")

        # And prove the lookup actually finds a transcript placed at that name.
        project_dir = home / ".claude" / "projects" / expected_dir
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "session.jsonl").write_text("{}\n", encoding="utf-8")

    # Round-trip one real workspace through the production lookup end-to-end.
    workspace = tmp_path / "workspace" / "chapter1"
    workspace.mkdir(parents=True)
    _write_fake_transcript(home, workspace, [{"type": "text", "text": "hi"}])
    assert _latest_session_transcript(workspace) is not None, \
        "lookup must find a transcript written at Claude Code's real directory name"


def test_real_run_sets_node_path_and_python_encoding_in_subprocess_env(mock_dirs):
    """SKILL.md step 1 mandates `export NODE_PATH="$PWD/node_modules"` so
    build.js can resolve `docx`, and the skill's Python tools need utf-8 stdio
    on Windows. config.CLAUDE_ALLOWED_TOOLS does not grant bare `export`, so on
    a real run the model tried it, was denied, and then failed with "Cannot
    find module 'docx'". Both are now set process-side so nothing has to be
    exported at all."""
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"result": "done"}', stderr="")
        (target / "chapter1.docx").touch()

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            with patch('src.claude_cli_subprocess.stage2_cli.capture_retro_findings',
                       return_value={"ok": False, "candidates": [], "log_records": 0}):
                run_stage2_chapter(target_dir=target, live_mode=True)

        args, kwargs = mock_popen.call_args
        env, cwd = kwargs["env"], kwargs["cwd"]
        assert env["PYTHONIOENCODING"] == "utf-8"
        # NODE_PATH must point at THIS chapter's workspace, since node_modules is
        # installed per-workspace rather than globally.
        assert env["NODE_PATH"] == str(Path(cwd) / "node_modules")
        assert "ANTHROPIC_API_KEY" not in env, "subscription billing must not see the API key"


def test_real_run_adds_skill_dirs_to_allowed_directories(monkeypatch, mock_dirs, tmp_path):
    """The model reads $S/lib/figlib.py, $S/tools/*.py etc. as it works. Without
    --add-dir for the skill install locations those reads are refused with "may
    only list files in the allowed working directories" -- each denial a wasted
    turn in a session with nobody present to approve it."""
    local_skill = tmp_path / "local_skill"
    local_skill.mkdir()
    global_skill = tmp_path / "global_skill"
    global_skill.mkdir()
    monkeypatch.setattr(config, "CLAUDE_SKILL_INSTALL_DIR", local_skill)
    monkeypatch.setattr(config, "CLAUDE_SKILL_GLOBAL_INSTALL_DIR", global_skill)

    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"result": "done"}', stderr="")
        (target / "chapter1.docx").touch()

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            with patch('src.claude_cli_subprocess.stage2_cli.capture_retro_findings',
                       return_value={"ok": False, "candidates": [], "log_records": 0}):
                run_stage2_chapter(target_dir=target, live_mode=True)

        cmd = mock_popen.call_args[0][0]
        added = {cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--add-dir"}
        assert str(target) in added, "chapter folder must stay granted"
        assert str(local_skill) in added
        assert str(global_skill) in added


def test_write_run_diagnostics_flags_adhoc_scripts_when_patch_unused(monkeypatch, tmp_path):
    """The ad-hoc-scratch-script pattern must become visible in an artifact.
    A real run wrote 16 throwaway *.py files to hand-patch content.json and
    used build.js --patch zero times -- the single most expensive habit
    measured, and it left no trace in any pipeline output."""
    home_dir = tmp_path / "home"
    _set_fake_home(monkeypatch, home_dir)

    target = tmp_path / "chapter1"
    target.mkdir()
    workspace = tmp_path / "workspace" / "chapter1"
    workspace.mkdir(parents=True)

    _write_fake_transcript(home_dir, workspace, [
        {"type": "tool_use", "name": "Write",
         "input": {"file_path": str(workspace / "_fix_slashes.py")}},
        {"type": "tool_use", "name": "Write",
         "input": {"file_path": str(workspace / "_insert_figs.py")}},
        # Figure scripts are the skill's OWN sanctioned output -- never counted.
        {"type": "tool_use", "name": "Write",
         "input": {"file_path": str(workspace / "fig_scripts" / "f01_states.py")}},
        # Non-Python writes are irrelevant to this signal.
        {"type": "tool_use", "name": "Write",
         "input": {"file_path": str(workspace / "content.json")}},
    ])

    result = write_run_diagnostics(target, workspace)

    assert result["adhoc_scripts"] == ["_fix_slashes.py", "_insert_figs.py"]
    assert result["patch_invocations"] == 0
    content = (target / config.RUN_DIAGNOSTICS).read_text(encoding="utf-8")
    assert "_fix_slashes.py" in content
    assert "f01_states.py" not in content, "fig_scripts/ is sanctioned, must not be flagged"
    assert "content.json" not in content


def test_write_run_diagnostics_counts_patch_invocations(monkeypatch, tmp_path):
    """A run that used the cheap path has nothing to answer for -- the counter
    must record --patch usage so the two cases stay distinguishable."""
    home_dir = tmp_path / "home"
    _set_fake_home(monkeypatch, home_dir)

    target = tmp_path / "chapter1"
    target.mkdir()
    workspace = tmp_path / "workspace" / "chapter1"
    workspace.mkdir(parents=True)

    _write_fake_transcript(home_dir, workspace, [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "node $S/lib/build.js content.json --patch patch.json -o out.docx"}},
    ])

    result = write_run_diagnostics(target, workspace)
    assert result["patch_invocations"] == 1
    assert result["adhoc_scripts"] == []


def test_write_run_diagnostics_writes_nothing_on_a_clean_run(monkeypatch, tmp_path):
    """No ad-hoc scripts and no --patch calls is an unremarkable run: no file,
    no warning, nothing for a human to read."""
    home_dir = tmp_path / "home"
    _set_fake_home(monkeypatch, home_dir)

    target = tmp_path / "chapter1"
    target.mkdir()
    workspace = tmp_path / "workspace" / "chapter1"
    workspace.mkdir(parents=True)
    _write_fake_transcript(home_dir, workspace, [{"type": "text", "text": "nothing notable"}])

    result = write_run_diagnostics(target, workspace)
    assert result == {"adhoc_scripts": [], "patch_invocations": 0}
    assert not (target / config.RUN_DIAGNOSTICS).exists()


def test_web_enrichment_enabled_adds_scoped_tools_and_search_cap(monkeypatch, mock_dirs):
    """When opted in, --allowedTools must grant bare WebSearch plus ONLY
    domain-scoped WebFetch(domain:...) rules for the approved domains --
    never a bare WebFetch, which would defeat the domain guardrail (see
    docs/web-enrichment-plan.md's "Core approach") -- and the session
    search cap must be passed through the subprocess environment."""
    monkeypatch.setattr(config, "ENABLE_WEB_ENRICHMENT", True)
    monkeypatch.setattr(config, "WEB_SEARCH_ALLOWED_DOMAINS", ["example.edu", "example.org"])
    monkeypatch.setattr(config, "MAX_WEB_SEARCHES_PER_CHAPTER", 3)

    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"ok": true, "result": "done"}', stderr="")
        (target / "chapter1.docx").touch()

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            with patch('src.claude_cli_subprocess.stage2_cli.write_web_sources_manifest'):
                with patch('src.claude_cli_subprocess.stage2_cli.capture_retro_findings',
                           return_value={"ok": False, "candidates": [], "log_records": 0}):
                    run_stage2_chapter(target_dir=target, live_mode=True)

        args, kwargs = mock_popen.call_args
        allowed_tools = args[0][args[0].index("--allowedTools") + 1]
        tokens = allowed_tools.split(",")
        assert "WebSearch" in tokens
        webfetch_tokens = [t for t in tokens if t.startswith("WebFetch")]
        assert webfetch_tokens, "expected domain-scoped WebFetch rules to be present"
        assert all(t.startswith("WebFetch(domain:") for t in webfetch_tokens), \
            "no bare WebFetch grant allowed -- it would defeat the domain guardrail"
        assert "WebFetch(domain:example.edu)" in tokens
        assert "WebFetch(domain:example.org)" in tokens
        assert kwargs["env"]["CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION"] == "3"

def test_web_enrichment_always_writes_manifest_with_the_grant_it_used(monkeypatch, mock_dirs):
    """write_web_sources_manifest() runs on EVERY successful chapter, and is
    told which grant the run actually got.

    It used to be skipped entirely when enrichment was off, which meant the
    single most useful fact ("this run could not search at all") was recorded
    nowhere -- and the model's own prose filled the vacuum with a wrong reason.
    `enabled` is passed explicitly rather than re-read from config inside the
    manifest writer, so the file can never disagree with what run_claude_cli
    was actually handed."""
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)
    (target / "chapter1.docx").touch()

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"ok": true, "result": "done"}', stderr="")

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            monkeypatch.setattr(config, "ENABLE_WEB_ENRICHMENT", False)
            with patch('src.claude_cli_subprocess.stage2_cli.write_web_sources_manifest') as mock_manifest:
                assert run_stage2_chapter(target_dir=target, live_mode=True) == EXIT_OK
                mock_manifest.assert_called_once()
                assert mock_manifest.call_args.kwargs["enabled"] is False

            monkeypatch.setattr(config, "ENABLE_WEB_ENRICHMENT", True)
            (target / config.MARKER).unlink(missing_ok=True)
            with patch('src.claude_cli_subprocess.stage2_cli.write_web_sources_manifest') as mock_manifest:
                assert run_stage2_chapter(target_dir=target, live_mode=True) == EXIT_OK
                mock_manifest.assert_called_once()
                assert mock_manifest.call_args.kwargs["enabled"] is True


# ---------------------------------------------------------------------------
# Skill-resolution truth-check (verify_resolved_skill) -- added after a live
# run confirmed /study-notes can silently fuzzy-resolve to the WRONG skill;
# see docs/cli-subprocess-plan.md's "Resolved" section for the full story.
# ---------------------------------------------------------------------------

def test_verify_resolved_skill_ok_when_resolved_dir_is_expected(monkeypatch, tmp_path):
    home_dir = tmp_path / "home"
    _set_fake_home(monkeypatch, home_dir)
    workspace = tmp_path / "workspace" / "chapter1"
    workspace.mkdir(parents=True)

    _write_fake_transcript(home_dir, workspace, [
        {"type": "text", "text": "Base directory for this skill: /expected/study-notes\n\n# Study Notes Generator\n..."},
    ])

    result = verify_resolved_skill(workspace, {"/expected/study-notes"})
    assert result == {"checked": True, "ok": True, "resolved": "/expected/study-notes"}

def test_verify_resolved_skill_flags_unexpected_resolution(monkeypatch, tmp_path):
    """The exact failure mode this function exists to catch: a DIFFERENT
    skill (e.g. a stale ~/.claude/skills/study-notes.bak-* directory)
    resolved instead of one of the expected, freshly-synced locations."""
    home_dir = tmp_path / "home"
    _set_fake_home(monkeypatch, home_dir)
    workspace = tmp_path / "workspace" / "chapter1"
    workspace.mkdir(parents=True)

    _write_fake_transcript(home_dir, workspace, [
        {"type": "text", "text": "Base directory for this skill: /home/anoop/.claude/skills/study-notes.bak-20260801\n\n# Study Notes Generator\n..."},
    ])

    result = verify_resolved_skill(workspace, {"/expected/study-notes", "/other/expected/study-notes"})
    assert result["checked"] is True
    assert result["ok"] is False
    assert result["resolved"] == "/home/anoop/.claude/skills/study-notes.bak-20260801"

def test_verify_resolved_skill_unchecked_when_no_resolution_line_present(monkeypatch, tmp_path):
    """No skill-resolution line found (e.g. dev-mode's dummy prompt never
    invokes /study-notes at all) -- must degrade to checked=False, ok=True,
    never treated as a failure just because nothing was found to check."""
    home_dir = tmp_path / "home"
    _set_fake_home(monkeypatch, home_dir)
    workspace = tmp_path / "workspace" / "chapter1"
    workspace.mkdir(parents=True)

    _write_fake_transcript(home_dir, workspace, [{"type": "text", "text": "just a normal reply, no skill invoked"}])

    result = verify_resolved_skill(workspace, {"/expected/study-notes"})
    assert result == {"checked": False, "ok": True, "resolved": None}

def test_verify_resolved_skill_unchecked_when_transcript_missing(monkeypatch, tmp_path):
    home_dir = tmp_path / "home"
    _set_fake_home(monkeypatch, home_dir)
    result = verify_resolved_skill(tmp_path / "workspace" / "no-such-chapter", {"/expected/study-notes"})
    assert result == {"checked": False, "ok": True, "resolved": None}

def test_run_stage2_chapter_fatal_when_resolved_skill_is_unexpected(monkeypatch, mock_dirs):
    """Regression test for the exact bug diagnosed live: even though the
    claude CLI reports success AND the expected .docx exists (a wrong
    skill can still produce a file at the right path, which is exactly
    what happened), a mismatched resolved-skill dir must still be treated
    as FATAL -- this check runs independently of, and before, the
    expected_docx.exists() success gate."""
    home_dir = mock_dirs / "home"
    _set_fake_home(monkeypatch, home_dir)

    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)
    (target / "chapter1.docx").touch()  # wrong skill still produced the file

    workspace = config.CLAUDE_WORKSPACE_ROOT / "chapter1"
    _write_fake_transcript(home_dir, workspace, [
        {"type": "text", "text": "Base directory for this skill: /home/anoop/.claude/skills/study-notes.bak-20260801\n\n# Study Notes Generator\n..."},
    ])

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"ok": true, "result": "done"}', stderr="")

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            result = run_stage2_chapter(target_dir=target, live_mode=True)

    assert result == EXIT_FATAL
    assert (target / config.FAILMARK).exists()
    assert not (target / config.MARKER).exists()


# ---------------------------------------------------------------------------
# retro.py findings capture -- added after a live run's real skill-improvement
# candidates (backed by actual repeated-failure counts) never reached a human,
# because the model correctly declined to pause for approval mid-unattended-run
# and just summarized them away in one dismissive sentence instead.
# ---------------------------------------------------------------------------

def _mock_retro_run(returncode=0, payload=None):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = json.dumps(payload) if payload is not None else ""
    return result

def test_capture_retro_findings_writes_file_when_candidates_found(tmp_path):
    target = tmp_path / "chapter1"
    target.mkdir()
    payload = {"log_records": 42, "candidates": [
        {"issue": "label-on-geometry collisions", "observed": 35,
         "change": "give labels prefer= hints or a larger canvas"},
    ]}
    with patch('subprocess.run', return_value=_mock_retro_run(0, payload)):
        result = capture_retro_findings(target, tmp_path / "workspace")

    assert result == {"ok": True, "candidates": payload["candidates"], "log_records": 42}
    manifest = target / config.RETRO_FINDINGS
    assert manifest.exists()
    content = manifest.read_text()
    assert "label-on-geometry collisions" in content
    assert "observed 35x" in content
    assert "NOT applied" in content  # must be unmistakable this isn't auto-accepted

def test_capture_retro_findings_no_file_when_no_candidates(tmp_path):
    target = tmp_path / "chapter1"
    target.mkdir()
    payload = {"log_records": 10, "candidates": []}
    with patch('subprocess.run', return_value=_mock_retro_run(0, payload)):
        result = capture_retro_findings(target, tmp_path / "workspace")

    assert result == {"ok": True, "candidates": [], "log_records": 10}
    assert not (target / config.RETRO_FINDINGS).exists()

def test_capture_retro_findings_handles_missing_run_log(tmp_path):
    """retro.py exits non-zero (e.g. 'no run log at ...') when the session
    never touched the real pipeline (DEV_TOKEN_SAVER_MODE, or a run that
    failed before generating anything) -- must degrade cleanly, not raise."""
    target = tmp_path / "chapter1"
    target.mkdir()
    with patch('subprocess.run', return_value=_mock_retro_run(2, None)):
        result = capture_retro_findings(target, tmp_path / "workspace")

    assert result == {"ok": False, "candidates": [], "log_records": 0}
    assert not (target / config.RETRO_FINDINGS).exists()

def test_capture_retro_findings_handles_exception(tmp_path):
    target = tmp_path / "chapter1"
    target.mkdir()
    with patch('subprocess.run', side_effect=OSError("boom")):
        result = capture_retro_findings(target, tmp_path / "workspace")

    assert result == {"ok": False, "candidates": [], "log_records": 0}
    assert not (target / config.RETRO_FINDINGS).exists()

def test_run_stage2_chapter_logs_action_needed_when_retro_candidates_found(mock_dirs, caplog):
    """The whole point of capturing this: a human watching the log for an
    otherwise-successful run must see a clear, unmissable signal that
    something needs review -- not just a quiet file drop."""
    import logging
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)
    (target / "chapter1.docx").touch()

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"ok": true, "result": "done"}', stderr="")

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            with patch('src.claude_cli_subprocess.stage2_cli.capture_retro_findings',
                       return_value={"ok": True, "log_records": 5,
                                     "candidates": [{"issue": "x", "observed": 2, "change": "y"}]}):
                with caplog.at_level(logging.WARNING):
                    result = run_stage2_chapter(target_dir=target, live_mode=True)

    assert result == EXIT_OK
    assert (target / config.MARKER).exists()  # retro findings never block success
    assert any("ACTION NEEDED" in r.message for r in caplog.records)

def test_run_stage2_chapter_silent_when_no_retro_candidates(mock_dirs, caplog):
    import logging
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)
    (target / "chapter1.docx").touch()

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"ok": true, "result": "done"}', stderr="")

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            with patch('src.claude_cli_subprocess.stage2_cli.capture_retro_findings',
                       return_value={"ok": True, "log_records": 5, "candidates": []}):
                with caplog.at_level(logging.WARNING):
                    result = run_stage2_chapter(target_dir=target, live_mode=True)

    assert result == EXIT_OK
    assert not any("ACTION NEEDED" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Autonomous skill improvement (apply_retro_fixes) -- opt-in, regress.py-gated
# self-application of retro.py candidates. The accept/reject gate is
# INDEPENDENTLY re-run by this code, never trusted from the session's own
# report -- these tests exist mainly to prove that boundary actually holds.
# ---------------------------------------------------------------------------

def _make_fake_skill_zip(path: Path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("SKILL.md", "# fake skill\n")
        zf.writestr("tools/regress.py", "# placeholder, never actually executed in tests\n")
    return path

def _fake_subprocess_run(regress_returncode=0, regress_stdout="PASS"):
    """apply_retro_fixes() only reaches plain subprocess.run() for the npm
    install (docx) and regress.py steps now -- the claude CLI call itself
    goes through run_claude_cli() (subprocess.Popen), mocked separately via
    _popen_simulating_skill_edit() below."""
    def _run(cmd, **kwargs):
        result = MagicMock()
        if any("regress.py" in str(c) for c in cmd):
            result.returncode = regress_returncode
            result.stdout = regress_stdout
        else:
            result.returncode = 0
            result.stdout = ""
        result.stderr = ""
        return result
    return _run

def _popen_simulating_skill_edit(skill_copy, returncode=0, stdout='{"result": "done"}'):
    """Stand-in for subprocess.Popen matching run_claude_cli()'s actual
    mechanism (see _mock_popen above), with the side effect of touching a
    file in skill_copy -- simulating a session that actually edited the
    skill, which apply_retro_fixes() now requires (mtime-modified check)
    before it will even look at regress.py's result."""
    def _popen(cmd, **kwargs):
        if skill_copy.exists():
            (skill_copy / "SKILL.md").write_text("# fake skill (edited)\n", encoding="utf-8")
        return _mock_popen(returncode=returncode, stdout=stdout, stderr="")
    return _popen

@pytest.fixture
def skill_improvement_dirs(monkeypatch, tmp_path):
    skill_dir = tmp_path / "templates"
    skill_dir.mkdir()
    skill_file = _make_fake_skill_zip(skill_dir / "study-notes.skill")

    monkeypatch.setattr(config, "LOCAL_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(config, "CLAUDE_SKILL_SOURCE_GLOB", "templates/*.skill")
    monkeypatch.setattr(config, "AUTO_SKILL_IMPROVEMENT_WORKSPACE", tmp_path / "improve_ws")
    monkeypatch.setattr(config, "CLAUDE_SKILL_INSTALL_DIR", tmp_path / "install_local")
    monkeypatch.setattr(config, "CLAUDE_SKILL_GLOBAL_INSTALL_DIR", tmp_path / "install_global")

    source_workspace = tmp_path / "chapter_workspace"
    (source_workspace / ".study-notes").mkdir(parents=True)
    return {"skill_file": skill_file, "source_workspace": source_workspace, "tmp_path": tmp_path}

def test_repackage_skill_dir_round_trips(tmp_path):
    original = _make_fake_skill_zip(tmp_path / "orig.skill")
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(original) as zf:
        zf.extractall(extracted)
    # newline="" disables Python's text-mode "\n" -> "\r\n" translation, which
    # on Windows would otherwise make the byte-exact assertion below fail for a
    # reason that has nothing to do with repackage_skill_dir().
    (extracted / "LESSONS.md").write_text("# new file\n", encoding="utf-8", newline="")

    out = tmp_path / "repackaged.skill"
    _repackage_skill_dir(extracted, out)

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert "SKILL.md" in names
        assert "tools/regress.py" in names
        assert "LESSONS.md" in names
        assert zf.read("LESSONS.md") == b"# new file\n"

def test_apply_retro_fixes_applies_and_resyncs_when_regress_passes(skill_improvement_dirs):
    d = skill_improvement_dirs
    candidates = [{"issue": "label collisions", "observed": 5, "change": "bigger canvas"}]
    skill_copy = d["tmp_path"] / "improve_ws" / "skill_copy"

    with patch('subprocess.Popen', side_effect=_popen_simulating_skill_edit(skill_copy)), \
         patch("subprocess.run", side_effect=_fake_subprocess_run(regress_returncode=0)):
        result = apply_retro_fixes(candidates, d["source_workspace"])

    assert result["applied"] is True
    assert (d["tmp_path"] / "install_local" / "SKILL.md").exists()
    assert (d["tmp_path"] / "install_global" / "SKILL.md").exists()

def test_apply_retro_fixes_discards_change_when_regress_fails(skill_improvement_dirs):
    d = skill_improvement_dirs
    original_bytes = d["skill_file"].read_bytes()
    candidates = [{"issue": "label collisions", "observed": 5, "change": "bigger canvas"}]
    skill_copy = d["tmp_path"] / "improve_ws" / "skill_copy"

    with patch('subprocess.Popen', side_effect=_popen_simulating_skill_edit(skill_copy)), \
         patch("subprocess.run", side_effect=_fake_subprocess_run(
            regress_returncode=1, regress_stdout="REGRESSION FAILED: words")):
        result = apply_retro_fixes(candidates, d["source_workspace"])

    assert result["applied"] is False
    assert "regress.py failed" in result["reason"]
    # the source .skill must be byte-for-byte untouched -- a failed
    # self-improvement attempt must never partially land
    assert d["skill_file"].read_bytes() == original_bytes
    assert not (d["tmp_path"] / "install_local").exists()
    assert not (d["tmp_path"] / "install_global").exists()

def test_apply_retro_fixes_reports_no_change_when_session_edits_nothing(skill_improvement_dirs):
    """New regression test for the mtime-modification check added in
    8d088ed: a session that returns ok=True but never actually touched the
    skill copy (e.g. decided every candidate was a false positive) must not
    be reported as applied, and must never reach/trust regress.py at all."""
    d = skill_improvement_dirs
    candidates = [{"issue": "label collisions", "observed": 5, "change": "bigger canvas"}]

    with patch('subprocess.Popen') as mock_popen, \
         patch("subprocess.run", side_effect=_fake_subprocess_run(regress_returncode=0)) as mock_run:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"result": "done"}', stderr="")
        result = apply_retro_fixes(candidates, d["source_workspace"])

    assert result["applied"] is False
    assert "without modifying" in result["reason"]
    # returned before ever reaching the npm-install/regress.py truth-check
    mock_run.assert_not_called()

def test_apply_retro_fixes_no_source_skill_found(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "LOCAL_RUNTIME_ROOT", tmp_path)
    monkeypatch.setattr(config, "CLAUDE_SKILL_SOURCE_GLOB", "templates/*.skill")
    monkeypatch.setattr(config, "AUTO_SKILL_IMPROVEMENT_WORKSPACE", tmp_path / "improve_ws")

    result = apply_retro_fixes([{"issue": "x", "observed": 1, "change": "y"}], tmp_path / "chapter")
    assert result["applied"] is False
    assert "no templates" in result["reason"]

def test_apply_retro_fixes_handles_claude_cli_timeout(monkeypatch, skill_improvement_dirs):
    d = skill_improvement_dirs
    original_bytes = d["skill_file"].read_bytes()
    # Force run_claude_cli()'s deadline to already be in the past on its
    # first loop check, so this exercises the real timeout path (proc.kill()
    # + "timeout" error) without actually waiting out a real deadline.
    monkeypatch.setattr(config, "AUTO_SKILL_IMPROVEMENT_TIMEOUT_SECONDS", -1)

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = MagicMock()
        result = apply_retro_fixes([{"issue": "x", "observed": 1, "change": "y"}], d["source_workspace"])

    assert result["applied"] is False
    assert "timeout" in result["reason"]
    assert d["skill_file"].read_bytes() == original_bytes

def test_run_stage2_chapter_attempts_auto_apply_when_enabled(monkeypatch, mock_dirs):
    monkeypatch.setattr(config, "ENABLE_AUTO_SKILL_IMPROVEMENT", True)
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)
    (target / "chapter1.docx").touch()

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"ok": true, "result": "done"}', stderr="")

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            with patch('src.claude_cli_subprocess.stage2_cli.capture_retro_findings',
                       return_value={"ok": True, "log_records": 5,
                                     "candidates": [{"issue": "x", "observed": 2, "change": "y"}]}):
                with patch('src.claude_cli_subprocess.stage2_cli.apply_retro_fixes',
                           return_value={"applied": True, "reason": "regress.py passed"}) as mock_apply:
                    result = run_stage2_chapter(target_dir=target, live_mode=True)

    assert result == EXIT_OK
    mock_apply.assert_called_once()
    assert (target / config.MARKER).exists()  # a failed/successful fix attempt never blocks the chapter
