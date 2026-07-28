"""
test_main_cli.py

Battle-tests for main.py's flag-combination logic (resolve_stage()) --
the part of the CLI that decides, from the --run-* flags, whether each
stage runs at all and whether it uses the LLM. This is exactly the logic
that turns e.g. "--run-assemble --run-generate-no-llm" into "assemble runs
WITH the LLM, generate runs WITHOUT it" -- and rejects contradictory flag
combinations (like asking for a stage both with and without the LLM at
once) instead of silently picking one.
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import pytest

from src.main import build_parser, resolve_stage


def _parse(argv):
    return build_parser().parse_args(argv)


# ── resolve_stage(): the core per-stage decision logic ──────────────────────

def test_stage_not_requested_returns_none():
    parser = build_parser()
    assert resolve_stage(parser, "assemble", with_flag=False, without_flag=False, both_flag=False) is None


def test_stage_with_llm_flag_returns_true():
    parser = build_parser()
    assert resolve_stage(parser, "assemble", with_flag=True, without_flag=False, both_flag=False) is True


def test_stage_no_llm_flag_returns_false():
    parser = build_parser()
    assert resolve_stage(parser, "generate", with_flag=False, without_flag=True, both_flag=False) is False


def test_both_flag_implies_with_llm():
    parser = build_parser()
    assert resolve_stage(parser, "assemble", with_flag=False, without_flag=False, both_flag=True) is True
    assert resolve_stage(parser, "generate", with_flag=False, without_flag=False, both_flag=True) is True


def test_conflicting_with_and_without_flags_exits():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        resolve_stage(parser, "assemble", with_flag=True, without_flag=True, both_flag=False)
    assert exc_info.value.code == 2   # argparse's standard usage-error exit code


def test_run_both_conflicts_with_explicit_no_llm():
    """--run-both means WITH the LLM for both stages, so pairing it with
    --run-generate-no-llm for the SAME stage is a contradiction and must be
    rejected, not silently resolved one way or the other."""
    parser = build_parser()
    with pytest.raises(SystemExit) as exc_info:
        resolve_stage(parser, "generate", with_flag=False, without_flag=True, both_flag=True)
    assert exc_info.value.code == 2


# ── End-to-end flag parsing: the exact examples from real usage ────────────

def test_nightly_policy_combo_parses_correctly():
    """--run-assemble --run-generate-no-llm: assemble WITH the LLM, generate
    WITHOUT it -- this is the exact combination the nightly 3 AM task uses."""
    parser = build_parser()
    args = _parse(["--run-assemble", "--run-generate-no-llm"])
    assert resolve_stage(parser, "assemble", args.run_assemble, args.run_assemble_no_llm, args.run_both) is True
    assert resolve_stage(parser, "generate", args.run_generate, args.run_generate_no_llm, args.run_both) is False


def test_run_both_parses_as_both_stages_with_llm():
    parser = build_parser()
    args = _parse(["--run-both"])
    assert resolve_stage(parser, "assemble", args.run_assemble, args.run_assemble_no_llm, args.run_both) is True
    assert resolve_stage(parser, "generate", args.run_generate, args.run_generate_no_llm, args.run_both) is True


def test_assemble_only_leaves_generate_unset():
    parser = build_parser()
    args = _parse(["--run-assemble-no-llm"])
    assert resolve_stage(parser, "assemble", args.run_assemble, args.run_assemble_no_llm, args.run_both) is False
    assert resolve_stage(parser, "generate", args.run_generate, args.run_generate_no_llm, args.run_both) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
