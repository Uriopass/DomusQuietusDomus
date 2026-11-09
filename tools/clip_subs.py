"""Progressive word-by-word subtitles (ASS) for a clip of the movie.

Each screen shows one line of dialogue, laid out in full from the start (at most two rows, broken
where the rows are most even) so nothing moves, but every word stays invisible (fill and outline)
until the moment it is spoken: the sentence builds up without giving away what comes next.

    make_ass(words, clip_start, clip_end) -> ASS text, times relative to the clip start
words: [{"w", "s", "e", "seg"}] from align_words.py (seg: the transcript line they belong to).

Also the ffmpeg settings shared by the site (prepare.py) and the editor: the mp4 of a clip, with the
subtitles burned in (ffmpeg needs libass for that), and its GIF, small enough to send on WhatsApp
(made by gifski when installed, else by ffmpeg and gifsicle).
"""
import re

MAX_ROW = 38          # characters per row at this font size on a 640 px wide clip
HOLD = 1.2            # seconds a finished line stays up when nothing follows
FONT = "DejaVu Sans"  # available on the server; libass falls back to a similar font elsewhere

HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 640
PlayResY: 272
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},21,&H00FFFFFF,&HFFFFFFFF,&H00000000,&HFF000000,-1,0,0,0,100,100,0,0,1,2,0,2,20,20,10,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def ass_time(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    return "%d:%02d:%02d.%02d" % (cs // 360000, cs // 6000 % 60, cs // 100 % 60, cs % 100)


def escape(w):
    """ASS text of a word; a word between asterisks (stressed in the quote: "*Caeser*") in italics."""
    w = w.replace("\\", "\\\\").replace("{", "(").replace("}", ")")
    return re.sub(r"\*([^*]+)\*", r"{\\i1}\1{\\i0}", w)


def shown_text(ws):
    return " ".join(x["w"].replace("*", "") for x in ws)


def screens(line_words):
    """Split a line's words into screens of at most two rows, each row at most MAX_ROW characters."""
    out, cur = [], []
    for w in line_words:
        if cur and len(shown_text(cur + [w])) > 2 * MAX_ROW:
            out.append(cur)
            cur = []
        cur.append(w)
    if cur:
        out.append(cur)
    return out


def break_at(screen):
    """Index of the first word of the second row, or None for a single row."""
    if len(shown_text(screen)) <= MAX_ROW:
        return None
    return min(range(1, len(screen)), key=lambda k: max(len(shown_text(screen[:k])), len(shown_text(screen[k:]))))


def make_ass(words, clip_start, clip_end, font=FONT):
    inside = [w for w in words if w["s"] < clip_end and w["e"] > clip_start - 10]
    lines = []
    for w in inside:  # consecutive words of the same transcript line
        if lines and lines[-1][-1]["seg"] == w["seg"]:
            lines[-1].append(w)
        else:
            lines.append([w])
    shown = [s for line in lines for s in screens(line)]
    events = []
    for n, screen in enumerate(shown):
        start = screen[0]["s"]
        nxt = shown[n + 1][0]["s"] if n + 1 < len(shown) else None
        end = min(screen[-1]["e"] + HOLD, nxt) if nxt is not None else screen[-1]["e"] + HOLD
        if end <= clip_start or start >= clip_end or screen[-1]["s"] < clip_start:
            continue  # outside the clip, or entirely said before it starts
        brk = break_at(screen)
        appear = max(start, clip_start)  # words spoken before the clip starts are already there
        # Each word shows at the moment it is spoken (words said before the clip are there at once).
        # With \\k tags, a word shows once the durations of all the words before it have elapsed.
        reveal, last = [], appear
        for w in screen:
            last = max(last, w["s"])
            reveal.append(last)
        parts = []
        for k, w in enumerate(screen):
            dur = (reveal[k + 1] if k + 1 < len(screen) else end) - reveal[k]
            sep = "\\N" if k == brk else (" " if k else "")
            parts.append("%s{\\ko%d}%s" % (sep, round(dur * 100), escape(w["w"])))
        events.append("Dialogue: 0,%s,%s,Default,,0,0,0,,%s" % (
            ass_time(appear - clip_start), ass_time(min(end, clip_end) - clip_start), "".join(parts)))
    return HEADER.format(font=font) + "\n".join(events) + "\n"


def has_subtitles(ass):
    return "\nDialogue:" in ass


def video_filter(ass_path=None):
    """ffmpeg -vf for a clip: 640 px wide, with the subtitles from ass_path burned in."""
    if not ass_path:
        return "scale=640:-2"
    return "scale=640:-2,ass=" + ass_path.replace("\\", "/").replace(":", "\\:").replace("'", "\\'")


MP4_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-ac", "2", "-movflags", "+faststart"]


GIF_BUDGET = 8 * 1024 * 1024  # bytes: sendable on WhatsApp or Discord, quick to load on a phone
# gifski (per-image palettes, temporal dithering): measured on 8 clips against ffmpeg's palette +
# gifsicle --lossy (what came before), quality 85 gives GIFs 7 % smaller with a better VMAF (+1.4 on
# average, +1.5 on the worst clip), much less dithering noise. Settings tried in turn, best first, until
# the GIF fits GIF_BUDGET (fewer images/s hardly helps with gifski: only what changes is stored).
# (width, gifski quality, images/s); images/s go down only for the longest clips
GIF_LADDER = [(480, 85, 24), (480, 75, 24), (400, 80, 24), (360, 80, 24), (320, 75, 24), (320, 65, 20), (240, 65, 15)]
FALLBACK_LADDER = [(480, 24), (480, 20), (480, 15), (400, 15), (320, 12), (240, 10)]  # (width, images/s)
_has_gifski = None


def gif_filter(width, fps):
    """Without gifski: ffmpeg -filter_complex turning a clip into a looping GIF, with a 256-colour palette
    made for the clip, ordered dithering and only the changed part of each image stored."""
    return ("fps=%d,scale=%d:-1:flags=lanczos,split[a][b];[a]palettegen=stats_mode=diff[p];"
            "[b][p]paletteuse=dither=bayer:bayer_scale=3:diff_mode=rectangle" % (fps, width))


def make_gif(mp4, run):
    """The GIF of a clip (mp4 bytes): the best setting of GIF_LADDER that fits GIF_BUDGET.
    run(argv, input bytes) -> output bytes runs commands where ffmpeg and gifski are."""
    global _has_gifski
    if _has_gifski is None:
        try:
            run(["gifski", "--version"], b"")
            _has_gifski = True
        except Exception:
            _has_gifski = False
    if not _has_gifski:
        return make_gif_ffmpeg(mp4, run)
    def relative_size(width, quality, fps):  # measured on sample clips, relative to (480, 85, 24)
        q = {85: 1.0, 80: 0.9, 75: 0.78, 65: 0.47}[quality]
        return (width / 480) ** 2 * q * (fps / 24) ** 0.6
    rung, scale = 0, None
    while True:
        width, quality, fps = GIF_LADDER[rung]
        # ffmpeg decodes and scales, gifski encodes: piped, as the raw images of a long clip take GBs
        gif = run(["sh", "-c", "ffmpeg -v error -f mp4 -i pipe:0 -vf fps=%d,scale=%d:-2:flags=lanczos -f yuv4mpegpipe "
                               "-pix_fmt yuv420p pipe:1 | gifski --quiet -Q %d -o - -" % (fps, width, quality)], mp4)
        if len(gif) <= GIF_BUDGET or rung == len(GIF_LADDER) - 1:
            return gif
        # Skip to the first setting expected to fit
        scale = len(gif) / relative_size(width, quality, fps)
        rung += 1
        while rung < len(GIF_LADDER) - 1 and scale * relative_size(*GIF_LADDER[rung]) > 0.95 * GIF_BUDGET:
            rung += 1


def make_gif_ffmpeg(mp4, run):
    """Without gifski: ffmpeg's palette GIF, shrunk by gifsicle's lossy compression when installed."""
    rung, lossy = 0, True
    while True:
        width, fps = FALLBACK_LADDER[rung]
        gif = run(["ffmpeg", "-v", "error", "-f", "mp4", "-i", "pipe:0", "-filter_complex", gif_filter(width, fps),
                   "-loop", "0", "-f", "gif", "pipe:1"], mp4)
        if lossy:
            try:
                gif = run(["gifsicle", "-O2", "--lossy=80"], gif)
            except Exception:  # gifsicle not installed: GIFs about twice as big
                lossy = False
        if len(gif) <= GIF_BUDGET or rung == len(FALLBACK_LADDER) - 1:
            return gif
        # Skip to the first setting expected to fit: the size goes with images/s × width²
        rung += 1
        while rung < len(FALLBACK_LADDER) - 1:
            w, f = FALLBACK_LADDER[rung]
            if len(gif) * f / fps * (w / width) ** 2 <= 0.9 * GIF_BUDGET:
                break
            rung += 1


def ffmpeg_has_libass(ffmpeg="ffmpeg"):
    import subprocess
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True).stdout
    except OSError:
        return False
    return any(line.split()[1:2] == ["ass"] for line in out.splitlines())
