# Architecture & Invocation Flow

This document details the execution pipeline for the Study Notes Automation suite. Steps 1-3
are linear; step 4 branches per-stage across three parallel implementations, selected via
`--stage1-impl`/`--stage2-impl` (see readme.md's Quick Start).

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
4. main.py (Python Engine -- dispatches to ONE of THREE parallel implementations per stage)
   │
   ├─► STAGE 1: Chapter Folder Assembler                    [src/direct_api/func_assemble_chapters.py::run_assemble()]
   │   │  [Env Stabilization]
   │   ├─► Load `ANTHROPIC_API_KEY` from `~/.anthropic_env` & SHA-256 state (`state/assemble-state.json`)
   │   ├─► Read incoming files from `Telegram-A27-Download/` & `Collected-Study-Materials/`
   │   │  [Per-file routing -- dispatch.route_file(impl), src/agents/dispatch.py]
   │   ├─ --stage1-impl legacy (default) ─► llm_route()        [src/direct_api/func_assemble_chapters.py]
   │   │                                      └─► run_router() ─► client.messages.create()          ⟵ Anthropic API
   │   ├─ --stage1-impl graph ────────────► route_one_file()    [src/agents/stage1_graph.py]
   │   │                                      └─► extract/triage/reconcile nodes ─► client.messages.create()   ⟵ Anthropic API
   │   ├─ --stage1-impl subprocess ───────► route_file()        [src/claude_cli_subprocess/stage1.py]
   │   │                                      └─► run_router() ─► subprocess.run(["claude","-p",...])   ⟵ claude CLI
   │   │  [Env Stabilization]
   │   └─► Copy files into `AI-Chapter-Notes/<Folder>/` & update `_hold` / SHA-256 state
   │
   └─► STAGE 2: Target Selection + Note Generation           [dispatch.generate_notes(impl), src/agents/dispatch.py]
       │  [Env Stabilization]
       ├─► Select first folder missing `.docx`, `_notes_done`, `_hold`, or `_notes_FAILED.txt`
       │  [API/CLI Execution]  (Mock/no-llm mode: metadata-only check, 0 body tokens)
       ├─ --stage2-impl legacy (default) ─► run_generate()      [src/direct_api/func_generate_notes.py]
       │                                      └─► tool-call loop (up to config.MAX_TURNS) ─► client.messages.create()   ⟵ Anthropic API
       ├─ --stage2-impl graph ────────────► run_stage2_chapter() [src/agents/stage2_graph.py]
       │                                      └─► author/figure/compiler nodes [src/agents/base.py] ─► client.messages.create()   ⟵ Anthropic API
       └─ --stage2-impl subprocess ───────► run_stage2_chapter() [src/claude_cli_subprocess/stage2.py]
                                              └─► NotImplementedError  (not yet built -- see docs/cli-subprocess-plan.md)
       │  [Env Stabilization]
       └─► Confirm `.docx` file written to disk & drop `_notes_done` completion marker (llm-full mode only)
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
