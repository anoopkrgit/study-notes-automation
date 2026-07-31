"""
Tests for src/agents/stage1_graph.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import pytest
from unittest.mock import patch, MagicMock, PropertyMock

def test_extract_node_txt_file(tmp_path):
    """extract_node reads a text file and returns content blocks."""
    test_file = tmp_path / 'test.txt'
    test_file.write_text('Hello world', encoding='utf-8')
    from src.agents.stage1_graph import extract_node
    state = {'file_path': str(test_file), 'buckets': [], 'prior': None,
             'extracted_content': None, 'matches': [], 'limited': False, 'model_used': ''}
    result = extract_node(state)
    assert result['extracted_content'] is not None
    assert result['extracted_content'][0]['type'] == 'text'
    assert 'Hello world' in result['extracted_content'][0]['text']

def test_extract_node_missing_file(tmp_path):
    """extract_node returns None content for missing file."""
    from src.agents.stage1_graph import extract_node
    state = {'file_path': str(tmp_path / 'missing.txt'), 'buckets': [], 'prior': None,
             'extracted_content': None, 'matches': [], 'limited': False, 'model_used': ''}
    result = extract_node(state)
    assert result['extracted_content'] is None

def test_triage_node_no_content():
    """triage_node returns empty matches if no content was extracted."""
    from src.agents.stage1_graph import triage_node
    state = {'file_path': '/fake', 'buckets': [], 'prior': None,
             'extracted_content': None, 'matches': [], 'limited': False, 'model_used': ''}
    result = triage_node(state)
    assert result['matches'] == []
    assert result['model_used'] == ''

def test_reconcile_node_filters_matches():
    """reconcile_node passes through valid matches."""
    from src.agents.stage1_graph import reconcile_node
    matches = [{'subject': 'Physics', 'chapter_no': 1, 'confidence': 0.9}]
    state = {'file_path': '/fake', 'buckets': ['Physics Chapter 1'], 'prior': None,
             'extracted_content': None, 'matches': matches, 'limited': False, 'model_used': 'test-model'}
    result = reconcile_node(state)
    assert len(result['matches']) == 1
    assert result['matches'][0]['subject'] == 'Physics'

def test_reconcile_node_limited_passthrough():
    """reconcile_node passes through limited=True without filtering."""
    from src.agents.stage1_graph import reconcile_node
    state = {'file_path': '/fake', 'buckets': [], 'prior': None,
             'extracted_content': None, 'matches': [], 'limited': True, 'model_used': 'test-model'}
    result = reconcile_node(state)
    assert result['limited'] is True

def test_route_one_file_returns_correct_shape():
    """route_one_file returns (matches, limited, model_used) tuple."""
    with patch('src.agents.stage1_graph.anthropic') as mock_anthropic:
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        
        mock_usage = MagicMock()
        mock_usage.input_tokens = 10
        mock_usage.output_tokens = 5
        
        mock_tool_block = MagicMock()
        mock_tool_block.type = 'tool_use'
        mock_tool_block.name = 'route_file'
        mock_tool_block.input = {'matches': [{'subject': 'Physics', 'chapter_no': 1, 'confidence': 0.95}]}
        
        mock_response = MagicMock()
        mock_response.content = [mock_tool_block]
        mock_response.usage = mock_usage
        mock_client.messages.create.return_value = mock_response
        
        from src.agents.stage1_graph import route_one_file
        # Create a temp file for extraction
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.txt', mode='w', delete=False) as f:
            f.write('test content')
            tmp_path = f.name
        
        try:
            result = route_one_file(Path(tmp_path), {'Physics Chapter 1': '/some/path'})
            assert isinstance(result, tuple)
            assert len(result) == 3
            matches, limited, model_used = result
            assert isinstance(matches, list)
            assert isinstance(limited, bool)
            assert isinstance(model_used, str)
        finally:
            import os
            os.unlink(tmp_path)
