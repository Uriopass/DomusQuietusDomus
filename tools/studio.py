"""What the site's clip editor ("studio") loads for each joke, written into build/studio by prepare.py:

- <id>.json: the clip's bounds and the timed words around it, so the browser can preview any cut and
  any text without asking the server anything;
- src/<key>_l.mp4: a very light video (320 px) of the clip and MARGIN seconds on each side, which starts
  playing at once even on a poor connection;
- src/<key>_h.mp4: the clip alone at 640 px, without subtitles, which replaces it once loaded. It stops at
  the clip's bounds so that the editor makes no new part of the film available in good quality;
- src/<key>.jpg: small images along the window, for the timeline.

Usage, to generate everything ahead of a deployment (from the repo root):
    python3 tools/studio.py movie.mkv [parallel jobs]
"""
import csv
import gzip
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

MARGIN = 5        # seconds of film around the clip that the editor can reach
FPS = 24
FILMSTRIP = 16    # images in a timeline strip
DIR = os.path.join("build", "studio")
SRC = os.path.join(DIR, "src")

COMMON = ["-map", "0:v:0", "-map", "0:a:0", "-map_chapters", "-1", "-map_metadata", "-1", "-dn", "-sn",
          "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-sc_threshold", "0",
          "-movflags", "+faststart", "-f", "mp4"]
# A keyframe every half second, so that seeking is immediate
LOW = ["-vf", "fps=15,scale=320:-2", "-crf", "33", "-g", "8", "-keyint_min", "8",
       "-c:a", "aac", "-b:a", "40k", "-ac", "1", "-ar", "22050"]
HIGH = ["-vf", "fps=24,scale=640:-2", "-crf", "26", "-g", "12", "-keyint_min", "12",
        "-c:a", "aac", "-b:a", "96k", "-ac", "2"]


def movie_duration(movie):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", movie],
                         capture_output=True, text=True, check=True).stdout
    return float(out)


def entry(joke_id, clip_start, clip_end, duration):
    """Bounds of a joke's window, on whole frames, and the names of its files."""
    first = max(0, int((clip_start - MARGIN) * FPS))
    last = int(min(duration, clip_end + MARGIN) * FPS)
    return {"id": joke_id, "first": first, "last": last, "clip_start": clip_start, "clip_end": clip_end,
            "key": "%s_%d-%d_%d-%d" % (joke_id, first, last, round(clip_start * FPS), round(clip_end * FPS))}


def encode(args, path):
    """Runs ffmpeg into path.part, then moves it into place."""
    part = path + ".part"
    try:
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y"] + args + [part], check=True, capture_output=True)
        os.replace(part, path)
    finally:
        if os.path.exists(part):
            os.remove(part)


def make_sources(e, movie):
    start, end = e["first"] / FPS, e["last"] / FPS
    base = os.path.join(SRC, e["key"])
    if not os.path.exists(base + "_l.mp4"):
        encode(["-ss", "%.3f" % start, "-t", "%.3f" % (end - start), "-i", movie] + COMMON + LOW, base + "_l.mp4")
    if not os.path.exists(base + "_h.mp4"):
        encode(["-ss", "%.3f" % e["clip_start"], "-t", "%.3f" % (e["clip_end"] - e["clip_start"]), "-i", movie]
               + COMMON + HIGH, base + "_h.mp4")
    if not os.path.exists(base + ".jpg"):
        rate = FILMSTRIP / max(0.1, end - start)
        encode(["-i", base + "_l.mp4", "-vf", "fps=%.5f,crop=ih*4/3:ih,scale=80:60,tile=%dx1" % (rate, FILMSTRIP),
                "-frames:v", "1", "-q:v", "7", "-f", "image2", "-c:v", "mjpeg"], base + ".jpg")


def meta(e, title, words):
    """What the browser needs for a joke; all times in seconds from the start of the window."""
    start, end = e["first"] / FPS, e["last"] / FPS
    lines, current = [], None
    for w in words:
        if w["e"] <= start or w["s"] >= end:
            continue
        if w["seg"] != current:
            current = w["seg"]
            lines.append([])
        lines[-1].append({"w": w["w"], "s": round(max(0, w["s"] - start), 3), "e": round(min(end, w["e"]) - start, 3)})
    return {"id": e["id"], "title": title, "film_start": round(start, 3), "duration": round(end - start, 3),
            "clip_start": round(e["clip_start"] - start, 3), "clip_end": round(e["clip_end"] - start, 3),
            "low": "/studio/src/%s_l.mp4" % e["key"], "high": "/studio/src/%s_h.mp4" % e["key"],
            "strip": "/studio/src/%s.jpg" % e["key"], "strip_count": FILMSTRIP,
            "lines": [{"words": line} for line in lines]}


def write_meta(path, data):
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
    if os.path.exists(path):
        with open(path, "rb") as f:
            if f.read() == text:
                return
    for target, content in ((path + ".gz", gzip.compress(text, mtime=0)), (path, text)):
        with open(target + ".part", "wb") as f:
            f.write(content)
        os.replace(target + ".part", target)


def build(jokes, movie, words, log=print, jobs=1):
    """jokes: [(id, title, clip_start, clip_end)]. Encodes what's missing, updates the JSON files and
    removes the files of windows no joke uses any more."""
    os.makedirs(SRC, exist_ok=True)
    duration = movie_duration(movie)
    entries = [(entry(i, a, b, duration), title) for i, title, a, b in jokes]
    todo = [e for e, _ in entries if not all(os.path.exists(os.path.join(SRC, e["key"] + x))
                                             for x in ("_l.mp4", "_h.mp4", ".jpg"))]
    done = []

    def one(e):
        try:
            make_sources(e, movie)
            done.append(e["id"])
        except subprocess.CalledProcessError as err:
            log("studio: error on joke %s: %s" % (e["id"], err.stderr.decode(errors="replace")[-300:]))
        if todo and len(done) % 25 == 0:
            log("studio: %d/%d" % (len(done), len(todo)))

    with ThreadPoolExecutor(jobs) as pool:
        list(pool.map(one, todo))
    ready = [(e, title) for e, title in entries if all(os.path.exists(os.path.join(SRC, e["key"] + x))
                                                       for x in ("_l.mp4", "_h.mp4", ".jpg"))]
    for e, title in ready:
        write_meta(os.path.join(DIR, e["id"] + ".json"), meta(e, title, words))
    keys, ids = {e["key"] for e, _ in entries}, {e["id"] for e, _ in entries}
    for name in os.listdir(SRC):
        key = name.split(".")[0]
        key = key[:-2] if key.endswith(("_l", "_h")) else key
        if key not in keys:
            os.remove(os.path.join(SRC, name))
    for name in os.listdir(DIR):
        if name.endswith((".json", ".json.gz")) and name.split(".")[0] not in ids:
            os.remove(os.path.join(DIR, name))
    log("studio: %d jokes ready, %d encoded" % (len(ready), len(done)))


def main():
    def seconds(tc):
        p = [int(x) for x in tc.split(":")]
        return p[0] * 3600 + p[1] * 60 + p[2] + (p[3] / FPS if len(p) == 4 else 0)
    movie = sys.argv[1]
    jobs = int(sys.argv[2]) if len(sys.argv) > 2 else 2
    with open("data.csv", encoding="utf8") as f:
        rows = [r for r in csv.DictReader(f) if r["Timecode"] and r["Début extrait"] and r["Fin extrait"]]
    with open(os.path.join("build", "sous-titres", "mots.json"), encoding="utf8") as f:
        words = json.load(f)
    build([(r["ID"], r["Titre/Citation"].strip(), seconds(r["Début extrait"]), seconds(r["Fin extrait"])) for r in rows],
          movie, words, jobs=jobs)


if __name__ == "__main__":
    main()
