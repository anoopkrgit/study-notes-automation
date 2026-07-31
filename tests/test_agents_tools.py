"""
Tests for src/agents/tools.py.
"""
from __future__ import annotations

import sys
import json
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import pytest
from unittest.mock import patch, MagicMock

def test_validate_path_within_root(tmp_path):
    """_validate_path succeeds when path is within root."""
    from src.agents.tools import _validate_path
    child = tmp_path / 'subdir' / 'file.txt'
    child.parent.mkdir(parents=True, exist_ok=True)
    child.touch()
    result = _validate_path(str(child), str(tmp_path))
    assert result == child.resolve()

def test_validate_path_traversal_blocked(tmp_path):
    """_validate_path raises ValueError on traversal attempt."""
    from src.agents.tools import _validate_path
    with pytest.raises(ValueError, match='Path traversal'):
        _validate_path('/etc/passwd', str(tmp_path))

def test_tool_ingest_dev_mode():
    """In DEV mode, tool_ingest returns dummy without calling subprocess."""
    with patch('src.agents.tools.config') as mock_config:
        mock_config.DEV_TOKEN_SAVER_MODE = True
        from src.agents.tools import tool_ingest
        result = tool_ingest('/fake.pdf', '/fake/chapter')
        assert result['ok'] is True
        assert 'dummy.txt' in result['extracted_files']

def test_tool_read_source_dev_mode():
    """In DEV mode, tool_read_source returns dummy string."""
    with patch('src.agents.tools.config') as mock_config:
        mock_config.DEV_TOKEN_SAVER_MODE = True
        from src.agents.tools import tool_read_source
        result = tool_read_source('/fake/file.txt', '/fake')
        assert result == 'Sample transcript content for testing.'

def test_tool_write_content_json_dev_mode(tmp_path):
    """In DEV mode, tool_write_content_json writes JSON without validation."""
    with patch('src.agents.tools.config') as mock_config:
        mock_config.DEV_TOKEN_SAVER_MODE = True
        from src.agents.tools import tool_write_content_json
        test_content = {'title': 'Test Chapter'}
        result = tool_write_content_json(test_content, str(tmp_path))
        assert result['ok'] is True
        written = json.loads((tmp_path / 'content.json').read_text())
        assert written == test_content

def test_tool_run_structural_gates_dev_mode_still_runs_for_real():
    """DEV_TOKEN_SAVER_MODE must NOT short-circuit the QA gate tools -- only
    models/prompts/max_tokens/ingestion are stubbed under that flag. Gates
    always execute the real validate.py/verify.py/invariants.py scripts, so a
    nonexistent content.json path must fail structurally, not fake a pass."""
    with patch('src.agents.tools.config') as mock_config:
        mock_config.DEV_TOKEN_SAVER_MODE = True
        from src.agents.tools import tool_run_structural_gates
        result = tool_run_structural_gates('/fake/content.json', '/fake/figures.json')
        assert result['schema_ok'] is False
        assert result['failures'] != []

def test_tool_compile_docx_dev_mode(tmp_path):
    """In DEV mode, tool_compile_docx creates a dummy file."""
    with patch('src.agents.tools.config') as mock_config:
        mock_config.DEV_TOKEN_SAVER_MODE = True
        from src.agents.tools import tool_compile_docx
        result = tool_compile_docx('/fake/content.json', '/fake/figures.json', str(tmp_path))
        assert result['ok'] is True
        assert Path(result['docx_path']).exists()

def test_tool_registry_contains_all_tools():
    """TOOL_REGISTRY has entries for all expected tool names."""
    from src.agents.tools import TOOL_REGISTRY
    expected = ['tool_read_source', 'tool_view_source_page', 'tool_write_content_json',
                'tool_write_figures_json', 'tool_read_qa_feedback', 'tool_figbuild',
                'tool_view_figure', 'tool_compile_docx']
    for name in expected:
        assert name in TOOL_REGISTRY, f'{name} missing from TOOL_REGISTRY'
