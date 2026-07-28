"""
main.py

Master CLI entry point for the Study Notes Automation Pipeline.

Flags (combine freely -- see `python3 main.py --help` for the full picture):
  --run-assemble / --run-assemble-no-llm   Stage 1, with/without the LLM router
  --run-generate / --run-generate-no-llm   Stage 2, with/without the LLM (tokens)
  --run-both                               shorthand: both stages, both WITH the LLM
  --doctor                                 environment health check, then exit
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
from src.func_assemble_chapters import run_assemble
from src.func_generate_notes import run_generate

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

def build_parser() -> argparse.ArgumentParser:
    """Build the CLI's flag definitions.

    DESIGN, for readers new to this project: there are two SEPARATE
    questions for each of the two stages (assemble, generate):
      1. Should this stage run at all?
      2. If it runs, should it use the LLM (spend tokens) or not?

    Older versions of this CLI answered both questions with a single
    subcommand name (e.g. a "nightly" command that always ran both stages,
    with a couple of flags bolted on) -- which made it hard to express "run
    assemble WITH the LLM, but generate WITHOUT it" in one obvious command.
    This version answers question 1 and question 2 with SEPARATE flags per
    stage, which can be freely combined in one invocation:

      --run-assemble            assemble runs, WITH the LLM
      --run-assemble-no-llm     assemble runs, WITHOUT the LLM
      --run-generate            generate runs, WITH the LLM (spends tokens, produces a .docx)
      --run-generate-no-llm     generate runs, WITHOUT the LLM (zero-token preview only)
      --run-both                shorthand for "--run-assemble --run-generate"
                                 (both stages, both WITH the LLM)

    Any flag you don't pass for a given stage means "don't run that stage
    at all". Examples:
      --run-assemble --run-generate-no-llm
          -> assemble WITH the LLM, generate WITHOUT it (the nightly policy)
      --run-assemble-no-llm
          -> ONLY assemble runs, and without the LLM; generate doesn't run
      --run-both
          -> both stages run, both WITH the LLM (a full, real, paid run)
    """
    parser = argparse.ArgumentParser(
        description="Study Notes Automation Pipeline CLI",
        epilog="Example -- the nightly policy (assemble WITH the LLM, generate WITHOUT it):\n"
               "  python3 src/main.py --run-assemble --run-generate-no-llm\n"
               "Example -- a full real run (both stages spend tokens):\n"
               "  python3 src/main.py --run-both",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--run-assemble", action="store_true",
                         help="Run Stage 1 (assemble) WITH the LLM content router")
    parser.add_argument("--run-assemble-no-llm", action="store_true",
                         help="Run Stage 1 (assemble) WITHOUT the LLM (deterministic filename routing only)")
    parser.add_argument("--run-generate", action="store_true",
                         help="Run Stage 2 (generate) WITH the LLM -- spends tokens, actually produces the .docx")
    parser.add_argument("--run-generate-no-llm", action="store_true",
                         help="Run Stage 2 (generate) WITHOUT the LLM -- zero-token preview, no .docx produced")
    parser.add_argument("--run-both", action="store_true",
                         help="Shorthand for --run-assemble --run-generate (both stages, both WITH the LLM)")
    parser.add_argument("--doctor", action="store_true",
                         help="Run environment health check diagnostics and exit (ignores every other flag)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Assemble stage only: show planned actions without modifying any files")
    parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
    parser.add_argument("--quiet", action="store_true", help="Suppress INFO logs; show only warnings and errors")
    return parser


def resolve_stage(parser: argparse.ArgumentParser, stage_name: str, with_flag: bool, without_flag: bool, both_flag: bool):
    """Work out whether ONE stage should run, and if so, with the LLM or not.

    Returns:
      True  -- run this stage, WITH the LLM
      False -- run this stage, WITHOUT the LLM
      None  -- don't run this stage at all (no flag for it was given)

    `parser.error(...)` prints a usage error and exits the process (this is
    argparse's own standard way of reporting a bad combination of flags --
    the same mechanism it uses for its own built-in validation), used here
    when the flags for a stage contradict each other (e.g. asking to run
    the SAME stage both with and without the LLM at once).
    """
    requested_with = with_flag or both_flag
    if requested_with and without_flag:
        parser.error(
            f"--run-{stage_name} (or --run-both) and --run-{stage_name}-no-llm "
            f"both apply to {stage_name} but disagree on whether to use the LLM -- pick one."
        )
    if requested_with:
        return True
    if without_flag:
        return False
    return None


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.quiet:
        logger.setLevel(logging.WARNING)
    elif args.verbose:
        logger.setLevel(logging.DEBUG)

    if args.doctor:
        sys.exit(run_doctor())

    assemble_with_llm = resolve_stage(parser, "assemble", args.run_assemble, args.run_assemble_no_llm, args.run_both)
    generate_with_llm = resolve_stage(parser, "generate", args.run_generate, args.run_generate_no_llm, args.run_both)

    if assemble_with_llm is None and generate_with_llm is None:
        parser.print_help()
        sys.exit(EXIT_OK)

    exit_code = EXIT_OK
    if assemble_with_llm is not None:
        exit_code = run_assemble(dry_run=args.dry_run, no_llm=not assemble_with_llm, verbose=args.verbose)
        if exit_code != EXIT_OK:
            # Assembly failed: don't proceed to generation even if it was
            # also requested in this same command -- generating notes from
            # a chapter folder that assembly failed to finish setting up
            # would be working from incomplete/stale input.
            logger.error("Assembly stage failed; skipping generation.")
            sys.exit(exit_code)

    if generate_with_llm is not None:
        exit_code = run_generate(live_mode=generate_with_llm, verbose=args.verbose)

    sys.exit(exit_code)

if __name__ == "__main__":
    main()
