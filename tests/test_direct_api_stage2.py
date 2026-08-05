"""
Tests for the web-enrichment + retro port in src/direct_api/stage2_api.py,
and the shared logic it uses from src/common/web_enrichment.py. All tests are
API-free and network-free (DEV_TOKEN_SAVER_MODE + mocked client).
"""
from __future__ import annotations

import sys
import json
from pathlib import Path
from types import SimpleNamespace

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import pytest
from unittest.mock import patch, MagicMock

import settings as config


# ---------------------------------------------------------------------------
# Shared web_enrichment behavior
# ---------------------------------------------------------------------------

def test_is_domain_allowed_matches_config_and_subdomains(monkeypatch):
    from src.common import web_enrichment
    monkeypatch.setattr(config, "WEB_SEARCH_ALLOWED_DOMAINS", ["ncert.nic.in", "khanacademy.org"])
    assert web_enrichment.is_domain_allowed("khanacademy.org")
    assert web_enrichment.is_domain_allowed("sub.ncert.nic.in")
    assert not web_enrichment.is_domain_allowed("en.wikipedia.org")
    # substring-but-not-subdomain must NOT match (evilncert.nic.in.attacker.com)
    assert not web_enrichment.is_domain_allowed("ncert.nic.in.attacker.com")


def test_web_fetch_rejects_off_whitelist_domain(monkeypatch):
    from src.common import web_enrichment
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", False)
    monkeypatch.setattr(config, "WEB_SEARCH_ALLOWED_DOMAINS", ["ncert.nic.in"])
    out = json.loads(web_enrichment.web_fetch("https://evil.example.com/x"))
    assert "not in the whitelist" in out["error"]


def test_web_fetch_rejects_non_http_scheme(monkeypatch):
    """file:// (and other non-http(s) schemes) must never reach urlopen()."""
    from src.common import web_enrichment
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", False)
    monkeypatch.setattr(config, "WEB_SEARCH_ALLOWED_DOMAINS", ["ncert.nic.in"])
    out = json.loads(web_enrichment.web_fetch("file:///etc/passwd"))
    assert "whitelist" in out["error"]


def test_web_search_rejects_when_no_domain_in_whitelist(monkeypatch):
    from src.common import web_enrichment
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", False)
    monkeypatch.setattr(config, "WEB_SEARCH_ALLOWED_DOMAINS", ["ncert.nic.in"])
    out = json.loads(web_enrichment.web_search("photosynthesis", ["en.wikipedia.org"]))
    assert "whitelist" in out["error"]


def test_web_tools_return_mock_in_dev_mode(monkeypatch):
    from src.common import web_enrichment
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", True)
    assert "Mock content" in web_enrichment.web_fetch("https://anything/x")
    assert json.loads(web_enrichment.web_search("q", ["ncert.nic.in"]))  # non-empty mock list


def test_write_web_sources_manifest_noop_when_empty(tmp_path, monkeypatch):
    from src.common import web_enrichment
    web_enrichment.write_web_sources_manifest(tmp_path, [])
    assert not (tmp_path / config.WEB_SOURCES).exists()


def test_write_web_sources_manifest_lists_urls(tmp_path):
    from src.common import web_enrichment
    web_enrichment.write_web_sources_manifest(tmp_path, ["https://ncert.nic.in/a", "https://byjus.com/b"])
    text = (tmp_path / config.WEB_SOURCES).read_text(encoding="utf-8")
    assert "https://ncert.nic.in/a" in text and "https://byjus.com/b" in text


# ---------------------------------------------------------------------------
# direct_api tool wiring
# ---------------------------------------------------------------------------

def test_execute_tool_routes_web_tools(monkeypatch):
    from src.direct_api import stage2_api
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", True)  # avoid network
    fetch_blocks = stage2_api.execute_tool("tool_web_fetch", {"url": "https://ncert.nic.in/x"})
    assert fetch_blocks[0]["type"] == "text" and "Mock content" in fetch_blocks[0]["text"]
    search_blocks = stage2_api.execute_tool("tool_web_search", {"query": "q", "allowed_domains": ["ncert.nic.in"]})
    assert search_blocks[0]["type"] == "text"


def test_web_tools_schema_advertises_config_domains():
    from src.direct_api import stage2_api
    names = [t["name"] for t in stage2_api.WEB_TOOLS]
    assert names == ["tool_web_search", "tool_web_fetch"]
    desc = stage2_api.WEB_TOOLS[0]["description"]
    for d in config.WEB_SEARCH_ALLOWED_DOMAINS:
        assert d in desc  # advertised list is built from config, can't drift


# ---------------------------------------------------------------------------
# Budget enforcement inside the generation loop
# ---------------------------------------------------------------------------

def _text_response(text):
    return SimpleNamespace(content=[SimpleNamespace(type="text", text=text)], usage=None)


def _tool_use(tool_id, name, tool_input):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=tool_input)


def test_web_search_budget_capped_in_loop(tmp_path, monkeypatch):
    """With MAX_WEB_SEARCHES_PER_CHAPTER=1, a turn requesting two web
    searches must run the first and return an is_error budget block for the
    second, without ever exceeding the cap."""
    from src.direct_api import stage2_api

    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", True)   # mock web + skip skill sync
    monkeypatch.setattr(config, "ENABLE_WEB_ENRICHMENT", True)
    monkeypatch.setattr(config, "MAX_WEB_SEARCHES_PER_CHAPTER", 1)
    monkeypatch.setattr(config, "CHAPTER_PROGRESS_DIR", tmp_path / "progress")
    monkeypatch.setattr(config, "MAX_TURNS", 5)
    monkeypatch.setattr(config, "MAX_ATTEMPTS", 3)

    # A ready chapter folder with one transcript, and a pre-made .docx so the
    # 2nd (no-tool) turn confirms success and exits cleanly.
    chapter = tmp_path / "Physics-Ch1"
    (chapter / config.TRANSCRIPTS_DIR).mkdir(parents=True)
    (chapter / config.TRANSCRIPTS_DIR / "t.txt").write_text("lecture", encoding="utf-8")
    (chapter / f"{chapter.name}.docx").write_text("docx", encoding="utf-8")

    turn1 = SimpleNamespace(content=[
        _tool_use("a", "tool_web_search", {"query": "q1", "allowed_domains": ["ncert.nic.in"]}),
        _tool_use("b", "tool_web_search", {"query": "q2", "allowed_domains": ["ncert.nic.in"]}),
    ], usage=None)
    turn2 = _text_response("Done.")

    # `messages` is mutated in place across turns, so we must snapshot it at
    # each call (deep copy) rather than trust the mock's by-reference record.
    import copy
    snapshots = []
    responses = iter([turn1, turn2])

    def _create(**kwargs):
        snapshots.append(copy.deepcopy(kwargs["messages"]))
        return next(responses)

    mock_client = MagicMock()
    mock_client.messages.create.side_effect = _create
    monkeypatch.setattr(stage2_api, "client", mock_client)

    rc = stage2_api.run_generate(target_dir=chapter, live_mode=True)
    assert rc == stage2_api.EXIT_OK

    # The 2nd create call carries turn 1's tool_results -- exactly one search
    # ran, the second was refused with an is_error budget block.
    tool_results = snapshots[1][-1]["content"]
    errors = [b for b in tool_results if b.get("is_error")]
    assert len(errors) == 1
    assert "budget" in errors[0]["content"][0]["text"].lower()
