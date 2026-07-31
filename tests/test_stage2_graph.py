"""
Tests for src/agents/stage2_graph.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import pytest
from unittest.mock import patch, MagicMock

def test_ingest_node_dev_mode():
    """ingest_node returns state unchanged in DEV mode."""
    with patch('src.agents.stage2_graph.config') as mock_config:
        mock_config.DEV_TOKEN_SAVER_MODE = True
        from src.agents.stage2_graph import ingest_node
        state = {
            'chapter_dir': '/fake/chapter',
            'transcripts': ['/fake/transcript.pdf'],
            'supporting': [],
            'content_json': None, 'figures_json': None, 'qa_report': None,
            'qa_pass_count': 0, 'turn_count': 0, 'attempt_count': 0,
            'messages': {}, 'status': 'running'
        }
        result = ingest_node(state)
        assert result == state  # State returned unchanged in dev mode

def test_qa_node_dev_mode_runs_real_gates_against_dummy_content(tmp_path):
    """qa_node must run the REAL QA gates even in DEV_TOKEN_SAVER_MODE -- this
    mode fakes models/prompts/max_tokens/ingestion, but QA gates are never
    stubbed (that would make the QA<->Author retry loop untestable and could
    mark a chapter 'done' with nothing actually generated). Dummy placeholder
    content is expected to genuinely FAIL real validation; that's the point."""
    with patch('src.agents.stage2_graph.config') as mock_config:
        mock_config.DEV_TOKEN_SAVER_MODE = True
        # Dummy/placeholder files, same as what DEV_TOKEN_SAVER_MODE's other
        # stubs (tool_write_content_json, tool_compile_docx, etc.) would produce.
        (tmp_path / 'content.json').write_text('{"text": "test"}')
        (tmp_path / 'figures.json').write_text('[]')
        (tmp_path / 'output.docx').write_text('dummy')

        from src.agents.stage2_graph import qa_node
        state = {
            'chapter_dir': str(tmp_path),
            'transcripts': [], 'supporting': [],
            'content_json': None, 'figures_json': None, 'qa_report': None,
            'qa_pass_count': 0, 'turn_count': 0, 'attempt_count': 0,
            'messages': {}, 'status': 'running'
        }
        result = qa_node(state)
        assert 'qa_report' in result
        # Real gates run against dummy content -> must fail, not fake a pass.
        assert result['qa_report']['all_passed'] is False
        assert result['status'] == 'running'  # not 'done' -- QA correctly rejected it

def test_route_after_qa_pass():
    """route_after_qa returns 'pass' when all gates passed."""
    from src.agents.stage2_graph import route_after_qa
    state = {
        'status': 'running',
        'qa_report': {'all_passed': True},
        'qa_pass_count': 0
    }
    assert route_after_qa(state) == 'pass'

def test_route_after_qa_retry():
    """route_after_qa returns 'retry' when gates failed and retries remain."""
    from src.agents.stage2_graph import route_after_qa
    state = {
        'status': 'running',
        'qa_report': {'all_passed': False},
        'qa_pass_count': 0
    }
    with patch('src.agents.stage2_graph.config') as mock_config:
        mock_config.QA_MAX_RETRY_LOOPS = 5
        assert route_after_qa(state) == 'retry'

def test_route_after_qa_fail_max_retries():
    """route_after_qa returns 'fail' when retries exhausted."""
    from src.agents.stage2_graph import route_after_qa
    state = {
        'status': 'running',
        'qa_report': {'all_passed': False},
        'qa_pass_count': 5
    }
    with patch('src.agents.stage2_graph.config') as mock_config:
        mock_config.QA_MAX_RETRY_LOOPS = 5
        assert route_after_qa(state) == 'fail'

def test_route_after_qa_fail_fatal_status():
    """route_after_qa returns 'fail' when status is already fatal."""
    from src.agents.stage2_graph import route_after_qa
    state = {
        'status': 'failed_fatal',
        'qa_report': {'all_passed': True},  # Even with passed QA, fatal status wins
        'qa_pass_count': 0
    }
    assert route_after_qa(state) == 'fail'

def test_run_stage2_chapter_mock_mode(tmp_path):
    """run_stage2_chapter returns EXIT_OK immediately in mock mode, given a
    real chapter directory (mock mode still validates --target-dir exists --
    see run_stage2_chapter's is_dir() check -- it just does no real work
    beyond that)."""
    from src.agents.stage2_graph import run_stage2_chapter
    from src.func_tools_and_utils import EXIT_OK
    chapter_dir = tmp_path / "chapter"
    chapter_dir.mkdir()
    result = run_stage2_chapter(str(chapter_dir), live_mode=False)
    assert result == EXIT_OK

def test_run_stage2_chapter_rejects_nonexistent_target_dir():
    """A bad/typo'd --target-dir must fail fast (EXIT_FATAL), not silently
    report success as if it were just an empty-but-real chapter folder."""
    from src.agents.stage2_graph import run_stage2_chapter
    from src.func_tools_and_utils import EXIT_FATAL
    result = run_stage2_chapter('/definitely/does/not/exist', live_mode=False)
    assert result == EXIT_FATAL

def test_build_stage2_graph_structure():
    """build_stage2_graph creates a graph with the expected nodes."""
    from src.agents.stage2_graph import build_stage2_graph
    graph = build_stage2_graph()
    # Verify all expected nodes are in the graph
    node_names = list(graph.nodes.keys())
    assert 'ingest' in node_names
    assert 'author' in node_names
    assert 'figure' in node_names
    assert 'compiler' in node_names
    assert 'qa' in node_names
