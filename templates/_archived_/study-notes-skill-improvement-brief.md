# Brief: rebuild the `study-notes` skill as a token- and time-efficient pipeline

Paste this as a single instruction. It assumes you have access to a folder containing a
previous production run: the finished document, the working scripts under `_build/`, and
a `SESSION_LOG.md` describing how it was made.

---

## Context

The `study-notes` skill generates print-ready `.docx` self-study notes for one school
chapter (Physics / Chemistry / Maths), written so a 13-year-old can study alone.

A previous run produced **`Circles_Class9_Study_Notes.docx`** — 56 pages, ~16,800 words,
40 annotated figures, 20 practice problems, 21 worked examples. **I approve of that
document** for accuracy, diagram clarity, content organisation, presentation and
worked-example quality. It is the reference. Its working files are in `_build/`.

The run cost roughly 158,000 distinct tokens and took about 40 minutes across ~45 model
turns. The skill itself was a single ~7 KB `SKILL.md` with no executable code, so every
mechanical thing was re-derived from scratch and 13 defects were introduced and fixed by
hand during the session.

## Task

Rebuild the skill so it ships **prebuilt scripts** and the model writes **data, not code**.
Cut token cost and wall-clock time substantially, without any loss of output quality.

## Working constraints

- **Treat the provided folder as read-only.** Do not modify anything in it. Deliverables
  are new files only.
- **Show me a plan before you start**, and wait for approval.
- **Ask for my approval before installing or updating the skill.** The skill directory is
  a read-only cache — package the result as a `.skill` bundle (a zip of the skill
  directory) so it installs with all supporting files, since a SKILL.md-only save cannot
  carry them.
- Prefer measuring over asserting. Where you claim a saving, show the number you measured
  it from. If a claim turns out wrong, say so plainly rather than quietly dropping it.

## Quality is the fixed point — non-negotiable

Derive quality gates **from the reference document**, not from invented targets, and
enforce them in code:

```
DENSITY (scale-invariant)
  words per figure       <= 525    (reference 420)
  figures per section    >= 1.2    (reference 2.22)
  annotations per figure >= 5.0    (reference 6.5: labels, callouts, arcs, ticks)
  words per sentence     <= 18     (reference 15.0)
  callout kinds used     >= 4 of 5 (reference used all five)
  checks per numeric answer >= 1.0 (reference 48 checks for 41 answers)
  inline-slash divisions  = 0

COVERAGE — the SOURCE sets the scope
  every transcript topic present in the notes
  worked examples in the notes >= worked examples in the source
```

**No absolute size targets.** Document length depends on the scope of the chapter; a word
or figure count would either fail a legitimately short chapter or mean nothing for a long
one. Express quality only as density ratios and source coverage.

**Do not let optimisation erode quality.** Every token and time saving pushes toward less:
fewer figures, thinner prose, fewer checks. Individually each looks like efficiency;
together they are how the document gets worse. Any change that lowers a gate is rejected
regardless of what it saves.

## Conciseness — treat this as an optimisation problem

Study notes that sprawl defeat their purpose. Set **ceilings** alongside the quality
floors so the document is forced into the band between:

```
  <= 2,000 words per section
  <=   420 words and <= 8 steps per worked example
```

## Gates that measure teaching, not syntax

Syntax checks are not enough. Implement gates for:

- **Diagram truth.** Measure the drawn geometry against every numeric claim: an angle
  labelled "80°" must measure 80°, equal tick marks require equal lengths, right-angle
  marks require true 90° corners. A readable-but-wrong diagram mis-teaches with total
  confidence, so it must fail the build.
- **Worked examples must explain why.** Use a structured block — given / find / steps as
  (what, why) pairs / answer / self-check — and reject any step whose reason is missing.
  A bare calculation teaches imitation, not understanding.
- **Teaching floors.** An analogy roughly every two teaching sections; a recall prompt
  (redraw, try it, check for yourself) every three; each figure's labelled points actually
  discussed by the prose around it; no dangling section cross-references.
- **Document skeleton.** Required sections present; ≥15 practice problems; every question
  matched to a solution; all questions, then a page break, then all solutions; difficulty
  ramping with hard problems in the final third.
- **Mechanics.** OOXML integrity, stacked OMML fractions (never inline `a/b`), page-break
  safety so no heading or solution box widows, margin overflow, figures embedded and
  captioned.

Note what these gates **cannot** catch — whether an analogy lands, whether prose flows,
whether a diagram teaches the *right* idea — and say so rather than implying coverage you
do not have.

## Figure style — match the reference

- Bold arc bands, saturated colours, both arc values labelled including reflex angles.
- Symbolic labels (θ, 2θ) in theorem figures; concrete numbers belong in worked examples.
- Parallel cases (Case 1 / 2 / 3) go in **one multi-panel figure** with per-panel
  sub-captions — not split apart or reduced to a single case.
- Summary notes inside the figure; callouts at the edges with short leaders.
- **Never resolve a layout collision by deleting content.** Add space — canvas, panels,
  label radius. Stripping bands or callouts passes the gate by making the figure worse.
- After redesigning a figure, check the prose for references to points it no longer has.

## Source handling

Class transcripts are typically **handwritten digital-whiteboard captures** — cursive on a
dark ground, mixed with hand-drawn geometry and notation.

- **Do not OCR them.** OCR fails on the handwriting, fails worse on the maths, returns
  nothing for the diagrams, and fails *confidently* — a corrupted transcript yields a
  polished, wrong document.
- **Read the page images in a subagent** and keep only the JSON it returns. Context is
  re-billed every turn, so images read early are paid for on every later turn.
- **Choose render resolution by measurement, not by guess.** Measure ink stroke width and
  pick the lowest legible dpi, capped by the point beyond which extra resolution is
  discarded anyway. Sources vary by more than 2× — a marker-on-whiteboard capture and
  handwriting over printed notes need very different settings.
- Autocrop to the ink bounding box and cache results on the file hash so a re-run or a
  revision costs nothing.

## Speed

The previous run's 40 minutes was **not** compute — figures render in seconds. It was ~45
sequential model turns against a context that grew past 150k tokens. Speed and token cost
share one lever: fewer turns, smaller context. Address it through prebuilt scripts,
fail-fast validation with precise error locations, incremental figure rebuilds, sectioned
content files so a rejection re-emits one section rather than the chapter, and patch-based
revisions.

## Self-improvement — after delivery only

Have the tools log what they rejected, and provide a retrospective that reports correction
rounds per stage, where the tokens went, and candidate skill changes derived from failures
actually observed.

- **Run it only once the document has been delivered.** Never interrupt document generation
  to ask about skill improvements.
- It proposes; it never edits the skill.
- Present candidates briefly, then ask for approval.
- **Before accepting any change, re-verify quality**: rebuild a verbatim section of the
  reference document through the current pipeline and compare it against the original, and
  re-run the quality gates. A change that degrades either is rejected.
- On approval, record accepted items in an accumulating lessons file so the next run starts
  from them.

## Deliverables

1. A short analysis of where the tokens and time actually went, with measured numbers.
2. The rebuilt skill as an installable `.skill` bundle: prebuilt libraries, validation and
   QA tooling, schemas, a worked example, the quality baseline, and the lessons file.
3. Evidence it works: negative tests showing each gate fires, and a full pipeline run from
   a read-only copy of the bundle.
4. **Regenerate one substantial section of the reference document through the new pipeline
   and compare it to the original** — word count, sentence length, callout mix, figure
   count and figure style. Show me the result before claiming success.
