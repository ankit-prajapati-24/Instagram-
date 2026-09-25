"""ASS subtitle generation with karaoke word highlighting.

ffmpeg's ``subtitles`` filter renders these through libass, which uses
HarfBuzz — the reason this project burns captions here rather than compositing
them in Pillow. Verified on this machine: the bundled ffmpeg v7.1 reports
``--enable-libass --enable-libharfbuzz --enable-fontconfig``, while Pillow
reports ``RAQM=False`` and therefore cannot shape Devanagari at all.

The karaoke ``\\k`` tag is the retention feature, not decoration: words land as
they are spoken, which is what holds a scrolling viewer on a muted feed.
"""

from __future__ import annotations

from pathlib import Path

from engine.contract import ReelPlan

# ASS colours are &HAABBGGRR. Unspoken words sit in plain white; the active
# word flips to gold, which survives compression and reads on any background.
COLOUR_SPOKEN = "&H0000D7FF"   # gold  (highlighted)
# The active word is also lit, because at speaking speed a colour swap on
# one word of a 72px line is easy to miss -- a reviewer watching a
# finished reel read these captions as static.
#
# A glow, not a size change, and that distinction is the whole point.
# Scaling was tried first and shipped a reel whose caption block jumped
# up and down. Scaling both axes reflows the line sideways, which is
# obvious; what is not obvious is that `\fscy` alone grows the *line
# box*, so a block anchored at the bottom walks upward. Measured on real
# captions over black, sampling one beat:
#
#     variant  block top moves   lit pixels min..max
#     none                 0px   42372..42962
#     fscy                72px   42372..54406   <- the jumping
#     glow                 2px   42372..85289
#
# A glow changes only how the glyph is painted, so the metrics cannot
# move; the 2px is blur bleeding past the top row, not the text. And it
# is the more visible of the two -- twice the lit pixels of a plain
# line, where the scale managed a quarter more.
#
# Set HIGHLIGHT_BLUR to 0 to leave the colour sweep on its own.
HIGHLIGHT_GLOW = "&H00FFFFFF"   # white, against gold text and dark footage
HIGHLIGHT_BLUR = 7
# Up fast, down slower, both clamped inside the word. An earlier attempt
# let the settle run to the end of the word, which left the effect still
# fading two-thirds of the way through and the line permanently lit.
HIGHLIGHT_RISE_MS = 100
HIGHLIGHT_SETTLE_MS = 160
COLOUR_UPCOMING = "&H00FFFFFF"  # white (not yet reached)
COLOUR_OUTLINE = "&H00000000"   # black
COLOUR_SHADOW = "&HA0000000"    # translucent black

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
; WrapStyle 0 = smart wrapping, lower line wider. Style 2 (manual breaks
; only) let long Hinglish lines run off both edges of the frame.
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, \
OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, \
ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, \
MarginR, MarginV, Encoding
Style: Default,{font},{size},{primary},{secondary},{outline},{shadow},\
-1,0,0,0,100,100,0,0,1,5,3,2,110,110,{margin_v},1
Style: Punch,{font},{punch_size},{secondary},{secondary},{outline},{shadow},\
-1,0,0,0,100,100,1,0,1,6,4,5,130,130,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(seconds: float) -> str:
    """ASS wants H:MM:SS.cc with a single-digit hour."""
    seconds = max(seconds, 0.0)
    centis = int(round(seconds * 100))
    hours, rest = divmod(centis, 360000)
    minutes, rest = divmod(rest, 6000)
    secs, cs = divmod(rest, 100)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def escape_ass(text: str) -> str:
    """Neutralise the characters libass reads as markup."""
    return (text.replace("\\", "\\\\")
                .replace("{", "\\{")
                .replace("}", "\\}")
                .replace("\r", " ")
                .replace("\n", "\\N"))


def _pop_tags(word) -> str:
    """The rise-and-settle transform for one word, or "" if disabled.

    Times are milliseconds from the start of the dialogue line, which is
    the start of the beat -- the same clock ``word.start`` is measured
    on.

    Both edges are clamped inside the word. "ye" at Hinglish speaking
    speed can be under 100ms, and a rise that outran its own word would
    still be growing while the next word started popping, leaving two
    large at once.
    """
    if not HIGHLIGHT_BLUR:
        return ""
    start = max(int(round(word.start * 1000)), 0)
    end = max(int(round(word.end * 1000)), start)
    peak = min(start + HIGHLIGHT_RISE_MS, end)
    settled = min(peak + HIGHLIGHT_SETTLE_MS, end)
    return (f"\\t({start},{peak},\\3c{HIGHLIGHT_GLOW}"
            f"\\blur{HIGHLIGHT_BLUR})"
            f"\\t({peak},{settled},\\3c{COLOUR_OUTLINE}\\blur0)")


def _karaoke_line(beat, source: str) -> str:
    """One dialogue body, with per-word \\k timings when we have them."""
    if not beat.words:
        return escape_ass(getattr(beat, source) or "")

    # Devanagari captions come from voice_text, whose word count will not match
    # the Roman timings, so fall back to an untagged line rather than
    # mis-timing it.
    if source != "caption_text":
        return escape_ass(getattr(beat, source) or "")

    parts: list[str] = []
    for word in beat.words:
        centis = max(int(round((word.end - word.start) * 100)), 1)
        parts.append(f"{{\\k{centis}{_pop_tags(word)}}}"
                     f"{escape_ass(word.word)}")
    return " ".join(parts)


def build_ass(plan: ReelPlan, *, source: str = "caption_text",
              font: str = "Arial", font_size: int = 96,
              width: int = 1080, height: int = 1920,
              margin_v: int = 300) -> str:
    """Render the whole plan as one ASS document.

    ``margin_v`` lifts the caption clear of the Instagram and YouTube UI
    chrome that overlays the bottom of a vertical video.
    """
    lines = [HEADER.format(
        width=width, height=height, font=font, size=font_size,
        punch_size=int(font_size * 1.15), primary=COLOUR_SPOKEN,
        secondary=COLOUR_UPCOMING, outline=COLOUR_OUTLINE,
        shadow=COLOUR_SHADOW, margin_v=margin_v)]

    offset = 0.0
    for beat in plan.script.beats:
        duration = beat.seconds()
        start, end = ass_time(offset), ass_time(offset + duration)
        body = _karaoke_line(beat, source)
        if body:
            lines.append(
                f"Dialogue: 0,{start},{end},Default,,0,0,0,,{body}")

        # An on-screen punch line is a separate centred layer that appears for
        # the middle of the beat: it is the pattern interrupt, so it should not
        # compete with the running caption for the whole duration.
        if beat.on_screen_text:
            punch_start = ass_time(offset + duration * 0.15)
            punch_end = ass_time(offset + duration * 0.85)
            lines.append(
                f"Dialogue: 1,{punch_start},{punch_end},Punch,,0,0,0,,"
                f"{{\\fad(180,180)}}{escape_ass(beat.on_screen_text)}")

        offset += duration

    return "\n".join(lines) + "\n"


def write_ass(plan: ReelPlan, path: str | Path, **kwargs) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # libass reads UTF-8; a BOM makes it render the first glyph as a box.
    path.write_text(build_ass(plan, **kwargs), encoding="utf-8")
    return str(path)
