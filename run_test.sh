#!/usr/bin/env bash
# run_test.sh
# Quick manual smoke test against the real AI-Chapter-Notes target root:
# lists real chapter folders, then runs both pipeline stages in the cheap
# --llm-token-saver mode (real API/CLI calls, near-zero cost, dummy
# content/prompts) so the full wiring gets exercised without spending real
# generation-scale tokens. See docs/setup-guide.md for the equivalent
# nightly-automation flags.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="/mnt/g/My Drive/000-Education/00-Avyaan/Bakliwal-Study-9th-26-27/AI-Chapter-Notes"

echo "Chapter folders under: $TARGET_DIR"
find "$TARGET_DIR" -maxdepth 1 -mindepth 1 -type d -not -name "_*" | sort

echo
echo "Running a cheap smoke test of both stages (--stage1-mode llm-token-saver --stage2-mode llm-token-saver)..."
python3 "$SCRIPT_DIR/src/main.py" --stage1-mode llm-token-saver --stage2-mode llm-token-saver --stage2-impl graph --verbose
