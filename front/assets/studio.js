// The clip editor: choose a joke's start and end, change its text, set the GIF's options.
// Everything is previewed here in the browser, without asking the server: a very light video plays at
// once, its HD version replaces it when loaded, and the subtitles are drawn over it exactly as they
// will be burned in (same layout as tools/clip_subs.py). Only the final GIF is rendered on the server.
(() => {
    'use strict';

    const API = '/api/clip', MAX_SECONDS = 20, FPS = 24, HOLD = 1.2, MAX_ROW = 38, BUDGET = 8 * 1024 * 1024;
    const FONT_SIZES = {s: 17, m: 21, l: 27};
    const QUALITY_COST = {light: 0.62, normal: 1, high: 1.45};
    const DEFAULTS = {text: 'words', size: 'm', position: 'bottom', width: 480, fps: 24, quality: 'normal', fit: true};
    const CHOICES = [
        ['text', 'Texte', [['words', 'Mot à mot'], ['line', 'Phrase entière'], ['none', 'Sans texte']]],
        ['size', 'Taille du texte', [['s', 'Petit'], ['m', 'Moyen'], ['l', 'Grand']]],
        ['position', 'Position', [['bottom', 'En bas'], ['top', 'En haut']]],
        ['width', 'Largeur', [[320, '320'], [400, '400'], [480, '480'], [640, '640']]],
        ['fps', 'Fluidité', [[12, '12 i/s'], [15, '15 i/s'], [24, '24 i/s']]],
        ['quality', 'Qualité', [['light', 'Légère'], ['normal', 'Normale'], ['high', 'Max']]],
    ];
    const icon = (d) => `<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="${d}" fill="currentColor"/></svg>`;
    const ICONS = {
        play: icon('M8 5v14l11-7z'), pause: icon('M6 19h4V5H6v14zm8-14v14h4V5h-4z'),
        sound: icon('M3 9v6h4l5 5V4L7 9H3zm13.5 3A4.5 4.5 0 0 0 14 7.97v8.05A4.5 4.5 0 0 0 16.5 12z'),
        muted: icon('M16.5 12A4.5 4.5 0 0 0 14 7.97v2.21l2.45 2.45c.03-.2.05-.41.05-.63zM19 12c0 .94-.2 1.82-.54 2.64l1.51 1.51A8.8 8.8 0 0 0 21 12c0-4.28-2.99-7.86-7-8.77v2.06c2.89.86 5 3.54 5 6.71zM4.27 3 3 4.27 7.73 9H3v6h4l5 5v-6.73l4.25 4.25c-.67.52-1.42.93-2.25 1.18v2.06a8.99 8.99 0 0 0 3.69-1.81L19.73 21 21 19.73l-9-9L4.27 3zM12 4 9.91 6.09 12 8.18V4z'),
        close: icon('M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z'),
        reset: icon('M12 5V1L7 6l5 5V7a6 6 0 1 1-6 6H4a8 8 0 1 0 8-8z'),
        link: icon('M3.9 12c0-1.71 1.39-3.1 3.1-3.1h4V7H7c-2.76 0-5 2.24-5 5s2.24 5 5 5h4v-1.9H7c-1.71 0-3.1-1.39-3.1-3.1zM8 13h8v-2H8v2zm9-6h-4v1.9h4c1.71 0 3.1 1.39 3.1 3.1s-1.39 3.1-3.1 3.1h-4V17h4c2.76 0 5-2.24 5-5s-2.24-5-5-5z'),
        download: icon('M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z'),
        share: icon('M18 16.08c-.76 0-1.44.3-1.96.77L8.91 12.7c.05-.23.09-.46.09-.7s-.04-.47-.09-.7l7.05-4.11c.54.5 1.25.81 2.04.81 1.66 0 3-1.34 3-3s-1.34-3-3-3-3 1.34-3 3c0 .24.04.47.09.7L8.04 9.81C7.5 9.31 6.79 9 6 9c-1.66 0-3 1.34-3 3s1.34 3 3 3c.79 0 1.5-.31 2.04-.81l7.12 4.16c-.05.21-.08.43-.08.65 0 1.61 1.31 2.92 2.92 2.92s2.92-1.31 2.92-2.92-1.31-2.92-2.92-2.92z'),
        check: icon('M9 16.17 4.83 12l-1.42 1.42L9 19 21 7l-1.41-1.41z'),
        back: icon('M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20v-2z'),
    };

    const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
    const clamp = (x, a, b) => Math.min(b, Math.max(a, x));
    const onFrame = (t) => Math.round(t * FPS) / FPS;
    const seconds = (t) => t.toFixed(2).replace('.', ',') + ' s';

    // ---------------------------------------------------------------------------------------------
    // Text: new words take the timing of the words they replace
    // ---------------------------------------------------------------------------------------------

    const norm = (w) => w.toLowerCase().normalize('NFD').replace(/[^\p{L}\p{N}]/gu, '');
    const syllables = (w) => Math.max(1, (norm(w).match(/[aeiouy]+/g) || []).length);
    const textOf = (words) => words.map((w) => w.w).join(' ');

    // Words of a line; French-style detached "!" / "?" stay on the word before them.
    function tokenize(text) {
        const out = [];
        for (const t of text.trim().split(/\s+/)) {
            if (!t) continue;
            if (!norm(t) && out.length) out[out.length - 1] += ' ' + t;
            else out.push(t);
        }
        return out;
    }

    // Longest common subsequence of two lists, as [[i, j], ...]
    function commonWords(a, b) {
        const dp = Array.from({length: a.length + 1}, () => new Uint16Array(b.length + 1));
        for (let i = a.length - 1; i >= 0; i--) {
            for (let j = b.length - 1; j >= 0; j--) {
                dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
            }
        }
        const pairs = [];
        for (let i = 0, j = 0; i < a.length && j < b.length;) {
            if (a[i] === b[j]) pairs.push([i++, j++]);
            else if (dp[i + 1][j] >= dp[i][j + 1]) i++;
            else j++;
        }
        return pairs;
    }

    // Spreads words over [from, to] according to how long they take to say
    function spread(tokens, from, to) {
        const weights = tokens.map((t) => syllables(t) + 0.3);
        const total = weights.reduce((x, y) => x + y, 0);
        let at = from;
        return tokens.map((w, k) => {
            const s = at;
            at += (to - from) * weights[k] / total;
            return {w, s: +s.toFixed(3), e: +at.toFixed(3)};
        });
    }

    // The new text with the original pace: unchanged words keep their time, replaced words share the
    // time of the words they replace, added words take a gap or share it with a neighbour.
    function retime(orig, text, lo, hi) {
        const tokens = tokenize(text);
        if (!tokens.length) return [];
        if (!orig.length) {
            const length = tokens.reduce((n, t) => n + syllables(t) * 0.17 + 0.05, 0.1);
            return spread(tokens, lo, hi - lo >= 0.5 ? Math.min(hi, lo + length) : lo + length);
        }
        const a = orig.map((w) => norm(w.w)), b = tokens.map(norm);
        const pairs = commonWords(a, b);
        const needed = (n) => n * 0.14;
        for (let k = 0; k <= pairs.length; k++) {  // make room for added words that have no gap
            const [pi, pj] = k ? [pairs[k - 1][0] + 1, pairs[k - 1][1] + 1] : [0, 0];
            const [i, j] = k < pairs.length ? pairs[k] : [a.length, b.length];
            if (i > pi || j === pj) continue;
            const from = pi ? orig[pi - 1].e : Math.max(lo, orig[0].s - 0.25);
            const to = i < a.length ? orig[i].s : Math.min(hi, orig[a.length - 1].e + 0.6);
            if (to - from >= needed(j - pj)) continue;
            if (!pairs.length) break;
            pairs.splice(k ? k - 1 : 0, 1);  // the neighbouring word shares its time with the new ones
            k = -1;
        }
        const out = [];
        let pi = 0, pj = 0;
        for (const [i, j] of [...pairs, [a.length, b.length]]) {
            if (j > pj) {
                const from = i > pi ? orig[pi].s : (pi ? orig[pi - 1].e : Math.max(lo, orig[0].s - 0.25));
                const to = i > pi ? orig[i - 1].e : (i < a.length ? orig[i].s : Math.min(hi, orig[a.length - 1].e + 0.6));
                out.push(...spread(tokens.slice(pj, j), from, Math.max(to, from + 0.08 * (j - pj))));
            }
            if (i < a.length) out.push({w: tokens[j], s: orig[i].s, e: orig[i].e});
            pi = i + 1;
            pj = j + 1;
        }
        return out;
    }

    // ---------------------------------------------------------------------------------------------
    // Subtitle layout, as in tools/clip_subs.py
    // ---------------------------------------------------------------------------------------------

    const shown = (ws) => ws.map((x) => x.w.replace(/\*/g, '')).join(' ');

    function screensOf(words, maxRow) {
        const out = [];
        let cur = [];
        for (const w of words) {
            if (cur.length && shown([...cur, w]).length > 2 * maxRow) {
                out.push(cur);
                cur = [];
            }
            cur.push(w);
        }
        if (cur.length) out.push(cur);
        return out;
    }

    function breakAt(screen, maxRow) {
        if (shown(screen).length <= maxRow) return -1;
        let best = 1, bestLen = Infinity;
        for (let k = 1; k < screen.length; k++) {
            const len = Math.max(shown(screen.slice(0, k)).length, shown(screen.slice(k)).length);
            if (len < bestLen) { best = k; bestLen = len; }
        }
        return best;
    }

    // ---------------------------------------------------------------------------------------------
    // The editor
    // ---------------------------------------------------------------------------------------------

    let S = null;  // the open editor

    function layout() {
        const maxRow = Math.round(MAX_ROW * 21 / FONT_SIZES[S.opts.size]);
        const all = S.lines.filter((l) => l.words.length).sort((a, b) => a.words[0].s - b.words[0].s)
            .flatMap((l) => screensOf(l.words, maxRow));
        S.screens = [];
        all.forEach((screen, n) => {
            const first = screen[0].s, last = screen[screen.length - 1];
            const next = n + 1 < all.length ? all[n + 1][0].s : null;
            const end = next !== null ? Math.min(last.e + HOLD, next) : last.e + HOLD;
            if (end <= S.start || first >= S.end || last.s < S.start) return;
            const appear = Math.max(first, S.start);
            let at = appear;
            const reveal = screen.map((w) => (at = Math.max(at, w.s)));
            S.screens.push({words: screen, brk: breakAt(screen, maxRow), appear, end: Math.min(end, S.end), reveal});
        });
        S.drawn = undefined;
    }

    function drawSubtitles(t) {
        const box = S.el.subs;
        const screen = S.opts.text === 'none' ? null : S.screens.find((x) => t >= x.appear && t < x.end) || null;
        if (screen !== S.drawn) {
            S.drawn = screen;
            box.innerHTML = screen ? screen.words.map((w, k) => {
                const html = esc(w.w).replace(/\*([^*]+)\*/g, '<i>$1</i>');
                return (k === screen.brk ? '<br>' : k ? ' ' : '') + `<span>${html}</span>`;
            }).join('') : '';
            S.spans = [...box.querySelectorAll('span')];
            S.revealed = -1;
        }
        if (!screen) return;
        let count = S.opts.text === 'line' ? screen.words.length : 0;
        if (S.opts.text !== 'line') while (count < screen.reveal.length && screen.reveal[count] <= t) count++;
        if (count !== S.revealed) {
            S.spans.forEach((el, k) => { el.style.visibility = k < count ? 'visible' : 'hidden'; });
            S.revealed = count;
        }
    }

    const now = () => S.video.currentTime + S.video._offset;

    function seek(t) {
        t = clamp(t, 0, S.meta.duration);
        S.video.currentTime = Math.max(0, t - S.video._offset);
        S.lastTime = t;
    }

    function setPlaying(on) {
        S.playing = on;
        S.el.play.innerHTML = on ? ICONS.pause : ICONS.play;
        S.el.play.setAttribute('aria-label', on ? 'Pause' : 'Lecture');
        S.el.stage.classList.toggle('st-paused', !on);
        if (!on) return S.video.pause();
        const t = now();
        if (t < S.start || t >= S.end - 0.03) seek(S.start);
        const started = S.video.play();
        if (started) started.catch(() => {  // sound needs a click on this browser: play muted
            if (!S) return;
            if (S.video.muted) return setPlaying(false);
            setMuted(true);
            S.video.play().catch(() => setPlaying(false));
        });
    }

    function setMuted(on) {
        S.muted = on;
        S.video.muted = on;
        S.el.mute.innerHTML = on ? ICONS.muted : ICONS.sound;
        S.el.mute.setAttribute('aria-label', on ? 'Activer le son' : 'Couper le son');
    }

    // The HD video covers only the original clip: it takes over while the cut stays inside it.
    function syncQuality() {
        if (!S || S.result) return;
        const m = S.meta;
        const inside = S.start >= m.clip_start - 0.02 && S.end <= m.clip_end + 0.02;
        const target = S.highReady && inside ? S.el.high : S.el.low;
        let label = 'Aperçu rapide';
        if (target === S.el.high) label = 'HD';
        else if (S.highProgress !== null && inside) label = `HD ${S.highProgress} %`;
        S.el.badge.textContent = label;
        S.el.badge.classList.toggle('st-hd', target === S.el.high);
        if (target === S.video || S.swapping) return;
        S.swapping = true;
        const from = S.video, t = clamp(now(), S.start, S.end - 0.01);
        const done = () => {
            S && (S.swapping = false);
            if (!S) return;
            target.muted = S.muted;
            from.muted = true;
            S.video = target;
            target.classList.add('st-on');
            from.classList.remove('st-on');
            if (S.playing) { const p = target.play(); if (p) p.catch(() => {}); }
            from.pause();
            syncQuality();
        };
        target.addEventListener('seeked', done, {once: true});
        target.currentTime = Math.max(0, t - target._offset);
    }

    const slowConnection = () => {
        const c = navigator.connection;
        return !!c && (c.saveData || /(^|-)2g$/.test(c.effectiveType || ''));
    };

    // Loads the HD clip in the background; on a slow or data-saving connection, stays on the light one
    async function loadHigh() {
        if (slowConnection()) return;
        const me = S;
        me.highProgress = 0;
        syncQuality();
        try {
            const res = await fetch(me.meta.high, {signal: me.abort.signal});
            if (!res.ok || !res.body) return;
            const total = Number(res.headers.get('Content-Length')) || 0, chunks = [];
            let got = 0;
            for (const reader = res.body.getReader(); ;) {
                const {done, value} = await reader.read();
                if (done) break;
                chunks.push(value);
                got += value.length;
                if (total && S === me) { me.highProgress = Math.round(100 * got / total); syncQuality(); }
            }
            if (S !== me) return;
            me.highUrl = URL.createObjectURL(new Blob(chunks, {type: 'video/mp4'}));
            me.el.high.addEventListener('loadeddata', () => {
                if (S !== me) return;
                me.highReady = true;
                me.highProgress = null;
                syncQuality();
            }, {once: true});
            me.el.high.src = me.highUrl;
            me.el.high.load();
        } catch (e) {  // closed, or no network: the light video stays
            if (S === me) { me.highProgress = null; syncQuality(); }
        }
    }

    function tick() {
        if (!S) return;
        if (!S.result || !S.result.shown) {
            let t = now();
            if (S.playing && t >= S.end - 0.015) {
                seek(S.start);
                t = S.start;
            } else if (S.video.ended && S.playing) {
                seek(S.start);
                S.video.play().catch(() => {});
            }
            drawSubtitles(t);
            if (t !== S.lastShown) {
                S.lastShown = t;
                S.el.head.style.left = 100 * t / S.meta.duration + '%';
                S.el.clock.textContent = seconds(Math.max(0, t - S.start));
                markWord(t);
            }
        }
        S.raf = requestAnimationFrame(tick);
    }

    // ------------------------------------- timeline -------------------------------------------------

    function paintBounds() {
        const d = S.meta.duration, a = 100 * S.start / d, b = 100 * S.end / d;
        Object.assign(S.el.shadeL.style, {width: a + '%'});
        Object.assign(S.el.shadeR.style, {left: b + '%'});
        Object.assign(S.el.sel.style, {left: a + '%', width: b - a + '%'});
        S.el.hStart.style.left = a + '%';
        S.el.hEnd.style.left = b + '%';
        S.el.hStart.setAttribute('aria-valuenow', S.start.toFixed(2));
        S.el.hEnd.setAttribute('aria-valuenow', S.end.toFixed(2));
        S.el.startValue.textContent = seconds(S.start - S.meta.clip_start).replace(/^(\d)/, '+$1');
        S.el.endValue.textContent = seconds(S.end - S.meta.clip_end).replace(/^(\d)/, '+$1');
        S.el.length.textContent = seconds(S.end - S.start);
        estimate();
    }

    function paintTicks() {
        const d = S.meta.duration;
        S.el.ticks.innerHTML = S.lines.flatMap((l) => l.words).map((w) =>
            `<i style="left:${100 * w.s / d}%;width:${Math.max(0.25, 100 * (w.e - w.s) / d)}%"></i>`).join('');
    }

    function paintStrip() {
        const track = S.el.strip, h = track.clientHeight || 44, tile = h * 4 / 3, width = track.clientWidth;
        if (!width) return;
        const count = Math.max(1, Math.ceil(width / tile)), n = S.meta.strip_count;
        track.innerHTML = Array.from({length: count}, (_, k) => {
            const frame = Math.min(n - 1, Math.floor((k + 0.5) / count * n));
            return `<b style="background-image:url('${S.meta.strip}');background-size:${tile * n}px ${h}px;` +
                   `background-position:${-frame * tile}px 0"></b>`;
        }).join('');
    }

    // The nearest word edge within a few pixels, so cuts fall between words rather than inside one
    function snap(t, which) {
        const reach = 7 / S.el.track.clientWidth * S.meta.duration;
        let best = t, gap = reach;
        for (const line of S.lines) {
            for (const w of line.words) {
                const edge = which === 'start' ? w.s - 0.08 : w.e + 0.12;
                if (Math.abs(edge - t) < gap) { gap = Math.abs(edge - t); best = edge; }
            }
        }
        return best;
    }

    function setBound(which, t, snapping) {
        const m = S.meta, longest = Math.max(MAX_SECONDS, m.clip_end - m.clip_start);
        if (snapping) t = snap(t, which);
        t = onFrame(t);
        if (which === 'start') S.start = clamp(t, Math.max(0, S.end - longest), S.end - 0.25);
        else S.end = clamp(t, S.start + 0.25, Math.min(m.duration, S.start + longest));
        S.touched = true;
        layout();
        paintBounds();
        syncQuality();
    }

    function dragOn(el, onMove, onEnd) {
        el.addEventListener('pointerdown', (e) => {
            if (e.button) return;
            e.preventDefault();
            e.stopPropagation();
            el.setPointerCapture(e.pointerId);
            el.classList.add('st-grab');
            const move = (ev) => onMove(ev);
            const up = () => {
                el.removeEventListener('pointermove', move);
                el.removeEventListener('pointerup', up);
                el.removeEventListener('pointercancel', up);
                el.classList.remove('st-grab');
                if (onEnd) onEnd();
            };
            el.addEventListener('pointermove', move);
            el.addEventListener('pointerup', up);
            el.addEventListener('pointercancel', up);
            onMove(e);
        });
    }

    function bindTimeline() {
        const at = (e) => {
            const r = S.el.track.getBoundingClientRect();
            return clamp((e.clientX - r.left) / r.width, 0, 1) * S.meta.duration;
        };
        let wasPlaying;
        const bound = (which, el) => {
            dragOn(el, (e) => {
                if (wasPlaying === undefined) { wasPlaying = S.playing; setPlaying(false); }
                setBound(which, at(e), true);
                seek(which === 'start' ? S.start : S.end - 1 / FPS);
            }, () => {
                seek(which === 'start' ? S.start : Math.max(S.start, S.end - 1.2));
                if (wasPlaying) setPlaying(true);
                wasPlaying = undefined;
            });
            el.addEventListener('keydown', (e) => {
                const step = e.shiftKey ? 0.5 : 1 / FPS;
                if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
                    e.preventDefault();
                    setBound(which, (which === 'start' ? S.start : S.end) + (e.key === 'ArrowLeft' ? -step : step));
                    seek(which === 'start' ? S.start : S.end - 1 / FPS);
                }
            });
        };
        bound('start', S.el.hStart);
        bound('end', S.el.hEnd);
        dragOn(S.el.track, (e) => {
            if (wasPlaying === undefined) { wasPlaying = S.playing; setPlaying(false); }
            seek(clamp(at(e), S.start, S.end - 0.01));
        }, () => {
            if (wasPlaying) setPlaying(true);
            wasPlaying = undefined;
        });
        S.root.querySelectorAll('[data-nudge]').forEach((b) => {
            b.onclick = () => {
                const [which, sign] = b.dataset.nudge.split(':');
                setBound(which, (which === 'start' ? S.start : S.end) + Number(sign) * 0.25);
                seek(which === 'start' ? S.start : Math.max(S.start, S.end - 1.2));
            };
        });
    }

    // ---------------------------------------- text --------------------------------------------------

    // The lines said during the cut, in order
    const visibleLines = () => S.lines.filter((l) => {
        const ws = l.words.length ? l.words : l.orig;
        return !ws.length || (ws[ws.length - 1].e > S.start - 0.5 && ws[0].s < S.end + 0.5);
    });

    function lineBounds(line) {
        const all = S.lines.filter((l) => l !== line && (l.words.length || l.orig.length));
        const ref = (line.orig.length ? line.orig : line.words)[0];
        const at = ref ? ref.s : line.at;
        let lo = 0, hi = S.meta.duration;
        for (const l of all) {
            const ws = l.words.length ? l.words : l.orig;
            if (ws[0].s < at) lo = Math.max(lo, ws[ws.length - 1].e);
            else hi = Math.min(hi, ws[0].s);
        }
        return [lo, hi];
    }

    function paintLines() {
        const box = S.el.lines, lines = visibleLines();
        box.innerHTML = lines.length ? '' : '<p class="st-empty">Personne ne parle dans cette partie.</p>';
        for (const line of lines) {
            const row = document.createElement('div');
            row.className = 'st-line' + (line === S.selected ? ' st-sel' : '');
            row.innerHTML = '<textarea rows="1" spellcheck="false" aria-label="Texte de la réplique"></textarea>' +
                `<button class="st-icon st-line-reset" title="Remettre le texte d'origine" aria-label="Remettre le texte d'origine">${ICONS.reset}</button>`;
            const input = row.querySelector('textarea'), reset = row.querySelector('button');
            input.value = line.text;
            const fit = () => { input.style.height = 'auto'; input.style.height = input.scrollHeight + 'px'; };
            const showReset = () => { reset.hidden = line.text === textOf(line.orig) && !line.moved; };
            showReset();
            input.addEventListener('focus', () => {
                if (S.selected !== line) selectLine(line, row);
                const w = line.words[0];
                if (w && !S.playing && (now() < w.s - 0.3 || now() > line.words[line.words.length - 1].e)) {
                    seek(clamp(w.s - 0.15, S.start, S.end - 0.01));
                }
            });
            input.addEventListener('input', () => {
                line.text = input.value;
                const [lo, hi] = lineBounds(line);
                line.words = retime(line.base, line.text, line.orig.length ? lo : line.at, hi);
                S.touched = true;
                fit();
                showReset();
                layout();
                paintTicks();
                paintWords();
            });
            input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); input.blur(); } });
            reset.onclick = () => {
                line.base = line.orig.map((w) => ({...w}));
                line.words = line.orig.map((w) => ({...w}));
                line.text = textOf(line.orig);
                line.moved = false;
                input.value = line.text;
                fit();
                showReset();
                layout();
                paintTicks();
                paintWords();
            };
            line.refresh = showReset;
            box.appendChild(row);
            requestAnimationFrame(fit);
        }
        if (S.selected && !lines.includes(S.selected)) selectLine(null);
        else paintWords();
    }

    function selectLine(line, row) {
        S.selected = line;
        S.el.lines.querySelectorAll('.st-line').forEach((x) => x.classList.toggle('st-sel', x === row));
        paintWords();
    }

    // The selected line's words on their own little timeline: drag one to change when it appears
    function paintWords() {
        const box = S.el.words, line = S.selected;
        S.chips = [];
        if (!line || !line.words.length) {
            box.hidden = true;
            return;
        }
        box.hidden = false;
        const w = line.words, from = Math.max(0, w[0].s - 0.25), to = Math.min(S.meta.duration, w[w.length - 1].e + 0.25);
        const track = box.querySelector('.st-word-track');
        track.innerHTML = '';
        const place = (el, k) => {
            const end = k + 1 < w.length ? w[k + 1].s : w[k].e;
            el.style.left = 100 * (w[k].s - from) / (to - from) + '%';
            el.style.width = Math.max(1.5, 100 * (end - w[k].s) / (to - from)) + '%';
        };
        w.forEach((word, k) => {
            const chip = document.createElement('button');
            chip.className = 'st-chip';
            chip.textContent = word.w.replace(/\*/g, '');
            chip.title = 'Glisser pour changer le moment où ce mot apparaît';
            place(chip, k);
            let grabX = 0, grabS = 0, started = false;
            dragOn(chip, (e) => {
                if (!started) {
                    started = true;
                    grabX = e.clientX;
                    grabS = word.s;
                    if (S.playing) setPlaying(false);
                }
                const dt = (e.clientX - grabX) / track.clientWidth * (to - from);
                const low = k ? w[k - 1].s + 0.04 : Math.max(0, grabS - 1), high = k + 1 < w.length ? w[k + 1].s - 0.04 : Math.min(S.meta.duration, grabS + 1);
                word.s = +clamp(grabS + dt, low, high).toFixed(3);
                if (k) w[k - 1].e = Math.min(w[k - 1].e, word.s);
                word.e = Math.max(word.e, word.s + 0.06);
                if (Math.abs(dt) > 0.005) {
                    line.moved = true;
                    S.touched = true;
                    line.refresh && line.refresh();
                }
                if (k) place(track.children[k - 1], k - 1);
                place(chip, k);
                layout();
                seek(clamp(word.s + 0.01, S.start, S.end - 0.01));
            }, () => {
                started = false;
                line.base = line.words.map((x) => ({...x}));  // later typing keeps this pace
                paintTicks();
            });
            track.appendChild(chip);
            S.chips.push({chip, word});
        });
    }

    function markWord(t) {
        if (!S.chips) return;
        for (const {chip, word} of S.chips) chip.classList.toggle('st-now', t >= word.s && t < word.e);
    }

    // A new empty line at the playhead; its words get a natural pace as they are typed
    function addLine() {
        const line = {orig: [], base: [], words: [], text: '', moved: false, at: clamp(now(), S.start, S.end - 0.2)};
        S.lines.push(line);
        S.lines.sort((a, b) => timeOf(a) - timeOf(b));
        S.selected = line;
        paintLines();
        const row = S.el.lines.querySelector('.st-line.st-sel textarea');
        if (row) row.focus();
    }

    const timeOf = (l) => {
        const ws = l.words.length ? l.words : l.orig;
        return ws.length ? ws[0].s : (l.at === undefined ? 0 : l.at);
    };

    // ---------------------------------------- options -----------------------------------------------

    function paintOptions() {
        S.el.options.innerHTML = CHOICES.map(([key, label, values]) =>
            `<div class="st-opt"><span>${label}</span><div class="st-seg" role="radiogroup" aria-label="${label}">` +
            values.map(([v, text]) => `<button role="radio" aria-checked="${S.opts[key] === v}" data-key="${key}" data-value="${v}">${text}</button>`).join('') +
            '</div></div>').join('') +
            `<label class="st-check"><input type="checkbox" ${S.opts.fit ? 'checked' : ''}> Limiter à 8 Mo, pour WhatsApp</label>`;
        S.el.options.querySelectorAll('button').forEach((b) => {
            b.onclick = () => {
                const raw = b.dataset.value;
                S.opts[b.dataset.key] = /^\d+$/.test(raw) ? Number(raw) : raw;
                paintOptions();
                layout();
                applySubtitleStyle();
                estimate();
            };
        });
        S.el.options.querySelector('input').onchange = (e) => {
            S.opts.fit = e.target.checked;
            estimate();
        };
    }

    function applySubtitleStyle() {
        const stage = S.el.stage, scale = stage.clientWidth / 640;
        stage.style.setProperty('--st-font', FONT_SIZES[S.opts.size] / 1.164 * scale + 'px');
        stage.style.setProperty('--st-stroke', 4 * scale + 'px');
        stage.style.setProperty('--st-margin', 10 * scale + 'px');
        S.el.subs.classList.toggle('st-top', S.opts.position === 'top');
    }

    function estimate() {
        const o = S.opts, seconds_ = S.end - S.start;
        let bytes = 0.42e6 * seconds_ * (o.width / 480) ** 2 * (o.fps / 24) ** 0.6 * QUALITY_COST[o.quality];
        const capped = o.fit && bytes > BUDGET;
        if (capped) bytes = BUDGET;
        const mb = (bytes / 1e6).toFixed(1).replace('.', ',');
        S.el.estimate.textContent = `≈ ${mb} Mo · ${seconds(seconds_)}` + (capped ? ' · largeur réduite' : '');
        S.el.estimate.classList.toggle('st-warn', !o.fit && bytes > BUDGET);
    }

    // ---------------------------------------- render ------------------------------------------------

    function setError(message) {
        S.el.error.textContent = message || '';
        S.el.error.hidden = !message;
    }

    function setBusy(on, label, progress) {
        S.el.create.disabled = on;
        S.el.create.classList.toggle('st-busy', on);
        S.el.createLabel.textContent = on ? label : 'Créer le GIF';
        S.el.create.style.setProperty('--st-progress', on ? (progress || 0) * 100 + '%' : '0%');
    }

    async function render() {
        const me = S, o = S.opts;
        setError('');
        setBusy(true, 'Envoi…', 0);
        const lines = o.text === 'none' ? [] : S.lines.filter((l) => l.words.length).sort((a, b) => a.words[0].s - b.words[0].s)
            .filter((l) => l.words[l.words.length - 1].e > S.start - 10 && l.words[0].s < S.end)
            .map((l) => ({words: l.words.map((w) => ({w: w.w, s: w.s, e: w.e}))}));
        const body = {id: S.meta.id, start: S.start, end: S.end, lines,
                      opts: {width: o.width, fps: o.fps, quality: o.quality, size: o.size, position: o.position,
                             mode: o.text === 'line' ? 'line' : 'words', subs: o.text !== 'none', fit: o.fit}};
        try {
            let state = await postWithRetry(body, me);
            while (S === me && state.state !== 'done') {
                if (state.state === 'error') throw new Error(state.error);
                if (state.state === 'queued') {
                    setBusy(true, state.position > 1 ? `En file d'attente (${state.position})…` : 'Préparation…', 0.02);
                } else {
                    const p = state.progress || 0;
                    setBusy(true, p >= 0.97 ? 'Finalisation…' : `Rendu ${Math.round(p * 100)} %`, Math.max(0.03, p));
                }
                await new Promise((r) => setTimeout(r, 600));
                state = await pollWithRetry(state.job || me.job, me);
            }
            if (S !== me) return;
            setBusy(false);
            showResult(state.url, state.bytes);
        } catch (e) {
            if (S !== me || e.name === 'AbortError') return;
            setBusy(false);
            setError(e.message && !/fetch|network|load/i.test(e.message) ? e.message : 'Connexion perdue, réessaie.');
        }
    }

    async function postWithRetry(body, me) {
        for (let attempt = 0; ; attempt++) {
            try {
                const res = await fetch(API + '/render', {method: 'POST', signal: me.abort.signal,
                    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
                const out = await res.json();
                if (!res.ok) throw Object.assign(new Error(out.error || 'Le rendu a échoué.'), {final: true});
                me.job = out.job;
                return out;
            } catch (e) {
                if (e.final || e.name === 'AbortError' || attempt >= 3) throw e;
                await new Promise((r) => setTimeout(r, 800 * (attempt + 1)));
            }
        }
    }

    // A poll lost on a bad connection is simply tried again
    async function pollWithRetry(job, me) {
        for (let attempt = 0; ; attempt++) {
            try {
                const res = await fetch(`${API}/job/${job}`, {signal: me.abort.signal, cache: 'no-store'});
                const out = await res.json();
                return Object.assign(out, {job});
            } catch (e) {
                if (e.name === 'AbortError' || attempt >= 25) throw e;
                await new Promise((r) => setTimeout(r, 1000));
            }
        }
    }

    // The preview keeps playing while the finished GIF downloads, then the GIF takes its place
    function showResult(url, bytes) {
        const me = S;
        me.result = {url, file: null, shown: false};
        me.root.classList.add('st-done');
        me.el.badge.textContent = 'Chargement du GIF…';
        me.el.badge.classList.remove('st-hd');
        const mb = bytes ? (bytes / 1e6).toFixed(1).replace('.', ',') + ' Mo' : '';
        me.el.doneInfo.textContent = `GIF prêt${mb ? ' · ' + mb : ''}`;
        me.el.save.href = url;
        me.el.save.download = me.filename;
        let tries = 0;
        const loaded = new Image();
        loaded.onerror = () => {  // dropped connection: try again, the buttons work meanwhile
            if (S !== me || !me.result || me.result.url !== url) return;
            if (++tries > 4) {
                me.el.badge.textContent = 'GIF prêt : utilise les boutons';
                return;
            }
            setTimeout(() => { if (S === me) loaded.src = url + '?retry=' + tries; }, 1000 * tries);
        };
        loaded.onload = () => {
            if (S !== me || !me.result || me.result.url !== url) return;
            me.result.shown = true;
            setPlaying(false);
            me.el.subs.innerHTML = '';
            me.drawn = undefined;
            me.el.result.src = url;
            me.el.result.hidden = false;
            me.root.classList.add('st-gif-shown');
            if (!navigator.canShare || slowConnection()) return;  // otherwise fetched only if shared
            fetch(url, {cache: 'force-cache'}).then((r) => r.blob()).then((blob) => {
                if (S === me && me.result && me.result.url === url) {
                    me.result.file = new File([blob], me.filename, {type: 'image/gif'});
                }
            }).catch(() => {});
        };
        loaded.src = url;
    }

    function backToEdit() {
        S.result = null;
        S.el.result.hidden = true;
        S.el.result.removeAttribute('src');
        S.root.classList.remove('st-done', 'st-gif-shown');
        S.el.shareBtn.classList.remove('st-ready');
        S.el.shareBtn.innerHTML = ICONS.share + '<span>Partager</span>';
        syncQuality();
        setPlaying(true);
    }

    const absolute = (url) => new URL(url, location.href).href;

    function flash(button, html) {
        const before = button.innerHTML;
        button.innerHTML = ICONS.check + html;
        setTimeout(() => { if (S) button.innerHTML = before; }, 1500);
    }

    async function shareResult() {
        const r = S.result, url = absolute(r.url), button = S.el.shareBtn;
        button.classList.remove('st-ready');
        try {
            if (!r.file) {
                const blob = await (await fetch(r.url, {cache: 'force-cache'})).blob();
                r.file = new File([blob], S.filename, {type: 'image/gif'});
            }
            if (navigator.canShare && navigator.canShare({files: [r.file]})) await navigator.share({files: [r.file]});
            else if (navigator.share) await navigator.share({url});
            else {
                await navigator.clipboard.writeText(url);
                flash(button, '<span>Lien copié</span>');
            }
        } catch (e) {
            if (e.name === 'NotAllowedError' && r.file) button.classList.add('st-ready');
        }
    }

    // ---------------------------------------- open / close ------------------------------------------

    function close() {
        if (!S) return;
        if (S.touched && !S.result && !confirm('Fermer sans créer le GIF ? Tes modifications seront perdues.')) return;
        const me = S;
        S = null;
        cancelAnimationFrame(me.raf);
        me.abort.abort();
        me.el.low.pause();
        me.el.high.pause();
        me.el.low.removeAttribute('src');
        me.el.high.removeAttribute('src');
        me.el.low.load();
        me.el.high.load();
        if (me.highUrl) URL.revokeObjectURL(me.highUrl);
        window.removeEventListener('keydown', onKey);
        window.removeEventListener('resize', onResize);
        document.documentElement.classList.remove('st-open');
        me.root.remove();
        if (me.opener) me.opener.focus();
    }

    function onKey(e) {
        if (e.key === 'Escape') {
            if (document.activeElement && document.activeElement.tagName === 'TEXTAREA') document.activeElement.blur();
            else close();
            return;
        }
        if (e.key === ' ' && !/^(TEXTAREA|INPUT|BUTTON|A)$/.test(document.activeElement.tagName) && !S.result) {
            e.preventDefault();
            setPlaying(!S.playing);
        }
    }

    function onResize() {
        if (!S || !S.meta) return;
        applySubtitleStyle();
        paintStrip();
    }

    const TEMPLATE = `
      <div class="st-modal">
        <header class="st-head">
          <div><h2>Modifier le GIF</h2><p class="st-title"></p></div>
          <button class="st-icon st-x" aria-label="Fermer">${ICONS.close}</button>
        </header>
        <div class="st-body">
          <div class="st-main">
            <div class="st-stage st-paused">
              <video class="st-low st-on" playsinline preload="auto" muted></video>
              <video class="st-high" playsinline preload="auto" muted></video>
              <div class="st-subs" aria-hidden="true"></div>
              <img class="st-result" alt="GIF créé" hidden>
              <span class="st-badge">Chargement…</span>
              <button class="st-bigplay" aria-label="Lecture">${ICONS.play}</button>
            </div>
            <div class="st-edit-only">
              <div class="st-transport">
                <button class="st-icon st-play" aria-label="Lecture">${ICONS.play}</button>
                <span class="st-clock">0,00 s</span>
                <span class="st-sep">/</span>
                <span class="st-length"></span>
                <button class="st-icon st-mute"></button>
              </div>
              <div class="st-timeline">
                <div class="st-track">
                  <div class="st-strip"></div>
                  <div class="st-ticks"></div>
                  <div class="st-shade st-shade-l"></div>
                  <div class="st-shade st-shade-r"></div>
                  <div class="st-selbox"></div>
                  <div class="st-playhead"></div>
                  <div class="st-handle st-h-start" role="slider" tabindex="0" aria-label="Début de l'extrait"></div>
                  <div class="st-handle st-h-end" role="slider" tabindex="0" aria-label="Fin de l'extrait"></div>
                </div>
              </div>
              <div class="st-bounds">
                <div><span>Début</span><button data-nudge="start:-1" aria-label="Début plus tôt">−</button><output class="st-start-v"></output><button data-nudge="start:1" aria-label="Début plus tard">+</button></div>
                <button class="st-reset-cut">Extrait d'origine</button>
                <div><span>Fin</span><button data-nudge="end:-1" aria-label="Fin plus tôt">−</button><output class="st-end-v"></output><button data-nudge="end:1" aria-label="Fin plus tard">+</button></div>
              </div>
            </div>
          </div>
          <div class="st-side st-edit-only">
            <section>
              <div class="st-section-head"><h3>Texte</h3><button class="st-add">+ Ajouter une ligne</button></div>
              <div class="st-lines"></div>
              <div class="st-words" hidden>
                <small>Glisse un mot pour changer quand il apparaît</small>
                <div class="st-word-track"></div>
              </div>
            </section>
            <section><h3>Options du GIF</h3><div class="st-options"></div></section>
          </div>
        </div>
        <footer class="st-foot">
          <div class="st-foot-edit">
            <span class="st-estimate"></span>
            <span class="st-error" role="alert" hidden></span>
            <button class="st-create"><span class="st-create-label">Créer le GIF</span></button>
          </div>
          <div class="st-foot-done">
            <button class="st-text-btn st-back-edit">${ICONS.back}<span>Modifier</span></button>
            <span class="st-done-info"></span>
            <button class="st-text-btn st-copy">${ICONS.link}<span>Lien</span></button>
            <a class="st-text-btn st-save">${ICONS.download}<span>Télécharger</span></a>
            <button class="st-text-btn st-primary st-share">${ICONS.share}<span>Partager</span></button>
          </div>
        </footer>
      </div>`;

    // A joke's data, fetched once and tried again if the connection drops
    const metas = {};
    function fetchMeta(id) {
        metas[id] = metas[id] || (async () => {
            for (let attempt = 0; ; attempt++) {
                try {
                    const res = await fetch(`/studio/${encodeURIComponent(id)}.json`);
                    if (res.status === 404) throw Object.assign(new Error('missing'), {final: true});
                    if (!res.ok) throw new Error('unavailable');
                    return await res.json();
                } catch (e) {
                    if (e.final || attempt >= 4) {
                        delete metas[id];
                        throw e;
                    }
                    await new Promise((r) => setTimeout(r, 500 * (attempt + 1)));
                }
            }
        })();
        return metas[id];
    }

    async function open(id, filename, opener, poster) {
        if (S) return;
        const root = document.createElement('div');
        root.className = 'st-backdrop';
        root.setAttribute('role', 'dialog');
        root.setAttribute('aria-modal', 'true');
        root.setAttribute('aria-label', 'Modifier le GIF');
        root.innerHTML = TEMPLATE;
        document.body.appendChild(root);
        document.documentElement.classList.add('st-open');
        const $ = (q) => root.querySelector(q);
        const el = {
            stage: $('.st-stage'), low: $('.st-low'), high: $('.st-high'), subs: $('.st-subs'), result: $('.st-result'),
            badge: $('.st-badge'), play: $('.st-play'), big: $('.st-bigplay'), mute: $('.st-mute'), clock: $('.st-clock'),
            length: $('.st-length'), track: $('.st-track'), strip: $('.st-strip'), ticks: $('.st-ticks'),
            shadeL: $('.st-shade-l'), shadeR: $('.st-shade-r'), sel: $('.st-selbox'), head: $('.st-playhead'),
            hStart: $('.st-h-start'), hEnd: $('.st-h-end'), startValue: $('.st-start-v'), endValue: $('.st-end-v'),
            lines: $('.st-lines'), words: $('.st-words'), options: $('.st-options'), estimate: $('.st-estimate'),
            error: $('.st-error'), create: $('.st-create'), createLabel: $('.st-create-label'), doneInfo: $('.st-done-info'),
            save: $('.st-save'), shareBtn: $('.st-share'),
        };
        S = {root, el, opener, filename: filename || id + '.gif', abort: new AbortController(), opts: {...DEFAULTS},
             lines: [], screens: [], playing: false, muted: false, highReady: false, highProgress: null, result: null,
             video: el.low, touched: false, start: 0, end: 1};
        const me = S;
        if (poster) el.low.poster = poster;  // already loaded: shows at once, until the video starts
        window.addEventListener('keydown', onKey);
        window.addEventListener('resize', onResize);
        $('.st-x').onclick = close;
        root.addEventListener('pointerdown', (e) => { if (e.target === root) close(); });
        try {
            me.meta = await fetchMeta(id);
        } catch (e) {
            if (S !== me) return;
            root.classList.add('st-unavailable');
            el.badge.textContent = 'Indisponible';
            setError(e.final ? 'Cet extrait n\'est pas encore disponible dans l\'éditeur.' : 'Connexion trop faible, réessaie.');
            el.create.disabled = true;
            return;
        }
        if (S !== me) return;
        const m = me.meta;
        $('.st-title').textContent = (m.title || '').split('\n')[0];
        me.start = m.clip_start;
        me.end = m.clip_end;
        me.lines = m.lines.map((l) => ({orig: l.words.map((w) => ({...w})), base: l.words.map((w) => ({...w})),
                                        words: l.words.map((w) => ({...w})), text: textOf(l.words), moved: false}));
        el.low._offset = 0;
        el.high._offset = m.clip_start;
        el.low.src = m.low;
        el.mute.onclick = () => setMuted(!me.muted);
        el.play.onclick = () => setPlaying(!me.playing);
        el.big.onclick = () => setPlaying(true);
        el.stage.addEventListener('click', (e) => {
            if (e.target === el.big || el.big.contains(e.target) || me.result) return;
            setPlaying(!me.playing);
        });
        el.low.addEventListener('loadeddata', () => {
            if (S !== me) return;
            seek(me.start);
            syncQuality();
            setMuted(false);
            setPlaying(true);
            loadHigh();
        }, {once: true});
        el.create.onclick = render;
        $('.st-back-edit').onclick = backToEdit;
        $('.st-add').onclick = addLine;
        $('.st-reset-cut').onclick = () => {
            me.start = m.clip_start;
            me.end = m.clip_end;
            layout();
            paintBounds();
            paintLines();
            syncQuality();
            seek(me.start);
        };
        $('.st-copy').onclick = (e) => {
            const b = e.currentTarget;
            navigator.clipboard.writeText(absolute(me.result.url)).then(() => flash(b, '<span>Copié</span>'));
        };
        el.shareBtn.onclick = shareResult;
        // Text follows the cut: the list of lines updates when a drag ends
        for (const h of [el.hStart, el.hEnd]) h.addEventListener('pointerup', () => paintLines());
        bindTimeline();
        paintOptions();
        applySubtitleStyle();
        layout();
        paintBounds();
        paintTicks();
        paintStrip();
        paintLines();
        me.raf = requestAnimationFrame(tick);
    }

    // Called when a GIF is shown: its data and light video are then in the cache if the editor is opened
    const warmed = new Set();
    function prefetch(id) {
        fetchMeta(id).then((meta) => {
            if (warmed.has(id) || slowConnection()) return;
            warmed.add(id);
            return fetch(meta.low, {priority: 'low'}).then((r) => r.arrayBuffer()).catch(() => warmed.delete(id));
        }).catch(() => {});
    }

    window.DomusStudio = {open, retime, prefetch};
})();
