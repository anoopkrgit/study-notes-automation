#!/usr/bin/env python3
"""
func_classify_and_rename.py

WHAT THIS FILE IS FOR (read this first if you don't know Python)
------------------------------------------------------------------
Every lecture transcript that gets downloaded has a filename like
"26-07-17_Phy FA27 Lec6.pdf". Before we can generate study notes for a
chapter, the pipeline needs to answer three questions about a file like that:

  1. Is it actually a class transcript, or is it some other kind of file
     (a syllabus PDF, an answer key, a poster, a schedule, ...)?
  2. Which SUBJECT is it — Physics, Chemistry, or Maths?
  3. Which CHAPTER of that subject does it belong to?

This file answers all three questions. It is the ONE place in the whole
project that knows "lecture 5 of Physics is Chapter 4, Vectors" — every
other file (stage1_api.py, tests, etc.) asks THIS file for the
answer instead of guessing on its own. Keeping that knowledge in one place
means that if the syllabus ever changes, you only have to fix it here.

SELF-SUFFICIENCY NOTE
------------------------------------------------------------------
An earlier version of this project tried to import this exact information
from a *different* project (the Telegram-download tool, which lives in a
separate folder and has its own copy of a very similar file). That was
fragile — this project would silently break if the other project's file
moved, was renamed, or was ever edited without this project's author
knowing. So instead, the real chapter data (which lecture belongs to which
chapter) is written out directly below, as plain Python dictionaries. This
project now works completely on its own — you could copy this one folder
to a brand new computer with nothing else, and chapter classification would
still work correctly.

As a bonus, IF a machine-readable syllabus spreadsheet
("Syllabus-PCM-BT.xlsx") or a class-timetable index ("timetable_subjects.json")
happens to exist in the Telegram-download project's folders, this file will
notice them and prefer their (possibly more up to date) information — but it
never REQUIRES them. If they're missing, everything below still works using
the hardcoded chapter maps.

(When the two projects are eventually merged into one folder, as planned,
this file can be pointed at the merged location, or simply left as-is —
either way it keeps working.)

WHAT EACH FUNCTION DOES (quick index)
------------------------------------------------------------------
  classify(filename)          -> "transcript" or "general"
  resolve_subject(filename)   -> (subject_code, how_we_know) e.g. ("Phy", "filename")
  chapter_from_name(filename) -> (chapter_no, chapter_name) or None
  detect_lecture_no(filename) -> lecture number, or None
  file_date(filename)         -> the date encoded in the filename, or None
  load_chapter_maps()         -> the full {lecture_number: (chapter_no, chapter_name)}
                                  table for each subject
"""

# ── Imports ──────────────────────────────────────────────────────────────────
# Python's standard library covers everything except the OPTIONAL syllabus
# spreadsheet reader (openpyxl), which is wrapped in a try/except below so
# this file still works fine even if that one optional package isn't
# installed — it just means the hardcoded chapter maps are used instead of
# the spreadsheet.
import json         # read/write JSON text (used for the optional timetable index)
import re           # regular expressions — pattern matching inside filenames
import sys          # sys.path — see the "self-sufficiency" note above; also
                    # used so `python3 func_classify_and_rename.py --self-test`
                    # can be run directly from a terminal for a quick sanity check
from datetime import datetime, date
from pathlib import Path
# `Path` = Python's object-oriented representation of a filesystem path.
# The `/` operator is OVERLOADED on Path objects to mean "join a path
# segment", e.g. `Path("a") / "b"` produces the path "a/b" without manual
# string concatenation — used throughout this file.

# This file needs to know where the Telegram-download project's folders are
# (only to OPTIONALLY look for the syllabus spreadsheet / timetable index
# mentioned above — never required). That location is already defined once,
# centrally, in config/settings.py, so we reuse it here instead of
# hardcoding a second copy of the path. `ROOT_DIR` climbs up from this
# file's own location (src/func_classify_and_rename.py) to the project root,
# then `sys.path.insert` makes Python able to `import settings`.
ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
import settings as config

try:
    import openpyxl
    # `openpyxl` reads/writes real Excel .xlsx files. It's an OPTIONAL
    # dependency (listed in requirements.txt) — wrapping the import in
    # try/except means: if it's not installed, `openpyxl` just becomes the
    # value `None` instead of crashing the whole program at startup, and the
    # code below treats "openpyxl is None" the same as "no spreadsheet
    # available", falling back to the hardcoded maps further down.
except ImportError:
    openpyxl = None


# ── Subject codes ──────────────────────────────────────────────────────────────
# Short internal code (used in filenames and folder names) -> full display name.
# Logic/Coding lecture transcripts exist in the source data too, but they are
# NOT part of the Physics/Chemistry/Maths (PCM) syllabus this pipeline builds
# study notes for, so the rest of this project simply ignores them (see
# stage1_api.py, which only keeps subjects in ("Phy","Chem","Maths")).
SUBJECT_FULL = {
    "Phy": "Physics",
    "Chem": "Chemistry",
    "Maths": "Maths",
    "Logic": "Logic",
    "Coding": "Coding",
}
# The reverse lookup of SUBJECT_FULL: full display name -> short code, e.g.
# "Physics" -> "Phy". Built once here (instead of re-computing it every time
# it's needed) with a DICT COMPREHENSION — `{value: key for key, value in
# some_dict.items()}` is the standard one-line way to "flip" a dictionary's
# keys and values.
_FULL_NAME_TO_CODE = {full: code for code, full in SUBJECT_FULL.items()}


def full_name_to_code(full_name: str):
    """"Physics"/"Chemistry"/"Maths" -> "Phy"/"Chem"/"Maths", or None if not
    recognised. Use this (not detect_subject/resolve_subject, which parse
    FILENAMES) whenever you already have a full subject name and just need
    its short code."""
    return _FULL_NAME_TO_CODE.get(full_name)

# ── Chapter maps: lecture number -> (chapter_no, chapter_name) ──────────────────
# This is the REAL syllabus data, transcribed from Syllabus-PCM-BT.pdf (verified
# page by page against the actual course). Physics & Maths have explicit chapter
# numbers in the syllabus; the Chemistry booklet is organised lecture-by-lecture
# with no chapter numbers printed in it, so lectures are grouped into chapters by
# hand below (Lec 1-5 = Gaseous State was the naming decision made when this data
# was first transcribed; update CHEM_CH here if the grouping should ever change).
#
# A Python "dict" (short for dictionary) is a lookup table: `{key: value, ...}`.
# Here every key is a lecture number (an integer) and every value is a TUPLE
# `(chapter_no, "chapter name")` — a tuple is just a small fixed-size group of
# values glued together, here read as "this lecture is chapter number N, named
# such-and-such".
PHYSICS_CH = {
    1: (1, "Force & Laws of Motion"), 2: (1, "Force & Laws of Motion"),
    3: (2, "Work Energy Theorem"),
    4: (3, "Conservation of Mechanical Energy"),
    5: (4, "Vectors"), 6: (4, "Vectors"),
    7: (5, "Optics"), 8: (5, "Optics"), 9: (5, "Optics"),
    10: (5, "Optics"), 11: (5, "Optics"),
    12: (6, "Fluids"), 13: (6, "Fluids"),
    14: (7, "Electrostatics"), 15: (7, "Electrostatics"),
    16: (8, "Current"), 17: (8, "Current"),
}
MATHS_CH = {
    1: (1, "Quadratic Equations"), 2: (1, "Quadratic Equations"),
    3: (1, "Quadratic Equations"), 4: (1, "Quadratic Equations"),
    5: (2, "Circles"), 6: (2, "Circles"),
    7: (3, "Permutations and Combination"), 8: (3, "Permutations and Combination"),
    9: (4, "Trigonometry"), 10: (4, "Trigonometry"),
    11: (4, "Trigonometry"), 12: (4, "Trigonometry"),
    13: (5, "Straight Lines"), 14: (5, "Straight Lines"),
    15: (6, "Set Theory"), 16: (6, "Set Theory"),
    17: (7, "Binomial Theorem"), 18: (7, "Binomial Theorem"),
    19: (8, "Probability"), 20: (8, "Probability"),
}
# Chemistry booklet is lecture-wise (no chapter numbers printed). Lec 1-5 =
# Gaseous State was the naming decision; 6+ are inferred topic groupings.
CHEM_CH = {
    1: (1, "Gaseous State"), 2: (1, "Gaseous State"), 3: (1, "Gaseous State"),
    4: (1, "Gaseous State"), 5: (1, "Gaseous State"),
    6: (2, "Chemical Equilibrium"), 7: (2, "Chemical Equilibrium"),
    8: (2, "Chemical Equilibrium"), 9: (2, "Chemical Equilibrium"),
    10: (2, "Chemical Equilibrium"), 11: (2, "Chemical Equilibrium"),
    12: (3, "Ionic Equilibrium"), 13: (3, "Ionic Equilibrium"),
    14: (3, "Ionic Equilibrium"), 15: (3, "Ionic Equilibrium"),
    16: (3, "Ionic Equilibrium"),
    17: (4, "Organic Chemistry"), 18: (4, "Organic Chemistry"),
    19: (4, "Organic Chemistry"), 20: (4, "Organic Chemistry"),
}
# Every subject's map, gathered into one dict-of-dicts so code can look up
# "the chapter map for subject X" generically instead of an if/elif chain.
CHAPTER_MAP = {"Phy": PHYSICS_CH, "Chem": CHEM_CH, "Maths": MATHS_CH}


# ── Optional layer 1: read the machine-readable syllabus spreadsheet ───────────
# If a maintained "Syllabus-PCM-BT.xlsx" spreadsheet exists in the
# Telegram-download project (this is written by that project as a
# human-editable machine-readable copy of the syllabus), prefer reading the
# CURRENT chapter numbers/names from it — that way, editing chapter names
# just means editing a spreadsheet, no code change needed. If the file is
# missing, unreadable, or the openpyxl package isn't installed, this quietly
# falls back to the CHAPTER_MAP dictionaries above, so this project never
# actually NEEDS the spreadsheet to function correctly.
SYLLABUS_XLS = config.DEFAULT_TRANSCRIPT_SRC / "general-files" / "Syllabus-PCM-BT.xlsx"
# Small in-memory cache so we don't re-read/re-parse the spreadsheet from disk
# on every single file classified in a run — only when it's never been read
# yet, or its on-disk modified time has changed since the last read.
_SYLL_CACHE = None
_SYLL_MTIME = None


def load_chapter_maps() -> dict:
    """{'Phy'/'Chem'/'Maths': {lecture: (chapter_no, chapter_name)}}.

    Tries the syllabus spreadsheet first (if present and readable); falls
    back to the hardcoded CHAPTER_MAP above otherwise. This is the ONLY
    function the rest of the project should call to get chapter data — never
    read PHYSICS_CH / CHEM_CH / MATHS_CH directly, so the spreadsheet-override
    behaviour is respected everywhere.
    """
    global _SYLL_CACHE, _SYLL_MTIME
    fallback = CHAPTER_MAP
    if openpyxl is None or not SYLLABUS_XLS.exists():
        return fallback
    try:
        mtime = SYLLABUS_XLS.stat().st_mtime
    except OSError:
        # File existed a moment ago (the .exists() check above) but is now
        # unreadable (e.g. a sync client mid-download) — use whatever we
        # cached last time, or the hardcoded fallback if we never cached.
        return _SYLL_CACHE or fallback
    if _SYLL_CACHE is not None and mtime == _SYLL_MTIME:
        return _SYLL_CACHE   # unchanged since last read; reuse the cache
    try:
        wb = openpyxl.load_workbook(SYLLABUS_XLS, read_only=True, data_only=True)
        ws = wb["Syllabus"]
        maps = {"Phy": {}, "Chem": {}, "Maths": {}}
        full_to_code = {"Physics": "Phy", "Chemistry": "Chem", "Maths": "Maths",
                        "Phy": "Phy", "Chem": "Chem"}
        headers = None
        for row in ws.iter_rows(values_only=True):
            if headers is None:
                # First row = column titles, e.g. "Subject", "Lecture", ...
                # Lower-cased so the lookups below aren't case-sensitive.
                headers = [str(c).strip().lower() if c else "" for c in row]
                continue
            rec = dict(zip(headers, row))
            code = full_to_code.get((rec.get("subject") or "").strip())
            if code not in maps:
                continue
            try:
                lec = int(rec["lecture"]); chno = int(rec["chapter no"])
            except (TypeError, ValueError, KeyError):
                continue   # a malformed row is skipped, not fatal
            maps[code][lec] = (chno, str(rec.get("chapter name") or "").strip())
        wb.close()
        if any(maps.values()):   # only trust it if at least one row parsed
            _SYLL_CACHE, _SYLL_MTIME = maps, mtime
            return maps
    except Exception:
        pass   # any spreadsheet-reading problem: fall back below, never crash
    return fallback


# ── Optional layer 2: timetable-based subject resolution ───────────────────────
# A transcript's FILENAME sometimes doesn't mention the subject clearly, but
# we usually also know the class TIMETABLE — which subject was scheduled on
# which date. A transcript uploaded shortly after a class is almost certainly
# that class's transcript. `timetable_subjects.json` (if present) is a
# {date: {"subject": "..."}} lookup table built by the Telegram-download
# project from the weekly schedule. Like the spreadsheet above, this is a
# purely OPTIONAL extra signal — if the file is missing, subject resolution
# just uses the filename instead (see resolve_subject() below).
SUBJECT_INDEX_FILE = config.DEFAULT_TRANSCRIPT_SRC / "scripts" / "timetable_subjects.json"
_FULL_TO_CODE = {"Physics": "Phy", "Chemistry": "Chem", "Maths": "Maths",
                 "Logic": "Logic", "Coding": "Coding"}
NEAR_DAYS = 10   # a transcript may be posted a few days after its class
_INDEX_CACHE = None
_INDEX_MTIME = None


def load_subject_index() -> dict:
    """Load {date_iso: {'subject': ...}}, cached, reloaded when the file changes."""
    global _INDEX_CACHE, _INDEX_MTIME
    try:
        mtime = SUBJECT_INDEX_FILE.stat().st_mtime
    except OSError:
        return _INDEX_CACHE or {}   # file doesn't exist (or unreadable) -> no signal
    if _INDEX_CACHE is None or mtime != _INDEX_MTIME:
        try:
            _INDEX_CACHE = json.loads(SUBJECT_INDEX_FILE.read_text(encoding="utf-8"))
        except Exception:
            _INDEX_CACHE = {}
        _INDEX_MTIME = mtime
    return _INDEX_CACHE


def subject_from_timetable(fdate, index=None):
    """(subject_code | None, exact_date_match: bool) for a transcript dated fdate:
    the class scheduled on the latest timetable date <= fdate, within NEAR_DAYS."""
    if index is None:
        index = load_subject_index()
    if not index or fdate is None:
        return None, False
    best = None   # will hold (class_date, subject) for the best match found so far
    for iso, info in index.items():
        try:
            cd = date.fromisoformat(iso)
        except ValueError:
            continue
        if cd <= fdate and (fdate - cd).days <= NEAR_DAYS:
            if best is None or cd > best[0]:
                best = (cd, info.get("subject"))
    if best is None or not best[1]:
        return None, False
    return _FULL_TO_CODE.get(best[1], best[1]), (best[0] == fdate)


# ── Filename parsing helpers ─────────────────────────────────────────────────
# `re.compile(...)` pre-builds a regular expression once (faster than
# re-parsing the pattern text every time it's used). `(?:...)` is a
# "non-capturing group" — groups characters together for the `?`/`*`
# quantifiers without creating an extra numbered capture. `[\s._-]*`
# matches zero or more of: space, dot, underscore, hyphen (so "lec5",
# "lec-5", "lec 5", "lec_5", "lecture5" all match the same pattern).
_LEC_RE = re.compile(r"lec(?:ture)?[\s._-]*0*(\d+)", re.IGNORECASE)
DATE_PREFIX_RE = re.compile(r"^(\d{2}-\d{2}-\d{2})_(.*)$")
# A file that's already been renamed by the downloader looks like
# "<date>_<Subject>_Ch-<n>_<Chapter-Name>_...". If we see that shape, the
# chapter is already baked into the filename — no need to look it up again.
_RENAMED_CH_RE = re.compile(r"^\d{2}-\d{2}-\d{2}_(?:Phy|Chem|Maths)_Ch-(\d+)_([^_]+)")

# A file is "general" (not a class transcript) if its name contains any of
# these — checked case-insensitively via `filename.lower()` before matching.
GENERAL_KEYWORDS = [
    "doubt tt", "doubt forum", "doubt  tt",
    "a27_pimple_saudagar_offline_champions",   # weekly schedule PDF
    "syllabus", "answer key", "brochure", "poster", "notice",
    "workshop", "-schedule", "-calendar",
]
GENERAL_EXTS = {".ics", ".jpg", ".jpeg", ".png", ".webp"}

# Signals that a file IS a lecture transcript (beyond just "has a lecture
# number in the name", which is checked separately below).
TRANSCRIPT_KEYWORDS = [
    "phy fa27", "fadv", "champ", "logic lecture", "basics of chess",
    "fav-lec", "_ch-",   # "_ch-" = already-renamed transcript
]


def strip_date_prefix(name: str) -> tuple:
    """Return (date_prefix, remainder). date_prefix is '' if the filename
    doesn't start with a YY-MM-DD date."""
    m = DATE_PREFIX_RE.match(name)
    if m:
        return m.group(1), m.group(2)
    return "", name


def file_date(name: str):
    """Parse the YY-MM-DD prefix into a real `date` object, or None if absent
    or not a valid calendar date."""
    prefix, _ = strip_date_prefix(name)
    if not prefix:
        return None
    try:
        return datetime.strptime(prefix, "%y-%m-%d").date()
    except ValueError:
        return None


def detect_subject(name: str):
    """Best-effort subject purely from the filename TEXT (no timetable
    lookup — see resolve_subject() below for the combined version). Logic/
    Coding are checked before Physics/Chemistry/Maths because some of their
    filenames could otherwise be mistaken for one of the PCM subjects."""
    low = name.lower()
    ext = Path(name).suffix.lower()
    if "chess" in low or "logic" in low:
        return "Logic"
    if ext == ".py" or "coding" in low:
        return "Coding"
    if "phy" in low or "physics" in low:
        return "Phy"
    if "maths" in low or "math" in low:
        return "Maths"
    # Chemistry: teacher code "c7", or explicit words. ("fadv"/"champ" are
    # generic batch-name words, only trusted as a last resort below.)
    if "c7" in low or "chem" in low or "chemistry" in low or "fav-lec" in low:
        return "Chem"
    if "fadv" in low or "champ" in low:
        return "Chem"
    return None


def resolve_subject(name: str, index=None):
    """Subject code for a transcript: combines the TIMETABLE (by date) and the
    FILENAME, and returns (subject_code | None, source_string) so callers can
    log/display *how* the subject was decided.

      - timetable knows + filename agrees or is silent  -> use timetable subject
      - filename knows + timetable is silent             -> use filename subject
      - the two DISAGREE                                 -> (None, 'subject conflict ...')
        so the caller can park the file for human review instead of silently
        guessing wrong (a conflict usually means a late-posted file landed on
        a date that belongs to a different subject's class).
    """
    fsub = detect_subject(name)                       # filename-based (may be None)
    tsub, exact = subject_from_timetable(file_date(name), index)
    if tsub and fsub and tsub != fsub:
        return None, f"subject conflict: filename={fsub} vs timetable={tsub}"
    if tsub:
        return tsub, ("timetable(exact)" if exact else "timetable(nearest)")
    if fsub:
        return fsub, "filename"
    return None, "unresolved"


def detect_lecture_no(name: str):
    """The lecture number in a filename (e.g. "Lec5", "lecture-05"), or None."""
    m = _LEC_RE.search(name)
    return int(m.group(1)) if m else None


def chapter_from_name(name: str):
    """(chapter_no, chapter_name) for a transcript filename, worked out two ways:
      1. If the filename was already renamed to the "<date>_<Subject>_Ch-<n>_
         <Name>_..." shape, read the chapter straight out of the name.
      2. Otherwise, resolve the subject + lecture number and look the chapter
         up in load_chapter_maps().
    Returns None if neither approach works (e.g. subject/lecture unclear)."""
    m = _RENAMED_CH_RE.match(Path(name).stem)   # .stem = filename without extension
    if m:
        return int(m.group(1)), m.group(2).replace("-", " ")
    subject, _ = resolve_subject(name)
    lec = detect_lecture_no(name)
    if subject in ("Phy", "Chem", "Maths") and lec:
        return load_chapter_maps().get(subject, {}).get(lec)
    return None


def classify(name: str) -> str:
    """'transcript' or 'general'. Unknown/ambiguous filenames default to
    'general' — it's safer to park an unclear file for a human to look at
    than to mis-file it as a chapter transcript."""
    low = name.lower()
    ext = Path(name).suffix.lower()
    if any(k in low for k in GENERAL_KEYWORDS):
        return "general"
    if ext in GENERAL_EXTS:
        return "general"
    if _LEC_RE.search(low):
        return "transcript"
    if any(k in low for k in TRANSCRIPT_KEYWORDS):
        return "transcript"
    # A file named just after a subject (e.g. "26-07-12_Maths.pdf") is a
    # transcript even with no lecture number in the name (it just can't be
    # auto-renamed by the downloader; it can still be routed by this project).
    _, remainder = strip_date_prefix(name)
    if Path(remainder).stem.lower() in {"maths", "math", "physics", "phy", "chemistry", "chem"}:
        return "transcript"
    # Timetable clincher: a file dated exactly on a scheduled class date that
    # dodged every "general" signal above is almost certainly that class's
    # transcript, even if its name gives no other clue.
    fdate = file_date(name)
    if fdate and fdate.isoformat() in load_subject_index():
        return "transcript"
    return "general"


# ── Self-test against real historical filenames ─────────────────────────────
# This isn't run automatically — it's a quick manual sanity check you can run
# any time with:  python3 src/func_classify_and_rename.py --self-test
# (the same real-world filenames and expected answers are also exercised
# automatically by tests/test_classify_and_rename.py, which pytest runs.)
_SELFTEST = [
    # (filename, expected classify() result, expected chapter_from_name() result)
    ("26-05-08_Phy FA27 Lec1.pdf", "transcript", (1, "Force & Laws of Motion")),
    ("26-05-31_Phy FA27 Lec2.pdf", "transcript", (1, "Force & Laws of Motion")),
    ("26-06-05_Phy FA27 Lec3.pdf", "transcript", (2, "Work Energy Theorem")),
    ("26-07-17_Phy FA27 Lec6.pdf", "transcript", (4, "Vectors")),
    ("26-05-10_FAV-lec01-PS CHAMP_C7.pdf", "transcript", (1, "Gaseous State")),
    ("26-06-09_fadv lec 3 champ c7.pdf", "transcript", (1, "Gaseous State")),
    ("26-05-03_01 Offline  Doubt TT.pdf", "general", None),
    ("26-05-22_Syllabus BTest 1 - Foundation Adv.pdf", "general", None),
    ("26-06-28_F.ADV Answer Key BTest-1.pdf", "general", None),
    ("26-07-04_VVM2026-27_brochure.pdf", "general", None),
    ("26-06-01_photo_100.jpg", "general", None),
    ("26-06-22-Schedule.ics", "general", None),
]


def _self_test() -> int:
    ok = 0
    print(f"{'RESULT':7} {'FILE':45} {'CLASS':10} CHAPTER")
    print("-" * 100)
    for name, exp_class, exp_chapter in _SELFTEST:
        got_class = classify(name)
        got_chapter = chapter_from_name(name) if got_class == "transcript" else None
        passed = (got_class == exp_class) and (got_chapter == exp_chapter)
        ok += passed
        flag = "PASS" if passed else "FAIL"
        print(f"[{flag}] {name[:45]:45} {got_class:10} {got_chapter}")
        if not passed:
            print(f"        ^ expected class={exp_class!r} chapter={exp_chapter!r}")
    print("-" * 100)
    print(f"{ok}/{len(_SELFTEST)} passed")
    return 0 if ok == len(_SELFTEST) else 1


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    for arg in sys.argv[1:]:
        base = Path(arg).name
        kind = classify(base)
        line = f"{base}  ->  [{kind}]"
        if kind == "transcript":
            line += f"  {chapter_from_name(base)}"
        print(line)
