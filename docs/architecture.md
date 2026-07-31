# Architecture & Invocation Flow

This document details the linear execution pipeline for the Study Notes Automation suite.

```text
1. Windows Task Scheduler (03:00 AM Trigger)
   │
   ▼
2. win-environment-setup.ps1 (Windows Host Scaffolding)
   │  [Env Stabilization]
   ├─► Assert C# Win32 keep-awake lock (`SetThreadExecutionState`) to prevent sleep
   ├─► Verify/Relaunch `GoogleDriveFS.exe` process & await `G:\` drive availability
   └─► Invoke WSL Bash wrapper: `wsl.exe -e bash -lc "/mnt/c/StudyNotesAutomation/wsl-study-notes-processor.sh"`
   │
   ▼
3. wsl-study-notes-processor.sh (WSL Entrypoint & Remount Helper)
   │  [Env Stabilization]
   ├─► Heal Linux mount via `sudo remount-gdrive` if `/mnt/g` handle is stale
   ├─► Check `DUMMY_UNTIL` timestamp gate (exit early if testing without tokens)
   └─► Launch Python engine: `python3 src/main.py --stage1-mode llm-full --stage2-mode no-llm`
       (assemble WITH the LLM, generate WITHOUT it -- zero tokens for generation;
        see readme.md's Quick Start for every --stageN-mode/--stageN-impl combination)
   │
   ▼
4. main.py / func-assemble-chapters.py / func-generate-notes.py (Python Engine)
   │
   ├─► STAGE 1: Chapter Folder Assembler
   │   │  [Env Stabilization]
   │   ├─► Load `ANTHROPIC_API_KEY` from `~/.anthropic_env` & SHA-256 state (`assemble-state.json`)
   │   ├─► Read incoming files from `Lecture-Downloads/` & `Collected-Study-Materials/`
   │   │  [API Execution]
   │   ├─► Log file classification step-by-step & route via `claude-haiku-4-5-20251001`
   │   │  [Env Stabilization]
   │   └─► Copy files into `AI-Chapter-Notes/<Folder>/` & update `_hold` / SHA-256 state
   │
   ├─► STAGE 2: Target Chapter Selection
   │   │  [Env Stabilization]
   │   └─► Select first folder missing `.docx`, `_notes_done`, `_hold`, or `_notes_FAILED.txt`
   │
   └─► STAGE 3: Agentic Study Notes Generator (Default Mock Mode)
       │  [Env Stabilization]
       ├─► Log inputs identified (spine transcripts, supporting files, target docx name)
       ├─► Inject the full `study-notes-skill.md` into the System Prompt
       │  [API Execution]  (Mock Mode: metadata-only ping, 0 body tokens)
       ├─► Live Mode: run the tool-call loop (tool_read/write/edit/glob/grep/bash,
       │   tool_convert_to_png, tool_view_image, tool_view_pdf_page) up to
       │   config.MAX_TURNS turns -- a generous safety valve, not a realistic cap;
       │   progress (full message history) is saved to state/progress/<chapter>.json
       │   after every turn so a rate-limit pause resumes exactly where it left off
       │  [Env Stabilization]
       └─► Confirm `.docx` file written to disk & drop `_notes_done` completion marker (in Live mode)
   │
   ▼
5. Return & Exit Handling (System Reset & Retry Handshake)
   │  [API Execution / Env Stabilization]
   ├─► On a RETRYABLE API error (rate limit / server overload / network issue --
   │   see `classify_api_error()` in func_tools_and_utils.py, which tells these
   │   apart from a non-retryable billing/config error):
   │   ├─► Pipeline writes the actual retry time (read from the API's response
   │   │   headers when available) to `C:\StudyNotesAutomation\state\retry-epoch.txt`
   │   │   & exits with code 42
   │   ├─► `wsl-study-notes-processor.sh` passes code 42 up to `win-environment-setup.ps1`
   │   └─► PowerShell registers one-time Windows wake task (`StudyNotesRetry`) for reset epoch
   │
   ├─► On a NON-retryable error (e.g. insufficient API credit): writes `_notes_FAILED.txt`
   │   with a clear explanation and exits non-zero -- no wake task is scheduled, since
   │   waiting would not fix it.
   │
   │  [Env Stabilization]
   └─► On Success (Exit Code 0):
       ├─► Clean up progress cache file
       ├─► If the job FAILED for a non-retry, non-success reason: leave the machine
       │   awake (do not sleep) so the failure is visible -- see win-environment-setup.ps1
       └─► Otherwise: release Win32 keep-awake lock & trigger Modern Standby (turn off screen)
```
