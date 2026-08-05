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
    apply_retro_fixes, _repackage_skill_dir
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
        assert cmd[cmd.index("--allowedTools") + 1] == config.CLAUDE_ALLOWED_TOOLS
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

def _write_fake_transcript(home_dir: Path, workspace: Path, content_blocks):
    """Build a fake Claude Code session transcript JSONL at the same path
    write_web_sources_manifest() (and _heartbeat_summary()) derive from
    `workspace` -- one line, one message, whose content is exactly the
    given list of blocks (tool_use / text)."""
    project_dir = home_dir / ".claude" / "projects" / str(workspace).replace("/", "-")
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
    monkeypatch.setenv("HOME", str(home_dir))

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

def test_write_web_sources_manifest_skips_when_no_web_tool_use(monkeypatch, tmp_path):
    home_dir = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home_dir))

    target = tmp_path / "chapter1"
    target.mkdir()
    workspace = tmp_path / "workspace" / "chapter1"
    workspace.mkdir(parents=True)

    _write_fake_transcript(home_dir, workspace, [{"type": "text", "text": "no web tools used this run"}])

    write_web_sources_manifest(target, workspace)
    assert not (target / config.WEB_SOURCES).exists()

def test_write_web_sources_manifest_silent_when_transcript_missing(monkeypatch, tmp_path):
    """Best-effort: a missing transcript must never raise or block the
    pipeline, same as _heartbeat_summary()'s own fallback."""
    home_dir = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home_dir))
    target = tmp_path / "chapter1"
    target.mkdir()
    write_web_sources_manifest(target, tmp_path / "workspace" / "no-such-chapter")
    assert not (target / config.WEB_SOURCES).exists()

def test_web_enrichment_off_by_default_no_tools_no_env_var(mock_dirs):
    """Default config.ENABLE_WEB_ENRICHMENT is False -- a real run must NOT
    grant WebSearch/WebFetch and must NOT set
    CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION, so an operator who never
    opted in never has Claude Code's web tools available."""
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

def test_web_enrichment_writes_manifest_only_when_enabled(monkeypatch, mock_dirs):
    """write_web_sources_manifest() must only run when the operator opted
    in -- otherwise a chapter generated without web tools would get a
    spurious/empty audit file."""
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)
    (target / "chapter1.docx").touch()

    with patch('subprocess.Popen') as mock_popen:
        mock_popen.return_value = _mock_popen(returncode=0, stdout='{"ok": true, "result": "done"}', stderr="")

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            with patch('src.claude_cli_subprocess.stage2_cli.write_web_sources_manifest') as mock_manifest:
                assert run_stage2_chapter(target_dir=target, live_mode=True) == EXIT_OK
                mock_manifest.assert_not_called()

            monkeypatch.setattr(config, "ENABLE_WEB_ENRICHMENT", True)
            (target / config.MARKER).unlink(missing_ok=True)
            with patch('src.claude_cli_subprocess.stage2_cli.write_web_sources_manifest') as mock_manifest:
                assert run_stage2_chapter(target_dir=target, live_mode=True) == EXIT_OK
                mock_manifest.assert_called_once()


# ---------------------------------------------------------------------------
# Skill-resolution truth-check (verify_resolved_skill) -- added after a live
# run confirmed /study-notes can silently fuzzy-resolve to the WRONG skill;
# see docs/cli-subprocess-plan.md's "Resolved" section for the full story.
# ---------------------------------------------------------------------------

def test_verify_resolved_skill_ok_when_resolved_dir_is_expected(monkeypatch, tmp_path):
    home_dir = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home_dir))
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
    monkeypatch.setenv("HOME", str(home_dir))
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
    monkeypatch.setenv("HOME", str(home_dir))
    workspace = tmp_path / "workspace" / "chapter1"
    workspace.mkdir(parents=True)

    _write_fake_transcript(home_dir, workspace, [{"type": "text", "text": "just a normal reply, no skill invoked"}])

    result = verify_resolved_skill(workspace, {"/expected/study-notes"})
    assert result == {"checked": False, "ok": True, "resolved": None}

def test_verify_resolved_skill_unchecked_when_transcript_missing(monkeypatch, tmp_path):
    home_dir = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home_dir))
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
    monkeypatch.setenv("HOME", str(home_dir))

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
    (extracted / "LESSONS.md").write_text("# new file\n", encoding="utf-8")

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
