"""Finds the jokes whose GIF fits a few words, for the public search API (tools/search_api.py, used by
the Slack command /cleopatre in moniprinter's slackmux).

Two passes:
- by words, instant: the words of the query in the joke's title, quote, tags, explanation and in
  what is said during its clip (accents and case ignored, word starts count a little, the whole
  phrase a lot);
- by meaning, when OPENROUTER_API_KEY is set: a fast model (gpt-oss-120b on Cerebras, ~1 s) picks
  from the list of all jokes the ones that fit the query as a reaction ("on est en retard"), even
  without a word in common. If it fails or is slow, the first pass alone answers.

Usage, to try it (from the repo root): python3 tools/gif_search.py words of the query
"""
import collections
import csv
import json
import math
import os
import random
import re
import sys
import threading
import unicodedata
import urllib.request

FPS = 24
FIELDS = {"title": 3.0, "quote": 2.0, "said": 1.5, "tags": 2.5, "explanation": 0.6}
STOP = set("""le la les l de du des d un une et a au aux en est es c ce ca cet cette ces que qu qui il ils elle
elles je j tu on nous vous me m te t se s ne n pas plus y lui leur mon ma mes ton ta tes son sa ses pour par sur
dans avec mais ou donc ni car si""".split())
MODEL, PROVIDER = "openai/gpt-oss-120b", "Cerebras"
MODEL_TIMEOUT = 4  # seconds


def norm(text):
    text = unicodedata.normalize("NFKD", text.lower().replace("’", "'"))
    return "".join(c for c in text if not unicodedata.combining(c))


def words(text):
    return [w for w in re.findall(r"[a-z0-9]+", norm(text)) if w not in STOP and len(w) > 1]


def flat(text):
    return " ".join(re.findall(r"[a-z0-9]+", norm(text)))


def seconds(tc):
    p = [int(x) for x in tc.split(":")]
    return p[0] * 3600 + p[1] * 60 + p[2] + (p[3] / FPS if len(p) == 4 else 0)


def clean_quote(text):
    """The quote without stage directions or "Name :" at the start of its lines."""
    text = re.sub(r"\*\([^)]*\)\*|\([^)]*\)", " ", text)
    return "\n".join(re.sub(r"^\s*-?\s*[^:\n]{1,30} : ", "", line) for line in text.split("\n"))


class Catalog:
    """The jokes that have a GIF, reloaded when data.csv or the transcript change."""

    def __init__(self, root="."):
        self.root = root
        self.signature = None
        self.docs = []
        self.lock = threading.Lock()
        self.cache = collections.OrderedDict()

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def load(self):
        files = [self.path("data.csv"), self.path("build", "sous-titres", "transcription.json"), self.path("build", "gif")]
        signature = tuple(os.path.getmtime(f) if os.path.exists(f) else 0 for f in files)
        with self.lock:
            if signature == self.signature:
                return
            lines = []
            if os.path.exists(files[1]):
                with open(files[1], encoding="utf8") as f:
                    lines = json.load(f)
            docs = []
            with open(files[0], encoding="utf8") as f:
                for r in csv.DictReader(f):
                    if not (r.get("Début extrait") and r.get("Fin extrait")):
                        continue
                    if not os.path.exists(self.path("build", "gif", r["ID"] + ".gif")):
                        continue
                    a, b = seconds(r["Début extrait"]), seconds(r["Fin extrait"])
                    said = " ".join(l["text"].replace("*", "") for l in lines if l["end"] > a and l["start"] < b)
                    fields = {"title": r["Titre/Citation"].replace("*", ""), "quote": clean_quote(r["Citation étendue (optionnel)"]),
                              "said": said, "tags": r["Tags"], "explanation": r["Explication"]}
                    docs.append({"id": r["ID"], "title": fields["title"].strip().split("\n")[0], "fields": fields,
                                 "timecode": int(seconds(r["Timecode"])) if r["Timecode"] else 0,
                                 "tags": [t.strip() for t in r["Tags"].split(",") if t.strip()],
                                 "words": {k: collections.Counter(words(v)) for k, v in fields.items()},
                                 "flat": {k: flat(v) for k, v in fields.items() if k in ("title", "quote", "said")}})
            df = collections.Counter(w for d in docs for w in set().union(*d["words"].values()))
            self.idf = {w: math.log(1 + (len(docs) - n + 0.5) / (n + 0.5)) for w, n in df.items()}
            self.docs = docs
            self.by_id = {d["id"]: d for d in docs}
            self.cache.clear()
            self.signature = signature

    # ------------------------------------------------------------------ by words

    def lexical(self, query):
        q = list(dict.fromkeys(words(query)))
        phrase = flat(query)
        scored = []
        for d in self.docs:
            score, matched = 0.0, 0
            for w in q:
                best = 0.0
                for field, weight in FIELDS.items():
                    counts = d["words"][field]
                    tf = counts.get(w, 0)
                    if not tf and len(w) >= 4:  # "architecte" finds "architecter", "archi" finds "architecte"
                        tf = 0.6 * sum(n for t, n in counts.items() if len(t) >= 4 and (t.startswith(w) or w.startswith(t)))
                    if tf:
                        best += weight * tf * 2.2 / (tf + 1.2)
                if best:
                    matched += 1
                    score += best * self.idf.get(w, 3.0)
            if len(phrase) >= 4 and any(phrase in text for text in d["flat"].values()):
                score += 6 + 2 * len(q)
                matched = len(q)
            if score:
                scored.append((score * (matched / max(1, len(q))) ** 1.5, d["id"]))
        if not scored and len(phrase) >= 3:  # typos: letters in common
            grams = lambda s: {s[i:i + 3] for i in range(len(s) - 2)}
            g = grams(phrase)
            for d in self.docs:
                h = grams(d["flat"]["title"] + " " + d["flat"]["quote"])
                sim = len(g & h) / max(1, len(g))
                if sim > 0.45:
                    scored.append((sim, d["id"]))
        scored.sort(reverse=True)
        return [i for _, i in scored]

    # ------------------------------------------------------------------ by meaning

    def by_meaning(self, query, key, candidates):
        """candidates: the best word matches, given in full (the catalog only shows the start of each quote)."""
        full = "\n".join("%s: %s | %s | dit : %s" % (i, self.by_id[i]["title"][:150], " ".join(self.by_id[i]["fields"]["quote"].split())[:300],
                                                   " ".join(self.by_id[i]["fields"]["said"].split())[:200]) for i in candidates)
        catalog = "\n".join("%s: %s%s" % (d["id"], d["title"][:110],
                                          " | " + " ".join(d["fields"]["quote"].split())[:120] if d["fields"]["quote"] else "")
                            for d in self.docs)
        prompt = ("Tu choisis des GIF du film « Astérix & Obélix : Mission Cléopâtre » à envoyer dans une conversation. "
                  "Quelqu'un tape quelques mots : une réplique dont il se souvient (parfois approximative), un personnage, "
                  "ou une situation à laquelle répondre par un GIF (« on est en retard », « bravo », « j'ai faim »). "
                  "Voici les blagues disponibles (id: titre | citation). Renvoie les id des 6 blagues qui correspondent le "
                  "mieux, la meilleure d'abord, et seulement celles qui correspondent vraiment.\n\n"
                  "Requête : %s\n\nLes plus proches par les mots, en entier (les mots peuvent tromper, ou contenir une faute de "
                  "frappe) :\n%s\n\nToutes les blagues :\n%s" % (query, full or "(aucune)", catalog))
        body = {"model": MODEL, "max_tokens": 2000, "reasoning": {"effort": "low"},
                "provider": {"order": [PROVIDER]},
                "response_format": {"type": "json_schema", "json_schema": {"name": "gifs", "strict": True, "schema": {
                    "type": "object", "additionalProperties": False, "required": ["ids"],
                    "properties": {"ids": {"type": "array", "items": {"type": "string"}}}}}},
                "messages": [{"role": "user", "content": prompt}]}
        req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                                              "X-Title": "DomusQuietusDomus"})
        msg = json.load(urllib.request.urlopen(req, timeout=MODEL_TIMEOUT))
        content = msg["choices"][0]["message"].get("content")
        if not content:
            raise ValueError("empty answer")
        ids = json.loads(content)["ids"]
        return [i for i in dict.fromkeys(str(i).strip() for i in ids) if i in self.by_id]

    # ------------------------------------------------------------------

    def search(self, query, limit=10, use_model=True, random_when_empty=True):
        """Ids of the jokes that fit the query, best first; a random selection for an empty query.
        use_model: may ask the model (a callable deciding it, e.g. a daily budget, or a bool)."""
        self.load()
        query = " ".join(query.split())[:200]
        if not words(query):
            if not random_when_empty:
                return []
            ids = [d["id"] for d in self.docs]
            random.shuffle(ids)
            return ids[:limit]
        # Results found without the model must not answer a query allowed to use it, and back
        cache_key = (flat(query), bool(use_model) and bool(os.environ.get("OPENROUTER_API_KEY")))
        if cache_key in self.cache:
            self.cache.move_to_end(cache_key)
            return self.cache[cache_key][:limit]
        lexical = self.lexical(query)
        meaning, key = [], os.environ.get("OPENROUTER_API_KEY")
        if key and (use_model() if callable(use_model) else use_model):
            try:
                meaning = self.by_meaning(query, key, lexical[:8])
            except Exception as e:  # slow or down: the words alone answer
                print("gif_search: model failed:", type(e).__name__, e, file=sys.stderr, flush=True)
        # A quote found word for word stays first; then the model's picks, then the other word matches
        strong = lexical[:1] if lexical and self.exact(query, lexical[0]) else []
        ids = list(dict.fromkeys(strong + meaning + lexical))
        self.cache[cache_key] = ids
        while len(self.cache) > 300:
            self.cache.popitem(last=False)
        return ids[:limit]

    def exact(self, query, joke_id):
        phrase = flat(query)
        return len(phrase) >= 4 and any(phrase in t for t in self.by_id[joke_id]["flat"].values())

    def title(self, joke_id):
        return self.by_id[joke_id]["title"] if joke_id in self.by_id else joke_id


if __name__ == "__main__":
    catalog = Catalog()
    for joke in catalog.search(" ".join(sys.argv[1:]), 5):
        print(joke, catalog.title(joke))
