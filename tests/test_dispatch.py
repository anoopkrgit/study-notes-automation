"""
Tests for src/agents/dispatch.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import pytest
from unittest.mock import patch, MagicMock

def test_generate_notes_legacy_by_default():
    """When STAGE2_IMPL='legacy' (default), dispatch calls run_generate."""
    with patch('src.agents.dispatch.config') as mock_config:
        mock_config.STAGE2_IMPL = 'legacy'
        with patch('src.direct_api.func_generate_notes.run_generate', return_value=0) as mock_rg:
            from src.agents.dispatch import generate_notes
            result = generate_notes(Path('/fake'), live_mode=False, verbose=False)
            assert result == 0
            mock_rg.assert_called_once()

def test_generate_notes_graph_mode():
    """When STAGE2_IMPL='graph', dispatch calls run_stage2_chapter."""
    with patch('src.agents.dispatch.config') as mock_config:
        mock_config.STAGE2_IMPL = 'graph'
        with patch('src.agents.stage2_graph.run_stage2_chapter', return_value=0) as mock_s2:
            from src.agents.dispatch import generate_notes
            result = generate_notes(Path('/fake'), live_mode=False, verbose=False)
            assert result == 0
            mock_s2.assert_called_once()

def test_route_file_legacy_by_default():
    """When STAGE1_IMPL='legacy', dispatch calls llm_route."""
    with patch('src.agents.dispatch.config') as mock_config:
        mock_config.STAGE1_IMPL = 'legacy'
        with patch('src.direct_api.func_assemble_chapters.llm_route', return_value=([], False, 'mock')) as mock_lr:
            from src.agents.dispatch import route_file
            result = route_file(Path('/fake'), {})
            assert result == ([], False, 'mock')

def test_route_file_graph_mode():
    """When STAGE1_IMPL='graph', dispatch calls route_one_file."""
    with patch('src.agents.dispatch.config') as mock_config:
        mock_config.STAGE1_IMPL = 'graph'
        with patch('src.agents.stage1_graph.route_one_file', return_value=([], False, 'mock')) as mock_rf:
            from src.agents.dispatch import route_file
            result = route_file(Path('/fake'), {})
            assert result == ([], False, 'mock')

def test_generate_notes_subprocess_mode_raises():
    """When STAGE2_IMPL='subprocess', dispatch calls run_stage2_chapter which raises NotImplementedError."""
    with patch('src.agents.dispatch.config') as mock_config:
        mock_config.STAGE2_IMPL = 'subprocess'
        from src.claude_cli_subprocess.stage2 import run_stage2_chapter
        with patch('src.claude_cli_subprocess.stage2.run_stage2_chapter', side_effect=NotImplementedError):
            from src.agents.dispatch import generate_notes
            with pytest.raises(NotImplementedError):
                generate_notes(Path('/fake'), live_mode=False, verbose=False)

def test_route_file_subprocess_mode():
    """When STAGE1_IMPL='subprocess', dispatch calls cli_route_file."""
    with patch('src.agents.dispatch.config') as mock_config:
        mock_config.STAGE1_IMPL = 'subprocess'
        with patch('src.claude_cli_subprocess.stage1.route_file', return_value=([], False, 'mock_subprocess')) as mock_rf:
            from src.agents.dispatch import route_file
            result = route_file(Path('/fake'), {})
            assert result == ([], False, 'mock_subprocess')
            mock_rf.assert_called_once()
