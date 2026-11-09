"""Align every word of a known transcript to the movie's audio (CTC forced alignment, French wav2vec2).

The audio model is only used for timing: the text always comes from the transcript.
The model gives, every 20 ms of the movie, the probability of each letter ("emissions"). That only
depends on the audio, so it is computed once, on a machine with torch (a Mac: ~2 min), and saved:

    python3 align_words.py --emissions audio.ogg emissions.npz

Everything else needs only numpy, so it runs on the server at each edit: the transcript's segments
are grouped into blocks of continuous speech (at most ~30 s), and for each block a Viterbi pass finds
the most likely way to fit the block's exact text to the letter probabilities, each word staying
within its segment's time give or take WINDOW. Each word gets a start and end time.

Usage: python3 align_words.py emissions.npz transcription.json words.json
Output: [{"w": "Alors", "s": 582.80, "e": 583.06, "seg": 0, "p": 0.91}, ...] where seg is the index of
the transcript segment (a line, for display) and p the word's mean peak letter probability (low for
words with silent letters, like "assez", so only a rough confidence).
"""
import json
import math
import re
import subprocess
import sys
import time
import unicodedata

import numpy as np
from num2words import num2words

MODEL = "jonatasgrosman/wav2vec2-large-xlsr-53-french"
SR = 16000
PAD = 0.6       # seconds of audio kept around a block, since segment times are approximate
MAX_BLOCK = 30  # seconds
GAP = 1.5       # a silence longer than this starts a new block
WINDOW = 0.7    # a word must be spoken within its segment's time, give or take this (seconds)
RATE = 50       # emission frames per second (the model's 20 ms stride)
CHUNK, CONTEXT = 10, 2  # seconds of audio per model pass, and audio heard on each side of it (the
                        # model's input is normalized per pass, so passes about as long as a line)

emissions = vocab = None  # (frames, letters) log-probabilities over the whole movie, float16
BLANK = SEP = None
memo, memo_used = {}, set()  # align() results by arguments, which subtitles.py keeps between runs


def load_emissions(path):
    global emissions, vocab, BLANK, SEP
    with np.load(path) as f:
        emissions, vocab = f["logp"], json.loads(str(f["vocab"]))
    BLANK, SEP = vocab["<pad>"], vocab["|"]


def init():
    if emissions is None:
        raise RuntimeError("load_emissions() first")


def compute_emissions(audio_path, out_path):
    """Runs the model over the whole audio, CHUNK seconds at a time (each hearing CONTEXT seconds on
    either side, so words at the cuts are heard whole), and saves the letter log-probabilities."""
    import torch
    from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor
    proc = Wav2Vec2Processor.from_pretrained(MODEL)
    model = Wav2Vec2ForCTC.from_pretrained(MODEL).eval()
    device = "mps" if torch.backends.mps.is_available() else "cpu"
    model.to(device)
    audio = load_audio(audio_path)
    duration = len(audio) / SR
    out = None
    t0 = time.time()
    for c0 in np.arange(0, duration, CHUNK):
        a, b = max(0.0, c0 - CONTEXT), min(duration, c0 + CHUNK + CONTEXT)
        with torch.no_grad():
            inputs = proc(audio[int(a * SR):int(b * SR)], sampling_rate=SR, return_tensors="pt").input_values.to(device)
            logp = torch.log_softmax(model(inputs).logits[0].float(), dim=-1).cpu().numpy()
        if out is None:
            out = np.zeros((int(math.ceil(duration * RATE)), logp.shape[1]), np.float16)
        f0, f1 = int(round(c0 * RATE)), min(out.shape[0], int(round((c0 + CHUNK) * RATE)))
        idx = np.clip(np.arange(f0, f1) - int(round(a * RATE)), 0, logp.shape[0] - 1)  # one frame per 320 samples
        out[f0:f1] = logp[idx]
        if int(c0) % 1200 == 0:
            print("  %.0f / %.0f s (%.0f s)" % (c0, duration, time.time() - t0), flush=True)
    np.savez_compressed(out_path, logp=out, vocab=json.dumps(proc.tokenizer.get_vocab()))
    print("emissions: %d frames x %d letters -> %s (%.0f s)" % (out.shape + (out_path, time.time() - t0)))


def load_audio(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    return np.frombuffer(raw, dtype=np.float32)


def spoken(word):
    """The letters the model can hear for a written word: lowercase, digits spelled out."""
    init()
    w = word.lower().replace("’", "'")

    def number(m):
        n, suffix = int(m.group(1)), m.group(2)
        return num2words(n, lang="fr", to="ordinal" if suffix in ("e", "ème", "eme", "er", "ère") else "cardinal")
    w = re.sub(r"(\d+)(ème|eme|ère|er|e)?", number, w)
    return "".join(c for c in unicodedata.normalize("NFC", w) if c in vocab and c != "|")


def split_words(text):
    """Words of a line; French-style detached "!" / "?" stay on the word before them."""
    out = []
    for w in text.split():
        if spoken(w):
            out.append(w)
        elif out:
            out[-1] += " " + w
    return out


def ctc_align(logp, tokens, lo=None, hi=None, free_edges=False):
    """Viterbi CTC alignment. logp: (T, V) log-probabilities; tokens: label ids; lo/hi: optional
    first/last allowed frame per token. free_edges: audio before the first and after the last token
    costs nothing, so other speech there doesn't pull the text toward it (to find *where* a text is
    said). Returns, per token, [first frame, last frame], or None if the text cannot fit the audio."""
    T, L = logp.shape[0], len(tokens)
    S = 2 * L + 1                       # blank, t1, blank, t2, ..., blank
    labels = np.full(S, BLANK)
    labels[1::2] = tokens
    can_skip = np.zeros(S, bool)        # jump over a blank between two different tokens
    can_skip[3::2] = np.array(tokens[1:]) != np.array(tokens[:-1])
    if T < L:
        return None
    score = np.full(S, -np.inf)
    score[0], score[1] = (0.0 if free_edges else logp[0, BLANK]), logp[0, tokens[0]]
    back = np.zeros((T, S), np.int8)    # 0 stay, 1 from previous state, 2 skipped a blank
    if lo is not None:
        state_lo = np.full(S, -1)
        state_hi = np.full(S, T)
        state_lo[1::2], state_hi[1::2] = lo, hi
        score[(0 < state_lo) | (0 > state_hi)] = -np.inf
    for t in range(1, T):
        prev1 = np.concatenate(([-np.inf], score[:-1]))
        prev2 = np.where(can_skip, np.concatenate(([-np.inf, -np.inf], score[:-2])), -np.inf)
        cand = np.stack([score, prev1, prev2])
        back[t] = cand.argmax(0)
        emit = logp[t, labels]
        if free_edges:
            emit = emit.copy()
            emit[0] = emit[S - 1] = 0.0
        score = cand.max(0) + emit
        if lo is not None:
            score[(t < state_lo) | (t > state_hi)] = -np.inf
    s = S - 1 if score[S - 1] >= score[S - 2] else S - 2
    if not np.isfinite(score[s]):
        return None
    spans = [[None, None] for _ in range(L)]
    for t in range(T - 1, -1, -1):
        if s % 2 == 1:
            k = s // 2
            spans[k][0] = t
            if spans[k][1] is None:
                spans[k][1] = t
        s -= int(back[t, s])
    return spans


def duration():
    return emissions.shape[0] / RATE


def align(start, end, words, windows=None, free_edges=False):
    """Aligns words (list of strings) to the audio from start to end (seconds). windows: optional
    (lo, hi) seconds per word. Returns [(start, end, prob)] per word, or None if the text can't fit."""
    init()
    key = json.dumps([round(start, 3), round(end, 3), words, windows and [[round(a, 3), round(b, 3)] for a, b in windows],
                      free_edges], ensure_ascii=False)
    memo_used.add(key)
    if key not in memo:
        memo[key] = _align(start, end, words, windows, free_edges)
    return memo[key]


def _align(start, end, words, windows, free_edges):
    f_start = max(0, int(round(start * RATE)))
    logp = emissions[f_start:max(f_start + 1, int(round(end * RATE)))].astype(np.float32)
    start = f_start / RATE
    T = logp.shape[0]
    frame = 1 / RATE
    tokens, owner, lo, hi = [], [], [], []  # letter ids, their word, and allowed frames
    for k, w in enumerate(words):
        if tokens:
            tokens.append(SEP); owner.append(None); lo.append(0); hi.append(T - 1)
        f_lo, f_hi = 0, T - 1
        if windows:
            f_lo = max(0, int((windows[k][0] - start) / frame))
            f_hi = min(T - 1, int((windows[k][1] - start) / frame))
        for c in spoken(w):
            tokens.append(vocab[c]); owner.append(k); lo.append(f_lo); hi.append(f_hi)
    spans = ctc_align(logp, tokens, np.array(lo), np.array(hi), free_edges) if windows else None
    if spans is None:  # no window, or the words don't fit them: align freely
        spans = ctc_align(logp, tokens, free_edges=free_edges)
    if spans is None:
        return None
    out = []
    for k in range(len(words)):
        idx = [j for j, o in enumerate(owner) if o == k]
        f0, f1 = spans[idx[0]][0], spans[idx[-1]][1] + 1
        # each letter's peak probability within its frames (CTC emits a letter as a short spike)
        prob = float(np.mean([math.exp(logp[spans[j][0]:spans[j][1] + 1, tokens[j]].max()) for j in idx]))
        out.append([round(start + f0 * frame, 3), round(start + f1 * frame, 3), round(prob, 3)])
    return out


def blocks(segments):
    block = []
    for i, seg in enumerate(segments):
        if block and (seg["start"] - max(segments[j]["end"] for j in block) > GAP
                      or seg["end"] - segments[block[0]]["start"] > MAX_BLOCK):
            yield block
            block = []
        block.append(i)
    if block:
        yield block


def align_segments(segments, log=True):
    """Aligns all segments' words, each within its segment's time ± WINDOW."""
    out, failed, t0 = [], 0, time.time()
    for n, block in enumerate(blocks(segments)):
        start = max(0.0, segments[block[0]]["start"] - PAD)
        end = min(duration(), max(segments[i]["end"] for i in block) + PAD)
        words = [(w, i) for i in block for w in split_words(segments[i]["text"])]
        if not words:
            continue
        windows = [(segments[i]["start"] - WINDOW, segments[i]["end"] + WINDOW) for _, i in words]
        timed = align(start, end, [w for w, _ in words], windows)
        if timed is None:
            failed += len(words)
            continue
        for (w, seg), (s, e, p) in zip(words, timed):
            out.append({"w": w, "s": round(s, 3), "e": round(e, 3), "seg": seg, "p": round(p, 2)})
        if log and n % 100 == 0:
            print("  block %d at %.0f s, %d words so far (%.0f s)" % (n, start, len(out), time.time() - t0), flush=True)
    return out, failed, time.time() - t0


def main():
    if sys.argv[1] == "--emissions":
        return compute_emissions(sys.argv[2], sys.argv[3])
    emissions_path, transcript_path, out_path = sys.argv[1:4]
    load_emissions(emissions_path)
    segments = json.load(open(transcript_path, encoding="utf8"))
    print("audio %.0f s, %d segments" % (duration(), len(segments)), flush=True)
    out, failed, took = align_segments(segments)
    json.dump(out, open(out_path, "w", encoding="utf8"), ensure_ascii=False)
    probs = sorted(w["p"] for w in out)
    print("aligned %d words (%d could not be placed) in %.0f s; median letter probability %.2f"
          % (len(out), failed, took, probs[len(probs) // 2]))


if __name__ == "__main__":
    main()
