import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import json
import subprocess
import pytest
from unittest.mock import patch, MagicMock

import settings as config
from src.claude_cli_subprocess.common import build_claude_env
from src.claude_cli_subprocess.stage1_cli import route_file, run_router, ROUTER_SCHEMA

def test_build_claude_env_pops_anthropic_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    env = build_claude_env()
    assert "ANTHROPIC_API_KEY" not in env

def test_run_router_success():
    with patch('subprocess.run') as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = 'some text\n{"type": "message", "result": "{\\"matches\\": []}"}'
        mock_result.stderr = ""
        mock_run.return_value = mock_result
        
        obj, limited = run_router("prompt", "model")
        assert limited is False
        assert obj == {"matches": []}
        mock_run.assert_called_once()
        
        args, kwargs = mock_run.call_args
        assert kwargs["cwd"] == str(config.LOCAL_RUNTIME_ROOT)
        assert "ANTHROPIC_API_KEY" not in kwargs["env"]

def test_run_router_usage_limit():
    with patch('subprocess.run') as mock_run:
        mock_result = MagicMock()
        mock_result.stdout = "rate limit reached | 1234567890"
        mock_result.stderr = ""
        mock_run.return_value = mock_result
        
        obj, limited = run_router("prompt", "model")
        assert limited is True
        assert obj is None

def test_run_router_file_not_found():
    with patch('subprocess.run', side_effect=FileNotFoundError):
        obj, limited = run_router("prompt", "model")
        assert limited is False
        assert obj is None

def test_run_router_nonzero_returncode_is_not_treated_as_zero_matches():
    """A hard CLI failure (crash, auth error, bad flag, ...) that ISN'T
    usage-limit-shaped must surface as limited=True, not as a fabricated
    'zero matches found' result that would silently misfile the file."""
    with patch('subprocess.run') as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "Error: invalid API key"
        mock_run.return_value = mock_result

        obj, limited = run_router("prompt", "model")
        assert limited is True
        assert obj is None

def test_route_file_escalates_on_low_confidence():
    with patch('src.claude_cli_subprocess.stage1_cli.run_router') as mock_rr:
        # First call returns low confidence, second returns high confidence
        mock_rr.side_effect = [
            ({"matches": [{"subject": "Physics", "chapter_no": 1, "confidence": 0.2}]}, False),
            ({"matches": [{"subject": "Physics", "chapter_no": 1, "confidence": 0.9}]}, False)
        ]

        buckets = {("Physics", 1): "Forces"}
        matches, limited, model = route_file(Path("/fake"), buckets)

        assert limited is False
        assert mock_rr.call_count == 2
        assert model == config.ESCALATE_MODEL
        assert matches[0][4] == 0.9


# ── DEV_TOKEN_SAVER_MODE parity (streamlined across all three
#    --stageN-impl choices -- see src/claude_cli_subprocess/__init__.py) ───

def test_route_file_dev_token_saver_mode_never_escalates(monkeypatch):
    """DEV_TOKEN_SAVER_MODE must skip escalation entirely, even at zero
    confidence -- escalating to a pricier model is the single biggest cost
    lever for this router."""
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", True)
    with patch('src.claude_cli_subprocess.stage1_cli.run_router') as mock_rr:
        mock_rr.return_value = ({"matches": []}, False)
        buckets = {("Physics", 1): "Forces"}
        matches, limited, model = route_file(Path("/fake"), buckets)

        assert limited is False
        mock_rr.assert_called_once()  # never escalates to a second call
        assert model == config.ROUTER_MODEL
        # the dummy prompt must never mention the real file path
        prompt_used = mock_rr.call_args[0][0]
        assert "/fake" not in prompt_used


def test_route_file_dev_token_saver_mode_uses_dummy_prompt(monkeypatch):
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", True)
    with patch('src.claude_cli_subprocess.stage1_cli.run_router') as mock_rr:
        mock_rr.return_value = ({"matches": []}, False)
        route_file(Path("/some/real/transcript.pdf"), {("Physics", 1): "Forces"})
        prompt_used = mock_rr.call_args[0][0]
        assert "DEV_TOKEN_SAVER_MODE" in prompt_used
        assert "/some/real/transcript.pdf" not in prompt_used


def test_run_router_dev_token_saver_mode_adds_budget_and_effort_flags(monkeypatch):
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", True)
    with patch('subprocess.run') as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"type": "message", "result": "{\\"matches\\": []}"}'
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        run_router("prompt", "model")

        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert "--max-budget-usd" in cmd
        assert cmd[cmd.index("--max-budget-usd") + 1] == str(config.DEV_TOKEN_SAVER_MAX_BUDGET_USD)
        assert "--effort" in cmd
        assert cmd[cmd.index("--effort") + 1] == config.DEV_TOKEN_SAVER_EFFORT


def test_run_router_no_budget_flags_when_dev_mode_off(monkeypatch):
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", False)
    with patch('subprocess.run') as mock_run:
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"type": "message", "result": "{\\"matches\\": []}"}'
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        run_router("prompt", "model")

        args, kwargs = mock_run.call_args
        assert "--max-budget-usd" not in args[0]
