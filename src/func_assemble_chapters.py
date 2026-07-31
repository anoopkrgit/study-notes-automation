"""
func_assemble_chapters.py

Stage 1 Assembler module: Reads transcripts & collected materials,
logs each file being processed/classified, calls LLM router when needed,
and files into chapter buckets under AI-Chapter-Notes/.
"""

import argparse
import base64
import io
import json
import os
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import settings as config
from src.func_tools_and_utils import logger, sha256, is_ignorable, load_state, save_state, classify_api_error, TokenTracker
import src.func_classify_and_rename as cr

try:
    import anthropic
    from dotenv import load_dotenv
    load_dotenv(os.path.expanduser("~/.anthropic_env"))
    client = anthropic.Anthropic()
except Exception:
    client = None

try:
    from pypdf import PdfReader, PdfWriter
except ImportError:
    PdfReader = PdfWriter = None

try:
    import docx as _docx
except ImportError:
    _docx = None

def target_folder_name(subject_code: str, ch_no: int, ch_name: str) -> str:
    full = cr.SUBJECT_FULL.get(subject_code, subject_code)
    name = re.sub(r"[^A-Za-z0-9]+", "-", ch_name.replace("&", "and")).strip("-")
    return f"{full}-Ch{ch_no}-{name}"

def taxonomy_buckets() -> dict:
    buckets = {}
    for code, m in cr.load_chapter_maps().items():
        full = cr.SUBJECT_FULL.get(code, code)
        for _lec, (chno, chname) in m.items():
            buckets[(full, chno)] = chname
    return buckets

def folder_is_generated(folder: Path) -> bool:
    if (folder / config.MARKER).exists():
        return True
    return any(folder.glob("*.docx"))

def safe_copy(src: Path, dst_dir: Path, dry: bool) -> Path:
    """Copy src into dst_dir (creating dst_dir if needed), unless dry=True.

    IMPORTANT: everything here that touches the disk is gated behind
    `if not dry`. --dry-run is supposed to mean "show what WOULD happen,
    change nothing" — a previous version of this function created the
    destination folder even in dry-run mode (Path.mkdir() ran
    unconditionally, before the dry check), which is how a test run ended
    up leaving empty chapter folders behind in the real Google Drive folder.
    `dst_dir.mkdir()` must never run when dry=True, same as the file copy.
    """
    dst = dst_dir / src.name
    if dst_dir.exists() and dst.exists() and dst.is_file():
        try:
            if sha256(dst) == sha256(src):
                return dst
        except OSError:
            pass
        short = sha256(src)[:8]
        dst = dst_dir / f"{src.stem}__{short}{src.suffix}"
    if not dry:
        dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    return dst

def park_for_review(src: Path, why: str, dry: bool) -> Path:
    review_dir = config.DEFAULT_TARGET_ROOT / config.REVIEW_DIR_NAME
    dst = safe_copy(src, review_dir, dry)
    if not dry:
        (review_dir / (dst.name + ".why.txt")).write_text(
            f"{why}\nSource: {src}\nFix/rename and drop back into source folder.\n",
            encoding="utf-8"
        )
    return dst

ROUTER_TOOL = {
    "name": "route_file",
    "description": "Return chapter routing decision for this file.",
    "input_schema": {
        "type": "object", "additionalProperties": False, "required": ["matches"],
        "properties": {
            "matches": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["subject", "chapter_no", "confidence"],
                "properties": {
                    "subject": {"type": "string", "enum": ["Physics", "Chemistry", "Maths"]},
                    "chapter_no": {"type": "integer"},
                    "role": {"type": "string", "enum": ["spine", "supporting"]},
                    "confidence": {"type": "number"},
                    "reason": {"type": "string"},
                }}},
            "agrees_with_filename": {"type": ["boolean", "null"]},
        }
    }
}

def extract_content(path: Path):
    try:
        suffix = path.suffix.lower()
        if suffix == ".pdf" and PdfReader is not None:
            reader = PdfReader(str(path))
            writer = PdfWriter()
            for page in reader.pages[:config.SNIPPET_PAGES]:
                writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
            data = base64.standard_b64encode(buf.getvalue()).decode("ascii")
            return [{"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": data}}]
        if suffix == ".docx" and _docx is not None:
            doc = _docx.Document(str(path))
            paras = [p.text for p in doc.paragraphs if p.text.strip()]
            text = "\n".join(paras).strip()[:config.SNIPPET_CHARS]
            return [{"type": "text", "text": text}] if text else None
        if suffix in (".txt", ".md"):
            text = path.read_text(encoding="utf-8", errors="ignore").strip()[:config.SNIPPET_CHARS]
            return [{"type": "text", "text": text}] if text else None
    except Exception as e:
        logger.warning(f"  content extraction failed for {path.name}: {e}")
    return None

def run_router(content, model: str, tracker: TokenTracker = None):
    """Call the router model. Returns (obj, limited) where `limited=True`
    means "the LLM isn't usable right now, for ANY reason (rate limit,
    server overload, billing) -- stop calling it this run and fall back to
    filename-based routing" (see llm_route()'s caller, run_assemble()).
    classify_api_error() (func_tools_and_utils.py) figures out WHICH of
    those reasons applies and logs it, but Stage 1 treats them all the same
    way here: give up on the router for now rather than burn the same
    (paid) failing call again on the very next file.
    """
    if not client:
        return None, True
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=2048,
            tools=[ROUTER_TOOL],
            tool_choice={"type": "tool", "name": "route_file"},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIError as e:
        info = classify_api_error(e)
        logger.warning(f"  router unavailable ({info['reason']})")
        return None, True

    if tracker is not None:
        tracker.record(model, getattr(resp, "usage", None))

    for block in resp.content:
        if block.type == "tool_use" and block.name == "route_file":
            return block.input, False
    logger.warning(f"  no tool_use block in router response: {resp.content!r}")
    return None, False

def _parse_matches(obj: dict, buckets: dict):
    out = []
    for item in (obj or {}).get("matches", []) or []:
        try:
            full = str(item["subject"]).strip()
            chno = int(item["chapter_no"])
            conf = float(item.get("confidence", 0))
        except (KeyError, TypeError, ValueError):
            continue
        if (full, chno) in buckets:
            role = str(item.get("role") or "").strip() or None
            reason = str(item.get("reason") or "").strip()
            out.append((full, chno, buckets[(full, chno)], role, conf, reason))
    return out

def llm_route(path: Path, buckets: dict, prior=None, tracker: TokenTracker = None):
    if not client:
        return [], False, "no-client"
    doc_blocks = extract_content(path)
    if not doc_blocks:
        return [], False, "no-snippet"

    tax = "\n".join(f"- {full} | Ch-{chno} | {name}" for (full, chno), name in sorted(buckets.items()))
    prior_line = ""
    if prior:
        prior_line = (f"\nA filename convention suggests this is: {prior[0]} | "
                      f"Ch-{prior[1]} | {prior[2]}. Confirm this from the content, "
                      "or flag disagreement — do not just trust the filename.\n")
    instructions = (
        "You are routing ONE study file for a 9th-grade PCM (Physics/Chemistry/Maths) "
        "course to the chapter(s) whose notes it would actually help build.\n\n"
        f"Above is the opening excerpt (title / headings / first pages) of the file "
        f"'{path.name}' — already trimmed to the relevant portion. Use ONLY that to "
        "identify subject + chapter.\n"
        f"{prior_line}\n"
        "Choose from these EXACT (subject, chapter) buckets. A file may span MULTIPLE "
        "chapters — list EVERY bucket that genuinely applies, each with a confidence "
        "0.0-1.0. If nothing fits, return an empty matches list. NEVER invent a bucket "
        "outside this list. Set `role` to \"spine\" only for an actual class lecture "
        "transcript of that chapter, else \"supporting\". Set `agrees_with_filename` to "
        "true/false/null relative to the filename hint above.\n\n"
        f"Valid buckets:\n{tax}"
    )
    content = doc_blocks + [{"type": "text", "text": instructions}]

    obj, limited = run_router(content, config.ROUTER_MODEL, tracker=tracker)
    if limited:
        return [], True, config.ROUTER_MODEL

    matches = _parse_matches(obj, buckets)
    return matches, False, config.ROUTER_MODEL

def parse_transcript(name: str):
    if cr.classify(name) != "transcript":
        return None
    ch = cr.chapter_from_name(name)
    if not ch:
        return None
    subj, _src = cr.resolve_subject(name)
    if subj not in ("Phy", "Chem", "Maths"):
        return None
    return subj, ch[0], ch[1], cr.detect_lecture_no(name), cr.file_date(name)

def write_sources(folder: Path, entries: list, dry: bool):
    if dry:
        return
    lines = [f"# Chapter sources — regenerated {datetime.now().isoformat(timespec='seconds')}", ""]
    for role, fn, note in entries:
        lines.append(f"[{role}] {fn}" + (f"  — {note}" if note else ""))
    (folder / config.SOURCES).write_text("\n".join(lines) + "\n", encoding="utf-8")

def run_assemble(dry_run: bool = False, no_llm: bool = False, verbose: bool = False, stage1_impl: str = None) -> int:
    logger.info("-" * 70)
    logger.info(f"=== Stage 1: Chapter Folder Assembler (dry_run={dry_run}, no_llm={no_llm}, verbose={verbose}) ===")

    if not config.DEFAULT_TRANSCRIPT_SRC.exists() or not config.DEFAULT_COLLECTED_SRC.exists():
        logger.error(f"Source directories missing. Check config.")
        logger.info("-" * 70)
        return 1

    state = load_state()
    processed = state["processed"]
    buckets = taxonomy_buckets()
    tracker = TokenTracker()
    copied_by_target = {}  # target folder (relative) -> [filenames actually copied this run]

    # Stage A: Transcripts grouping & readiness
    groups = {}
    for p in sorted(config.DEFAULT_TRANSCRIPT_SRC.iterdir()):
        if not p.is_file() or is_ignorable(p):
            continue
        parsed = parse_transcript(p.name)
        if not parsed:
            continue
        subj, chno, chname, _lec, fdate = parsed
        key = (subj, chno, chname)
        g = groups.setdefault(key, {"files": [], "dates": []})
        g["files"].append(p)
        if fdate:
            g["dates"].append(fdate)

    total_transcripts = sum(len(g["files"]) for g in groups.values())
    logger.info(f"INPUT: Found {total_transcripts} transcript file(s) across {len(groups)} chapter group(s) to evaluate:")
    for (subj, chno, chname) in sorted(groups):
        logger.info(f"  [{cr.SUBJECT_FULL.get(subj, subj)} Ch-{chno} {chname}]")
        for p in groups[(subj, chno, chname)]["files"]:
            logger.info(f"    - {p.name}")

    maxch = {}
    for (subj, chno, _n) in groups:
        maxch[subj] = max(maxch.get(subj, 0), chno)
    today = date.today()

    def is_ready(key):
        subj, chno, _n = key
        dates = groups[key]["dates"]
        latest = max(dates) if dates else None
        superseded = chno < maxch.get(subj, 0)
        idle = latest is not None and (today - latest).days > config.IDLE_DAYS
        return superseded or idle

    route_by_name = {rec.get("name", ""): rec.get("route", "")
                     for rec in processed.values()
                     if str(rec.get("kind", "")).startswith("transcript")}

    for key in sorted(groups):
        subj, chno, chname = key
        folder = config.DEFAULT_TARGET_ROOT / target_folder_name(subj, chno, chname)
        ready = is_ready(key)
        new_transcript = False
        prior = (cr.SUBJECT_FULL[subj], chno, chname)
        prior_key = (prior[0], chno)
        
        for src in groups[key]["files"]:
            h = sha256(src)
            if h in processed:
                continue

            route = "spine (filename)"
            if not no_llm:
                from src.agents.dispatch import route_file
                matches, limited_a, model_used = route_file(src, buckets, prior=prior, tracker=tracker, impl=stage1_impl)
                if limited_a:
                    route = "spine (filename-fallback, router unavailable)"
                    logger.info(f"  [A] router unavailable for {src.name}; filing by filename")
                else:
                    agrees = any((m[0], m[1]) == prior_key for m in matches)
                    confident_other = [m for m in matches if m[4] >= config.DISAGREE_MIN and (m[0], m[1]) != prior_key]
                    if confident_other and not agrees:
                        alt = confident_other[0]
                        why = (f"Transcript placement disagreement: filename says "
                               f"{prior[0]} Ch-{chno} {chname}; content router says "
                               f"{alt[0]} Ch-{alt[1]} {alt[2]} (conf {alt[4]:.2f}). "
                               f"Reason: {alt[5] or 'n/a'}")
                        review_dst = park_for_review(src, why, dry_run)
                        copied_by_target.setdefault(config.REVIEW_DIR_NAME, []).append(review_dst.name)
                        logger.info(f"  [A] NEEDS REVIEW (placement disagreement): {src.name}")
                        processed[h] = {"src": str(src), "name": src.name,
                                        "kind": "transcript-review", "targets": [],
                                        "route": "review (placement disagreement)",
                                        "ts": datetime.now().isoformat(timespec='seconds')}
                        route_by_name[src.name] = "review (placement disagreement)"
                        continue
                    
                    best = max((m[4] for m in matches if (m[0], m[1]) == prior_key),
                               default=max((m[4] for m in matches), default=0.0))
                    route = f"spine (llm-haiku-confirmed, conf {best:.2f})"

            if folder_is_generated(folder):
                logger.info(f"  [A] new transcript for GENERATED chapter '{folder.name}' -> archiving old doc, will rebuild")
                if not dry_run:
                    prev = folder / "_prev"
                    prev.mkdir(exist_ok=True)
                    for docx in folder.glob("*.docx"):
                        shutil.move(str(docx), str(prev / docx.name))
                    for mk in (config.MARKER, config.FAILMARK):
                        (folder / mk).unlink(missing_ok=True)
                        
            transcripts_target = f"{folder.name}/{config.TRANSCRIPTS_DIR}"
            dst = safe_copy(src, folder / config.TRANSCRIPTS_DIR, dry_run)
            copied_by_target.setdefault(transcripts_target, []).append(dst.name)
            logger.info(f"  [A] transcript -> {transcripts_target}/{dst.name}  [{route}]")
            processed[h] = {"src": str(src), "name": src.name, "kind": "transcript",
                            "targets": [transcripts_target], "route": route,
                            "ts": datetime.now().isoformat(timespec='seconds')}
            route_by_name[src.name] = route
            new_transcript = True

        if folder.exists() or new_transcript:
            if not dry_run:
                folder.mkdir(parents=True, exist_ok=True)
            hold_file = folder / config.HOLD
            if not ready and not folder_is_generated(folder):
                if not dry_run: hold_file.touch(exist_ok=True)
                if verbose:
                    logger.info(f"  [Hold] {folder.name} (not ready yet)")
            else:
                if not dry_run: hold_file.unlink(missing_ok=True)
            
            write_sources(folder, [(route_by_name.get(src.name, "unknown"), src.name, "")
                                   for src in groups[key]["files"]], dry_run)


    # ---- Stage B: content-route + place supporting material -----------------
    # "Supporting" files (Collected-Study-Materials/) usually have GENERIC
    # names -- unlike transcripts, their filename rarely tells you the
    # chapter reliably. So here the LLM content router is PRIMARY: it reads
    # the file itself and decides. A filename-based guess (`prior`, if the
    # name happens to look transcript-like) is only ever passed to the
    # router as a HINT for it to confirm or override -- never used to skip
    # the router outright. (An earlier version of this loop did the
    # opposite -- trusted any filename match at face value and skipped the
    # LLM check entirely -- which meant a coincidental substring match like
    # "chem" inside an unrelated word could silently misfile a file with no
    # sanity check at all.)
    collected_files = [p for p in sorted(config.DEFAULT_COLLECTED_SRC.iterdir()) if p.is_file() and not is_ignorable(p)]
    logger.info(f"INPUT: Found {len(collected_files)} collected study file(s) to evaluate:")
    for p in collected_files:
        logger.info(f"  - {p.name}")

    for p in collected_files:
        h = sha256(p)
        if h in processed:
            continue

        # Optional filename hint (rarely present for generic collected
        # names; the router reads content regardless of whether this exists).
        prior = None
        det = cr.chapter_from_name(p.name)
        subj_code, _ = cr.resolve_subject(p.name)
        if det and subj_code:
            prior = (cr.SUBJECT_FULL.get(subj_code, subj_code), det[0], det[1])

        matches, method = [], None
        if no_llm:
            # Zero-token path (used during the dummy-mode window): no LLM
            # call is allowed at all. If we have a reliable filename hint,
            # file by that. If we DON'T, this file must be DEFERRED --
            # skipped WITHOUT recording it in `processed` -- so a later run
            # with the LLM enabled still gets a fair chance to classify it
            # properly. Recording it as "reviewed" here would permanently
            # hide it from every future run too, since its content-hash
            # would already be marked processed -- that used to be a real
            # bug: generic files silently and PERMANENTLY misfiled to
            # _needs_review/ during the zero-token window, never revisited.
            if prior:
                matches = [(prior[0], prior[1], prior[2], "supporting", 0.95, "filename")]
                method = "filename"
            else:
                if verbose:
                    logger.info(f"  [B] deferred (no-llm, no filename match): {p.name}")
                continue
        else:
            from src.agents.dispatch import route_file
            matches, limited, model_used = route_file(p, buckets, prior=prior, tracker=tracker, impl=stage1_impl)
            if limited:
                logger.info("  [B] usage limit hit during routing; leaving the rest for next run.")
                break
            method = "llm-haiku"

        good = [m for m in matches if m[4] >= config.CONF_MIN]
        if not good:
            cand = ", ".join(f"{m[0]} Ch-{m[1]} ({m[4]:.2f})" for m in matches) or "no candidates"
            review_dst = park_for_review(p, f"Could not confidently route (method={method}). Candidates: {cand}.", dry_run)
            copied_by_target.setdefault(config.REVIEW_DIR_NAME, []).append(review_dst.name)
            logger.info(f"  [B] NEEDS REVIEW: {p.name} (method={method})")
            processed[h] = {"src": str(p), "name": p.name, "kind": "review",
                            "route": f"review ({method})", "ts": datetime.now().isoformat(timespec='seconds')}
            continue

        # A file may genuinely help MULTIPLE chapters -> copy into each
        # chapter's supporting/ subfolder.
        targets = []
        for full, chno, chname, _role, conf, _reason in good:
            code = cr.full_name_to_code(full) or "Phy"
            folder_name = target_folder_name(code, chno, chname)
            folder = config.DEFAULT_TARGET_ROOT / folder_name
            sup_folder = folder / config.SUP_DIR
            dst = safe_copy(p, sup_folder, dry_run)
            target_key = f"{folder_name}/{config.SUP_DIR}"
            targets.append(target_key)
            copied_by_target.setdefault(target_key, []).append(dst.name)
            logger.info(f"  [B] Filed as SUPPORTING (conf {conf:.2f}, {method}): {folder_name}/{config.SUP_DIR}/{dst.name}")

            if not dry_run:
                # This chapter's supporting material arrived before its
                # transcript spine (Stage A above never saw this subject+
                # chapter combination). HOLD it: func_generate_notes.py's
                # picker must never generate notes for a chapter that has
                # no transcript yet, since the transcript defines the
                # chapter's scope. Once the matching transcript arrives,
                # Stage A's readiness check clears this hold.
                key = (code, chno, chname)
                if key not in groups and not (folder / config.HOLD).exists() and not folder_is_generated(folder):
                    (folder / config.HOLD).write_text(
                        "held: awaiting transcript for this chapter\n", encoding="utf-8")
                    logger.info(f"  [B] holding {folder_name} (supporting material only, no transcript yet)")

            if folder_is_generated(folder):
                logger.info(f"  [B] supporting material for GENERATED chapter '{folder.name}' -> dropping {config.NEWMAT}")
                if not dry_run:
                    with open(folder / config.NEWMAT, "a", encoding="utf-8") as fh:
                        fh.write(f"{datetime.now().isoformat(timespec='seconds')}  new supporting file: {dst.name}\n")

        best_reason = next((m[5] for m in good if m[5]), "")
        processed[h] = {"src": str(p), "name": p.name, "kind": "supporting", "targets": targets,
                        "route": f"{method}" + (f" — {best_reason}" if best_reason else ""),
                        "ts": datetime.now().isoformat(timespec='seconds')}

    save_state(state, dry_run)
    tracker.log_summary(logger, "Stage 1 (Assembler)")

    total_copied = sum(len(v) for v in copied_by_target.values())
    verb = "Would copy" if dry_run else "Copied"
    if total_copied:
        logger.info(f"OUTPUT: {verb} {total_copied} file(s) into {len(copied_by_target)} target folder(s) this run:")
        for target in sorted(copied_by_target):
            logger.info(f"  [{target}]")
            for name in copied_by_target[target]:
                logger.info(f"    - {name}")
    else:
        logger.info("OUTPUT: No files copied this run (every input file was already processed in a prior run).")

    logger.info("Stage 1 Assembler run complete cleanly.")
    logger.info("-" * 70)
    return 0

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    sys.exit(run_assemble(dry_run=args.dry_run, no_llm=args.no_llm, verbose=args.verbose))
