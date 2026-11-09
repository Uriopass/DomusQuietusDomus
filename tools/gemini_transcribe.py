"""Verbatim transcription of the movie, passage by passage, with Gemini 4 "argon" through OpenRouter.

Only used to *propose* corrections to the DVD subtitles (which condense the dialogue): nothing it
writes reaches the site without the user's approval. The subtitles of each passage are given as a
hint for the spelling of names. Results are saved after each passage, so a run can be resumed.

Usage: python3 gemini_transcribe.py movie.mkv subtitles.json chunks.json out.json
Needs OPENROUTER_API_KEY in the environment.
"""
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

MODEL = "google/gemini-4-argon"
PROMPT = """Transcris mot pour mot, en français, tout ce qui est dit dans cet extrait audio du film « Astérix & Obélix : Mission Cléopâtre » (2002).
- Écris exactement ce qui est prononcé, y compris les répétitions et les mots que des sous-titres résumeraient. N'invente rien : si un passage est inaudible, écris [inaudible].
- Une ligne par réplique, au format « Personnage : texte » si le personnage est reconnaissable, sinon « - texte ».
- N'indique ni bruitages ni musique. Si personne ne parle, réponds seulement : (rien)
Pour l'orthographe des noms propres, voici les sous-titres du DVD pour ce passage (souvent résumés : ne les recopie pas, sers-t'en seulement comme indice) :
{subs}"""

movie, subs_json, chunks_json, out_json = sys.argv[1:5]
key = os.environ["OPENROUTER_API_KEY"]
subs = json.load(open(subs_json, encoding="utf8"))
chunks = json.load(open(chunks_json))
done = {tuple(r["chunk"]): r for r in json.load(open(out_json, encoding="utf8"))} if os.path.exists(out_json) else {}


def transcribe(chunk):
    lo, hi = chunk
    wav = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(lo), "-t", str(hi - lo), "-i", movie, "-vn", "-ac", "1",
                          "-ar", "16000", "-f", "wav", "-"], capture_output=True, check=True).stdout
    hint = "\n".join(c["text"] for c in subs if lo - 1 <= c["start"] <= hi) or "(aucun sous-titre)"
    body = {"model": MODEL, "messages": [{"role": "user", "content": [
        {"type": "text", "text": PROMPT.format(subs=hint)},
        {"type": "input_audio", "input_audio": {"data": base64.b64encode(wav).decode(), "format": "wav"}}]}]}
    for attempt in range(4):
        try:
            req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
                                         headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            msg = json.load(urllib.request.urlopen(req, timeout=300))
            return {"chunk": [lo, hi], "text": msg["choices"][0]["message"]["content"].strip(),
                    "cost": msg.get("usage", {}).get("cost", 0)}
        except (urllib.error.URLError, KeyError, TimeoutError) as e:
            time.sleep(5 * (attempt + 1))
            err = str(e)
    return {"chunk": [lo, hi], "error": err}


todo = [c for c in chunks if tuple(c) not in done or "error" in done[tuple(c)]]
print("%d passages to transcribe (%d already done)" % (len(todo), len(chunks) - len(todo)), flush=True)
with ThreadPoolExecutor(4) as pool:
    for n, result in enumerate(pool.map(transcribe, todo)):
        done[tuple(result["chunk"])] = result
        json.dump(sorted(done.values(), key=lambda r: r["chunk"][0]), open(out_json, "w", encoding="utf8"),
                  ensure_ascii=False, indent=0)
        if n % 20 == 0:
            print("  %d/%d" % (n + 1, len(todo)), flush=True)
results = list(done.values())
print("done: %d passages, %d errors, cost $%.2f" % (len(results), sum("error" in r for r in results),
                                                     sum(r.get("cost") or 0 for r in results)))
