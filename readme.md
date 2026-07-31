# 📚 Study Notes Automation

An AI-powered pipeline that automatically organises raw 9th-grade PCM (Physics, Chemistry, Mathematics) lecture transcripts and study materials, routes them to the right chapter folders, and generates complete **print-ready `.docx` study notes** — all running unattended at 3 AM every night.

---

## ✨ What It Does

1. **Content Routing (Stage 1 — Assembler):** Incoming transcripts and collected study materials are classified by subject and chapter using a two-tier LLM router (cheap Haiku first, Sonnet escalation when unsure) and filed into per-chapter folders.
2. **Notes Generation (Stage 2 — Generator):** An agentic loop picks the next ready chapter and generates a full `.docx` study document — with annotated matplotlib diagrams, worked examples, stacked-fraction formulae, colour-coded callouts, practice problems, and a one-page cheat sheet — aimed at a 13-year-old studying alone.
3. **Nightly Automation:** A Windows Scheduled Task wakes the laptop from sleep at 3 AM, runs both stages in WSL, and goes back to sleep — handling Google Drive mount races, usage-limit retries, and cross-window resume.

---

## 🏗️ Repository Structure

```text
study-notes-automation/
├── config/
│   └── settings.py                  # Master configuration & paths
├── docs/
│   ├── architecture.md              # Invocation tree & execution flow
│   ├── setup-guide.md               # Windows + WSL setup guide
│   ├── skill-integration-plan.md    # Skill integration design doc
│   ├── stage2-claude-cli-migration-plan.md
│   └── web-enrichment-plan.md
├── scripts/
│   ├── install.sh                   # Linux dependency & venv setup
│   ├── win-install-setup.ps1        # Windows directory & Task Scheduler setup
│   ├── win-environment-setup.ps1    # Windows host power & Google Drive wrapper
│   ├── wsl-study-notes-processor.sh # WSL Linux entrypoint
│   └── remount-gdrive              # Sudo-passwordless Drive remount helper
├── src/
│   ├── main.py                      # CLI entrypoint (--run-assemble, --run-generate, --doctor)
│   ├── stage1_api.py    # Stage 1 folder assembler module
│   ├── stage2_api.py       # Stage 2 agentic generator module
│   ├── func_classify_and_rename.py  # Chapter taxonomy classifier
│   └── func_tools_and_utils.py      # Shared tools, hashing & logging utilities
├── templates/
│   └── study-notes.skill            # Full skill prompt template
├── tests/
│   ├── test_utils.py                # Unit tests for utilities & API error handling
│   ├── test_classify_and_rename.py  # Battle tests for chapter taxonomy
│   └── test_main_cli.py             # CLI flag combination tests
├── .gitignore
├── install.bat                      # 1-Click Windows installer launcher
├── license                          # MIT License
├── readme.md                        # This file
└── requirements.txt                 # Python dependencies
```

---

## 🏛️ Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  Windows Task Scheduler (3 AM wake)                         │
│  └─► Run-StudyNotesNightly.ps1                              │
│       ├── Power lock, Google Drive health check             │
│       ├── wsl-study-notes-processor.sh  ─────────────┐      │
│       │   └─► python3 src/main.py                    │      │
│       │        ├── Stage 1: Assemble (LLM routing)   │      │
│       │        └── Stage 2: Generate (agentic .docx) │      │
│       ├── On rate-limit → schedule retry wake task   │      │
│       └── On completion → sleep after idle check     │      │
└─────────────────────────────────────────────────────────────┘
```

- **Dual-Layer Runtime:** PowerShell manages host power, sleep/wake, and Google Drive processes on Windows; WSL Ubuntu runs the Python agentic engine.
- **API-Based:** Talks directly to the Anthropic API with your own key — no Claude CLI subscription required.
- **Visual QA:** Stage 2's agentic loop can *see* generated images and PDF pages for diagram-QA and page-by-page accuracy checks.

---

## 🚀 Quick Start

### Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Python | 3.14+ | Core runtime |
| Node.js | 20+ | `.docx` generation via `docx` library |
| LibreOffice | 26+ | SVG → PDF conversion for diagrams |
| Poppler (`pdftoppm`) | 26+ | PDF → PNG rasterisation |
| Anthropic API key | — | LLM routing & generation |

### Installation

```bash
# 1. Clone the repo
git clone https://github.com/anoopkrgit/study-notes-automation.git
cd study-notes-automation

# 2. Run the installer (sets up venv + dependencies)
bash scripts/install.sh

# 3. Set your API key
export ANTHROPIC_API_KEY="sk-ant-..."
```

**Or on Windows** — double-click `install.bat` for 1-click setup.

### CLI Usage

Each stage is controlled by one `--stageN-mode {off,no-llm,llm-token-saver,llm-full}` flag
(default `off`) — see `python3 src/main.py --help` for the full picture, including
`--stage1-impl`/`--stage2-impl {legacy,graph,subprocess}` for choosing which implementation
runs each stage.

```bash
# Health check — verify all tools are available
python3 src/main.py --doctor

# Stage 1 only (assemble + LLM routing)
python3 src/main.py --stage1-mode llm-full

# Stage 1 only (deterministic filename routing, no tokens spent)
python3 src/main.py --stage1-mode no-llm

# Stage 2 only (generate notes — spends tokens)
python3 src/main.py --stage2-mode llm-full

# Stage 2 preview (zero tokens, shows what it would generate)
python3 src/main.py --stage2-mode no-llm

# Full pipeline (both stages, both with LLM)
python3 src/main.py --stage1-mode llm-full --stage2-mode llm-full

# Nightly policy: assemble with LLM, generate preview only
python3 src/main.py --stage1-mode llm-full --stage2-mode no-llm

# Cheap smoke test of both stages' wiring, near-zero cost
python3 src/main.py --stage1-mode llm-token-saver --stage2-mode llm-token-saver

# Any combination also accepts --verbose / --quiet / --dry-run
python3 src/main.py --stage1-mode no-llm --dry-run
```

---

## 🔑 Key Design Principles

| Principle | Detail |
|-----------|--------|
| **Sources are READ-ONLY** | Files are only ever *copied*, never moved or deleted from source folders |
| **Incremental Processing** | SHA-256 state tracking — each run processes only *new* files |
| **Fail-Safe** | A mid-run crash leaves remaining files "new" for the next run |
| **Readiness Gate** | Chapter folders carry `_hold` until the chapter is complete (higher chapter started, or idle > 14 days) |
| **Regeneration** | New transcripts for completed chapters archive old docs to `_prev/` and rebuild |
| **Token-Saving Mock Mode** | Stage 2 defaults to mock mode for safe testing |
| **Usage-Limit Resilience** | Rate-limited runs schedule a wake-up task for the reset window and resume the same chapter |
| **Model Escalation** | Stage 1 routes files on cheap Haiku first, escalates to Sonnet only on low confidence |
| **Resume Capability** | Stage 2 persists conversation state across runs, resuming interrupted generations |

---

## 🔧 Nightly Automation Flow

1. **3 AM** — Windows Task Scheduler wakes the laptop from Modern Standby.
2. **PowerShell wrapper** acquires a power lock, ensures Google Drive's `G:` is accessible.
3. **WSL script** remounts Google Drive if stale, runs the assembler then the generator.
4. **On usage-limit** — classifies the error, schedules a retry wake task for the actual reset time, goes back to sleep.
5. **On success** — waits 120s for local keyboard/mouse activity; if idle, sleeps the machine. If the user is active, stays awake.
6. **On failure** — stays awake so the error is visible, does not sleep.

---

## 🧪 Running Tests

```bash
# Run all tests
python3 -m pytest tests/ -v

# Run specific test files
python3 -m pytest tests/test_utils.py -v
python3 -m pytest tests/test_classify_and_rename.py -v
python3 -m pytest tests/test_main_cli.py -v
```

---

## 📄 License

[MIT License](license) — free to use, modify, and distribute.
