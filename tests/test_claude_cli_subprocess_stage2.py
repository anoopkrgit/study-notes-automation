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

@pytest.fixture
def mock_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "CLAUDE_WORKSPACE_ROOT", tmp_path / "workspace")
    monkeypatch.setattr(config, "CHAPTER_PROGRESS_DIR", tmp_path / "progress")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(config, "RETRY_EPOCH_FILE", tmp_path / "state" / "retry-epoch.txt")
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return tmp_path

def test_run_stage2_chapter_mock_mode_makes_zero_subprocess_calls(mock_dirs):
    with patch('subprocess.run') as mock_run:
        result = run_stage2_chapter(target_dir=mock_dirs / "chapter1", live_mode=False)
        assert result == EXIT_OK
        mock_run.assert_not_called()

def test_run_stage2_chapter_dev_token_saver_uses_dummy_prompt_and_flags(monkeypatch, mock_dirs):
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", True)
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)
    
    with patch('subprocess.run') as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"ok": true, "result": "done"}'
        mock_result.stderr = ""
        mock_run.return_value = mock_result
        
        # We need expected_docx to exist to get EXIT_OK and not EXIT_FATAL
        (target / "chapter1.docx").touch()
        
        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            run_stage2_chapter(target_dir=target, live_mode=True)
        
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
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
    
    with patch('subprocess.run') as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"ok": true, "result": "done"}'
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            result = run_stage2_chapter(target_dir=target, live_mode=True)

        assert result == EXIT_FATAL
        assert (target / config.FAILMARK).exists()
        assert not (target / config.MARKER).exists()

def test_success_marker_written_when_docx_exists(mock_dirs):
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)
    
    with patch('subprocess.run') as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"ok": true, "result": "done"}'
        mock_result.stderr = ""
        mock_run.return_value = mock_result

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

    with patch('subprocess.run') as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"result": "done"}'
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        (target / "chapter1.docx").touch()

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            run_stage2_chapter(target_dir=target, live_mode=True)

        args, kwargs = mock_run.call_args
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

    with patch('subprocess.run') as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = "launch failure: something"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            result = run_stage2_chapter(target_dir=target, live_mode=True)

        assert result == EXIT_RATE_LIMITED
        assert config.RETRY_EPOCH_FILE.exists()

def test_timeout_is_retryable(mock_dirs):
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)
    
    with patch('subprocess.run', side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=3600)):
        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            result = run_stage2_chapter(target_dir=target, live_mode=True)
        
        assert result == EXIT_RATE_LIMITED
        assert config.RETRY_EPOCH_FILE.exists()

def test_usage_limit_error_is_retryable(mock_dirs):
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)
    
    with patch('subprocess.run') as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = '{"is_error": true, "result": "rate limit reached | 123"}'
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        with patch('src.claude_cli_subprocess.stage2_cli.sync_skill_package'):
            result = run_stage2_chapter(target_dir=target, live_mode=True)

        assert result == EXIT_RATE_LIMITED
        assert config.RETRY_EPOCH_FILE.exists()

def test_unrecognized_error_is_fatal_not_retryable(mock_dirs):
    """Judgment call: an unrecognized/unhandled error shape defaults to FATAL,
    not transient, because Stage 2 is high-stakes."""
    target = mock_dirs / "chapter1"
    target.mkdir(parents=True, exist_ok=True)
    
    with patch('subprocess.run') as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = '{"is_error": true, "result": "Some entirely new error text"}'
        mock_result.stderr = ""
        mock_run.return_value = mock_result

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
