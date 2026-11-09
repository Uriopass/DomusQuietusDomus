"""Base transcript from the DVD subtitles only (Whisper's text is never used): one segment per subtitle,
without the line-break dots ("pluie,.." / "..ni") and the dialogue dashes ("-Assez !").

Corrections from corrections_sous_titres.csv (at the repo root) (columns Début, Fin, Original, Correction), for lines the DVD
subtitles condense or leave out, outside the jokes' quotes:
- with an Original, they replace the text of the subtitle shown at Début (within half a second) whose
  text matches it (with the same text: kept as is, but protected from the quotes; with an empty
  Correction: removed);
- without one, they add a line said between Début and Fin (default: 2.5 s after Début).

Usage: python3 -I subs_base.py subtitles.json out.json [corrections.csv]
"""
import csv
import json
import os
import re
import sys
import unicodedata


def norm(text):
    text = unicodedata.normalize("NFKD", text.lower())
    return "".join(c for c in text if c.isalnum() and not unicodedata.combining(c))


def seconds(tc):
    h, m, s = tc.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def read_corrections(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf8") as f:
        return [r for r in csv.DictReader(f) if r.get("Début", "").strip()]


def tidy(w):
    if w.startswith("..") and not w.startswith("..."):
        w = w[2:]
    if w.endswith("..") and not w.endswith("..."):
        w = w[:-2]
    if len(w) > 1 and w[0] == "-" and w[1].isalpha():
        w = w[1:]
    return w


def is_dialogue(text):
    """These subtitles also describe sounds ("Musique dramatique", "Fracas", "Ti amo (Umberto Tozzi)")
    and carry an ad: short lines without final punctuation, credits in parentheses and links aren't speech."""
    if re.search(r"https?://|www\.|osdb\.", text, re.I):
        return False
    if re.search(r"\([^)]*\)\s*$", text):
        return False
    return bool(re.search(r"[.!?…,;:»\"]$", text)) or len(text.split()) > 5


def build(subs, corrections):
    """Returns (segments, number of sound descriptions dropped, corrections not applied)."""
    fixes = [dict(r, t=seconds(r["Début"])) for r in corrections]
    used = set()
    out, dropped = [], 0
    for c in subs:
        text = re.sub(r"^\([^)]*\)\s*", "", c["text"].strip())  # "(Otis) Je vais charger." -> speaker removed
        text = " ".join(t for t in (tidy(w) for w in text.lstrip("-").split()) if t and t != "-")
        corrected = False
        for k, fix in enumerate(fixes):
            if fix["Original"].strip() and abs(c["start"] - fix["t"]) <= 0.5 and norm(text) == norm(fix["Original"]):
                text, corrected = fix["Correction"], True
                used.add(k)
        if text and (corrected or is_dialogue(text)):
            out.append({"start": c["start"], "end": c["end"], "text": text})
            if corrected:
                out[-1]["corrected"] = True  # checked by hand: no quote replaces it
        elif text:
            dropped += 1
    for k, fix in enumerate(fixes):  # lines the subtitles leave out
        if not fix["Original"].strip():
            end = seconds(fix["Fin"]) if fix.get("Fin", "").strip() else fix["t"] + 2.5
            out.append({"start": fix["t"], "end": end, "text": fix["Correction"], "corrected": True})
            used.add(k)
    out.sort(key=lambda c: c["start"])
    return out, dropped, [fix for k, fix in enumerate(fixes) if k not in used]


def main():
    subs_json, out_json = sys.argv[1:3]
    corrections_csv = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "corrections_sous_titres.csv")
    corrections = read_corrections(corrections_csv)
    out, dropped, unused = build(json.load(open(subs_json, encoding="utf8")), corrections)
    print("dropped %d sound descriptions or ads; corrections applied: %d of %d"
          % (dropped, len(corrections) - len(unused), len(corrections)))
    for fix in unused:
        print("  correction not applied (no subtitle with that text at that time): %(Début)s %(Original)s" % fix)
    json.dump(out, open(out_json, "w", encoding="utf8"), ensure_ascii=False, indent=0)
    print("%d subtitle lines" % len(out))


if __name__ == "__main__":
    main()
