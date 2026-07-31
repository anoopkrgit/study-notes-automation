"""
test_utils.py

Unit tests for core utility functions: classify_and_rename, sha256, is_ignorable,
the API-error classifier, the state-file lock, and dry-run safety.
Run with: python -m pytest tests/test_utils.py -v
"""

import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

# Setup path so imports work
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import httpx
import anthropic
import pytest

import logging

import settings as config
from src.func_tools_and_utils import (
    sha256, is_ignorable, classify_api_error,
    acquire_lock, release_lock, load_state, save_state,
    logger as pipeline_logger,
)
import src.func_classify_and_rename as cr
import src.direct_api.stage2_api as fgn
from src.direct_api.stage2_api import select_target_chapter, run_generate
from src.func_tools_and_utils import EXIT_OK
import src.direct_api.stage1_api as fac


# ── sha256 tests ──────────────────────────────────────────────────────────────

def test_sha256_known_content():
    """SHA-256 of known content matches expected hash."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
        f.write(b"hello world")
        f.flush()
        path = Path(f.name)
    try:
        result = sha256(path)
        expected = hashlib.sha256(b"hello world").hexdigest()
        assert result == expected, f"Expected {expected}, got {result}"
    finally:
        path.unlink()


def test_sha256_empty_file():
    """SHA-256 of an empty file matches empty-bytes hash."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
        path = Path(f.name)
    try:
        result = sha256(path)
        expected = hashlib.sha256(b"").hexdigest()
        assert result == expected
    finally:
        path.unlink()


def test_sha256_large_file():
    """SHA-256 correctly handles files larger than the 1MB chunk size."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        data = b"x" * (2 * 1024 * 1024)  # 2MB
        f.write(data)
        f.flush()
        path = Path(f.name)
    try:
        result = sha256(path)
        expected = hashlib.sha256(data).hexdigest()
        assert result == expected
    finally:
        path.unlink()


def test_sha256_same_content_same_hash():
    """Two files with identical content produce the same hash."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".a") as f1:
        f1.write(b"identical content")
        path1 = Path(f1.name)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".b") as f2:
        f2.write(b"identical content")
        path2 = Path(f2.name)
    try:
        assert sha256(path1) == sha256(path2)
    finally:
        path1.unlink()
        path2.unlink()


def test_sha256_different_content_different_hash():
    """Two files with different content produce different hashes."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".a") as f1:
        f1.write(b"content A")
        path1 = Path(f1.name)
    with tempfile.NamedTemporaryFile(delete=False, suffix=".b") as f2:
        f2.write(b"content B")
        path2 = Path(f2.name)
    try:
        assert sha256(path1) != sha256(path2)
    finally:
        path1.unlink()
        path2.unlink()


# ── is_ignorable tests ───────────────────────────────────────────────────────

def _make_temp_file(name: str, content: bytes = b"x") -> Path:
    """Helper: create a named temp file in a temp dir."""
    d = Path(tempfile.mkdtemp())
    p = d / name
    p.write_bytes(content)
    return p


def test_ignorable_desktop_ini():
    p = _make_temp_file("desktop.ini")
    assert is_ignorable(p) is True
    p.unlink()
    p.parent.rmdir()


def test_ignorable_pipeline_sources_manifest():
    """Regression test: _sources.txt (the pipeline's own manifest file
    written into every chapter folder) must never be treated as a real
    input file -- caught during sandbox testing, where it was showing up
    in the generator's "Spine Transcripts" file listing sent to the model."""
    p = _make_temp_file(config.SOURCES)
    assert is_ignorable(p) is True
    p.unlink()
    p.parent.rmdir()


def test_ignorable_pipeline_hold_marker():
    p = _make_temp_file(config.HOLD)
    assert is_ignorable(p) is True
    p.unlink()
    p.parent.rmdir()


def test_ignorable_thumbs_db():
    p = _make_temp_file("Thumbs.db")
    assert is_ignorable(p) is True
    p.unlink()
    p.parent.rmdir()


def test_ignorable_ds_store():
    p = _make_temp_file(".DS_Store")
    assert is_ignorable(p) is True
    p.unlink()
    p.parent.rmdir()


def test_ignorable_dotfile():
    p = _make_temp_file(".hidden_file")
    assert is_ignorable(p) is True
    p.unlink()
    p.parent.rmdir()


def test_ignorable_tmp_extension():
    p = _make_temp_file("download.tmp")
    assert is_ignorable(p) is True
    p.unlink()
    p.parent.rmdir()


def test_ignorable_part_extension():
    p = _make_temp_file("file.part")
    assert is_ignorable(p) is True
    p.unlink()
    p.parent.rmdir()


def test_ignorable_crdownload():
    p = _make_temp_file("file.crdownload")
    assert is_ignorable(p) is True
    p.unlink()
    p.parent.rmdir()


def test_ignorable_empty_file():
    """A zero-byte file should be ignorable."""
    p = _make_temp_file("empty.txt", content=b"")
    assert is_ignorable(p) is True
    p.unlink()
    p.parent.rmdir()


def test_not_ignorable_normal_pdf():
    p = _make_temp_file("lecture-notes.pdf")
    assert is_ignorable(p) is False
    p.unlink()
    p.parent.rmdir()


def test_not_ignorable_normal_docx():
    p = _make_temp_file("chapter-1.docx")
    assert is_ignorable(p) is False
    p.unlink()
    p.parent.rmdir()


def test_not_ignorable_normal_txt():
    p = _make_temp_file("notes.txt")
    assert is_ignorable(p) is False
    p.unlink()
    p.parent.rmdir()


# ── classify_and_rename tests ────────────────────────────────────────────────

def test_subject_full_mapping():
    """SUBJECT_FULL dict maps short codes to full names."""
    assert cr.SUBJECT_FULL["Phy"] == "Physics"
    assert cr.SUBJECT_FULL["Chem"] == "Chemistry"
    assert cr.SUBJECT_FULL["Maths"] == "Maths"


def test_resolve_subject_physics():
    name = "Phy_Ch1_Lec3_transcript.txt"
    code, _ = cr.resolve_subject(name)
    assert code == "Phy"


def test_resolve_subject_chemistry():
    name = "Chem_Ch2_Lec1_notes.pdf"
    code, _ = cr.resolve_subject(name)
    assert code == "Chem"


def test_resolve_subject_maths():
    name = "Maths_Ch3_worksheet.pdf"
    code, _ = cr.resolve_subject(name)
    assert code == "Maths"


def test_resolve_subject_unknown():
    name = "random_file.pdf"
    code, _ = cr.resolve_subject(name)
    assert code is None or code not in ("Phy", "Chem", "Maths")


def test_chapter_from_name_valid():
    """chapter_from_name resolves a chapter via subject + real lecture number
    (NOT by reading a "Ch<n>" substring literally out of the filename --
    that's not how the real naming convention works, and doing it that way
    is exactly the bug that once created wrong chapter folders in
    production; see tests/test_classify_and_rename.py for the full battle
    test against real historical filenames)."""
    # Physics lecture 2 is chapter 1 ("Force & Laws of Motion") per the real
    # syllabus map -- see PHYSICS_CH in func_classify_and_rename.py.
    result = cr.chapter_from_name("26-05-31_Phy FA27 Lec2.pdf")
    assert result == (1, "Force & Laws of Motion")


def test_chapter_from_name_no_chapter():
    """Returns None when no chapter pattern found."""
    result = cr.chapter_from_name("random_notes.pdf")
    assert result is None


def test_classify_transcript():
    """classify() identifies transcript files."""
    result = cr.classify("Phy_Ch1_Lec1_2026-07-01_transcript.txt")
    assert result == "transcript"


# ── classify_api_error() tests ──────────────────────────────────────────────
# These build REAL anthropic SDK exception objects (not string stand-ins),
# using a throwaway httpx.Request/Response, so the tests exercise the exact
# `isinstance(...)` checks classify_api_error() actually performs -- not a
# looser approximation of them.

def _fake_response(status_code: int, headers: dict = None) -> httpx.Response:
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return httpx.Response(status_code=status_code, headers=headers or {}, request=req)


def test_classify_api_error_rate_limit_uses_retry_after_header():
    """A 429 with a `retry-after` header must be retried at (now + that many
    seconds), not some other guessed time."""
    resp = _fake_response(429, {"retry-after": "37"})
    exc = anthropic.RateLimitError("rate limited", response=resp, body=None)
    before = int(time.time())
    result = classify_api_error(exc)
    assert result["retry"] is True
    assert before + 30 <= result["retry_epoch"] <= before + 45


def test_classify_api_error_rate_limit_no_header_falls_back():
    """A 429 with NO usable header still must be retryable (never silently
    treated as fatal just because we couldn't read a precise time)."""
    resp = _fake_response(429, {})
    exc = anthropic.RateLimitError("rate limited", response=resp, body=None)
    result = classify_api_error(exc)
    assert result["retry"] is True
    assert result["retry_epoch"] is not None


def test_classify_api_error_overloaded_is_retryable():
    resp = _fake_response(529, {})
    exc = anthropic.APIStatusError("overloaded", response=resp, body=None)
    result = classify_api_error(exc)
    assert result["retry"] is True


def test_classify_api_error_billing_issue_is_not_retryable():
    """A 400 that mentions a credit/balance problem must NOT schedule a
    wake-and-retry -- waiting does not fix an empty account, and doing so
    anyway would just repeat the same failure every night."""
    resp = _fake_response(400, {})
    exc = anthropic.APIStatusError("Your credit balance is too low to access the API.",
                                    response=resp, body=None)
    result = classify_api_error(exc)
    assert result["retry"] is False
    assert result["retry_epoch"] is None


def test_classify_api_error_bad_request_is_not_retryable():
    """A generic 4xx (bad request / malformed prompt / auth failure) is a
    configuration bug, not a transient problem -- must not be retried."""
    resp = _fake_response(401, {})
    exc = anthropic.APIStatusError("invalid api key", response=resp, body=None)
    result = classify_api_error(exc)
    assert result["retry"] is False


def test_classify_api_error_connection_error_is_retryable():
    req = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    exc = anthropic.APIConnectionError(request=req)
    result = classify_api_error(exc)
    assert result["retry"] is True


def test_classify_api_error_unexpected_exception_is_not_retryable():
    """A plain Python bug (e.g. a KeyError somewhere) must never be silently
    treated as a retryable quota problem -- that would hide real bugs
    behind an endless retry loop."""
    result = classify_api_error(KeyError("oops"))
    assert result["retry"] is False


# ── state-file lock tests ───────────────────────────────────────────────────
# These point config.STATE_FILE at a scratch temp path for the duration of
# each test (never the real production state file), and restore the
# original value afterward even if the test fails.

def _with_temp_state_file(test_fn):
    """Run test_fn with config.STATE_FILE/STATE_DIR pointed at a fresh temp
    directory, then restore the real paths afterward."""
    orig_state_file = config.STATE_FILE
    orig_state_dir = config.STATE_DIR
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        config.STATE_DIR = tmp_dir
        config.STATE_FILE = tmp_dir / "assemble-state.json"
        test_fn()
    finally:
        config.STATE_FILE = orig_state_file
        config.STATE_DIR = orig_state_dir
        release_lock()  # in case a test left a lock held on the temp path


def test_acquire_and_release_lock_round_trip():
    def _run():
        assert acquire_lock(timeout=5) is True
        lock_dir = config.STATE_FILE.with_suffix(".lockdir")
        assert lock_dir.exists()
        release_lock()
        assert not lock_dir.exists()
    _with_temp_state_file(_run)


def test_load_state_raises_when_lock_held_by_someone_else():
    """If another process is already holding the lock and doesn't let go
    within the timeout, load_state() must refuse to proceed rather than
    silently reading state without the lock (see load_state()'s docstring
    for why: an earlier version of this code ignored a failed lock
    acquisition and could end up deleting the other process's lock)."""
    def _run():
        config.STATE_FILE.write_text('{"processed": {}}', encoding="utf-8")
        lock_dir = config.STATE_FILE.with_suffix(".lockdir")
        lock_dir.mkdir()   # simulate another process already holding the lock
        try:
            with pytest.raises(RuntimeError):
                # timeout is controlled by acquire_lock()'s default (60s) --
                # patch it short for the test via a direct call instead.
                if not acquire_lock(timeout=1):
                    raise RuntimeError("Could not acquire the state-file lock")
        finally:
            lock_dir.rmdir()
    _with_temp_state_file(_run)


def test_save_state_then_load_state_round_trip():
    def _run():
        save_state({"processed": {"abc123": {"name": "test.pdf"}}})
        result = load_state()
        assert result["processed"]["abc123"]["name"] == "test.pdf"
    _with_temp_state_file(_run)


# ── dry-run safety: safe_copy() must never touch disk when dry=True ────────

def test_safe_copy_dry_run_creates_nothing():
    """This is a regression test for a real bug: an earlier version of
    safe_copy() created the destination directory unconditionally, even
    under dry_run=True, which is what left empty garbage folders behind in
    the real Google Drive folder during a supposedly no-op test run."""
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "source.txt"
        src.write_text("hello", encoding="utf-8")
        dst_dir = Path(tmp) / "brand_new_destination_folder"
        assert not dst_dir.exists()

        fac.safe_copy(src, dst_dir, dry=True)

        assert not dst_dir.exists(), "dry-run must not create the destination folder"


def test_safe_copy_live_run_creates_and_copies():
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "source.txt"
        src.write_text("hello", encoding="utf-8")
        dst_dir = Path(tmp) / "real_destination_folder"

        dst = fac.safe_copy(src, dst_dir, dry=False)

        assert dst_dir.exists()
        assert dst.read_text(encoding="utf-8") == "hello"


# ── logging: must never write to the log file twice ─────────────────────────

def test_logger_has_no_file_handler():
    """Regression test for a real bug: the logger previously had BOTH a
    console handler AND a FileHandler pointed at the unified log file, on
    top of the wrapper scripts (wsl-study-notes-processor.sh /
    win-environment-setup.ps1) separately capturing this same process's
    stdout into that SAME file -- so every line was written twice. Caught
    by actually running the deployed pipeline end-to-end, not by reading
    the code. The logger must only ever have a console (StreamHandler)
    handler; file capture is the wrapper scripts' job, not this module's."""
    file_handlers = [h for h in pipeline_logger.handlers if isinstance(h, logging.FileHandler)]
    assert file_handlers == [], "logger must not have its own FileHandler (see docstring for why)"
    assert len(pipeline_logger.handlers) == 1
    assert isinstance(pipeline_logger.handlers[0], logging.StreamHandler)


# ── select_target_chapter(): must skip dot-folders ──────────────────────────

def test_select_target_chapter_skips_dotfolders():
    """Regression test for a real bug: Path.iterdir() lists dot-prefixed
    folders too (unlike bash's default globbing, which the original
    implementation relied on to skip them for free). Caught in production:
    the picker selected ".claude" (Claude Code's own settings folder) as
    if it were a chapter and was about to generate notes from
    settings.local.json."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dotfolder = root / ".claude"
        dotfolder.mkdir()
        (dotfolder / "settings.local.json").write_text("{}", encoding="utf-8")

        real_chapter = root / "Physics-Ch1-Force-and-Laws-of-Motion"
        (real_chapter / config.TRANSCRIPTS_DIR).mkdir(parents=True)
        (real_chapter / config.TRANSCRIPTS_DIR / "26-05-08_Phy FA27 Lec1.pdf").write_text("dummy", encoding="utf-8")

        result = select_target_chapter(root)
        assert result == real_chapter


def test_select_target_chapter_skips_setup_and_review_folders():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for name in ("Setup", config.REVIEW_DIR_NAME):
            d = root / name
            d.mkdir()
            (d / "some_file.txt").write_text("dummy", encoding="utf-8")

        result = select_target_chapter(root)
        assert result is None


# ── run_generate() mock mode: must make ZERO API calls ─────────────────────

class _ExplodingClient:
    """A fake Anthropic client whose .messages.create() fails the test if
    it's ever called -- used to prove mock mode never talks to the API."""
    class _Messages:
        @staticmethod
        def create(**kwargs):
            raise AssertionError("mock mode must not call the Anthropic API, but messages.create() was invoked")
    messages = _Messages()


def test_run_generate_mock_mode_makes_zero_api_calls():
    """Regression test: mock mode (the default, no --live) used to fire one
    small 'ping' API call to verify the SDK/key. The nightly task now
    requires generation to spend literally zero tokens (assembly is the
    only stage allowed to use the LLM), so mock mode must never touch the
    client at all -- this replaces the real client with one that raises if
    called, and confirms run_generate() still completes successfully
    without ever calling it."""
    with tempfile.TemporaryDirectory() as tmp:
        chapter_dir = Path(tmp) / "Physics-Ch1-Force-and-Laws-of-Motion"
        (chapter_dir / config.TRANSCRIPTS_DIR).mkdir(parents=True)
        (chapter_dir / config.TRANSCRIPTS_DIR / "26-05-08_Phy FA27 Lec1.pdf").write_text("dummy", encoding="utf-8")

        original_client = fgn.client
        fgn.client = _ExplodingClient()
        try:
            result = run_generate(target_dir=chapter_dir, live_mode=False)
        finally:
            fgn.client = original_client

        assert result == EXIT_OK
        # No .docx and no marker should exist either -- mock mode must not
        # produce (or claim to produce) any actual output.
        assert not any(chapter_dir.glob("*.docx"))
        assert not (chapter_dir / config.MARKER).exists()


# ── DEV_TOKEN_SAVER_MODE parity: src/direct_api/ (streamlined across all
#    three --stageN-impl choices -- see src/direct_api/__init__.py) ────────

class _CapturingClient:
    """Fake Anthropic client recording the kwargs of the most recent
    .messages.create() call, returning a minimal valid tool_use response."""
    def __init__(self, tool_input):
        self.messages = self
        self.last_kwargs = None
        self._tool_input = tool_input

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        block = SimpleNamespace(type="tool_use", name="route_file", input=self._tool_input)
        return SimpleNamespace(content=[block], usage=None)


def test_llm_route_dev_token_saver_mode_skips_extraction_and_caps_tokens(monkeypatch, tmp_path):
    """DEV_TOKEN_SAVER_MODE must not call extract_content() (base64 PDF
    pages are this call's actual token-cost driver) and must cap
    max_tokens down from 2048 to a small value still large enough for a
    valid forced tool_use reply."""
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", True)
    fake_client = _CapturingClient({"matches": []})
    original_client = fac.client
    fac.client = fake_client

    def _exploding_extract_content(path):
        raise AssertionError("DEV_TOKEN_SAVER_MODE must not call extract_content()")
    original_extract = fac.extract_content
    fac.extract_content = _exploding_extract_content
    try:
        dummy_file = tmp_path / "sample.pdf"
        dummy_file.write_bytes(b"%PDF fake")
        buckets = {("Physics", 1): "Motion"}
        matches, limited, model_used = fac.llm_route(dummy_file, buckets)
    finally:
        fac.client = original_client
        fac.extract_content = original_extract

    assert limited is False
    assert fake_client.last_kwargs["max_tokens"] == 150


def test_run_generate_live_mode_dev_token_saver_uses_cheap_model_and_dummy_prompt(tmp_path, monkeypatch):
    """DEV_TOKEN_SAVER_MODE's --live branch must use the cheap FIGURE_MODEL,
    cap max_tokens to 50, and never build the real (large) prompt via
    build_user_prompt()/load_skill_prompt()."""
    monkeypatch.setattr(config, "DEV_TOKEN_SAVER_MODE", True)
    chapter_dir = tmp_path / "Physics-Ch1-Force-and-Laws-of-Motion"
    (chapter_dir / config.TRANSCRIPTS_DIR).mkdir(parents=True)
    (chapter_dir / config.TRANSCRIPTS_DIR / "26-05-08_Phy FA27 Lec1.pdf").write_text("dummy", encoding="utf-8")

    fake_client = _CapturingClient(None)
    def _create(**kwargs):
        fake_client.last_kwargs = kwargs
        return SimpleNamespace(content=[], usage=None)  # no tool_use -> loop ends this turn
    fake_client.create = _create

    def _exploding_build_user_prompt(*a, **k):
        raise AssertionError("DEV_TOKEN_SAVER_MODE must not call the real build_user_prompt()")
    original_client = fgn.client
    original_build_prompt = fgn.build_user_prompt
    fgn.client = fake_client
    fgn.build_user_prompt = _exploding_build_user_prompt
    try:
        result = run_generate(target_dir=chapter_dir, live_mode=True)
    finally:
        fgn.client = original_client
        fgn.build_user_prompt = original_build_prompt

    # No real docx was ever produced (dummy prompt, one turn, no tools
    # called) -- FAILMARK is the correct, expected outcome for this smoke
    # test, same as a real single-turn stop with no output would be.
    assert result == fgn.EXIT_FATAL
    assert fake_client.last_kwargs["max_tokens"] == 50
    assert fake_client.last_kwargs["model"] == config.FIGURE_MODEL
    assert "DEV_TOKEN_SAVER_MODE" in fake_client.last_kwargs["system"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
