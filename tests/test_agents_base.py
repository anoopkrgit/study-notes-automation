"""
Tests for src/agents/base.py's make_agent_node loop.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

from unittest.mock import patch, MagicMock


def _mock_config():
    """A config stand-in with DEV_TOKEN_SAVER_MODE off and no
    CHAPTER_PROGRESS_DIR, so make_agent_node's debug-dump step (which
    writes to disk) is skipped -- these tests care about the in-memory
    tool-call/status behavior only."""
    mock_config = MagicMock(spec=['DEV_TOKEN_SAVER_MODE'])
    mock_config.DEV_TOKEN_SAVER_MODE = False
    return mock_config


def _tool_use_response(tool_name: str, tool_input: dict, tool_id: str = 'toolu_1'):
    block = MagicMock()
    block.type = 'tool_use'
    block.name = tool_name
    block.input = tool_input
    block.id = tool_id
    resp = MagicMock()
    resp.content = [block]
    resp.stop_reason = 'tool_use'
    resp.usage = MagicMock(input_tokens=10, output_tokens=5)
    resp.model_dump.return_value = {'role': 'assistant', 'content': [tool_input]}
    return resp


def _end_turn_response():
    resp = MagicMock()
    resp.content = []
    resp.stop_reason = 'end_turn'
    resp.usage = MagicMock(input_tokens=1, output_tokens=1)
    resp.model_dump.return_value = {'role': 'assistant', 'content': []}
    return resp


def test_tool_call_uses_trusted_chapter_dir_not_model_supplied():
    """A model-supplied chapter_dir must never reach the tool -- the real
    chapter_dir for this run (from state) always wins, closing the
    path-traversal sandbox bypass where chapter_dir was both the value
    being checked and the boundary checking it."""
    calls = []

    def spy_tool(path, chapter_dir):
        calls.append({'path': path, 'chapter_dir': chapter_dir})
        return 'ok'

    with patch('src.agents.base.anthropic') as mock_anthropic, \
         patch('src.agents.base.config', _mock_config()):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.side_effect = [
            _tool_use_response('tool_read_source', {'path': '/etc/passwd', 'chapter_dir': '/'}),
            _end_turn_response(),
        ]

        from src.agents.base import make_agent_node
        node_fn = make_agent_node(
            node_name='author',
            model_name='claude-test',
            tools_for_anthropic=[],
            system_prompt_fn=lambda state: 'system prompt',
            max_agent_turns=5,
            tool_registry={'tool_read_source': spy_tool},
        )
        state = {'chapter_dir': '/trusted/chapter-dir', 'messages': {}, 'turn_count': 0, 'status': 'running'}
        node_fn(state)

    assert len(calls) == 1
    assert calls[0]['chapter_dir'] == '/trusted/chapter-dir'
    assert calls[0]['chapter_dir'] != '/'


def test_turn_budget_exhausted_sets_failed_status():
    """If max_agent_turns is exhausted while Claude keeps asking for tools
    (no terminal stop_reason, no exception), status must end up as
    'failed_turns_exhausted' instead of silently staying 'running'."""
    def spy_tool(**kwargs):
        return 'ok'

    with patch('src.agents.base.anthropic') as mock_anthropic, \
         patch('src.agents.base.config', _mock_config()):
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        # Every turn asks for a tool and never stops on its own -- forces
        # the loop to exhaust max_agent_turns without ever hitting break.
        mock_client.messages.create.side_effect = lambda **kw: _tool_use_response(
            'tool_read_source', {'path': 'x', 'chapter_dir': '/trusted'}
        )

        from src.agents.base import make_agent_node
        node_fn = make_agent_node(
            node_name='author',
            model_name='claude-test',
            tools_for_anthropic=[],
            system_prompt_fn=lambda state: 'system prompt',
            max_agent_turns=3,
            tool_registry={'tool_read_source': spy_tool},
        )
        state = {'chapter_dir': '/trusted', 'messages': {}, 'turn_count': 0, 'status': 'running'}
        result = node_fn(state)

    assert result['status'] == 'failed_turns_exhausted'
