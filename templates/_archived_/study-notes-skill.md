# Study Notes Generator

Produce ONE chapter's self-study notes as a `.docx`. Work silently, one pass, deliver the
file with one or two sentences. No progress narration, no mid-task confirmation.

## Inputs (from the folder you were given)
- Subject + chapter (required). Scope = this chapter only; never attempt other chapters.
- Class/level (reading age is 13 regardless).
- Optional: transcript, textbook scans, prepared summary, a draft to improve.

## Audience
Reader is 13, studying alone, no teacher. No JEE/NEET/IIT framing. Define terms on first
use. Short sentences. Every abstract concept gets a real-world analogy (cricket, phone
batteries, elevators, marbles, tug-of-war). For every formula: what each symbol means and
why it's shaped that way.

When the source is above level (coaching-institute/JEE-foundation transcripts often are): keep the
content complete but do the work to make it self-studiable — strip exam branding, add
scaffolding and analogies where the concept is hardest. Softening tone alone is not enough.

## Sources
1. Transcript attached → extract every concept, worked problem, aside; mark "(from class)".
   Completeness vs transcript is non-negotiable.
2. Summary/skeleton → optional spine to expand. If absent, build from transcript +
   research; absence is not a gap.
3. Textbook scans → match terminology, notation, sign conventions. On conflict, follow the
   textbook and note it.
4. Draft attached ("improve my draft") → review task: keep what works, fix what's wrong,
   and report kept/changed/dropped in one line at delivery.
5. Research the web for depth. Depth = (a) more explanation, never harder prose, and
   (b) diversity of problem types the concept applies to, so the reader is rarely caught
   off-guard later. Minimise the "waao — never seen this" factor.

If a source is internally contradictory (e.g. "same T/P/V" holds P constant while molar
mass varies — impossible): say so in one line, state your interpretation, proceed. Do not
stop; do not resolve silently.

## Structure
Numbered sections; sub-sections 1.1, 1.2. Per section: explanation → analogy → boxed
formula → step-by-step derivation (reason for each step) → 1–2 worked examples → "watch
out" note. Where a chapter has parallel cases/methods, each gets its own diagram + fully
worked problem.

Include: title block; **What You Will Learn** box (12pt, not 10pt); opening hook (2–3
paras, no formulas); TOC (Word field — note it renders empty until "Update Field");
decision guide (table/flowchart for choosing method/case); Common Mistakes; Master Formula
Reference Card (one page); One-Page Recap; 15+ practice problems colour-coded by
difficulty, **ramped** to the top of the class range by the last; single-page A4 cheat
sheet.

Practice problems: ALL questions first → page break → ALL solutions, fully worked.

## Visuals — core, not garnish
More than ~½ page of prose needs a visual. Annotated diagrams (labelled callouts), plots,
graphs, flowcharts throughout. **Every figure must actually exist and be embedded** —
never list one as planned. Italic caption on each. Callouts: yellow analogies, blue key
ideas, green worked examples, red pitfalls. Tables for anything comparative.

- Generate with `matplotlib`, saved as SVG. Convert SVG -> PNG using the `tool_convert_to_png`
  tool (internally: `soffice --headless --convert-to pdf` then `pdftoppm -r 200 -png`;
  `cairosvg`/`rsvg-convert` are often unavailable in this environment, so don't rely on them).
- Never render trees/concept-maps as ASCII (`+--`, `|`, `>>`) — use tables or diagrams.
- Diagram QA before embedding: use the `tool_view_image` tool to actually LOOK at every
  generated PNG before embedding it. Check: arrows have arrowheads (`FancyArrowPatch`,
  `-|>`); labels don't overlap arcs/arrows/atoms (box them or use leader lines); titles
  don't overlap the first element; give crowded figures more height. Fix and re-render if
  any of these fail — do not embed a diagram you have not visually checked.

## Formatting
- `.docx` via Node.js `docx` library (run via the `tool_bash` tool, e.g. `node build.js`).
  A4, 0.75" margins. Body 12pt Calibri.
- Headers dark blue (#1F4E79 H1/title, #2E75B6 subheads). Header every page; footer
  "Page N of M".
- Real symbols (√ ² × ÷ ≈ θ π ∴ ⇒), never ASCII (sqrt, ^2, pi, theta, *).
- Divisions = **stacked fractions** via OMML (numerator / bar / denominator). Never inline
  `a / b`.
- Formula runs in **Cambria, NOT Cambria Math** (its √ drops below baseline as a text run).
- Chemistry: real sub/superscripts (H₃PO₄, MnO₄⁻, S₂O₃²⁻), never H3PO4. Text formulae
  only, never LaTeX; chemistry formulae centred, bold, boxed.
- Separate numbering reference per list, else counts continue instead of restarting at 1.
- Target is MS Word. Ignore LibreOffice rendering limits (it drops OMML math entirely — do
  not use it to verify fractions). Optimise for print: fewest pages without cramping.

## Accuracy — a passing validator only means the file opens
- **Fraction parsing:** split on phrase boundaries (=, ≈, ⇒, comma, semicolon) FIRST, then
  find `/` within each phrase, then walk outward for a clean atom. Recognise function names
  so `sin θ / cos θ` → `(sin θ)/(cos θ)`. Never take the last `/` as the bar. (This bug
  once made ~60/84 fractions wrong in a doc reported as clean.)
- **Code hygiene:** type-guard rich-content helpers (`typeof run === 'string'`); a bare
  string hitting a `.bold` mapper serializes `String.prototype.bold()` into the XML and
  fails validation. Make banner prefixes conditional.
- Recompute every worked example independently. Audit EVERY fraction, not a subset. If
  revising, diff every formula against the prior version. State sign conventions once.
  Carry units through.
- **Convert to PDF and inspect EVERY page** — use `tool_bash` to run
  `soffice --headless --convert-to pdf` on the finished .docx, then `tool_view_pdf_page`
  once per page (not a sample) to visually proofread it before declaring the document done.

## Pre-delivery checklist
Every transcript concept present · every figure exists/embedded/annotated/captioned ·
every worked example recomputed · EVERY fraction audited · formulas diffed if a revision ·
stacked fractions, no inline `/` · Cambria (not Cambria Math), radicals on-baseline ·
chemistry sub/superscripts + centred bold boxed formulae · no ASCII trees · diagram-QA
pass (every diagram viewed with `tool_view_image`) · numbered lists restart at 1 ·
dark-blue headers, 12pt Calibri, A4 · overview box 12pt · Q&A separated by page break ·
cheat sheet fits one A4 page · no duplicated example across sections · draft-review
reported if a draft was attached · hardest sections scaffolded for solo study · PDF
inspected every page with `tool_view_pdf_page`.
