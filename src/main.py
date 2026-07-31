"""
main.py

Master CLI entry point for the Study Notes Automation Pipeline.

Flags (combine freely -- see `python3 main.py --help` for the full picture):
  --stage1-mode {off,no-llm,llm-token-saver,llm-full}   Stage 1: whether/how it spends (default: off)
  --stage2-mode {off,no-llm,llm-token-saver,llm-full}   Stage 2: whether/how it spends (default: off)
  --stage1-impl {legacy,graph,subprocess}               which Stage 1 implementation runs (default: legacy)
  --stage2-impl {legacy,graph,subprocess}               which Stage 2 implementation runs (default: legacy)
  --doctor                                              environment health check, then exit
"""

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import settings as config
from src.func_tools_and_utils import logger, EXIT_OK, EXIT_FATAL
from src.direct_api.func_assemble_chapters import run_assemble
from src.agents.dispatch import generate_notes as run_generate

def run_doctor():
    """Environment health check diagnostics."""
    logger.info("=== Pipeline Health Check (Doctor Pass) ===")
    
    # 1. Local Runtime Check
    logger.info(f"Local runtime root: {config.LOCAL_RUNTIME_ROOT}")
    logger.info(f"  - Log Dir exists: {config.LOG_DIR.exists()}")
    logger.info(f"  - State Dir exists: {config.STATE_DIR.exists()}")

    # 2. Source Paths
    logger.info(f"Source Directories:")
    logger.info(f"  - Transcripts: {config.DEFAULT_TRANSCRIPT_SRC} (exists: {config.DEFAULT_TRANSCRIPT_SRC.exists()})")
    logger.info(f"  - Collected Materials: {config.DEFAULT_COLLECTED_SRC} (exists: {config.DEFAULT_COLLECTED_SRC.exists()})")
    logger.info(f"  - Target Root: {config.DEFAULT_TARGET_ROOT} (exists: {config.DEFAULT_TARGET_ROOT.exists()})")

    # 3. Environment Keys
    # ANTHROPIC_API_KEY is the only key this pipeline actually uses (both
    # the Stage 1 content router and the Stage 2 generator call the
    # Anthropic API directly). It's normally loaded from ~/.anthropic_env
    # by func_assemble_chapters.py / func_generate_notes.py at import time
    # (via python-dotenv), so by the time doctor() runs it should already
    # be present in os.environ if that file exists and is readable.
    has_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    logger.info(f"API Keys:")
    logger.info(f"  - ANTHROPIC_API_KEY: {'[SET]' if has_anthropic else '[MISSING]'}")

    # 4. System Binaries
    # pdftoppm is required by the new visual-QA tools (tool_view_pdf_page,
    # tool_convert_to_png) as well as the diagram-conversion pipeline the
    # skill documents -- without it, diagram QA and page-by-page accuracy
    # checking silently can't run.
    import shutil
    for tool in ("node", "soffice", "pdftoppm", "python3", "python"):
        path = shutil.which(tool)
        logger.info(f"  - Command '{tool}': {'[AVAILABLE]' if path else '[NOT FOUND]'}")

    # 5. Node "docx" package (the library the generator's Node.js build
    # script uses to actually produce the .docx file -- see the skill's
    # Formatting section). This is a GLOBAL npm package, not something
    # requirements.txt (which only lists Python packages) can capture.
    import subprocess
    try:
        result = subprocess.run(["npm", "ls", "-g", "docx"], capture_output=True, text=True, timeout=15)
        has_docx_pkg = "docx@" in result.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired):
        has_docx_pkg = False
    logger.info(f"  - Node package 'docx' (global): {'[AVAILABLE]' if has_docx_pkg else '[NOT FOUND] (install with: npm install -g docx)'}")

    logger.info("Doctor diagnostics check finished.")
    return EXIT_OK

STAGE_MODE_CHOICES = ["off", "no-llm", "llm-token-saver", "llm-full"]


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI's flag definitions.

    DESIGN, for readers new to this project: each stage (assemble,
    generate) is controlled by ONE 4-state mode flag, answering three
    questions in a single choice instead of three separate flags:
      1. Should this stage run at all?
      2. If it runs, should it use the LLM (spend tokens/CLI cost) or not?
      3. If it uses the LLM, should it run in the cheap DEV_TOKEN_SAVER_MODE
         smoke-test mode (dummy prompts/content, capped output, cheapest
         model, no escalation), or a real full-cost run?

      --stage1-mode off               don't run Stage 1 at all (default)
      --stage1-mode no-llm            deterministic filename routing only, zero LLM calls
      --stage1-mode llm-token-saver   WITH the LLM, cheap smoke-test mode
      --stage1-mode llm-full          WITH the LLM, real cost

      --stage2-mode off               don't run Stage 2 at all (default)
      --stage2-mode no-llm            zero-token preview only, no .docx produced
      --stage2-mode llm-token-saver   WITH the LLM, cheap smoke-test mode
      --stage2-mode llm-full          WITH the LLM, real cost -- actually produces the .docx

    Each stage's mode is fully independent. Earlier versions of this CLI had
    a single global --dev-token-saver flag that toggled the same cost-safety
    behavior for BOTH stages at once -- that meant "real Stage 1, but
    smoke-test Stage 2" (or vice versa) couldn't be expressed in one
    invocation. Folding the toggle into each stage's own mode choice removes
    that coupling: --stage1-mode llm-full --stage2-mode llm-token-saver runs
    a real (cheap-anyway, Haiku) Stage 1 pass while smoke-testing Stage 2's
    wiring for pennies, in one command.

    Examples:
      --stage1-mode llm-full --stage2-mode no-llm
          -> the nightly policy: assemble WITH the LLM, generate WITHOUT it
      --stage1-mode llm-full --stage2-mode llm-full
          -> a full, real, paid run of both stages
      --stage1-mode llm-token-saver --stage2-mode llm-token-saver
          -> cheap end-to-end smoke test of both stages' wiring
    """
    parser = argparse.ArgumentParser(
        description="Study Notes Automation Pipeline CLI",
        epilog="Example -- the nightly policy (assemble WITH the LLM, generate WITHOUT it):\n"
               "  python3 src/main.py --stage1-mode llm-full --stage2-mode no-llm\n"
               "Example -- a full real run (both stages spend tokens):\n"
               "  python3 src/main.py --stage1-mode llm-full --stage2-mode llm-full\n"
               "Example -- cheap smoke test of both stages' wiring:\n"
               "  python3 src/main.py --stage1-mode llm-token-saver --stage2-mode llm-token-saver",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--stage1-mode", choices=STAGE_MODE_CHOICES, default="off",
                         help="Stage 1 (assemble/route files): 'off' don't run (default), 'no-llm' "
                              "deterministic filename routing only, 'llm-token-saver' cheap smoke-test "
                              "mode (real API/CLI call, near-zero cost, dummy content), 'llm-full' real "
                              "routing at full cost")
    parser.add_argument("--stage2-mode", choices=STAGE_MODE_CHOICES, default="off",
                         help="Stage 2 (generate notes): 'off' don't run (default), 'no-llm' zero-token "
                              "preview only, no .docx produced, 'llm-token-saver' cheap smoke-test mode "
                              "(real API/CLI call, near-zero cost, dummy prompt, no usable output), "
                              "'llm-full' real generation at full cost -- actually produces the .docx")
    parser.add_argument("--doctor", action="store_true",
                         help="Run environment health check diagnostics and exit (ignores every other flag)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Assemble stage only: show planned actions without modifying any files")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--quiet", action="store_true", help="Suppress INFO logs; show only warnings and errors")
    parser.add_argument("--stage1-impl", choices=["legacy", "graph", "subprocess"], default="legacy",
                         help="Stage 1 implementation: 'legacy' single-call SDK router (default), 'graph' "
                              "multi-step Triage/Extraction flowchart (src/agents/stage1_graph.py), or "
                              "'subprocess' -- the `claude` CLI billed via Claude subscription "
                              "(src/claude_cli_subprocess/stage1.py)")
    parser.add_argument("--stage2-impl", choices=["legacy", "graph", "subprocess"], default="legacy",
                         help="Stage 2 implementation: 'legacy' monolithic loop (default), 'graph' "
                              "multi-agent flowchart (src/agents/stage2_graph.py), or 'subprocess' -- "
                              "the `claude` CLI billed via Claude subscription "
                              "(src/claude_cli_subprocess/stage2.py)")
    return parser


def resolve_stage_mode(mode: str):
    """Translate one --stageN-mode choice into (with_llm, token_saver), or
    None if that stage shouldn't run at all.

      "off"              -> None (don't run this stage)
      "no-llm"            -> (False, False)
      "llm-token-saver"   -> (True, True)   -- with_llm, DEV_TOKEN_SAVER_MODE on
      "llm-full"          -> (True, False)  -- with_llm, real full-cost run
    """
    if mode == "off":
        return None
    if mode == "no-llm":
        return (False, False)
    if mode == "llm-token-saver":
        return (True, True)
    if mode == "llm-full":
        return (True, False)
    raise ValueError(f"unknown --stageN-mode value: {mode!r}")  # unreachable: argparse choices= already validates


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.quiet:
        logger.setLevel(logging.WARNING)
    elif args.verbose:
        logger.setLevel(logging.DEBUG)

    if args.doctor:
        sys.exit(run_doctor())

    stage1 = resolve_stage_mode(args.stage1_mode)
    stage2 = resolve_stage_mode(args.stage2_mode)

    if stage1 is None and stage2 is None:
        parser.print_help()
        sys.exit(EXIT_OK)

    exit_code = EXIT_OK
    if stage1 is not None:
        with_llm, token_saver = stage1
        # Set directly on the already-imported config module rather than via
        # os.environ -- settings.py reads DEV_TOKEN_SAVER_MODE from the
        # environment only once, at import time (which has already happened
        # by now), so an env var set here would be read too late. Set fresh
        # for EACH stage right before that stage runs, since the two stages'
        # modes are independent -- e.g. Stage 1 llm-full, Stage 2
        # llm-token-saver must not leak Stage 1's setting into Stage 2.
        config.DEV_TOKEN_SAVER_MODE = token_saver
        if token_saver:
            logger.info("Stage 1: DEV_TOKEN_SAVER_MODE enabled via --stage1-mode llm-token-saver "
                        "(dummy content, capped tokens, cheapest model, no escalation).")
        exit_code = run_assemble(dry_run=args.dry_run, no_llm=not with_llm, verbose=args.verbose,
                                  stage1_impl=args.stage1_impl)
        if exit_code != EXIT_OK:
            # Assembly failed: don't proceed to generation even if it was
            # also requested in this same command -- generating notes from
            # a chapter folder that assembly failed to finish setting up
            # would be working from incomplete/stale input.
            logger.error("Assembly stage failed; skipping generation.")
            sys.exit(exit_code)

    if stage2 is not None:
        with_llm, token_saver = stage2
        config.DEV_TOKEN_SAVER_MODE = token_saver
        if token_saver:
            logger.info("Stage 2: DEV_TOKEN_SAVER_MODE enabled via --stage2-mode llm-token-saver "
                        "(dummy prompt, capped output, cheapest model).")
        exit_code = run_generate(live_mode=with_llm, verbose=args.verbose, impl=args.stage2_impl)

    sys.exit(exit_code)

if __name__ == "__main__":
    main()
