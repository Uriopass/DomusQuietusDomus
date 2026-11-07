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
"""
import argparse
import csv
import http.server
import json
import os
import re
import shlex
import subprocess
import threading
import urllib.parse

DATA = "data.csv"
FPS = 24  # frame rate of movie.mkv; a timecode can end with :ff to pick a frame, like in prepare.py
TIMECODE = re.compile(r"^\d+:\d\d:\d\d(:\d\d?)?$")


def to_seconds(timecode):
    parts = [int(x) for x in timecode.split(":")]
    h, m, s = parts[:3]
    return h * 3600 + m * 60 + s + (parts[3] / FPS if len(parts) == 4 else 0)


class Movie:
    def __init__(self, local=None, ssh=None):
        self.local = local
        self.host, self.path = ssh.split(":", 1) if ssh else (None, None)
        self.frames = {}

    def command(self, args):
        path = self.local or self.path
        cmd = ["ffmpeg", "-v", "error"] + [path if a == "{movie}" else a for a in args]
        return ["ssh", self.host, shlex.join(cmd)] if self.host else cmd

    def ffmpeg(self, args):
        return subprocess.run(self.command(args), capture_output=True, check=True).stdout

    def frame(self, t):
        # Same frame the site shows: the image at the joke's timecode.
        if t not in self.frames:
            self.frames[t] = self.ffmpeg(["-ss", "%.3f" % t, "-i", "{movie}", "-frames:v", "1",
                                          "-vf", "scale=640:-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"])
        return self.frames[t]

    def clip(self, t):
        """Starts encoding 4 s before to 15 s after t. Read stdout as it comes: fragmented mp4 with a
        keyframe every half second, so the browser can start playing after the first fragment."""
        start = max(0, t - 4)
        return subprocess.Popen(self.command(
            ["-ss", "%.3f" % start, "-t", "19", "-i", "{movie}", "-vf", "scale=640:-2",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", "-g", "12", "-c:a", "aac", "-ac", "2",
             "-movflags", "frag_keyframe+empty_moov+default_base_moof", "-f", "mp4", "pipe:1"]),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)


def load():
    with open(DATA, encoding="utf8", newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def save(fields, rows):
    ids = [r["ID"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("deux blagues ont le même ID")
    for r in rows:
        if not TIMECODE.match(r["Timecode"]) or (r["Timecode"].count(":") == 3 and int(r["Timecode"].split(":")[3]) >= FPS):
            raise ValueError("timecode invalide pour #%s : %r" % (r["ID"], r["Timecode"]))
    rows = sorted(rows, key=lambda r: to_seconds(r["Timecode"]))  # stable: same-second jokes keep their order
    with open(DATA + ".tmp", "w", encoding="utf8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: r.get(k, "") for k in fields} for r in rows)
    os.replace(DATA + ".tmp", DATA)


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
    drafts = []
    transcript = []
    use_git = False
    rebuild = None
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

    def stream(self, process):
        # No Content-Length: the HTTP/1.0 response ends when ffmpeg is done and the connection closes.
        self.send_response(200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")  # tells nginx to pass chunks through right away
        self.end_headers()
        try:
            while chunk := process.stdout.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the user started another clip or closed the page
        finally:
            process.kill()
            process.wait()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(url.query)
        try:
            if url.path == "/":
                self.send(PAGE, "text/html; charset=utf-8")
            elif url.path == "/api/jokes":
                fields, rows = load()
                ids = {r["ID"] for r in rows}
                drafts = [d for d in self.drafts if d.get("ID") not in ids]  # already saved ones are in data.csv
                self.send(json.dumps({"fields": fields, "rows": rows, "drafts": drafts,
                                      "transcript": self.transcript}), "application/json")
            elif url.path == "/frame":
                self.send(self.movie.frame(float(query["t"][0])), "image/jpeg", cache=True)
            elif url.path == "/clip":
                self.stream(self.movie.clip(float(query["t"][0])))
            else:
                self.send("not found", "text/plain", 404)
        except subprocess.CalledProcessError as e:
            self.send("ffmpeg: " + e.stderr.decode(errors="replace"), "text/plain", 500)

    def do_POST(self):
        if self.path != "/api/save":
            return self.send("not found", "text/plain", 404)
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.lock:
            fields, old = load()
            try:
                save(fields, body["rows"])
            except ValueError as e:
                return self.send(json.dumps({"error": str(e)}), "application/json", 400)
            status = "data.csv enregistré"
            if self.use_git:
                status = commit_and_push(describe_changes(old, body["rows"]))
            if self.rebuild and status != "rien à commiter":
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
  #status { color: #888; font-size: 13px; margin-left: auto; }
  label { display: block; font-size: 12px; color: #666; margin: 10px 0 3px; }
  input[type=text], textarea { width: 100%; box-sizing: border-box; padding: 6px; font: inherit; font-size: 14px; }
  textarea { resize: vertical; }
  #tc { width: 110px; font-variant-numeric: tabular-nums; }
  .row { display: flex; gap: 6px; align-items: center; }
  #transcript { max-height: 220px; overflow-y: auto; border: 1px solid #ddd; font-size: 13px; }
  .seg { padding: 3px 6px; cursor: pointer; }
  .seg:hover { background: #f0f6ff; }
  .seg .tc { color: #888; margin-right: 6px; font-variant-numeric: tabular-nums; }
  .seg.at { background: #fff4e0; }
  #media { display: flex; gap: 12px; margin-top: 8px; align-items: flex-start; }
  #media img, #media video { width: 420px; background: #000; min-height: 100px; }
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
      <label style="margin:0 0 0 12px"><input id="interesting" type="checkbox"> Intéressant</label>
    </div>
    <div id="media">
      <img id="frame" alt="">
      <div><button id="play">▶ Extrait (−4 s / +15 s)</button><br><video id="clip" controls hidden></video></div>
    </div>
    <label>Titre/Citation</label><textarea id="title" rows="3"></textarea>
    <label>Citation étendue (optionnel)</label><textarea id="extended" rows="4"></textarea>
    <label>Transcription Whisper à ±20 s (clic sur une ligne = l'ajouter à la citation étendue, à corriger)</label>
    <div id="transcript"></div>
    <label>Tags</label><input id="tags" type="text">
    <label>Explication</label><textarea id="explanation" rows="5"></textarea>
  </div>
  <p id="empty" hidden>Aucune blague sélectionnée.</p>
</div>
<script>
const F = {title: "Titre/Citation", extended: "Citation étendue (optionnel)", tags: "Tags", explanation: "Explication"};
let rows = [], transcript = [], lastSave = "", sel = null, dirty = new Set(), frameTimer = null;
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
    dirty.add(row);
  }
  sortRows();
  render();
  select(rows.find((r) => r._draft) || rows[0]);
}

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
  $("save").disabled = dirty.size === 0;
  $("status").textContent = dirty.size ? `${dirty.size} modification(s) non enregistrée(s)` : (lastSave || "Tout est enregistré");
}

function select(r) {
  sel = r;
  $("form").hidden = !r; $("empty").hidden = !!r;
  if (!r) return render();
  $("id").textContent = "#" + r.ID + (r._draft ? " (nouvelle)" : "");
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
  const at = $("transcript").querySelector(".at");
  if (at) at.scrollIntoView({block: "center"});
}

// Preload the frames the step buttons lead to, so clicking them shows the image almost instantly.
function prefetch() {
  const f = frames($("tc").value);
  for (const d of [-24, -1, 1, 24]) if (f + d >= 0) new Image().src = "frame?t=" + (f + d) / FPS;
}

function showFrame() {
  showTranscript();
  clearTimeout(frameTimer);
  frameTimer = setTimeout(() => { if (validTc($("tc").value)) { $("frame").src = "frame?t=" + secs($("tc").value); prefetch(); } }, 250);
}

function edit(field, value) {
  if (!sel || sel[field] === value) return;
  sel[field] = value;
  dirty.add(sel);
  render();
}

for (const [k, f] of Object.entries(F)) $(k).oninput = () => edit(f, $(k).value);
$("interesting").onchange = () => edit("Intéressant", $("interesting").checked ? "TRUE" : "FALSE");
$("tc").oninput = () => { if (validTc($("tc").value)) { edit("Timecode", $("tc").value); showFrame(); } };
$("tc").onchange = () => { sortRows(); render(); };
document.querySelectorAll("[data-step]").forEach((b) => b.onclick = () => {
  if (!validTc($("tc").value)) return;
  $("tc").value = fmtFrames(Math.max(0, frames($("tc").value) + Number(b.dataset.step)));
  edit("Timecode", $("tc").value); sortRows(); render(); showFrame();
});
$("play").onclick = () => {
  if (!validTc($("tc").value)) return;
  $("clip").hidden = false; $("clip").src = "clip?t=" + secs($("tc").value); $("clip").play();
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
  if (!dirty.size) return;
  const clean = rows.map(({_draft, ...r}) => r);
  const res = await fetch("api/save", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({rows: clean})});
  const out = await res.json();
  if (!res.ok) return alert("Pas enregistré : " + out.error);
  lastSave = "Enregistré : " + out.status;
  rows.forEach((r) => delete r._draft);
  dirty.clear();
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
    parser.add_argument("--drafts", help="JSON list of new jokes to review, loaded as unsaved rows")
    parser.add_argument("--transcript", help="Whisper segments JSON ([{start, end, text}]); default: transcript.json if present")
    parser.add_argument("--git", action="store_true", help="commit and push data.csv after each save")
    parser.add_argument("--rebuild", help="shell command run in the background after each save")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    Handler.use_git, Handler.rebuild = args.git, args.rebuild
    Handler.movie = Movie(local=args.movie and os.path.abspath(args.movie), ssh=args.ssh)
    if args.drafts:
        with open(args.drafts, encoding="utf8") as f:
            Handler.drafts = json.load(f)
    os.chdir(os.path.dirname(os.path.abspath(__file__)))  # data.csv sits next to this script
    transcript = args.transcript or ("transcript.json" if os.path.exists("transcript.json") else None)
    if transcript:
        with open(transcript, encoding="utf8") as f:
            Handler.transcript = json.load(f)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print("Éditeur de blagues sur http://localhost:%d" % args.port)
    server.serve_forever()


if __name__ == "__main__":
    main()
