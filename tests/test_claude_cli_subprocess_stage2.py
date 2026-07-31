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
from unittest.mock import patch, MagicMock

import settings as config
from src.claude_cli_subprocess.common import build_claude_env
from src.func_tools_and_utils import EXIT_OK, EXIT_RATE_LIMITED, EXIT_FATAL

from src.claude_cli_subprocess.stage2_cli import (
    run_stage2_chapter, run_claude_cli, classify_cli_result, sync_skill_package
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
