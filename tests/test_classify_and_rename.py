"""
test_classify_and_rename.py

Battle-tests for src/func_classify_and_rename.py -- the module that decides
which real chapter a transcript/collected file belongs to. This is the
single most important correctness check in the whole project: a wrong
answer here doesn't crash anything, it just silently files a file under
the WRONG chapter, and that used to actually happen in production (see
the project history: a previous, fabricated taxonomy created garbage
folders like "Physics-Ch1-Chapter-1" instead of the real
"Physics-Ch1-Force-and-Laws-of-Motion").

These test cases use REAL filenames the pipeline has actually seen, with
their REAL, syllabus-verified expected answers -- not made-up examples.
"""

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR / "config"))
sys.path.insert(0, str(ROOT_DIR))

import src.func_classify_and_rename as cr


# ── classify() + chapter_from_name(): real historical filenames ────────────
# Each entry: (filename, expected classify() result, expected chapter_from_name() result)
REAL_TRANSCRIPT_CASES = [
    ("26-05-08_Phy FA27 Lec1.pdf", "transcript", (1, "Force & Laws of Motion")),
    ("26-05-31_Phy FA27 Lec2.pdf", "transcript", (1, "Force & Laws of Motion")),
    ("26-06-05_Phy FA27 Lec3.pdf", "transcript", (2, "Work Energy Theorem")),
    ("26-07-10_Phy FA27 Lec5-Physics.pdf", "transcript", (4, "Vectors")),
    ("26-07-17_Phy FA27 Lec6.pdf", "transcript", (4, "Vectors")),
    ("26-05-10_FAV-lec01-PS CHAMP_C7.pdf", "transcript", (1, "Gaseous State")),
    ("26-05-22_Lec02-fadv c7 champ.pdf", "transcript", (1, "Gaseous State")),
    ("26-06-09_fadv lec 3 champ c7.pdf", "transcript", (1, "Gaseous State")),
    ("26-07-08_fadv lec 5 c7-Chemistry.pdf", "transcript", (1, "Gaseous State")),
]

REAL_GENERAL_CASES = [
    "26-05-03_01 Offline  Doubt TT.pdf",
    "26-05-22_Syllabus BTest 1 - Foundation Adv.pdf",
    "26-06-28_F.ADV Answer Key BTest-1.pdf",
    "26-07-04_VVM2026-27_brochure.pdf",
    "26-06-27_DHBBVC_2026_notice.pdf",
    "26-07-02_NSE-2026-Poster.pdf",
    "26-06-01_photo_100.jpg",
    "26-06-22-Schedule.ics",
]

# A file that's already been through the downloader's renaming step: the
# chapter is baked straight into the filename in the
# "<date>_<Subject>_Ch-<n>_<Name>_..." shape.
ALREADY_RENAMED_CASES = [
    ("26-05-08_Phy_Ch-1_Force-&-Laws-of-Motion_FA27-Lec1.pdf", (1, "Force & Laws of Motion")),
    ("26-06-05_Phy_Ch-2_Work-Energy-Theorem_FA27-Lec3.pdf", (2, "Work Energy Theorem")),
    ("26-05-10_Chem_Ch-1_Gaseous-State_FAV-lec01-PS-CHAMP-C7.pdf", (1, "Gaseous State")),
]


def test_classify_real_transcripts():
    for name, expected_class, _ in REAL_TRANSCRIPT_CASES:
        assert cr.classify(name) == expected_class, f"classify({name!r}) misclassified"


def test_chapter_from_name_real_transcripts():
    for name, _, expected_chapter in REAL_TRANSCRIPT_CASES:
        got = cr.chapter_from_name(name)
        assert got == expected_chapter, f"chapter_from_name({name!r}) = {got}, expected {expected_chapter}"


def test_classify_real_general_files():
    for name in REAL_GENERAL_CASES:
        assert cr.classify(name) == "general", f"classify({name!r}) should be 'general'"


def test_chapter_from_name_already_renamed():
    """A transcript that already carries its chapter in the filename (the
    downloader's own renaming convention) must be read straight off the
    name, without needing the lecture-number lookup table at all."""
    for name, expected_chapter in ALREADY_RENAMED_CASES:
        got = cr.chapter_from_name(name)
        assert got == expected_chapter, f"chapter_from_name({name!r}) = {got}, expected {expected_chapter}"


# ── resolve_subject(): filename-only resolution (no timetable file present) ─

def test_resolve_subject_physics_filename():
    # Passing index={} explicitly bypasses the OPTIONAL timetable file (see
    # resolve_subject()'s `index` parameter) so this test checks pure
    # filename-based resolution regardless of whether a real
    # timetable_subjects.json happens to exist on the machine running the
    # tests -- resolve_subject("...", index=None) would otherwise pick up
    # the real one if present and correctly prefer it, which is desired
    # behaviour in production but would make this specific test
    # machine-dependent.
    code, source = cr.resolve_subject("26-05-08_Phy FA27 Lec1.pdf", index={})
    assert code == "Phy"
    assert source == "filename"


def test_resolve_subject_chemistry_filename():
    code, source = cr.resolve_subject("26-05-10_FAV-lec01-PS CHAMP_C7.pdf", index={})
    assert code == "Chem"


def test_resolve_subject_unresolved():
    code, source = cr.resolve_subject("random_file_with_no_subject_hint.pdf")
    assert code is None
    assert source == "unresolved"


# ── detect_lecture_no(): different separator styles must all match ─────────

def test_detect_lecture_no_variants():
    assert cr.detect_lecture_no("26-05-08_Phy FA27 Lec1.pdf") == 1
    assert cr.detect_lecture_no("something-lecture-05.pdf") == 5
    assert cr.detect_lecture_no("lec_12.pdf") == 12
    assert cr.detect_lecture_no("no lecture number here.pdf") is None


# ── file_date(): the YY-MM-DD filename prefix ───────────────────────────────

def test_file_date_valid_prefix():
    d = cr.file_date("26-07-17_Phy FA27 Lec6.pdf")
    assert d is not None
    assert (d.year, d.month, d.day) == (2026, 7, 17)


def test_file_date_no_prefix():
    assert cr.file_date("no_date_here.pdf") is None


# ── load_chapter_maps(): falls back to the hardcoded real syllabus data ────

def test_load_chapter_maps_matches_hardcoded_fallback_when_no_spreadsheet():
    """Without a Syllabus-PCM-BT.xlsx present (the normal case for most
    machines), load_chapter_maps() must return exactly the hardcoded,
    syllabus-verified chapter data -- this is what makes the classifier
    correct even with zero optional files present, i.e. genuinely
    self-sufficient."""
    maps = cr.load_chapter_maps()
    assert maps["Phy"][1] == (1, "Force & Laws of Motion")
    assert maps["Chem"][1] == (1, "Gaseous State")
    assert maps["Maths"][5] == (2, "Circles")


# ── full_name_to_code(): the reverse of SUBJECT_FULL ────────────────────────

def test_full_name_to_code_known_subjects():
    assert cr.full_name_to_code("Physics") == "Phy"
    assert cr.full_name_to_code("Chemistry") == "Chem"
    assert cr.full_name_to_code("Maths") == "Maths"


def test_full_name_to_code_unknown():
    assert cr.full_name_to_code("Biology") is None
