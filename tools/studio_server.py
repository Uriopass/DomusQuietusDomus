#!/usr/bin/env python3
"""Renders the GIFs and videos (mp4 with sound) made in the site's clip editor. Public behind nginx (/api/clip/), so everything it
receives is checked and bounded:

- a render stays inside the window of a known joke (build/studio/<id>.json), and lasts at most
  MAX_SECONDS, or the joke's own clip if that is longer;
- the text is only ever drawn: control characters and subtitle override tags are removed;
- one render at a time, a short queue, one job per visitor and RENDERS_PER_VISITOR per RATE_WINDOW;
- finished GIFs are cached by content in build/studio/out (served by nginx), up to CACHE_BYTES.

POST /render {id, start, end, lines: [{words: [{w, s, e}]}], opts} -> {job} or {url}
GET /job/<job> -> {state: queued|running|done|error, progress, position, url, bytes, error}
Times are in seconds from the start of the joke's window.

Usage: python3 tools/studio_server.py --port 8767   (from the repo root)
"""
import argparse
import collections
import hashlib
import http.server
import json
import math
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import clip_subs  # noqa: E402

MOVIE = "movie.mkv"
STUDIO = os.path.join("build", "studio")
OUT = os.path.join(STUDIO, "out")

MAX_SECONDS = 20
MAX_BODY = 64 * 1024
MAX_WORDS = 200
MAX_WORD_CHARS = 40
MAX_PIXELS = 150_000_000      # width x height x images, the cost of a render
MAX_QUEUE = 12
RENDERS_PER_VISITOR = 40       # the editor's "final GIF" view renders after each pause in the edits
RATE_WINDOW = 600             # seconds
RENDER_TIMEOUT = 240          # seconds
JOB_KEPT = 900                # seconds a finished job stays readable
CACHE_BYTES = 4 * 1024 ** 3

WIDTHS = {320, 400, 480, 640}
VIDEO_WIDTHS = {480, 640, 960, 1280}
VIDEO_CRF = {"light": 30, "normal": 26, "high": 22}  # x264: lower is better and bigger
RATES = {12, 15, 20, 24}
QUALITIES = {"light": 70, "normal": 85, "high": 95}
SIZES = {"s": 17, "m": 21, "l": 27}
ASPECT = 272 / 640

jobs, lock = {}, threading.Lock()
pending = queue.Queue(MAX_QUEUE)
order = collections.deque()           # ids of queued jobs, oldest first
recent = collections.defaultdict(collections.deque)  # visitor -> times of their renders


class Refused(Exception):
    """A request we won't serve; its message is shown to the visitor."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def number(value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Refused("valeur invalide")
    return min(high, max(low, float(value)))


def clean_word(word):
    if not isinstance(word, str):
        raise Refused("texte invalide")
    word = "".join(c for c in word if c.isprintable())
    word = " ".join(re.sub(r"[\\{}]", "", word).split())
    return word[:MAX_WORD_CHARS]


def check(body):
    """The normalized render spec, or Refused."""
    joke = body.get("id")
    if not isinstance(joke, str) or not re.fullmatch(r"[0-9A-Za-z_-]{1,20}", joke):
        raise Refused("blague inconnue", 404)
    try:
        with open(os.path.join(STUDIO, joke + ".json"), encoding="utf8") as f:
            meta = json.load(f)
    except OSError:
        raise Refused("blague inconnue", 404)
    start = round(number(body.get("start"), 0, meta["duration"]) * 24) / 24
    end = round(number(body.get("end"), 0, meta["duration"]) * 24) / 24
    longest = max(MAX_SECONDS, meta["clip_end"] - meta["clip_start"] + 0.1)
    if end - start < 0.2:
        raise Refused("extrait trop court")
    if end - start > longest:
        raise Refused("extrait trop long (%d s max)" % longest)
    opts = body.get("opts") if isinstance(body.get("opts"), dict) else {}
    video = opts.get("format", "gif") == "mp4"
    if opts.get("format", "gif") not in ("gif", "mp4"):
        raise Refused("options invalides")
    width = opts.get("width", 640 if video else 480)
    fps = 24 if video else opts.get("fps", 24)  # a video keeps the film's images
    quality = opts.get("quality", "normal")
    size = opts.get("size", "m")
    if (width not in (VIDEO_WIDTHS if video else WIDTHS) or fps not in RATES or quality not in QUALITIES
            or size not in SIZES):
        raise Refused("options invalides")
    spec = {"id": joke, "film_start": meta["film_start"], "start": start, "end": end, "width": width, "fps": fps,
            "quality": quality, "size": size, "top": opts.get("position") == "top",
            "progressive": opts.get("mode", "words") != "line", "subs": opts.get("subs", True) is not False,
            "fit": not video and opts.get("fit", True) is not False, "format": "mp4" if video else "gif"}
    if not video and width * round(width * ASPECT) * fps * (end - start) > MAX_PIXELS:
        raise Refused("trop lourd : réduis la largeur, la fluidité ou la durée")
    words, lines = [], body.get("lines")
    if spec["subs"] and isinstance(lines, list):
        for n, line in enumerate(lines[:60]):
            items = line.get("words") if isinstance(line, dict) else None
            for item in items if isinstance(items, list) else []:
                if not isinstance(item, dict):
                    raise Refused("texte invalide")
                text = clean_word(item.get("w"))
                if text:
                    s = number(item.get("s"), 0, meta["duration"])
                    words.append({"w": text, "s": round(s, 3), "e": round(number(item.get("e"), s, meta["duration"]), 3),
                                  "seg": n})
        if len(words) > MAX_WORDS:
            raise Refused("trop de texte")
    spec["words"] = words
    return spec


def even(n):
    return int(n) // 2 * 2


def follow(processes, decoder, frames, job):
    """Waits for a render, its progress read from ffmpeg's frame count; kills it past RENDER_TIMEOUT."""
    timer = threading.Timer(RENDER_TIMEOUT, lambda: [p.kill() for p in processes])
    timer.start()
    try:
        for raw in decoder.stderr:
            if raw.startswith(b"frame="):
                job["progress"] = min(0.97, int(raw[6:].strip() or 0) / max(1, frames))
        for p in processes:
            p.wait()
    finally:
        timer.cancel()
    if any(p.returncode for p in processes):
        raise RuntimeError("render failed")


def render_video(spec, ass_path, target, job):
    """The cut as an mp4 with its sound and the subtitles burned in (without the film's chapters,
    which would make it claim the whole film's duration)."""
    duration = spec["end"] - spec["start"]
    vf = "scale=%d:-2:flags=lanczos" % spec["width"] + (",ass=" + ass_path if ass_path else "")
    encoder = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-nostats", "-progress", "pipe:2", "-y",
                                "-ss", "%.3f" % (spec["film_start"] + spec["start"]), "-t", "%.3f" % duration, "-i", MOVIE,
                                "-map", "0:v:0", "-map", "0:a:0", "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                                "-crf", str(VIDEO_CRF[spec["quality"]]), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                                "-ac", "2", "-map_chapters", "-1", "-map_metadata", "-1", "-dn", "-sn",
                                "-movflags", "+faststart", "-f", "mp4", target],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    follow([encoder], encoder, duration * 24, job)


def render_once(spec, ass_path, width, target, job):
    a = spec["film_start"] + spec["start"]
    duration = spec["end"] - spec["start"]
    vf = "fps=%d,scale=%d:-2:flags=lanczos" % (spec["fps"], width)
    if ass_path:
        vf += ",ass=" + ass_path
    decoder = subprocess.Popen(["ffmpeg", "-nostdin", "-v", "error", "-nostats", "-progress", "pipe:2", "-ss", "%.3f" % a,
                                "-t", "%.3f" % duration, "-i", MOVIE, "-map", "0:v:0", "-vf", vf, "-f", "yuv4mpegpipe",
                                "-pix_fmt", "yuv420p", "pipe:1"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    encoder = subprocess.Popen(["gifski", "--quiet", "-Q", str(QUALITIES[spec["quality"]]), "-o", target, "-"],
                               stdin=decoder.stdout, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    decoder.stdout.close()
    follow([decoder, encoder], decoder, duration * spec["fps"], job)


def render(job):
    spec, key = job["spec"], job["key"]
    ext = "." + spec["format"]
    final = os.path.join(OUT, key + ext)
    tmp = os.path.join(OUT, ".%s" % key)
    ass_path = None
    if spec["subs"] and spec["words"]:
        ass = clip_subs.make_ass(spec["words"], spec["start"], spec["end"], size=SIZES[spec["size"]],
                                 top=spec["top"], progressive=spec["progressive"])
        if clip_subs.has_subtitles(ass):
            ass_path = tmp + ".ass"
            with open(ass_path, "w", encoding="utf8") as f:
                f.write(ass)
    try:
        if spec["format"] == "mp4":
            render_video(spec, ass_path, tmp + ext, job)
        else:
            width = spec["width"]
            for attempt in range(3):
                render_once(spec, ass_path, width, tmp + ext, job)
                if not spec["fit"] or os.path.getsize(tmp + ext) <= clip_subs.GIF_BUDGET or width <= 240:
                    break
                # Too big to send in a chat: narrower, by what the size calls for
                width = max(240, even(width * math.sqrt(0.9 * clip_subs.GIF_BUDGET / os.path.getsize(tmp + ext))))
                job["progress"] = 0
        job["bytes"] = os.path.getsize(tmp + ext)
        os.replace(tmp + ext, final)
    finally:
        for path in (tmp + ext, tmp + ".ass"):
            if os.path.exists(path):
                os.remove(path)
    prune()


def prune():
    files = [os.path.join(OUT, f) for f in os.listdir(OUT) if f.endswith((".gif", ".mp4")) and not f.startswith(".")]
    files.sort(key=os.path.getmtime, reverse=True)
    total = 0
    for path in files:
        total += os.path.getsize(path)
        if total > CACHE_BYTES:
            os.remove(path)


def worker():
    while True:
        job = pending.get()
        with lock:
            order.remove(job["id"])
            job["state"] = "running"
        try:
            render(job)
            job.update(state="done", progress=1, url="/studio/out/%s.%s" % (job["key"], job["spec"]["format"]))
        except Exception as e:
            print("render error", job["key"], e, flush=True)
            job.update(state="error", error="Le rendu a échoué, réessaie.")
        job["finished"] = time.time()
        job.pop("spec", None)


def public(job):
    out = {k: job[k] for k in ("state", "progress", "url", "bytes", "error") if job.get(k) is not None}
    if job["state"] == "queued":
        out["position"] = list(order).index(job["id"]) + 1 if job["id"] in order else 1
    return out


def submit(body, visitor):
    spec = check(body)
    key = hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
    final = os.path.join(OUT, "%s.%s" % (key, spec["format"]))
    if os.path.exists(final):
        os.utime(final)
        return {"state": "done", "progress": 1, "url": "/studio/out/%s.%s" % (key, spec["format"]), "bytes": os.path.getsize(final)}
    now = time.time()
    with lock:
        for k in [k for k, j in jobs.items() if j.get("finished", now) < now - JOB_KEPT]:
            del jobs[k]
        for job in jobs.values():  # the same GIF is already being made
            if job["key"] == key and job["state"] in ("queued", "running"):
                return dict(public(job), job=job["id"])
        if any(j["visitor"] == visitor and j["state"] in ("queued", "running") for j in jobs.values()):
            raise Refused("Un GIF est déjà en cours pour toi, attends qu'il finisse.", 429)
        times = recent[visitor]
        while times and times[0] < now - RATE_WINDOW:
            times.popleft()
        if len(times) >= RENDERS_PER_VISITOR:
            raise Refused("Beaucoup de GIF d'un coup : réessaie dans quelques minutes.", 429)
        job = {"id": secrets.token_urlsafe(12), "key": key, "spec": spec, "state": "queued", "progress": 0,
               "visitor": visitor}
        try:
            pending.put_nowait(job)
        except queue.Full:
            raise Refused("Trop de GIF en cours, réessaie dans un instant.", 503)
        order.append(job["id"])
        jobs[job["id"]] = job
        times.append(now)
        return dict(public(job), job=job["id"])


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 15

    def reply(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        m = re.fullmatch(r"/job/([\w-]{8,32})", self.path)
        with lock:
            job = jobs.get(m.group(1)) if m else None
            data = public(job) if job else None
        self.reply(data or {"state": "error", "error": "Rendu introuvable, relance-le."}, 200 if data else 404)

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        if self.path != "/render" or not 0 < length <= MAX_BODY:
            self.close_connection = True
            return self.reply({"error": "requête invalide"}, 400)
        try:
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise Refused("requête invalide")
            self.reply(submit(body, self.headers.get("X-Real-IP", "local")))
        except Refused as e:
            self.reply({"error": str(e)}, e.status)
        except (ValueError, KeyError, TypeError):
            self.reply({"error": "requête invalide"}, 400)

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    os.makedirs(OUT, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
