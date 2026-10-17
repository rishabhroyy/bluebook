"""Text for the glasses: glyph sanitising, pagination, and the strings each screen shows.

The G2 font is proportional with no size control, and glyphs outside its set are silently
dropped, so every string goes through sanitize() before it reaches a container.
Char budgets are v1 guesses; tune them on hardware and record the numbers in docs/spikes.md.
"""

from __future__ import annotations

import unicodedata

from bluebook_core.skill import SkillPackage, Step

TIP_CHARS = 120  # teach_page "tip": ~3 lines
STATUS_CHARS = 60  # teach_page "status": one dim line
MSG_CHARS = 1000  # message_page build limit (SDK)
MAX_ANSWER_PAGES = 4

# Non-ASCII glyphs the G2 font has (workstreams/part2-teaching.md), plus box drawing U+2500..U+257F.
GLYPHS = set("█▲△▶▷▼▽◀◁●○■□★☆")
_ASCII = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        "⁄": "/",
        "•": "-",
        "·": "|",
        "×": "x",
        "°": " deg",
        "→": "->",
        "←": "<-",
        "\t": " ",
    }
)

SEVERITY_ORDER = {"danger": 0, "caution": 1, "info": 2}

EMPTY_LIBRARY = (
    "No skills yet.\nCopy a skill folder into data/skills on the laptop, then reopen bluebook.\n\nDouble tap to exit."
)
LIBRARY_TITLE = "Pick a skill. Hold to ask."


def sanitize(text: str) -> str:
    """Map common typography to ASCII, strip accents, drop anything the font can't draw."""
    out = []
    for ch in unicodedata.normalize("NFKD", text).translate(_ASCII):
        if ch == "\n" or " " <= ch <= "~" or ch in GLYPHS or "─" <= ch <= "╿":
            out.append(ch)
    return "".join(out)


def paginate(text: str, limit: int = TIP_CHARS) -> list[str]:
    """Split on word boundaries into pages of <= limit chars. Newlines become spaces."""
    pages: list[str] = []
    cur = ""
    for w in sanitize(text).split():
        while len(w) > limit:  # unbreakable run, e.g. a part number or URL
            if cur:
                pages.append(cur)
                cur = ""
            pages.append(w[:limit])
            w = w[limit:]
        if not cur:
            cur = w
        elif len(cur) + 1 + len(w) <= limit:
            cur += " " + w
        else:
            pages.append(cur)
            cur = w
    if cur:
        pages.append(cur)
    return pages


def step_notes(pkg: SkillPackage, step: Step) -> list[str]:
    """Package-level safety notes that name this step, most severe first."""
    notes = sorted((n for n in pkg.safety if step.id in n.step_ids), key=lambda n: SEVERITY_ORDER[n.severity])
    return [f"{n.severity.capitalize()}. {n.text}" for n in notes]


def detail_pages(pkg: SkillPackage, step: Step, extra_substeps: list[str] = ()) -> list[str]:
    """Scroll order on a step: tooltip, instruction, substeps, safety + warnings, tips, why."""
    pages = paginate(step.tooltip) or [""]
    pages += paginate(step.instruction)
    subs = [*step.substeps, *extra_substeps]
    for i, t in enumerate(subs, 1):
        pages += paginate(f"Substep {i}/{len(subs)}. {t}")
    for t in step_notes(pkg, step) + [f"Warning. {w}" for w in step.warnings]:
        pages += paginate(t)
    for t in step.tips:
        pages += paginate(f"Tip. {t}")
    if step.why:
        pages += paginate(f"Why. {step.why}")
    return pages


def answer_pages(pages: list[str]) -> list[str]:
    """Model output is not trusted to respect the page budget: re-paginate and cap."""
    out = [p for page in pages for p in paginate(page)]
    return out[:MAX_ANSWER_PAGES]


def status_line(pkg: SkillPackage, i: int, view: int, n_pages: int, note: str = "") -> str:
    """'2/5 | Turn off water | 1/4' or, with a note, '2/5 | ● listening'."""
    step = pkg.steps[i]
    parts = [f"{i + 1}/{len(pkg.steps)}"]
    if note:
        parts.append(note)
    else:
        parts.append(step.title + (" (optional)" if step.optional else ""))
        if n_pages > 1:
            parts.append(f"{view + 1}/{n_pages}")
    return sanitize(" | ".join(parts))[:STATUS_CHARS]


def overview_text(pkg: SkillPackage) -> str:
    """Built most important first, then cut on a line boundary, so truncation never drops safety."""
    lines = [pkg.title, "Tap to start. Double tap for the library."]
    n = len(pkg.steps)
    lines.append(f"{n} steps, about {round(pkg.est_duration_s / 60)} min." if pkg.est_duration_s else f"{n} steps.")
    for note in sorted(pkg.safety, key=lambda s: SEVERITY_ORDER[s.severity]):
        hedge = " Typically, check your manual." if note.provenance == "inferred" else ""
        lines.append(f"{note.severity.upper()}. {note.text}{hedge}")
    need = [o.name for o in pkg.objects if o.category in ("tool", "material", "safety_gear")]
    if need:
        lines.append("You need: " + ", ".join(need) + ".")
    if pkg.prerequisites:
        lines.append("Before you start: " + ", ".join(pkg.prerequisites) + ".")
    lines.append(pkg.summary)
    out = ""
    for line in (sanitize(x) for x in lines):
        if len(out) + len(line) + 1 > MSG_CHARS:
            break
        out += line + "\n"
    return out.rstrip() or sanitize(pkg.title)[:MSG_CHARS]


def done_text(pkg: SkillPackage) -> str:
    return sanitize(f"Done. {pkg.title}\n\nTap for the library. Double tap to go back to the last step.")[:MSG_CHARS]


def is_danger(pkg: SkillPackage, step: Step) -> bool:
    """Steps under a danger note (or a whole-task danger note) are never auto-confirmed."""
    return any(n.severity == "danger" and (not n.step_ids or step.id in n.step_ids) for n in pkg.safety)
