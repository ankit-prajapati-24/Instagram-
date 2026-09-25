r"""Every caption style decision, as one thing you can choose.

The style used to be six loose arguments -- font, size, margin, two
colours, a punch size -- threaded from `Settings` through
`pipeline.render_stage` into `build_ass`. Nobody ever changed the font,
because there was nothing to change: you would have had to touch four
places and then render a whole reel to see it.

A Look is that one thing. The presets are answers already chosen,
rendered onto real footage and picked by eye rather than argued about.

`plain` is exactly what shipped before this module existed, and it is
the default, so adding looks restyles nothing until somebody picks.
"""

from __future__ import annotations

from dataclasses import dataclass

# ASS colours are &HAABBGGRR.
GOLD = "&H0000D7FF"
WHITE = "&H00FFFFFF"
DIM = "&H00909090"
YELLOW = "&H0000F0FF"
CYAN = "&H00F0E000"

# Where the caption block sits. Rendered with the phone's own controls
# drawn in: 220 puts the second line under them and 160 puts both, so
# every preset stays at 300 and a shorter font is what buys room.
MARGIN_V = 300

ANIMATIONS = ("fade", "stamp", "letters", "blur-in", "bounce", "swing",
              "flash")


@dataclass(frozen=True)
class Look:
    """One complete caption and punch treatment."""

    look_id: str
    label: str
    font: str
    caption_size: int
    spoken: str
    upcoming: str
    punch_font: str
    punch_size: int
    punch_animation: str
    margin_v: int = MARGIN_V


DEFAULT_LOOK_ID = "plain"

PRESETS: dict[str, Look] = {
    # Chosen from the rendered sheets.
    "blocky-urban": Look(
        "blocky-urban", "Blocky Urban", "Bungee", 56, GOLD, WHITE,
        "Bungee", 72, "letters"),
    "clean-modern": Look(
        "clean-modern", "Clean Modern", "Outfit Black", 70, GOLD, WHITE,
        "Outfit Black", 92, "stamp"),
    "editorial": Look(
        "editorial", "Editorial", "Playfair Display Black", 68, WHITE, DIM,
        "Playfair Display Black", 90, "blur-in"),
    "poster": Look(
        "poster", "Poster", "Staatliches", 84, YELLOW, WHITE,
        "Staatliches", 108, "bounce"),
    # The only bundled face that also draws Devanagari.
    "indian-display": Look(
        "indian-display", "Indian Display", "Teko", 92, GOLD, WHITE,
        "Teko", 116, "swing"),
    "techno": Look(
        "techno", "Techno", "Chakra Petch", 70, CYAN, WHITE,
        "Chakra Petch", 90, "flash"),
    # What shipped before any of this. The default, deliberately.
    "plain": Look(
        "plain", "Plain", "Arial", 72, GOLD, WHITE, "Arial", 82, "fade"),
}


def resolve(look_id: str | None) -> Look:
    """A preset by id, or the default.

    Unknown rather than raising, because a preset can be renamed or
    dropped while a reel that was reviewed under it is still waiting at
    a gate. That reel must still render.
    """
    return PRESETS.get(look_id or "", PRESETS[DEFAULT_LOOK_ID])


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\")
                .replace("{", "\\{")
                .replace("}", "\\}"))


def _letters(text: str, *, stagger: int = 55, blur: int = 10,
             hold: int = 200) -> str:
    r"""Each character lit in turn, inside one Dialogue.

    Not one Dialogue per character with its own ``\pos``: measured, a
    fixed advance per character does not match the glyphs and "Not
    Enough" rendered with N, E and g colliding. One Dialogue lets libass
    lay the string out, and each character's override block changes only
    how it is painted -- alpha, blur, colour -- so nothing can move.
    """
    out: list[str] = []
    when = 0
    for char in text:
        if char == " ":
            out.append(" ")
            continue
        rise, settle = when + 120, when + 120 + hold
        out.append(
            "{" + rf"\alpha&HFF&\blur{blur}\1c{WHITE}"
            rf"\t({when},{rise},\alpha&H00&\blur0)"
            rf"\t({rise},{settle},\1c{GOLD})" + "}" + _escape(char))
        when += stagger
    return "".join(out)


_SIMPLE = {
    "fade": r"{\fad(180,180)}",
    "stamp": (r"{\fscx128\fscy128\alpha&H60&"
              r"\t(0,110,\fscx100\fscy100\alpha&H00&)\fad(0,160)}"),
    "blur-in": (r"{\blur26\fscx112\fscy112"
                r"\t(0,260,\blur0\fscx100\fscy100)\fad(0,150)}"),
    "bounce": (r"{\fscx10\fscy10\t(0,130,\fscx120\fscy120)"
               r"\t(130,230,\fscx92\fscy92)\t(230,310,\fscx106\fscy106)"
               r"\t(310,390,\fscx100\fscy100)\fad(0,150)}"),
    "swing": (r"{\frz-28\fscx118\fscy118\alpha&H80&"
              r"\t(0,200,\frz6\fscx100\fscy100\alpha&H00&)"
              r"\t(200,300,\frz0)\fad(0,150)}"),
    "flash": (r"{\alpha&HFF&\t(0,40,\alpha&H00&)\t(90,120,\alpha&HC0&)"
              r"\t(150,190,\alpha&H00&)\fscx116\fscy116"
              r"\t(0,220,\fscx100\fscy100)\fad(0,150)}"),
}


def punch_tags(animation: str, text: str) -> str:
    """The whole body of the punch's Dialogue line, text included.

    Text included rather than returned separately because ``letters``
    interleaves tags with characters and cannot hand back a prefix.
    """
    if animation == "letters":
        return _letters(text)
    return _SIMPLE.get(animation, _SIMPLE["fade"]) + _escape(text)
