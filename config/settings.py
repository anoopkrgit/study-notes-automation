# Configuration file for Study Notes Automation Pipeline
import os
from pathlib import Path

# Base Paths — auto-detected from this file's location (config/settings.py → project root)
# Works from any clone location on any machine, no manual editing required.
LOCAL_RUNTIME_ROOT = Path(__file__).resolve().parent.parent

LOG_DIR = LOCAL_RUNTIME_ROOT / "logs"
STATE_DIR = LOCAL_RUNTIME_ROOT / "state"

UNIFIED_LOG_FILE = LOG_DIR / "study-notes-pipeline.log"
STATE_FILE = STATE_DIR / "assemble-state.json"
RETRY_EPOCH_FILE = STATE_DIR / "retry-epoch.txt"
CHAPTER_PROGRESS_DIR = STATE_DIR / "progress"

# Default Target Folders (Can be overridden by env vars)
DEFAULT_TRANSCRIPT_SRC = Path(os.environ.get("TRANSCRIPT_SRC", "/mnt/g/My Drive/Education/Student/Lecture-Downloads"))
DEFAULT_COLLECTED_SRC  = Path(os.environ.get("COLLECTED_SRC",  "/mnt/g/My Drive/Education/Student/Study-Programme/Collected-Study-Materials"))
DEFAULT_TARGET_ROOT    = Path(os.environ.get("TARGET_ROOT",    "/mnt/g/My Drive/Education/Student/Study-Programme/AI-Chapter-Notes"))

# Tunables
IDLE_DAYS = 14
CONF_MIN = 0.55
DISAGREE_MIN = 0.80
SNIPPET_CHARS = 6000
SNIPPET_PAGES = 3
# How many separate PROCESS RUNS (e.g. across nights, if the model keeps
# hitting a retryable API error) one chapter's generation may need before
# the pipeline gives up on it and writes a failure marker for a human.
MAX_ATTEMPTS = 5
# Safety valve on how many back-and-forth turns (one turn = one model
# response, which may include several tool calls) a SINGLE generation
# attempt may take, WITHIN one process run, before that run gives up. This
# is deliberately generous -- a full illustrated chapter document
# realistically needs many dozens of tool calls (writing sections, drawing
# diagrams, converting them, checking each one, assembling and proofreading
# the .docx page by page). This number exists only to catch a genuinely
# stuck/looping run, not to cap normal work.
MAX_TURNS = 200

# Models
ROUTER_MODEL = os.environ.get("ASSEMBLE_ROUTER_MODEL", "claude-haiku-4-5-20251001")
GENERATOR_MODEL = os.environ.get("STUDY_NOTES_MODEL", "claude-sonnet-5")

# Special File Markers
HOLD = "_hold"
MARKER = "_notes_done"
FAILMARK = "_notes_FAILED.txt"
SOURCES = "_sources.txt"
NEWMAT = "_new-material.txt"
SUP_DIR = "supporting"
TRANSCRIPTS_DIR = "transcripts"
REVIEW_DIR_NAME = "_needs_review"

IGNORE_NAMES = {"desktop.ini", "Thumbs.db", ".DS_Store"}
IGNORE_EXTS = {".tmp", ".part", ".crdownload"}
