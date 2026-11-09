"""Proposed corrections to the DVD subtitles, from Gemini's verbatim transcription.

Nothing here is applied: the output lists differences for the user to review one by one (the editor's
"Relecture" page), in the format of corrections_sous_titres.csv plus "Type" and "Personnage" columns.

Per passage, the subtitles' words and Gemini's are compared as two word streams. Words both have in
common tie each Gemini word to its subtitle; Gemini words in between (what the subtitles condensed
away) go to the neighbouring subtitle closest in time, once Gemini's words are timed on the audio (the
audio model only gives times). Then each subtitle is compared with its Gemini words:
- complément: Gemini holds every word of the subtitle and more (the subtitle condensed it);
- différence: Gemini says something else;
- ajout: Gemini words far from any subtitle (a line the subtitles leave out).
Subtitles inside the jokes' quotes (which override them) and lines already corrected are skipped;
differences of spelling only ("3" / "trois", "Ici-même" / "Ici même", accents) don't count.

Usage: python3 propose_corrections.py subtitles_base.json gemini.json transcription.json emissions.npz out.csv
"""
import csv
import difflib
import json
import re
import sys
import unicodedata

from num2words import num2words

import align_words as aw

base_json, gemini_json, transcript_json, emissions_path, out_csv = sys.argv[1:6]


def toks(text):
    """Comparison units: lowercase letters only, numbers spelled out, split at hyphens and apostrophes."""
    text = re.sub(r"\d+", lambda m: " " + num2words(int(m.group()), lang="fr") + " ", text.lower().replace("’", "'"))
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return re.findall(r"[a-zœæ]+", text)


def tc(t):
    return "%d:%02d:%04.1f" % (t // 3600, t % 3600 // 60, t % 60)


def gemini_lines(text):
    """[(speaker, line)] from "Personnage : texte" / "- texte" lines."""
    out = []
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.lower().startswith(("(rien)", "voici")):
            continue
        speaker = ""
        m = re.match(r"^([A-ZÉÈÀÂ][^:]{0,30}?)\s*:\s+(.*)$", line)
        if m:
            speaker, line = m.group(1), m.group(2)
        line = re.sub(r"^-\s*", "", line).replace("[inaudible]", "").strip()
        if aw.split_words(line):
            out.append((speaker, line))
    return out


base = json.load(open(base_json, encoding="utf8"))
gemini = json.load(open(gemini_json, encoding="utf8"))
quotes = [(s["start"] - 0.3, s["end"] + 0.3) for s in json.load(open(transcript_json, encoding="utf8"))
          if s["source"].startswith("joke:")]
aw.load_emissions(emissions_path)


def in_quote(a, b):
    return any(min(b, qb) - max(a, qa) > 0.5 * max(b - a, 0.1) for qa, qb in quotes)


def distance(t, c):
    return 0 if c["start"] <= t <= c["end"] else min(abs(t - c["start"]), abs(t - c["end"]))


proposals = []
for passage in gemini:
    if "error" in passage:
        continue
    lo, hi = passage["chunk"]
    lines = gemini_lines(passage["text"])
    gw = [{"w": w, "speaker": lines[k][0], "line": k} for k, (_, l) in enumerate(lines) for w in aw.split_words(l)]
    if gw:
        timed = aw.align(lo, hi, [g["w"] for g in gw])
        for g, t in zip(gw, timed or [(lo, lo, 0)] * len(gw)):
            g["s"], g["e"] = t[0], t[1]
    cues = [i for i, c in enumerate(base) if lo <= c["start"] < hi]
    # Comparison units of both streams, each pointing back to its subtitle / Gemini word
    s_units = [(u, i) for i in cues for u in toks(base[i]["text"])]
    g_units = [(u, n) for n, g in enumerate(gw) for u in toks(g["w"])]
    owner = [None] * len(gw)
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, [u for u, _ in s_units], [u for u, _ in g_units],
                                                        autojunk=False).get_opcodes():
        if tag == "equal":
            for j in range(j1, j2):
                owner[g_units[j][1]] = s_units[i1 + (j - j1)][1]
    # Words said during a joke's quote are the quote's business
    for n, g in enumerate(gw):
        if in_quote(g["s"], max(g["e"], g["s"] + 0.05)):
            owner[n] = "quote"
    # Gemini sentences: a sentence belongs to the subtitle(s) its words have in common with; its other
    # words go to the subtitle of the nearest shared word. A sentence with no word in common is a line
    # the subtitles left out, or reworded entirely if it is said during a subtitle.
    sentence, sid = [], 0
    for n, g in enumerate(gw):
        if n and (g["line"] != gw[n - 1]["line"] or re.search(r"[.!?…]['\"»]?$", gw[n - 1]["w"])):
            sid += 1
        sentence.append(sid)
    for k in set(sentence):
        idx = [n for n in range(len(gw)) if sentence[n] == k]
        anchors = [n for n in idx if owner[n] not in (None, "quote")]
        for n in idx:
            if owner[n] is not None:
                continue
            if anchors:
                owner[n] = owner[min(anchors, key=lambda a: abs(a - n))]
            else:
                s0, e0 = gw[idx[0]]["s"], gw[idx[-1]]["e"]
                over = [(min(e0, base[i]["end"]) - max(s0, base[i]["start"]), i) for i in cues]
                best = max(over, default=(0, None))
                if best[1] is not None and best[0] > 0.5 * max(e0 - s0, 0.1):
                    owner[n] = best[1]
    for i in cues:
        c = base[i]
        if c.get("corrected") or in_quote(c["start"], c["end"]):
            continue
        mine = [g for g, o in zip(gw, owner) if o == i]
        if not mine or toks(c["text"]) == toks(" ".join(g["w"] for g in mine)):
            continue
        it = iter(toks(" ".join(g["w"] for g in mine)))
        kind = "complément" if all(u in it for u in toks(c["text"])) else "différence"
        proposals.append({"Début": tc(c["start"]), "Fin": "", "Original": c["text"],
                          "Correction": " ".join(g["w"] for g in mine), "Type": kind, "Personnage": mine[0]["speaker"],
                          "Écoute": "%.2f-%.2f" % (min(c["start"], mine[0]["s"]) - 0.8, max(c["end"], mine[-1]["e"]) + 0.8)})
    # Gemini words tied to no subtitle: lines the subtitles leave out (runs of the same Gemini line)
    run = []
    for g, o in list(zip(gw, owner)) + [(None, 0)]:
        if g is not None and o is None and (not run or g["line"] == run[-1]["line"]):
            run.append(g)
            continue
        if len(run) >= 2 and not in_quote(run[0]["s"], run[-1]["e"]):
            proposals.append({"Début": tc(run[0]["s"] - 0.2), "Fin": tc(run[-1]["e"] + 0.3), "Original": "",
                              "Correction": " ".join(x["w"] for x in run), "Type": "ajout", "Personnage": run[0]["speaker"],
                              "Écoute": "%.2f-%.2f" % (run[0]["s"] - 1.0, run[-1]["e"] + 1.0)})
        run = [g] if g is not None and o is None else []

proposals.sort(key=lambda p: p["Début"])
with open(out_csv, "w", encoding="utf8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=["Début", "Fin", "Original", "Correction", "Type", "Personnage", "Écoute"], lineterminator="\n")
    w.writeheader()
    w.writerows(proposals)
kinds = {}
for p in proposals:
    kinds[p["Type"]] = kinds.get(p["Type"], 0) + 1
print("%d proposals: %s" % (len(proposals), ", ".join("%d %s" % (n, k) for k, n in sorted(kinds.items()))))
