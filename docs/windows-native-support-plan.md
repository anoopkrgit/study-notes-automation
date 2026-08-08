# Native Windows support — plan

## Context

`study-notes-runner` (CLI wrapper) pulls in `study-notes-automation` (this repo, the agent pipeline) as a git dependency (`@develop`). Both are WSL/Linux/macOS-only today. The runner actively **rejects** native Windows in `setup_check.check_platform_supported()`; the documented root cause is this repo's `src/common/skill_package.py:19` doing `import fcntl` at module level, which is unimportable on native Windows. Goal: let a Windows user run the runner (and thus this pipeline) natively, both repos auto-detecting the platform and branching instead of hard-failing.

## Hard blockers (native Windows fails without these)

1. **`src/common/skill_package.py:19` — `import fcntl`** (automation). Replace `fcntl.flock` (`:39`, `:45`) with a cross-platform lock: branch on `platform.system()` using `msvcrt.locking` on Windows / `fcntl.flock` elsewhere, or adopt a small cross-platform lock lib (e.g. `filelock`). This single import makes the whole package unimportable on Windows — nothing else runs until it's fixed.
2. **`src/study_notes_runner/setup_check.py:43-52` — the reject gate** (runner). After blocker 1 lands, change `check_platform_supported()` to stop returning `False` on Windows, and update `print_platform_unsupported_message()` (`:55`) accordingly.
3. **`src/func_tools_and_utils.py:467` — `ALLOWED_BASH_COMMANDS`** (automation) includes Unix-only `ls`, `cat`, `cp`, `mv`, invoked via `subprocess` (`:501-504`). These have no `.exe` on Windows PATH and will fail. On Windows, service these via direct Python (`os.listdir`, `Path.read_text`, `shutil.copy`, `shutil.move`) gated on `platform.system()`, rather than shelling out.

## External-tool dependencies (code likely fine; require Windows installs + docs)

4. **`pdftoppm` / `soffice` calls** — `src/func_tools_and_utils.py:578`, `:621`, `:628`. Both have native Windows builds (poppler-for-Windows, LibreOffice). `subprocess.run(["pdftoppm", ...])` resolves `pdftoppm.exe` from PATH, so no code change is expected — but document the Windows install step and verify PATH resolution.
5. **git auto-install** — `setup_check.ensure_git_installed()` (`:75`) only handles apt/dnf/pacman/brew. Add a Windows branch (`winget install Git.Git`, else manual link). Low urgency: `pip install` already needed git to resolve the `git+https` dependency, so git is present at install time by construction.

## Nice-to-haves (already degrade gracefully; not required for a working run)

6. **`config/settings.py:33-35` `/mnt/g/` defaults** (automation). Only affects users of study-notes-automation **directly** — via the runner, `paths_config.py` sets these env vars first, so the hardcoded defaults are never used. Optionally branch the defaults per-OS for direct users.
7. **`paths_config.py:36` XDG config dir** (runner). `~/.config` works on Windows but isn't idiomatic. Optionally branch to `%APPDATA%\study-notes-runner`.
8. **`_preflight.ensure_python_installed()` (`:52`)** (runner). Already falls back to a python.org message on Windows and `_which` already handles `.exe/.bat/.cmd`. Optionally add `winget install Python.Python.3` auto-install to match the Linux/macOS branches.
9. **`install.sh` → `install.ps1`** (runner). Add a PowerShell installer mirroring `install.sh` (detect Python/git via `winget`, no `sudo`) so Windows users get one-command setup instead of manual `pip install`.

## Already done (no work needed — noted to prevent rework)

- Runner **claude-CLI install is Windows-aware**: `setup_check.py:21-40` `_INSTALL_INSTRUCTIONS["Windows"]` (winget/npm) + OS-keyed `print_install_instructions` (`:141`).
- `_preflight._which` (`:86-97`) already handles Windows executable extensions.
- `config/settings.py:18-22` `LOCAL_RUNTIME_ROOT` is `Path(__file__)`-derived — already cross-platform.

## Sequencing

- Land the automation fixes (blocker 1, plus items 3/4) to `develop` first, since the runner pins `study-notes-automation@develop`.
- Then flip the runner gate (blocker 2) and add the Windows branches (items 5, 7–9).

## Verification

- **Native Windows** (not WSL): `pip install git+https://github.com/anoopkrgit/study-notes-runner.git`, run `study-notes-runner` — confirm `setup_check` passes, config resolves, and a full note-generation run succeeds (needs `pdftoppm`/poppler + LibreOffice/`soffice` on PATH, plus `claude` CLI and git).
- **Linux/WSL/macOS**: re-run existing flows to confirm the new `platform.system()` branches don't regress (`run_test.sh`, a manual `study-notes-runner` run).
