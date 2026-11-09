"""Transcript of the movie: the DVD subtitles, where each joke's hand-corrected quote replaces them.

Never uses a speech-to-text model's text. The audio model only says *when*:
1. the subtitles' words are aligned to the audio, each within its subtitle's time;
2. each joke's quote (its "Citation étendue", or the title when it's a line of dialogue), cleaned of
   "- " / "Name :" prefixes, *stage directions* and (directions), is located in the audio of the joke's clip, with
   other speech around it costing nothing so it doesn't pull the quote toward it;
3. a quote that shares too few words with the subtitles of that moment, or doesn't fit the audio, is
   left out (a visual gag described in words);
4. subtitle words spoken during the quote are replaced by the quote's lines. Words said before or
   after it stay, even when they share a subtitle line with it.

Usage: python3 build_transcript.py data.csv subtitles_base.json emissions.npz transcription.json
"""
import csv
import json
import re
import statistics
import sys
import unicodedata

import align_words as aw

FPS = 24
SOUNDS = {"rugissement"}  # *word* that describes a sound rather than stressing a spoken word
MIN_PROB = 0.4  # median word probability below which a quote is taken as not spoken there


def norm(w):
    w = unicodedata.normalize("NFKD", w.lower())
    return "".join(c for c in w if c.isalnum() and not unicodedata.combining(c))


def seconds(tc):
    p = [int(x) for x in tc.split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] + (p[3] / FPS if len(p) == 4 else 0)


def build(rows, base, log=print):
    """rows: data.csv's rows (dicts); base: subs_base.build's segments. Returns the transcript segments.
    Needs align_words.load_emissions() first."""
    base_words, _, took = aw.align_segments(base, log=False)
    log("subtitle words aligned: %d (%.0f s)" % (len(base_words), took))
    removed = [False] * len(base_words)

    names = {t.strip() for r in rows for t in r["Tags"].split(",") if t.strip() and t.strip()[0].isupper()}
    names |= {"Pano", "Obé", "Le goûteur", "Le chat", "Le palais", "La secrétaire"}
    prefix = re.compile(r"^\s*(?:-\s*)?(?P<who>[^:]{1,40}?)\s*:\s+(?P<rest>.*)$")

    def clean_line(line):
        # *stage directions* are not spoken, but one emphasized word is ("*Caeser* ou César ?"): it
        # stays, between asterisks, and shows in italics. (Directions in parentheses) are not spoken.
        line = re.sub(r"\*([^*]*)\*", lambda m: m.group() if re.fullmatch(r"[^\W\d_][\w'-]*", m.group(1))
                      and m.group(1).lower() not in SOUNDS else " ", line)
        line = re.sub(r"\([^)]*\)", " ", line)
        line = line.replace("[...]", " ")
        m = prefix.match(line)
        if m:
            who = m.group("who").strip()
            first = re.sub(r"^\(.*?\)\s*", "", who).split(",")[0].split(" (")[0].strip()
            if who.startswith("(") or first in names or who.lower().startswith("voix-off"):
                line = m.group("rest")
        line = re.sub(r"^\s*-\s*", "", line)
        return re.sub(r"\s+([,.])", r"\1", " ".join(line.split()))

    claimed = []  # (start, end) of quotes already placed
    quote_segments, stats = [], {"placed": 0, "not spoken": 0, "overlap": 0}
    for r in sorted(rows, key=lambda r: seconds(r["Timecode"])):
        quote = r["Citation étendue (optionnel)"].strip() or r["Titre/Citation"].strip()
        lines = [l for l in (clean_line(x) for x in quote.split("\n")) if l]
        words = [(w, li) for li, l in enumerate(lines) for w in aw.split_words(l)]
        if len(words) < 2 or not (r.get("Début extrait") and r.get("Fin extrait")):
            stats["not spoken"] += 1
            continue
        # An extended quote often starts before the clip or ends after it: look for it in the clip
        # widened by about the time it takes to say it.
        said = sum(len(aw.spoken(w)) for w, _ in words) / 14 + 1.5
        lo, hi = max(0, seconds(r["Début extrait"]) - said), min(aw.duration(), seconds(r["Fin extrait"]) + said)
        # Text check: enough words in common with the subtitles of that moment (fewer for an extended
        # quote, since the subtitles condense). Rules out titles describing a visual gag, which the
        # audio alone can't, as any text finds some matching letters in speech.
        sub_words = {norm(w) for c in base if c["end"] >= lo - 2 and c["start"] <= hi + 2 for w in c["text"].split()}
        quote_words = [norm(w) for w, _ in words if len(norm(w)) > 2]
        shared = sum(w in sub_words for w in quote_words) / max(1, len(quote_words))
        if shared < (0.2 if r["Citation étendue (optionnel)"].strip() else 0.4):
            stats["not spoken"] += 1
            continue
        timed = aw.align(lo, hi, [w for w, _ in words], free_edges=True)
        if timed is None:
            stats["not spoken"] += 1
            continue
        # Lines that don't fit the audio (an unmarked description) are left out, and the others
        # placed again without them, since they pulled the rest out of place.
        line_probs = {li: [timed[k][2] for k, (_, l) in enumerate(words) if l == li] for li in range(len(lines))}
        keep = [li for li, ps in line_probs.items() if ps and statistics.median(ps) >= 0.2]
        if len(keep) < len(lines):
            words = [(w, li) for w, li in words if li in keep]
            timed = aw.align(lo, hi, [w for w, _ in words], free_edges=True) if len(words) >= 2 else None
        if timed is None or statistics.median(p for _, _, p in timed) < MIN_PROB:
            stats["not spoken"] += 1
            continue
        kept = list(range(len(words)))
        # A first or last word cut off from the rest by a silence was caught on the same word said
        # nearby: it doesn't mark where the quote starts or ends.
        first, last = 0, len(kept) - 1
        while first < last and timed[kept[first + 1]][0] - timed[kept[first]][1] > 1.0:
            first += 1
        while last > first and timed[kept[last]][0] - timed[kept[last - 1]][1] > 1.0:
            last -= 1
        span = (timed[kept[first]][0], timed[kept[last]][1])
        if any(min(span[1], b) - max(span[0], a) > 0.5 * (span[1] - span[0]) for a, b in claimed):
            stats["overlap"] += 1  # the same dialogue already belongs to another joke's quote
            continue
        claimed.append(span)
        for li in keep:
            ts = [timed[k] for k in kept[first:last + 1] if words[k][1] == li] or [timed[k] for k in kept if words[k][1] == li]
            quote_segments.append({"start": round(max(ts[0][0], span[0]) - 0.1, 2), "end": round(ts[-1][1] + 0.3, 2),
                                   "text": lines[li], "source": "joke:" + r["ID"]})
        for k, w in enumerate(base_words):  # subtitle words said during the quote (not hand-corrected lines)
            if span[0] - 0.15 <= w["s"] <= span[1] + 0.15 and not base[w["seg"]].get("corrected"):
                removed[k] = True
        stats["placed"] += 1

    # What remains of each subtitle line, with times from its own words
    rest, partly = {}, set()
    for k, w in enumerate(base_words):
        if removed[k]:
            partly.add(w["seg"])
        else:
            rest.setdefault(w["seg"], []).append(w)
    # One or two words left from a subtitle line the quote mostly replaced ("Il" of "Il n'y a ni
    # bonnes ni mauvaises situations.") are leftovers, not a line.
    rest = {seg: ws for seg, ws in rest.items() if not (seg in partly and len(ws) <= 2)}
    sub_segments = [{"start": round(ws[0]["s"] - 0.3, 2), "end": round(ws[-1]["e"] + 0.5, 2),
                     "text": " ".join(x["w"] for x in ws),
                     "source": "correction" if base[ws[0]["seg"]].get("corrected") else "sub"} for ws in rest.values()]
    segments = sorted(quote_segments + sub_segments, key=lambda s: s["start"])
    log("quotes placed: %(placed)d, not spoken or no clip: %(not spoken)d, same dialogue as another quote: %(overlap)d" % stats)
    log("subtitle words replaced: %d of %d; segments: %d" % (sum(removed), len(base_words), len(segments)))
    return segments


def main():
    data_csv, base_json, emissions_path, out_json = sys.argv[1:5]
    aw.load_emissions(emissions_path)
    rows = list(csv.DictReader(open(data_csv, encoding="utf8")))
    segments = build(rows, json.load(open(base_json, encoding="utf8")))
    json.dump(segments, open(out_json, "w", encoding="utf8"), ensure_ascii=False, indent=0)


if __name__ == "__main__":
    main()
