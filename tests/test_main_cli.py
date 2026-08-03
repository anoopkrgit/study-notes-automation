"""
test_main_cli.py

Battle-tests for main.py's flag-combination logic (resolve_stage_mode()) --
the part of the CLI that decides, from each --stageN-mode choice, whether
that stage runs at all, whether it uses the LLM, and whether it runs in the
cheap DEV_TOKEN_SAVER_MODE smoke-test mode. This is exactly the logic that
turns e.g. "--stage1-mode llm-full --stage2-mode no-llm" into "assemble runs
WITH the LLM at full cost, generate runs WITHOUT it" -- and that the two
stages' modes (including DEV_TOKEN_SAVER_MODE) are fully independent of
each other.
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import pytest

from src.main import build_parser, resolve_stage_mode


def _parse(argv):
    return build_parser().parse_args(argv)


# ── resolve_stage_mode(): the core per-stage decision logic ────────────────

def test_off_mode_returns_none():
    assert resolve_stage_mode("off") is None


def test_no_llm_mode_returns_false_false():
    assert resolve_stage_mode("no-llm") == (False, False)


def test_llm_token_saver_mode_returns_true_true():
    assert resolve_stage_mode("llm-token-saver") == (True, True)


def test_llm_full_mode_returns_true_false():
    assert resolve_stage_mode("llm-full") == (True, False)


def test_invalid_mode_raises():
    with pytest.raises(ValueError):
        resolve_stage_mode("not-a-real-mode")


# ── argparse defaults and choices ───────────────────────────────────────────

def test_default_mode_is_off_for_both_stages():
    args = _parse([])
    assert args.stage1_mode == "off"
    assert args.stage2_mode == "off"


def test_invalid_mode_choice_rejected_by_argparse():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["--stage1-mode", "bogus"])
    assert exc_info.value.code == 2


# ── End-to-end flag parsing: the exact examples from real usage ────────────

def test_nightly_policy_combo_parses_correctly():
    """--stage1-mode llm-full --stage2-mode no-llm: assemble WITH the LLM
    at full cost, generate WITHOUT it -- the exact combination the nightly
    3 AM task uses by default."""
    args = _parse(["--stage1-mode", "llm-full", "--stage2-mode", "no-llm"])
    assert resolve_stage_mode(args.stage1_mode) == (True, False)
    assert resolve_stage_mode(args.stage2_mode) == (False, False)


def test_both_stages_llm_full_parses_as_a_real_paid_run():
    args = _parse(["--stage1-mode", "llm-full", "--stage2-mode", "llm-full"])
    assert resolve_stage_mode(args.stage1_mode) == (True, False)
    assert resolve_stage_mode(args.stage2_mode) == (True, False)


def test_assemble_only_leaves_generate_off():
    args = _parse(["--stage1-mode", "no-llm"])
    assert resolve_stage_mode(args.stage1_mode) == (False, False)
    assert resolve_stage_mode(args.stage2_mode) is None


def test_mixed_token_saver_combo_is_independent_per_stage():
    """The whole point of folding --dev-token-saver into the per-stage mode
    choice: Stage 1 can run at full real cost while Stage 2 runs in the
    cheap smoke-test mode, in ONE invocation -- the old global
    --dev-token-saver flag could not express this (it toggled
    DEV_TOKEN_SAVER_MODE identically for both stages at once)."""
    args = _parse(["--stage1-mode", "llm-full", "--stage2-mode", "llm-token-saver"])
    stage1 = resolve_stage_mode(args.stage1_mode)
    stage2 = resolve_stage_mode(args.stage2_mode)
    assert stage1 == (True, False)   # Stage 1: with LLM, NOT token-saver
    assert stage2 == (True, True)    # Stage 2: with LLM, token-saver ON
    assert stage1[1] != stage2[1]    # the two stages' token-saver settings differ


def test_stage2_impl_subprocess_no_longer_fails_fast():
    from src.main import build_parser
    parser = build_parser()
    args = parser.parse_args(["--stage2-mode", "off", "--stage2-impl", "subprocess"])
    assert args.stage2_impl == "subprocess"

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
