#!/usr/bin/env python3
"""Small local editor for data.csv: browse, edit and add jokes, with the frame and a short clip
of the movie at each timecode.

Usage:
    python3 editor.py --movie movie.mkv
    python3 editor.py --ssh user@host:DomusQuietusDomus/movie.mkv   # the movie stays on the server
    then open http://localhost:8765

Needs ffmpeg where the movie is. Nothing is written until you click "Enregistrer".
With --git, each save is committed and pushed, and --rebuild runs a command afterwards
(on the server: --rebuild "venv/bin/python prepare.py movie.mkv" to update the site).
All URLs are relative, so it also works behind a reverse proxy under a sub-path like /editeur/.
Jokes ticked "Brouillon" are kept in the --drafts JSON file instead of data.csv: they stay off the
site and out of git until unticked and saved.
"""
import argparse
import csv
import http.server
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import urllib.parse

DATA = "data.csv"
FPS = 24  # frame rate of movie.mkv; a timecode can end with :ff to pick a frame, like in prepare.py
CLIP_START, CLIP_END = "Début extrait", "Fin extrait"  # clip bounds; "Timecode" is the screenshot
CLIPS_KEPT = 40  # encoded clips cached on disk
MAX_CLIP = 120  # seconds
TIMECODE = re.compile(r"^\d+:\d\d:\d\d(:\d\d?)?$")


def valid_timecode(timecode):
    return bool(TIMECODE.match(timecode)) and (timecode.count(":") < 3 or int(timecode.split(":")[3]) < FPS)


def to_seconds(timecode):
    parts = [int(x) for x in timecode.split(":")]
    h, m, s = parts[:3]
    return h * 3600 + m * 60 + s + (parts[3] / FPS if len(parts) == 4 else 0)


class Movie:
    def __init__(self, local=None, ssh=None):
        self.local = local
        self.host, self.path = ssh.split(":", 1) if ssh else (None, None)
        self.frames = {}
        self.clip_dir = tempfile.mkdtemp(prefix="domus-clips-")
        self.clip_locks = {}
        self.clip_locks_lock = threading.Lock()

    def resolve(self, args):
        path = self.local or self.path
        return ["ffmpeg", "-v", "error"] + [path if a == "{movie}" else a for a in args]

    def command(self, args):
        cmd = self.resolve(args)
        return ["ssh", self.host, shlex.join(cmd)] if self.host else cmd

    def ffmpeg(self, args):
        return subprocess.run(self.command(args), capture_output=True, check=True).stdout

    def frame(self, t):
        # Same frame the site shows: the image at the joke's timecode.
        if t not in self.frames:
            self.frames[t] = self.ffmpeg(["-ss", "%.3f" % t, "-i", "{movie}", "-frames:v", "1",
                                          "-vf", "scale=640:-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"])
        return self.frames[t]

    def clip(self, start, end):
        """Path to an mp4 of the movie from start to end (seconds), encoded once (~2.5 s for 19 s) and cached.
        The index is at the front (faststart), so the browser knows the duration at once and can seek."""
        key = "%.3f-%.3f" % (start, end)
        path = os.path.join(self.clip_dir, key + ".mp4")
        with self.clip_locks_lock:
            lock = self.clip_locks.setdefault(key, threading.Lock())
        with lock:  # a click during the background preparation waits for it instead of encoding twice
            if os.path.exists(path):
                return path
            args = ["-ss", "%.3f" % start, "-t", "%.3f" % (end - start), "-i", "{movie}", "-vf", "scale=640:-2",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-c:a", "aac", "-ac", "2",
                    "-movflags", "+faststart", "-f", "mp4", "-y"]
            with open(path + ".part", "wb") as out:
                if self.host:
                    # faststart needs a seekable output: encode to a temp file on the server, then send it.
                    encode = shlex.join(self.resolve(args)) + ' "$f"'
                    remote = 'f=$(mktemp --suffix=.mp4) && %s && cat "$f"; rm -f "$f"' % encode
                    subprocess.run(["ssh", self.host, remote], stdout=out, check=True)
                else:
                    subprocess.run(self.resolve(args + [path + ".part"]), check=True)
            os.replace(path + ".part", path)
            clips = sorted((os.path.join(self.clip_dir, f) for f in os.listdir(self.clip_dir) if f.endswith(".mp4")),
                           key=os.path.getmtime)
            for old in clips[:-CLIPS_KEPT]:
                os.remove(old)
            return path


def load():
    with open(DATA, encoding="utf8", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def save(fields, rows):
    ids = [r["ID"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("deux blagues ont le même ID")
    for r in rows:
        for col in ("Timecode", CLIP_START, CLIP_END):
            tc = r.get(col, "")
            if (tc or col == "Timecode") and not valid_timecode(tc):
                raise ValueError("%s invalide pour #%s : %r" % (col, r["ID"], tc))
    rows = sorted(rows, key=lambda r: to_seconds(r["Timecode"]))  # stable: same-second jokes keep their order
    with open(DATA + ".tmp", "w", encoding="utf8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in fields} for r in rows)
    os.replace(DATA + ".tmp", DATA)


def read_drafts(path):
    if not path or not os.path.exists(path):
        return []
    with open(path, encoding="utf8") as f:
        return json.load(f)


def write_drafts(path, fields, drafts):
    with open(path + ".tmp", "w", encoding="utf8") as f:
        json.dump([{k: d.get(k, "") for k in fields} for d in drafts], f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(path + ".tmp", path)


def describe_changes(old, new):
    """Commit message listing added, edited and deleted jokes, e.g. "ajout #355, modif #12"."""
    old_by_id = {r["ID"]: r for r in old}
    new_by_id = {r["ID"]: r for r in new}
    added = [i for i in new_by_id if i not in old_by_id]
    deleted = [i for i in old_by_id if i not in new_by_id]
    edited = [i for i in new_by_id if i in old_by_id and any(new_by_id[i].get(k, "") != v for k, v in old_by_id[i].items())]
    parts = [label + " " + ", ".join("#" + i for i in ids)
             for label, ids in (("ajout", added), ("modif", edited), ("suppression", deleted)) if ids]
    if len(added) + len(edited) == 1 and not deleted:
        title = new_by_id[(added + edited)[0]]["Titre/Citation"].split("\n")[0]
        parts[0] += " : " + (title[:60] + "…" if len(title) > 60 else title)
    return "Éditeur : " + ("; ".join(parts) or "réordonnancement")


def git(*args):
    return subprocess.run(["git"] + list(args), capture_output=True, text=True)


def commit_and_push(message):
    """Commits data.csv and pushes it. Returns a short status for the page."""
    if not git("status", "--porcelain", DATA).stdout.strip():
        return "rien à commiter"
    git("add", DATA)
    result = git("commit", "-m", message)
    if result.returncode:
        return "commit échoué : " + (result.stderr or result.stdout).strip()
    sha = git("rev-parse", "--short", "HEAD").stdout.strip()
    pull = git("pull", "--rebase", "--quiet")
    if pull.returncode:
        git("rebase", "--abort")
        return "commit %s fait, mais pas poussé (git pull a échoué : %s)" % (sha, pull.stderr.strip())
    push = git("push", "--quiet")
    if push.returncode:
        return "commit %s fait, mais pas poussé : %s" % (sha, push.stderr.strip())
    return "commit %s poussé" % git("rev-parse", "--short", "HEAD").stdout.strip()


class Handler(http.server.BaseHTTPRequestHandler):
    movie = None
    drafts_path = None
    transcript = []
    use_git = False
    rebuild = None
    share_dir = None
    share_url = ""
    lock = threading.Lock()

    def send(self, body, content_type, status=200, cache=False):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if cache:  # frames never change, so preloaded ones are reused by the browser
            self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        """Sends a file, honouring Range requests so the browser can seek in videos."""
        size = os.path.getsize(path)
        start, end = 0, size - 1
        match = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
        if match and (match.group(1) or match.group(2)):
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
            else:  # "bytes=-N": the last N bytes
                start = max(0, size - int(match.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", "bytes */%d" % size)
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        else:
            self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser often drops a request once it has what it needs

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                self.send(PAGE, "text/html; charset=utf-8")
            elif url.path == "/api/jokes":
                fields, rows = load()
                ids = {r["ID"] for r in rows}
                drafts = [d for d in read_drafts(self.drafts_path) if d.get("ID") not in ids]
                self.send(json.dumps({"fields": fields, "rows": rows, "drafts": drafts,
                                      "transcript": self.transcript}), "application/json")
            elif url.path == "/frame":
                self.send(self.movie.frame(float(query["t"][0])), "image/jpeg", cache=True)
            elif url.path in ("/clip", "/prepare", "/share"):
                start, end = float(query["s"][0]), float(query["e"][0])
                if not 0 <= start < end <= start + MAX_CLIP:
                    return self.send(json.dumps({"error": "début/fin invalides (%d s max)" % MAX_CLIP}),
                                     "application/json", 400)
                if url.path == "/clip":
                    self.send_file(self.movie.clip(start, end), "video/mp4")
                elif url.path == "/prepare":  # encode the clip in the background, before the click
                    threading.Thread(target=self.movie.clip, args=(start, end), daemon=True).start()
                    self.send("", "text/plain")
                else:
                    self.send(json.dumps({"url": self.share(start, end, query.get("id", [""])[0])}),
                              "application/json")
            else:
                self.send("not found", "text/plain", 404)
        except subprocess.CalledProcessError as e:
            self.send("ffmpeg: " + e.stderr.decode(errors="replace"), "text/plain", 500)

    def share(self, start, end, joke_id):
        """Copies the clip where the public site serves it and returns its URL. Without --share-dir,
        returns the editor's own clip URL, which only works for whoever can open the editor."""
        if not self.share_dir:
            return "clip?s=%.3f&e=%.3f" % (start, end)
        name = "%s_%.2f-%.2f.mp4" % (re.sub(r"[^0-9A-Za-z]", "", joke_id) or "clip", start, end)
        target = os.path.join(self.share_dir, name)
        if not os.path.exists(target):
            os.makedirs(self.share_dir, exist_ok=True)
            shutil.copyfile(self.movie.clip(start, end), target + ".part")
            os.replace(target + ".part", target)
        return self.share_url + name

    def do_POST(self):
        if self.path != "/api/save":
            return self.send("not found", "text/plain", 404)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.lock:
            fields, old = load()
            # A page loaded before a column existed doesn't send it: keep its current value.
            old_by_id = {r["ID"]: r for r in old}
            for r in body["rows"] + body.get("drafts", []):
                for k in fields:
                    if k not in r and r["ID"] in old_by_id:
                        r[k] = old_by_id[r["ID"]][k]
            if "drafts" in body:
                published, drafts = body["rows"], body["drafts"]
            else:
                # Page opened before drafts could be saved: it sends drafts mixed with the other jokes.
                # Keep the ones from the drafts file as drafts rather than publishing them.
                old_ids = {r["ID"] for r in old}
                draft_ids = {d["ID"] for d in read_drafts(self.drafts_path)} - old_ids
                published = [r for r in body["rows"] if r["ID"] not in draft_ids]
                drafts = [r for r in body["rows"] if r["ID"] in draft_ids]
            if drafts and not self.drafts_path:
                return self.send(json.dumps({"error": "brouillons impossibles sans --drafts"}), "application/json", 400)
            if {r["ID"] for r in published} & {d["ID"] for d in drafts}:
                return self.send(json.dumps({"error": "un ID est à la fois publié et brouillon"}), "application/json", 400)
            try:
                save(fields, published)
            except ValueError as e:
                return self.send(json.dumps({"error": str(e)}), "application/json", 400)
            if self.drafts_path:
                write_drafts(self.drafts_path, fields, drafts)
            status = "data.csv enregistré"
            if self.use_git:
                status = commit_and_push(describe_changes(old, published))
            status += " (%d brouillon%s)" % (len(drafts), "s" if len(drafts) > 1 else "")
            if self.rebuild and not status.startswith("rien à commiter"):
                subprocess.Popen(self.rebuild, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                status += ", site en reconstruction"
        self.send(json.dumps({"ok": True, "status": status}), "application/json")

    def log_message(self, *args):
        pass


PAGE = r"""<!doctype html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Éditeur de blagues</title>
<style>
  :root { color-scheme: light; }
  body { margin: 0; font-family: system-ui, sans-serif; display: flex; height: 100vh; color: #222; background: #fff; }
  #side { width: 360px; border-right: 1px solid #ddd; display: flex; flex-direction: column; }
  #side header { padding: 8px; display: flex; gap: 6px; border-bottom: 1px solid #ddd; }
  #search { flex: 1; padding: 6px; }
  #list { overflow-y: auto; flex: 1; }
  .item { padding: 6px 8px; cursor: pointer; border-bottom: 1px solid #f0f0f0; font-size: 13px;
          white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .item:hover { background: #f5f5f5; }
  .item.sel { background: #dbe9ff; }
  .item .tc { color: #888; margin-right: 6px; font-variant-numeric: tabular-nums; }
  .item.changed::before { content: "● "; color: #e08a00; }
  .item.draft { background: #fff4e0; }
  .item.draft.sel { background: #ffe2a8; }
  #main { flex: 1; overflow-y: auto; padding: 16px 24px; }
  #bar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; }
  #status { color: #888; font-size: 13px; margin-left: auto; display: flex; align-items: center; gap: 6px; }
  .spinner { width: 12px; height: 12px; border: 2px solid #ccc; border-top-color: #555; border-radius: 50%;
             animation: spin .8s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  label { display: block; font-size: 12px; color: #666; margin: 10px 0 3px; }
  input[type=text], textarea { width: 100%; box-sizing: border-box; padding: 6px; font: inherit; font-size: 14px; }
  textarea { resize: vertical; }
  #tc { width: 110px; font-variant-numeric: tabular-nums; }
  .clipTc { width: 95px; font-variant-numeric: tabular-nums; }
  .clipRow { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .clipCtl { display: flex; flex-direction: column; gap: 3px; }
  .clipCtl > div { display: flex; gap: 3px; align-items: center; }
  #clipBox { flex: 1; min-width: 360px; }
  .clipCtl button { padding: 3px 6px; }
  .clipLabel { display: inline-block; width: 48px; }
  #media img.clipThumb { width: 128px; min-height: 0; height: 54px; object-fit: cover; flex-shrink: 0; transition: opacity .1s; }
  #media img.clipThumb.loading { opacity: .45; }
  #sharemsg { font-size: 13px; color: #555; }
  .row { display: flex; gap: 6px; align-items: center; }
  #transcript { max-height: 220px; overflow-y: auto; border: 1px solid #ddd; font-size: 13px; }
  .seg { padding: 3px 6px; cursor: pointer; }
  .seg:hover { background: #f0f6ff; }
  .seg .tc { color: #888; margin-right: 6px; font-variant-numeric: tabular-nums; }
  .seg.at { background: #fff4e0; }
  #media { flex-wrap: wrap; display: flex; gap: 12px; margin-top: 8px; align-items: flex-start; }
  #media img, #media video { width: 420px; background: #000; min-height: 100px; }
  #frame { transition: opacity .1s; }
  #frame.loading { opacity: .45; }
  button { padding: 5px 10px; cursor: pointer; white-space: nowrap; }
  .row { flex-wrap: wrap; }
  #save { font-weight: bold; }
  #empty { color: #888; }
</style>
</head>
<body>
<div id="side">
  <header><input id="search" placeholder="Rechercher…"><button id="new" title="Nouvelle blague au timecode affiché">+ Nouvelle</button></header>
  <div id="list"></div>
</div>
<div id="main">
  <div id="bar">
    <button id="save" disabled>Enregistrer data.csv</button>
    <button id="delete">Supprimer</button>
    <span id="status"></span>
  </div>
  <div id="form">
    <div class="row">
      <strong id="id"></strong>
      <button data-step="-120">−5 s</button><button data-step="-24">−1 s</button><button data-step="-1" title="Image précédente">−1 img</button>
      <input id="tc" type="text" title="H:MM:SS ou H:MM:SS:II (II = image de 0 à 23)">
      <button data-step="1" title="Image suivante">+1 img</button><button data-step="24">+1 s</button><button data-step="120">+5 s</button>
      <label style="margin:0 0 0 12px"><input id="draft" type="checkbox"> Brouillon</label>
      <label style="margin:0 0 0 12px"><input id="interesting" type="checkbox"> Intéressant</label>
    </div>
    <div id="media">
      <img id="frame" alt="">
      <div id="clipBox">
        <div class="clipRow">
          <div class="clipCtl">
            <div><span class="clipLabel">Début</span><input id="cs" class="clipTc" type="text"></div>
            <div><button data-clip="cs" data-step="-24">−1 s</button><button data-clip="cs" data-step="-5">−5 img</button><button data-clip="cs" data-step="5">+5 img</button><button data-clip="cs" data-step="24">+1 s</button></div>
          </div>
          <img id="csImg" class="clipThumb" alt="">
        </div>
        <div class="clipRow">
          <div class="clipCtl">
            <div><span class="clipLabel">Fin</span><input id="ce" class="clipTc" type="text"></div>
            <div><button data-clip="ce" data-step="-24">−1 s</button><button data-clip="ce" data-step="-5">−5 img</button><button data-clip="ce" data-step="5">+5 img</button><button data-clip="ce" data-step="24">+1 s</button></div>
          </div>
          <img id="ceImg" class="clipThumb" alt="">
        </div>
        <div class="row" style="margin:6px 0">
          <button id="play">▶ Extrait</button><button id="share">Partager le clip</button><span id="sharemsg"></span>
        </div>
        <video id="clip" controls hidden></video>
      </div>
    </div>
    <label>Titre/Citation</label><textarea id="title" rows="3"></textarea>
    <label>Citation étendue (optionnel)</label><textarea id="extended" rows="4"></textarea>
    <label>Transcription à ±20 s (clic sur une ligne = l'ajouter à la citation étendue)</label>
    <div id="transcript"></div>
    <label>Tags</label><input id="tags" type="text">
    <label>Explication</label><textarea id="explanation" rows="5"></textarea>
  </div>
  <p id="empty" hidden>Aucune blague sélectionnée.</p>
</div>
<script>
const F = {title: "Titre/Citation", extended: "Citation étendue (optionnel)", tags: "Tags", explanation: "Explication"};
let rows = [], transcript = [], lastSave = "", sel = null, dirty = new Set(), frameTimer = null, clipTimer = null;
const CS = "Début extrait", CE = "Fin extrait";
let saving = false, editedWhileSaving = new Set();  // clip bounds; "Timecode" is only the screenshot
let frameToken = 0;
const $ = (id) => document.getElementById(id);
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));
const FPS = 24;  // a timecode can end with :ii, the frame (0 to 23) within the second
const frames = (tc) => { const p = tc.split(":").map(Number); return (p[0] * 3600 + p[1] * 60 + p[2]) * FPS + (p[3] || 0); };
const secs = (tc) => frames(tc) / FPS;
const fmt = (s) => `${Math.floor(s / 3600)}:${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
const fmtFrames = (f) => fmt(Math.floor(f / FPS)) + (f % FPS ? ":" + String(f % FPS).padStart(2, "0") : "");
const validTc = (tc) => /^\d+:\d\d:\d\d(:\d\d?)?$/.test(tc) && (tc.split(":").length < 4 || Number(tc.split(":")[3]) < FPS);

async function load() {
  const res = await (await fetch("api/jokes")).json();
  rows = res.rows;
  transcript = res.transcript;
  for (const d of res.drafts) {
    const row = Object.fromEntries(res.fields.map((f) => [f, d[f] ?? ""]));
    // Prefill new jokes with what Whisper heard around the timecode, for the user to correct.
    if (!row[F.extended] && validTc(row.Timecode)) {
      const t = secs(row.Timecode);
      row[F.extended] = near(t - 3, t + 7).map((s) => "- " + s.text).join("\n");
    }
    row._draft = true;
    rows.push(row);
  }
  sortRows();
  render();
  select(fromHash() || rows.find((r) => r._draft) || rows[0]);
}

// The URL ends with #<ID> of the joke being edited, so a reload or a link reopens it.
const fromHash = () => rows.find((r) => r.ID === decodeURIComponent(location.hash.slice(1)));
window.addEventListener("hashchange", () => { const r = fromHash(); if (r && r !== sel) select(r); });

function sortRows() { rows.sort((a, b) => (validTc(a.Timecode) ? secs(a.Timecode) : 0) - (validTc(b.Timecode) ? secs(b.Timecode) : 0)); }

function render() {
  const q = $("search").value.toLowerCase();
  $("list").innerHTML = "";
  for (const r of rows) {
    if (q && !Object.values(r).join(" ").toLowerCase().includes(q)) continue;
    const div = document.createElement("div");
    div.className = "item" + (r === sel ? " sel" : "") + (dirty.has(r) ? " changed" : "") + (r._draft ? " draft" : "");
    div.innerHTML = `<span class="tc">${esc(r.Timecode)}</span>#${esc(r.ID)} ${esc(r["Titre/Citation"].split("\n")[0])}`;
    div.onclick = () => select(r);
    $("list").appendChild(div);
    if (r === sel) setTimeout(() => div.scrollIntoView({block: "nearest"}));
  }
  $("save").disabled = dirty.size === 0 || saving;
  if (saving) {
    $("status").innerHTML = '<span class="spinner"></span>Enregistrement en cours (fichier, commit, push)…';
  } else {
    $("status").textContent = dirty.size ? `${dirty.size} modification(s) non enregistrée(s)` : (lastSave || "Tout est enregistré");
  }
}

function select(r) {
  sel = r;
  $("form").hidden = !r; $("empty").hidden = !!r;
  if (!r) return render();
  history.replaceState(null, "", "#" + encodeURIComponent(r.ID));
  $("sharemsg").textContent = "";
  if (!validTc(r[CS] || "") || !validTc(r[CE] || "")) {
    r._autoClip = true;  // no clip bounds yet: -4 s / +15 s around the screenshot, until edited
    defaultClip(r);
  }
  $("cs").value = r[CS]; $("ce").value = r[CE];
  showClipThumbs();
  $("id").textContent = "#" + r.ID + (r._draft ? " (brouillon)" : "");
  $("draft").checked = !!r._draft;
  $("tc").value = r.Timecode;
  $("interesting").checked = r["Intéressant"] === "TRUE";
  for (const [k, f] of Object.entries(F)) $(k).value = r[f];
  $("clip").hidden = true; $("clip").removeAttribute("src");
  showFrame();
  render();
}

const near = (a, b) => transcript.filter((s) => s.start >= a && s.start <= b);

function showTranscript() {
  $("transcript").innerHTML = "";
  if (!validTc($("tc").value)) return;
  const t = secs($("tc").value);
  for (const s of near(t - 20, t + 20)) {
    const div = document.createElement("div");
    div.className = "seg" + (Math.floor(s.start) === Math.floor(t) ? " at" : "");
    div.innerHTML = `<span class="tc">${fmt(Math.floor(s.start))}</span>${esc(s.text)}`;
    div.onclick = () => {
      const ext = $("extended");
      ext.value = (ext.value ? ext.value + "\n" : "") + "- " + s.text;
      edit(F.extended, ext.value);
    };
    $("transcript").appendChild(div);
  }
  const box = $("transcript"), at = box.querySelector(".at");
  if (at) box.scrollTop = at.offsetTop - box.offsetTop - box.clientHeight / 2;  // scroll the box, not the page
}

// Preload the frames the step buttons lead to, so clicking them shows the image almost instantly.
function prefetch() {
  const f = frames($("tc").value);
  for (const d of [-24, -1, 1, 24]) if (f + d >= 0) new Image().src = "frame?t=" + (f + d) / FPS;
}

function defaultClip(r) {
  if (!validTc(r.Timecode)) return;
  const f = frames(r.Timecode);
  r[CS] = fmtFrames(Math.max(0, f - 4 * FPS));
  r[CE] = fmtFrames(f + 15 * FPS);
}

// Name of the image the site already generated for a timecode, like prepare.py: 585 or 585f12.
const imageName = (tc) => { const f = frames(tc); return Math.floor(f / FPS) + (f % FPS ? "f" + (f % FPS) : ""); };

// Shows the frame at tc. The previous image stays, faded, until the new one is ready; at the joke's
// own timecode the site's already generated image shows at once while the exact frame loads.
function setFrame(tc) {
  const token = ++frameToken, img = $("frame");
  let exactShown = false;
  img.classList.add("loading");
  const exact = new Image();
  exact.onload = () => { if (token === frameToken) { exactShown = true; img.src = exact.src; img.classList.remove("loading"); } };
  exact.src = "frame?t=" + secs(tc);
  if (sel && tc === sel.Timecode) {
    const quick = new Image();
    quick.onload = () => { if (token === frameToken && !exactShown) { img.src = quick.src; img.classList.remove("loading"); } };
    quick.src = "../images/" + imageName(tc) + "_previewxl.jpg";
  }
}

function showFrame() {
  showTranscript();
  clearTimeout(frameTimer);
  frameTimer = setTimeout(() => { if (validTc($("tc").value)) { setFrame($("tc").value); prefetch(); } }, 150);
  if (sel && sel._autoClip) {
    defaultClip(sel);
    $("cs").value = sel[CS]; $("ce").value = sel[CE];
    showClipThumbs();
  }
  prepareClip();
}

// [start, end] of the clip in seconds, or null if the fields are not valid.
function clipRange() {
  if (!validTc($("cs").value) || !validTc($("ce").value)) return null;
  const s = secs($("cs").value), e = secs($("ce").value);
  return e > s && e - s <= 120 ? [s, e] : null;
}

// Have the server encode the clip once it stops changing, so "Extrait" plays right away.
function prepareClip() {
  clearTimeout(clipTimer);
  clipTimer = setTimeout(() => { const c = clipRange(); if (c) fetch(`prepare?s=${c[0]}&e=${c[1]}`); }, 1000);
}

function edit(field, value) {
  if (!sel || sel[field] === value) return;
  sel[field] = value;
  dirty.add(sel);
  if (saving) editedWhileSaving.add(sel);
  render();
}

for (const [k, f] of Object.entries(F)) $(k).oninput = () => edit(f, $(k).value);
$("interesting").onchange = () => edit("Intéressant", $("interesting").checked ? "TRUE" : "FALSE");
$("draft").onchange = () => {
  if (!sel) return;
  sel._draft = $("draft").checked;
  $("id").textContent = "#" + sel.ID + (sel._draft ? " (brouillon)" : "");
  dirty.add(sel);
  render();
};
$("tc").oninput = () => { if (validTc($("tc").value)) { edit("Timecode", $("tc").value); showFrame(); } };
$("tc").onchange = () => { sortRows(); render(); };
document.querySelectorAll("[data-step]:not([data-clip])").forEach((b) => b.onclick = () => {
  if (!validTc($("tc").value)) return;
  $("tc").value = fmtFrames(Math.max(0, frames($("tc").value) + Number(b.dataset.step)));
  edit("Timecode", $("tc").value); sortRows(); render(); showFrame();
});
// Small images of the clip's first and last frames, next to their fields.
let thumbTimer = null;
function showClipThumbs() {
  clearTimeout(thumbTimer);
  thumbTimer = setTimeout(() => {
    for (const id of ["cs", "ce"]) {
      const tc = $(id).value, img = $(id + "Img");
      if (!validTc(tc)) continue;
      const url = "frame?t=" + secs(tc), next = new Image();
      img.classList.add("loading");
      next.onload = () => { if ($(id).value === tc) { img.src = url; img.classList.remove("loading"); } };
      next.src = url;
      const f = frames(tc);  // preload where the buttons lead
      for (const d of [-24, -5, 5, 24]) if (f + d >= 0) new Image().src = "frame?t=" + (f + d) / FPS;
    }
  }, 150);
}

function editClip(id) {
  if (!validTc($(id).value)) return;
  sel._autoClip = false;
  edit(id === "cs" ? CS : CE, $(id).value);
  showClipThumbs();
  prepareClip();
}
for (const id of ["cs", "ce"]) $(id).oninput = () => editClip(id);
document.querySelectorAll("[data-clip]").forEach((b) => b.onclick = () => {
  const input = $(b.dataset.clip);
  if (!validTc(input.value)) return;
  input.value = fmtFrames(Math.max(0, frames(input.value) + Number(b.dataset.step)));
  editClip(b.dataset.clip);
});
$("play").onclick = () => {
  const c = clipRange();
  if (!c) return alert("Début/fin invalides (le début avant la fin, 2 min max).");
  $("clip").hidden = false; $("clip").src = `clip?s=${c[0]}&e=${c[1]}`; $("clip").play();
};
$("share").onclick = async () => {
  const c = clipRange();
  if (!c || !sel) return alert("Début/fin invalides (le début avant la fin, 2 min max).");
  $("sharemsg").textContent = "Préparation du clip…";
  const res = await fetch(`share?s=${c[0]}&e=${c[1]}&id=${encodeURIComponent(sel.ID)}`);
  const out = await res.json();
  if (!res.ok) return $("sharemsg").textContent = out.error;
  const url = new URL(out.url, location.href).href;
  const title = sel["Titre/Citation"].split("\n")[0];
  // On a phone, share the video itself (WhatsApp, Messages…); elsewhere copy the link.
  if (matchMedia("(pointer: coarse)").matches && navigator.canShare) {
    try {
      const blob = await (await fetch(`clip?s=${c[0]}&e=${c[1]}`)).blob();
      const file = new File([blob], url.split("/").pop(), {type: "video/mp4"});
      if (navigator.canShare({files: [file]})) {
        await navigator.share({files: [file], title, text: title + "\n" + url});
        return $("sharemsg").textContent = "Partagé";
      }
    } catch (e) {
      if (e.name === "AbortError") return $("sharemsg").textContent = "";
    }
  }
  const copied = await navigator.clipboard.writeText(url).then(() => true, () => false);
  $("sharemsg").innerHTML = (copied ? "Lien copié : " : "Lien : ") + `<a href="${esc(url)}" target="_blank">${esc(url)}</a>`;
};
$("search").oninput = render;
$("new").onclick = () => {
  const id = String(Math.max(0, ...rows.map((r) => Number(r.ID) || 0)) + 1);
  const row = {ID: id, Timecode: sel ? sel.Timecode : "0:00:00", "Titre/Citation": "", "Citation étendue (optionnel)": "",
               Tags: "", Explication: "", "Intéressant": "FALSE", _draft: true};
  rows.push(row); dirty.add(row); sortRows(); select(row); $("title").focus();
};
$("delete").onclick = () => {
  if (!sel || !confirm(`Supprimer #${sel.ID} « ${sel["Titre/Citation"].slice(0, 60)} » ?`)) return;
  const i = rows.indexOf(sel);
  rows.splice(i, 1); dirty.add({deleted: true}); dirty.delete(sel);
  select(rows[Math.min(i, rows.length - 1)]);
};
async function doSave() {
  if (!dirty.size || saving) return;
  // Drafts go to the drafts file, the rest to data.csv (and git).
  const strip = ({_draft, _autoClip, ...r}) => r;
  const body = {rows: rows.filter((r) => !r._draft).map(strip), drafts: rows.filter((r) => r._draft).map(strip)};
  const sent = new Set(dirty);
  saving = true; editedWhileSaving.clear(); render();
  let res, out;
  try {
    res = await fetch("api/save", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    out = await res.json();
  } catch (e) {
    out = {error: "le serveur n'a pas répondu (" + e.message + ")"};
  }
  saving = false;
  if (!res || !res.ok) {
    render();
    return alert("Pas enregistré : " + out.error);
  }
  lastSave = "Enregistré : " + out.status;
  for (const r of sent) if (!editedWhileSaving.has(r)) dirty.delete(r);  // keep edits made during the save
  select(sel);
}
$("save").onclick = doSave;
document.addEventListener("keydown", (e) => { if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); doSave(); } });
window.onbeforeunload = (e) => { if (dirty.size) e.preventDefault(); };
load();
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--movie", help="path to the movie file")
    source.add_argument("--ssh", help="host:path of the movie on a server with ffmpeg")
    parser.add_argument("--drafts", help="JSON file holding the draft jokes (created on first draft save)")
    parser.add_argument("--transcript", help="transcript segments JSON ([{start, end, text}]); default: "
                                             "transcription.json (corrected), else transcript.json (Whisper)")
    parser.add_argument("--git", action="store_true", help="commit and push data.csv after each save")
    parser.add_argument("--rebuild", help="shell command run in the background after each save")
    parser.add_argument("--share-dir", help="folder served by the public site where shared clips are copied")
    parser.add_argument("--share-url", default="", help="public URL of --share-dir, ending with /")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    Handler.use_git, Handler.rebuild = args.git, args.rebuild
    Handler.share_dir = args.share_dir and os.path.abspath(args.share_dir)
    Handler.share_url = args.share_url
    Handler.movie = Movie(local=args.movie and os.path.abspath(args.movie), ssh=args.ssh)
    Handler.drafts_path = args.drafts and os.path.abspath(args.drafts)
    os.chdir(os.path.dirname(os.path.abspath(__file__)))  # data.csv sits next to this script
    transcript = args.transcript or next((f for f in ("transcription.json", "transcript.json") if os.path.exists(f)), None)
    if transcript:
        with open(transcript, encoding="utf8") as f:
            Handler.transcript = json.load(f)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Éditeur de blagues sur http://localhost:%d" % args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
