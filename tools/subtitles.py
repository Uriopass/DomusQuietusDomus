"""Word-timed transcript of the movie, for the clips' subtitles: rebuilt by prepare.py whenever
data.csv, corrections_sous_titres.csv or subtitles.json change.

    DVD subtitles + corrections   (subs_base.py)
    -> the jokes' quotes replace them where they are said   (build_transcript.py)
    -> every word timed on the audio   (align_words.py, from the precomputed emissions)

Writes, in out_dir: transcription.json (lines), mots.json (words), and memo.json, the alignments
already done, so after an edit only the passages whose text changed are aligned again (a full run
takes a few minutes on the server, an edit a few seconds).

Usage: python3 subtitles.py emissions.npz out_dir   (from the repo root)
"""
import csv
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import align_words as aw  # noqa: E402
import build_transcript  # noqa: E402
import subs_base  # noqa: E402

TOOLS = ("align_words.py", "build_transcript.py", "subs_base.py", "subtitles.py")


def signature(paths, emissions_path):
    h = hashlib.sha256()
    for path in paths:
        with open(path, "rb") as f:
            h.update(f.read())
    st = os.stat(emissions_path)
    h.update(("%d %d" % (st.st_size, st.st_mtime)).encode())
    return h.hexdigest()


def build(emissions_path, out_dir, data_csv="data.csv", subs_json="subtitles.json",
          corrections_csv="corrections_sous_titres.csv", log=print):
    """Returns the timed words [{"w", "s", "e", "seg", "p"}], rebuilt only if an input changed."""
    here = os.path.dirname(os.path.abspath(__file__))
    inputs = [data_csv, subs_json] + ([corrections_csv] if os.path.exists(corrections_csv) else [])
    sig = signature(inputs + [os.path.join(here, t) for t in TOOLS], emissions_path)
    words_path, sig_path = os.path.join(out_dir, "mots.json"), os.path.join(out_dir, "signature")
    if os.path.exists(words_path) and os.path.exists(sig_path) and open(sig_path).read() == sig:
        with open(words_path, encoding="utf8") as f:
            return json.load(f)
    os.makedirs(out_dir, exist_ok=True)
    t0 = time.time()
    memo_path = os.path.join(out_dir, "memo.json")
    st = os.stat(emissions_path)
    emissions_id = "%d %d" % (st.st_size, st.st_mtime)
    if os.path.exists(memo_path):
        with open(memo_path, encoding="utf8") as f:
            saved = json.load(f)
        if saved.get("emissions") == emissions_id:
            aw.memo.update(saved["results"])
    before = set(aw.memo)
    aw.memo_used.clear()
    aw.load_emissions(emissions_path)

    with open(subs_json, encoding="utf8") as f:
        subs = json.load(f)
    corrections = subs_base.read_corrections(corrections_csv)
    base, dropped, unused = subs_base.build(subs, corrections)
    log("subtitles: %d lines (%d sound descriptions dropped), corrections applied: %d of %d"
        % (len(base), dropped, len(corrections) - len(unused), len(corrections)))
    for fix in unused:
        log("  correction not applied (no subtitle with that text at that time): %(Début)s %(Original)s" % fix)
    with open(data_csv, encoding="utf8") as f:
        rows = list(csv.DictReader(f))
    segments = build_transcript.build(rows, base, log=log)
    words, failed, _ = aw.align_segments(segments, log=False)
    reused = len(aw.memo_used & before)

    def write(name, obj):
        with open(os.path.join(out_dir, name + ".part"), "w", encoding="utf8") as f:
            json.dump(obj, f, ensure_ascii=False)
        os.replace(os.path.join(out_dir, name + ".part"), os.path.join(out_dir, name))
    write("transcription.json", segments)
    write("mots.json", words)
    write("memo.json", {"emissions": emissions_id, "results": {k: aw.memo[k] for k in aw.memo_used}})
    with open(sig_path, "w") as f:
        f.write(sig)
    log("subtitles: %d words timed (%d could not be placed), %d of %d passages already aligned, in %.0f s"
        % (len(words), failed, reused, len(aw.memo_used), time.time() - t0))
    return words


if __name__ == "__main__":
    build(sys.argv[1], sys.argv[2])
