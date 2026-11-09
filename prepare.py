import collections
import csv
import hashlib
import glob
import gzip
import html
import json
import re
import unicodedata
import urllib.parse
from string import Template
import os
import shutil
import sys
import subprocess
import time


def prepare_html(text: str) -> str:
    return text.strip().replace('\n', '<br/>')


def genSegment(text: str, title: str):
    if len(text) == 0:
        return ""
    html_text = prepare_html(text)
    return f"""
<div class="quote-explication">
    <div class="quote-explication-title">{title}</div>
    <div class="quote-explication-content">{html_text}</div>
</div>"""


def genLongText(long_text: str):
    return genSegment(long_text, "Citation entière")


def genExplanation(explanation: str):
    return genSegment(explanation, "Explication")


def genTags(tags: [str]):
    if len(tags) == 0:
        return ""
    tags_html = ''
    for tag in tags:
        tags_html += f'<span class="quote-tag">{tag}</span>\n'
    return f"""
<div class="quote-tags">
    <span class="quote-tag-title">Tags</span>
    {tags_html}
</div>"""


FPS = 24  # frame rate of movie.mkv, for hh:mm:ss:ff timecodes


def parseTimecode(timecode: str) -> float:
    """'hh:mm:ss' or 'hh:mm:ss:ff' to seconds."""
    parts = [int(x) for x in timecode.split(':')]
    return parts[0] * 3600 + parts[1] * 60 + parts[2] + (parts[3] / FPS if len(parts) == 4 else 0)


def parseData() -> [dict]:
    data = []
    with open('data.csv', 'r', encoding="utf8") as file:
        reader = csv.reader(file)

        next(reader)  # skip headers

        for row in reader:
            if row[1] == '':
                continue
            timecode_parsed = row[1].split(':')  # hh:mm:ss, or hh:mm:ss:ff to pick a precise frame

            if len(timecode_parsed) in (3, 4):
                hour, minute, second = map(int, timecode_parsed[:3])
                frame = int(timecode_parsed[3]) if len(timecode_parsed) == 4 else 0
                if frame >= FPS:
                    raise ValueError(f"Invalid frame in timecode: {row[1]} (frames go from 0 to {FPS - 1})")
                timecode_seconds: int = hour * 3600 + minute * 60 + second
            else:
                raise ValueError(f"Invalid timecode format: {row[1]} (expected hh:mm:ss or hh:mm:ss:ff)")

            tags = [tag.strip() for tag in row[4].split(',') if tag.strip() != '']

            # Optional "Début extrait" / "Fin extrait" columns: the joke's video clip
            clip_start = parseTimecode(row[7]) if len(row) > 8 and row[7] and row[8] else None
            clip_end = parseTimecode(row[8]) if clip_start is not None else None

            data.append({
                'id': row[0],  # ''
                'timecode': ':'.join(timecode_parsed[:3]),  # 'hh:mm:ss', the frame is not displayed
                'timecode_seconds': timecode_seconds,  # 1234
                'image_name': f'{timecode_seconds}f{frame}' if frame else str(timecode_seconds),  # 1234 or 1234f12
                'image_msec': (timecode_seconds + frame / FPS) * 1000,
                'title': row[2],  # 'title'
                'long_text': row[3],  # 'long text'
                'tags': tags,  # ['tag1', 'tag2']
                'explanation': row[5],  # 'explanation',
                'interesting': row[6],  # 'TRUE/FALSE'
                'clip_start': clip_start,  # seconds, or None when the joke has no clip
                'clip_end': clip_end,
                # The name changes with the bounds, so an edited clip is encoded again
                'clip_name': f'{row[0]}_{round(clip_start * FPS)}-{round(clip_end * FPS)}' if clip_start is not None else None,
            })

    return data


PLAY_ICON = ('<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">'
             '<path d="M8 5v14l11-7z" fill="#fff"/></svg>')


def genClipHtml(datum: dict, prefix: str):
    """The clap and GIF icons next to the share buttons, and the play button over the screenshot."""
    if not datum['clip_name']:
        return '', ''
    url = f"{prefix}extraits/{datum['clip_name']}.mp4"
    # Downloads the clip under a readable name, e.g. 355-amonbofis-menace-numerobis-de-devenir.mp4.
    # On a phone, shareClip hands the file to the share sheet (WhatsApp...) instead; on a computer,
    # the GIF shows over the screenshot, to drag into a chat or copy its link.
    filename = f"{datum['id']}-{slugify(datum['title'].split(chr(10))[0])[:50].rstrip('-')}"
    link = (f'<a href="{url}" download="{filename}.mp4" onclick="shareClip(this, event)">'
            f'<img src="{prefix}assets/clip.svg" height="16" width="16" '
            f'alt="Télécharger l\'extrait" title="Télécharger l\'extrait"/></a>'
            f'<a href="{url[:-4]}.gif" download="{filename}.gif" data-link="{SITE}/gif/{datum["id"]}.gif" '
            f'onclick="shareClip(this, event)">'
            f'<img src="{prefix}assets/gif.svg" height="16" width="16" '
            f'alt="GIF de l\'extrait" title="GIF de l\'extrait, à partager"/></a>')
    play = (f'<button class="quote-play" data-clip="{url}" onclick="playClip(this, event)" '
            f'title="Lancer l\'extrait" aria-label="Lancer l\'extrait">{PLAY_ICON}</button>')
    return link, play


def genQuotes(data: [dict]):
    quote_template = Template(open('front/quote_template.html', 'r', encoding="utf8").read())

    quotes: str = ""
    for datum in data:
        timecode_seconds = datum['timecode_seconds']
        image = f'images/{datum["image_name"]}.jpg'
        image_preview = f'images/{datum["image_name"]}_preview.jpg'
        image_previewxl = f'images/{datum["image_name"]}_previewxl.jpg'

        quote = quote_template.substitute(
            id=datum['id'],
            imagefull=image,
            imagepreview=image_preview,
            imagepreviewxl=image_previewxl,
            timecode=datum['timecode'],
            timecode_seconds=timecode_seconds,
            title=prepare_html(datum['title']),
            longtext=genLongText(datum['long_text']),
            tags=genTags(datum['tags']),
            explanation=genExplanation(datum['explanation']),
            interesting=datum['interesting'],
            cliplink=genClipHtml(datum, '')[0],
            clipplay=genClipHtml(datum, '')[1],
        )

        quotes += quote
    return quotes


def genQuotesPages(data: [dict]):
    quote_template = Template(open('front/quote_page_template.html', 'r', encoding="utf8").read())
    footer = open('front/footer.html', 'r', encoding="utf8").read()

    for datum in data:
        id = datum['id']
        image = f'images/{datum["image_name"]}.jpg'
        image_preview = f'images/{datum["image_name"]}_preview.jpg'
        image_previewxl = f'images/{datum["image_name"]}_previewxl.jpg'

        quote = quote_template.substitute(
            id=id,
            imagefull=image,
            imagepreview=image_preview,
            imagepreviewxl=image_previewxl,
            timecode=datum['timecode'],
            title=prepare_html(datum['title']),
            metatitle=datum['title'].replace('\n', ' ').strip(),
            longtext=genLongText(datum['long_text']),
            tags=genTags(datum['tags']),
            explanation=genExplanation(datum['explanation']),
            cliplink=genClipHtml(datum, '../')[0],
            clipplay=genClipHtml(datum, '../')[1],
            footer=footer,
            style="style.css?r=" + str(time.time())
        )

        with open(f'build/quotes/{id}.html', 'w', encoding="utf8") as quote_file:
            quote_file.write(quote)


SITE = 'https://domusquietusdomus.douady.paris'
EMISSIONS = 'emissions.npz'  # the audio model's output for the movie (tools/align_words.py --emissions)


def genSubtitles(data: [dict], printv=print):
    """Word-by-word subtitles of each clip (see tools/), when emissions.npz is there and ffmpeg can
    burn them in. A clip's name then ends with a hash of its subtitles, so it is encoded again when
    they change (an edited quote or correction)."""
    sys.path.insert(0, 'tools')
    import clip_subs
    if not os.path.exists(EMISSIONS):
        printv(f"no {EMISSIONS}: clips without subtitles")
        return
    if not clip_subs.ffmpeg_has_libass():
        printv("ffmpeg can't burn subtitles in (no libass): clips without subtitles")
        return
    import subtitles  # needs numpy and num2words
    words = subtitles.build(EMISSIONS, 'build/sous-titres', log=printv)
    for datum in data:
        if datum['clip_name']:
            ass = clip_subs.make_ass(words, datum['clip_start'], datum['clip_end'])
            if clip_subs.has_subtitles(ass):
                datum['clip_ass'] = ass
                datum['clip_name'] += '_' + hashlib.sha1(ass.encode()).hexdigest()[:8]


def genClips(data: [dict], videoFilePath: str):
    """Encodes each joke's clip (mp4 with its subtitles, and GIF) into build/extraits once, then
    removes the clips no joke uses any more (bounds or subtitles changed)."""
    sys.path.insert(0, 'tools')
    import clip_subs
    os.makedirs('build/extraits', exist_ok=True)
    for datum in data:
        if not datum['clip_name']:
            continue
        path = f"build/extraits/{datum['clip_name']}"
        if not os.path.exists(path + '.mp4'):
            ass = None
            if datum.get('clip_ass'):
                ass = path + '.ass'
                with open(ass, 'w', encoding='utf8') as f:
                    f.write(datum['clip_ass'])
            result = subprocess.run(
                ['ffmpeg', '-v', 'error', '-y', '-ss', '%.3f' % datum['clip_start'],
                 '-t', '%.3f' % (datum['clip_end'] - datum['clip_start']),
                 '-i', videoFilePath, '-vf', clip_subs.video_filter(ass)] + clip_subs.MP4_ARGS
                + ['-f', 'mp4', path + '.mp4.part'])
            if result.returncode != 0:
                print(f"Error while generating clip {path}.mp4")
                if os.path.exists(path + '.mp4.part'):
                    os.remove(path + '.mp4.part')
                continue
            os.replace(path + '.mp4.part', path + '.mp4')
            print(f"{path}.mp4 generated")
        if not os.path.exists(path + '.gif'):
            try:
                with open(path + '.mp4', 'rb') as f:
                    gif = clip_subs.make_gif(f.read(), lambda argv, data: subprocess.run(
                        argv, input=data, capture_output=True, check=True).stdout)
            except subprocess.CalledProcessError as e:
                print(f"Error while generating {path}.gif: {e.stderr.decode(errors='replace')}")
                continue
            with open(path + '.gif.part', 'wb') as f:
                f.write(gif)
            os.replace(path + '.gif.part', path + '.gif')
    # A link that stays the same when the subtitles change (and the clip's name with them), to paste in
    # a chat: gif/<id>.gif points to the joke's current GIF.
    os.makedirs('build/gif', exist_ok=True)
    links = {f"{datum['id']}.gif": f"../extraits/{datum['clip_name']}.gif" for datum in data
             if datum['clip_name'] and os.path.exists(f"build/extraits/{datum['clip_name']}.gif")}
    for name in os.listdir('build/gif'):
        if name not in links:
            os.remove(f'build/gif/{name}')
    for name, target in links.items():
        if not os.path.islink(f'build/gif/{name}') or os.readlink(f'build/gif/{name}') != target:
            os.symlink(target, f'build/gif/{name}.part')
            os.replace(f'build/gif/{name}.part', f'build/gif/{name}')
    wanted = {datum['clip_name'] for datum in data if datum['clip_name']}
    for name in os.listdir('build/extraits'):
        stem, ext = os.path.splitext(name)
        if ext in ('.mp4', '.gif', '.ass', '.part') and (stem.split('.')[0] not in wanted or ext == '.part'):
            os.remove(f'build/extraits/{name}')


def genImages(data: [dict], videoFilePath: str):
    import cv2
    video_file = cv2.VideoCapture(videoFilePath)

    for datum in data:
        name = datum['image_name']
        if os.path.exists(f'build/images/{name}.jpg'):
            continue
        video_file.set(cv2.CAP_PROP_POS_MSEC, datum['image_msec'])
        success, image = video_file.read()
        if success:
            cv2.imwrite(f'build/images/{name}.jpg', image)
            cv2.imwrite(f'build/images/{name}_preview.jpg', cv2.resize(image, (1280 // 8, 544 // 8)))
            cv2.imwrite(f'build/images/{name}_previewxl.jpg', cv2.resize(image, (1280 // 2, 544 // 2)))
            print(f"build/images/{name}.jpg generated")
        else:
            print(f"Error while generating image for {name}")


def genIndex(quotes: str):
    index = open('front/index_template.html', 'r', encoding="utf8").read()
    index = index.replace("$content", quotes)
    index = index.replace("$footer", open('front/footer.html', 'r', encoding="utf8").read())
    index = index.replace("$style", "style.css?r=" + str(time.time()))

    with open('build/index.html', 'w', encoding="utf8") as index_file:
        index_file.write(index)


def compressFront(verbose=True):
    import gzip

    printv = print if verbose else lambda *a, **k: None

    def listFiles():
        for file in os.listdir('build'):
            yield 'build/' + file
        for file in os.listdir('build/assets'):
            yield 'build/assets/' + file

    for file in listFiles():
        if not (file.endswith('.html') or file.endswith('.css') or file.endswith('.js')):
            continue
        with open(file, 'rb') as f:
            content = f.read()
        with gzip.open(file + '.gz', 'wb') as f:
            f.write(content)
        printv(f"compressed {file} to {file}.gz")


# ---------------------------------------------------------------------------
# /stats and /personnages pages
# ---------------------------------------------------------------------------

FILM_LENGTH_SECONDS = 6480  # same length as the density bar on the index

# Tags that describe the kind of joke. Every other tag is a character.
CATEGORY_TAGS = {
    'référence': "Clin d'œil à un film, une chanson, une marque, une expression…",
    'visuel': "Gag qui se voit plus qu'il ne s'entend",
    'audio': "Gag sonore : bruitage, musique, doublage",
    'cliché': "Joue avec un cliché ou un stéréotype",
    'historique': "Clin d'œil à l'Histoire (ou à ses anachronismes)",
    'astérixme': "Numérobis écorche un nom gaulois : Astérixme, Panoramixme…",
    'latin': "Latin, vrai ou de cuisine",
    'voix-off': "Le narrateur s'en mêle",
}

LOG_LINE = re.compile(
    r'^(\S+) \S+ \S+ \[(\d{2})/(\w{3})/(\d{4}):[^\]]*\] "GET /quotes/(\d+)\.html[^"]*" (\d{3}) \S+ "[^"]*" "([^"]*)"')
# Link previews from chat apps: someone pasted the joke's link in a conversation.
SHARE_AGENTS = re.compile(r'whatsapp|discord|telegram|slack|twitterbot|facebookexternalhit|skypeuripreview|linkedinbot|signal',
                          re.IGNORECASE)
BOT_AGENTS = re.compile(r'bot|crawl|spider|slurp|scrap|curl|wget|python|java|go-http|okhttp|axios|fetch|headless|turnitin|'
                        r'read-aloud|http', re.IGNORECASE)
# Someone opening more joke pages than this in one day is a scraper, not a reader.
MAX_QUOTES_PER_VISITOR_PER_DAY = 15
MONTHS = {m: i + 1 for i, m in enumerate(['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                                          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])}
MONTHS_FR = ['janvier', 'février', 'mars', 'avril', 'mai', 'juin',
             'juillet', 'août', 'septembre', 'octobre', 'novembre', 'décembre']


def updateViews(path='views.json') -> dict:
    """Count joke page views from the nginx access logs into views.json.

    nginx only keeps two weeks of logs, so views.json (not in git) keeps the
    older days: {"2026-11-07": {"124": [views, shares]}}. A view is one visitor
    (IP) opening a joke page on a given day; bots and scrapers are skipped.
    A share is a chat app (WhatsApp, Discord…) fetching the link preview.
    """
    try:
        with open(path, 'r', encoding='utf8') as file:
            views = json.load(file)
    except (OSError, ValueError):
        views = {}

    hits = []
    for log_file in sorted(glob.glob(os.environ.get('NGINX_LOGS', '/var/log/nginx/access.log*'))):
        opener = gzip.open if log_file.endswith('.gz') else open
        try:
            with opener(log_file, 'rt', encoding='utf8', errors='replace') as file:
                for line in file:
                    if '/quotes/' not in line:
                        continue
                    match = LOG_LINE.match(line)
                    if match and match.group(6) in ('200', '304') and match.group(3) in MONTHS:
                        ip, day, month, year, quote_id, _, agent = match.groups()
                        date = f'{year}-{MONTHS[month]:02d}-{day}'
                        hits.append((ip, date, quote_id, agent))
        except OSError:
            continue

    pages_per_visitor = collections.defaultdict(set)
    for ip, date, quote_id, agent in hits:
        if not SHARE_AGENTS.search(agent) and not BOT_AGENTS.search(agent):
            pages_per_visitor[(ip, date)].add(quote_id)

    days = {}
    seen = set()
    for ip, date, quote_id, agent in hits:
        if SHARE_AGENTS.search(agent):
            kind = 1
        elif BOT_AGENTS.search(agent) or len(pages_per_visitor[(ip, date)]) > MAX_QUOTES_PER_VISITOR_PER_DAY:
            continue
        else:
            kind = 0
        if (ip, date, quote_id, kind) in seen:
            continue
        seen.add((ip, date, quote_id, kind))
        days.setdefault(date, {}).setdefault(quote_id, [0, 0])[kind] += 1

    def total(day_counts):
        return sum(v + s for v, s in day_counts.values())

    for date, day_counts in days.items():
        # The oldest log file can start mid-day: never replace a fuller count.
        if total(day_counts) >= total(views.get(date, {})):
            views[date] = day_counts

    if days:
        with open(path, 'w', encoding='utf8') as file:
            json.dump(views, file, sort_keys=True)
    return views


def frenchDate(iso_date: str) -> str:
    year, month, day = map(int, iso_date.split('-'))
    return f"{'1er' if day == 1 else day} {MONTHS_FR[month - 1]} {year}"


def formatDuration(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    if minutes == 0:
        return f'{seconds} s'
    return f'{minutes} min {seconds:02d} s' if seconds else f'{minutes} min'


def formatTimecode(seconds: int) -> str:
    return f'{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}'


def quoteTitle(datum: dict) -> str:
    return html.escape(datum['title'].strip().split('\n')[0])


def tagLink(tag: str) -> str:
    return f'/?tag={urllib.parse.quote(tag)}'


def slugify(name: str) -> str:
    name = unicodedata.normalize('NFD', name.lower())
    name = ''.join(c for c in name if unicodedata.category(c) != 'Mn')
    return re.sub(r'[^a-z0-9]+', '-', name).strip('-')


def genBars(rows: [tuple], unit: str) -> str:
    """Horizontal bar chart. rows = [(label_html, value, tooltip_label)]"""
    if not rows:
        return ''
    top = max(value for _, value, _ in rows)
    html_rows = ''
    for label, value, tooltip in rows:
        html_rows += f"""
    <div class="bar-row" title="{html.escape(tooltip)} : {value} {unit}">
        <div class="bar-label">{label}</div>
        <div class="bar-track"><div class="bar" style="width: {100 * value / top:.1f}%"></div></div>
        <div class="bar-value">{value}</div>
    </div>"""
    return f'<div class="bars">{html_rows}\n</div>'


def genStatSection(title: str, intro: str, body: str) -> str:
    intro_html = f'<p class="stats-intro">{intro}</p>' if intro else ''
    return f"""
<section class="stats-section">
    <h2 class="stats-title">{title}</h2>
    {intro_html}
    {body}
</section>"""


def genTopViewed(data: [dict], views: dict) -> str:
    title = 'Les blagues les plus vues'
    if not views:
        return genStatSection(title, "Pas encore de données de visite : elles sont calculées sur le serveur à partir de ses "
                                     "journaux.", '')

    totals = collections.Counter()
    shares = collections.Counter()
    for day_counts in views.values():
        for quote_id, (view_count, share_count) in day_counts.items():
            totals[quote_id] += view_count
            shares[quote_id] += share_count

    by_id = {datum['id']: datum for datum in data}
    ranked = [quote_id for quote_id, _ in totals.most_common() if quote_id in by_id][:15]
    top = totals[ranked[0]] if ranked else 1
    rows = ''
    for rank, quote_id in enumerate(ranked, 1):
        datum = by_id[quote_id]
        share_html = ''
        if shares[quote_id]:
            share_html = f'<span class="top-shares">{shares[quote_id]} partage{"s" if shares[quote_id] > 1 else ""}</span>'
        rows += f"""
    <a class="top-quote" href="/quotes/{quote_id}.html">
        <span class="top-rank">{rank}</span>
        <span class="quote-timecode">{datum['timecode']}</span>
        <span class="top-title">{quoteTitle(datum)}</span>
        {share_html}
        <span class="top-count"><span class="top-bar" style="width: {100 * totals[quote_id] / top:.1f}%"></span>{totals[quote_id]}</span>
    </a>"""

    return genStatSection(title, '', f'<div class="top-quotes">{rows}\n</div>')


def genStatsPage(data: [dict], views: dict, characters: [dict]):
    count = len(data)
    tag_count = collections.Counter(tag for datum in data for tag in datum['tags'])
    character_tags = [(tag, n) for tag, n in tag_count.most_common() if tag not in CATEGORY_TAGS]
    on_screen = sum(1 for c in characters if c['category'] == 'personnage') or len(character_tags)

    hours, minutes = divmod(FILM_LENGTH_SECONDS // 60, 60)
    content = (f'<p class="stats-summary">{count} blagues en {hours} h {minutes:02d} de film, soit une toutes les '
               f'{round(FILM_LENGTH_SECONDS / count)} secondes. On y croise {tag_count["référence"]} références et '
               f'<a href="/personnages/">{on_screen} personnages</a>.</p>')

    content += genTopViewed(data, views)

    # Characters
    known = {c['tag']: c for c in characters}
    rows = []
    for tag, n in character_tags:
        label = f'<a href="{tagLink(tag)}">{html.escape(tag)}</a>'
        if tag in known:
            label += f' <a class="bar-aside" href="/personnages/#{slugify(known[tag]["name"])}" title="Origine du nom">?</a>'
        rows.append((label, n, tag))
    content += genStatSection('Personnages les plus cités', "Nombre de blagues où chaque personnage est concerné.",
                              genBars(rows, 'blagues'))

    # Kinds of jokes
    rows = []
    for tag, description in sorted(CATEGORY_TAGS.items(), key=lambda kv: -tag_count[kv[0]]):
        if tag_count[tag]:
            rows.append((f'<a href="{tagLink(tag)}">{tag}</a><div class="bar-note">{description}</div>',
                         tag_count[tag], tag))
    untagged = sum(1 for datum in data if not any(tag in CATEGORY_TAGS for tag in datum['tags']))
    content += genStatSection('Types de blagues',
                              f"Une blague peut avoir plusieurs types. {untagged} blagues n'en ont aucun : "
                              "des répliques drôles tout simplement.",
                              genBars(rows, 'blagues'))

    # Jokes along the film, by 5 minutes
    bucket = 300
    buckets = [0] * -(-FILM_LENGTH_SECONDS // bucket)
    for datum in data:
        if 0 <= datum['timecode_seconds'] < FILM_LENGTH_SECONDS:
            buckets[datum['timecode_seconds'] // bucket] += 1
    top = max(buckets)
    cols = ''
    for i, n in enumerate(buckets):
        start, end = i * bucket, min((i + 1) * bucket, FILM_LENGTH_SECONDS)
        label = f'{i * 5}′' if i % 3 == 0 else ''
        value = f'<div class="timeline-value">{n}</div>' if n == top else ''
        cols += f"""
        <div class="timeline-col" title="{formatTimecode(start)} – {formatTimecode(end)} : {n} blagues">
            {value}<div class="timeline-bar" style="height: {100 * n / top:.1f}%"></div>
            <div class="timeline-label">{label}</div>
        </div>"""
    busiest = buckets.index(top)
    calmest = min(range(len(buckets) - 1), key=lambda i: buckets[i])  # the last bucket is shorter
    times = sorted(datum['timecode_seconds'] for datum in data)
    gap, gap_start = max((b - a, a) for a, b in zip(times, times[1:]))
    densest_minute = collections.Counter(t // 60 for t in times).most_common(1)[0]
    facts = f"""
    <ul class="stats-facts">
        <li>Les 5 minutes les plus chargées : de {formatTimecode(busiest * bucket)} à {formatTimecode((busiest + 1) * bucket)}, avec {top} blagues.</li>
        <li>La minute la plus dense : {formatTimecode(densest_minute[0] * 60)}, avec {densest_minute[1]} blagues en 60 secondes.</li>
        <li>Les 5 minutes les plus calmes : de {formatTimecode(calmest * bucket)} à {formatTimecode((calmest + 1) * bucket)}, avec {buckets[calmest]} blague{'s' if buckets[calmest] > 1 else ''}.</li>
        <li>Le plus long moment sans blague : {formatDuration(gap)}, de {formatTimecode(gap_start)} à {formatTimecode(gap_start + gap)}.</li>
        <li>Première blague à {formatTimecode(times[0])}, dernière à {formatTimecode(times[-1])}.</li>
    </ul>"""
    content += genStatSection('Au fil du film', "Nombre de blagues par tranche de 5 minutes.",
                              f'<div class="timeline">{cols}\n    </div>{facts}')

    # Duos
    pairs = collections.Counter()
    for datum in data:
        names = sorted(tag for tag in datum['tags'] if tag not in CATEGORY_TAGS and tag != 'légionnaire')
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                pairs[(a, b)] += 1
    rows = [(f'{html.escape(a)} &amp; {html.escape(b)}', n, f'{a} & {b}') for (a, b), n in pairs.most_common(8)]
    content += genStatSection('Les duos les plus fréquents', "Personnages réunis dans une même blague.",
                              genBars(rows, 'blagues'))

    writePage('stats', 'Statistiques', content,
              "Statistiques sur les blagues d'Astérix et Obélix : Mission Cléopâtre : les plus vues, les personnages "
              "les plus cités, les types de blagues.")


def parseTimecodeMsec(timecode: str) -> float:
    """'hh:mm:ss', or 'hh:mm:ss:ff' to pick a precise frame like in data.csv, to milliseconds"""
    parts = [int(part) for part in timecode.split(':')]
    if len(parts) not in (3, 4) or (len(parts) == 4 and parts[3] >= FPS):
        raise ValueError(f"Invalid timecode: {timecode} (expected hh:mm:ss or hh:mm:ss:ff, frames from 0 to {FPS - 1})")
    hour, minute, second = parts[:3]
    frame = parts[3] if len(parts) == 4 else 0
    return (hour * 3600 + minute * 60 + second + frame / FPS) * 1000


def parseCharacters() -> [dict]:
    if not os.path.exists('personnages.csv'):
        return []
    characters = []
    with open('personnages.csv', 'r', encoding='utf8') as file:
        for row in csv.DictReader(file):
            if not row['Nom'].strip():
                continue
            characters.append({
                'name': row['Nom'].strip(),
                'tag': (row.get('Tag') or row['Nom']).strip(),  # its tag in data.csv, when it differs from the name
                'category': row['Catégorie'].strip(),  # 'personnage' or 'nom cité'
                'actor': row['Acteur'].strip(),
                'role': row['Rôle'].strip(),
                'pun': row['Jeu de mots'].strip(),
                'explanation': row['Explication'].strip(),
                'certainty': row['Certitude'].strip(),  # 'sûr', 'probable' or 'incertain'
                'source': row['Source'].strip(),
            })
            # Photo: a frame of the film ('Image', a timecode) cropped to a square ('Cadrage': x,y,size in
            # pixels of the 1280x544 frame). The file name changes with them, so a new choice is regenerated.
            image = (row.get('Image') or '').strip()
            crop = (row.get('Cadrage') or '').strip()
            characters[-1]['photo'] = ''
            if image:
                key = hashlib.md5(f'{image}|{crop}'.encode()).hexdigest()[:6]
                characters[-1].update({
                    'photo': f'perso_{slugify(characters[-1]["name"])}_{key}',
                    'photo_msec': parseTimecodeMsec(image),
                    'photo_crop': [int(v) for v in crop.split(',')] if crop else None,
                })
    return characters


def genCharacterImages(characters: [dict], videoFilePath: str):
    import cv2
    video_file = cv2.VideoCapture(videoFilePath)

    for c in characters:
        name = c['photo']
        if not name or os.path.exists(f'build/images/{name}.jpg'):
            continue
        video_file.set(cv2.CAP_PROP_POS_MSEC, c['photo_msec'])
        success, image = video_file.read()
        if not success:
            print(f"Error while generating the photo of {c['name']}")
            continue
        height, width = image.shape[:2]
        if c['photo_crop']:
            x, y, size = c['photo_crop']
        else:
            size = height
            x, y = (width - size) // 2, 0
        square = image[max(y, 0):y + size, max(x, 0):x + size]
        cv2.imwrite(f'build/images/{name}.jpg', cv2.resize(square, (160, 160), interpolation=cv2.INTER_AREA))
        print(f"build/images/{name}.jpg generated")


def genCharactersPage(data: [dict], characters: [dict]):
    tag_count = collections.Counter(tag for datum in data for tag in datum['tags'])

    def card(c: dict) -> str:
        name, tag = c['name'], c['tag']
        links = []
        if tag_count[tag]:
            links.append(f'<a href="{tagLink(tag)}">{tag_count[tag]} blague{"s" if tag_count[tag] > 1 else ""}</a>')
        else:
            # Names only said in dialogue: link to the jokes that mention them
            pattern = re.compile(r'\b' + re.escape(name) + r'\b', re.IGNORECASE)
            for datum in data:
                if pattern.search(datum['title'] + ' ' + datum['long_text']):
                    links.append(f'<a href="/quotes/{datum["id"]}.html">{datum["timecode"]}</a>')
            links = links[:3]
        if c['source']:
            links.append(f'<a href="{html.escape(c["source"])}" rel="nofollow">source</a>')
        badge = ''
        if c['certainty'] == 'probable':
            badge = '<span class="perso-badge" title="Explication courante, mais sans source officielle">probable</span>'
        elif c['certainty'] and c['certainty'] != 'sûr':
            badge = '<span class="perso-badge perso-badge-unsure" title="Interprétation, non vérifiée">à vérifier</span>'
        actor = f'<span class="perso-actor">{html.escape(c["actor"])}</span>' if c['actor'] else ''
        role = f'<div class="perso-role">{html.escape(c["role"])}</div>' if c['role'] else ''
        pun = f'<div class="perso-pun">{html.escape(c["pun"])} {badge}</div>' if c['pun'] else ''
        explanation = f'<div class="perso-text">{html.escape(c["explanation"])}</div>' if c['explanation'] else ''
        if c['photo']:
            photo = f'<img class="perso-photo" src="/images/{c["photo"]}.jpg" alt="{html.escape(name)}" loading="lazy">'
        else:
            photo = f'<div class="perso-photo perso-photo-empty" aria-hidden="true">{html.escape(name[0])}</div>'
        return f"""
    <div class="perso" id="{slugify(name)}">
        {photo}
        <div class="perso-body">
            <div class="perso-head"><span class="perso-name">{html.escape(name)}</span>{actor}</div>
            {role}
            {pun}
            {explanation}
            <div class="perso-links">{' · '.join(links)}</div>
        </div>
    </div>"""

    on_screen = [c for c in characters if c['category'] == 'personnage']
    cited = [c for c in characters if c['category'] != 'personnage']
    content = """
<p class="stats-intro">Presque tous les noms du film cachent un jeu de mots. Voici d'où ils viennent.
Les explications marquées « probable » ou « à vérifier » n'ont pas de source officielle.</p>"""
    content += genStatSection('Les personnages', '', f'<div class="persos">{"".join(card(c) for c in on_screen)}\n</div>')
    if cited:
        content += genStatSection('Les noms cités', "Des noms qu'on ne fait qu'entendre dans les dialogues.",
                                  f'<div class="persos">{"".join(card(c) for c in cited)}\n</div>')

    writePage('personnages', 'Personnages', content,
              "Dictionnaire des noms de personnages d'Astérix et Obélix : Mission Cléopâtre et des jeux de mots "
              "qu'ils cachent : Numérobis, Amonbofis, Otis, Itinéris, Caius Céplus…")


def writePage(path: str, title: str, content: str, description: str):
    page = Template(open('front/page_template.html', 'r', encoding="utf8").read()).substitute(
        title=title,
        description=html.escape(description),
        path=path,
        content=content,
        footer=open('front/footer.html', 'r', encoding="utf8").read(),
        style="style.css?r=" + str(time.time()),
    )
    os.makedirs(f'build/{path}', exist_ok=True)
    with open(f'build/{path}/index.html', 'w', encoding="utf8") as file:
        file.write(page)


def genExtraPages(data: [dict], release: bool):
    # Only read the server logs on a release build, not on every --watch tick
    views = updateViews() if release else {}
    if not views and os.path.exists('views.json'):
        with open('views.json', 'r', encoding='utf8') as file:
            views = json.load(file)
    characters = parseCharacters()
    genStatsPage(data, views, characters)
    genCharactersPage(data, characters)


def doTheProcessing(release=False, verbose=True):
    import fcntl
    os.makedirs('build/images', exist_ok=True)
    os.makedirs('build/quotes', exist_ok=True)

    printv = print if verbose else lambda *a, **k: None

    # Each editor save starts a build: they run one after the other, so the last one wins.
    lock = open('build/.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX)

    data = parseData()
    printv("data parsed")

    genSubtitles(data, printv)
    if len(sys.argv) > 1:
        # Before the pages, which link to the clips: a page never points to a clip not encoded yet.
        genClips(data, sys.argv[1])
        printv("clips generated")

    quotes = genQuotes(data)
    printv("quotes generated")

    genIndex(quotes)
    printv("build/index.html generated")

    genQuotesPages(data)
    printv("build/quotes generated")

    genExtraPages(data, release)
    printv("build/stats and build/personnages generated")

    shutil.copytree('front/assets', 'build/assets', dirs_exist_ok=True)
    printv("build/assets copied from front/assets")

    if release:
        compressFront(verbose)

    if len(sys.argv) > 1:
        genImages(data, sys.argv[1])
        printv("images generated")
        genCharacterImages(parseCharacters(), sys.argv[1])
        printv("character photos generated")
    else:
        printv("No video file provided, skipping image generation")

def startDevServer():
    import threading
    from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory="build", **kwargs)

    server = ThreadingHTTPServer(("0.0.0.0", 8000), Handler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.daemon = True
    server_thread.start()


if __name__ == '__main__':
    if any(map(lambda x: x == "--stats", sys.argv)):
        # Daily cron job on the server: add new nginx logs to views.json, then rebuild /stats and /personnages
        genExtraPages(parseData(), release=True)
        sys.exit(0)
    if any(map(lambda x: x == "--serve", sys.argv)):
        startDevServer()
        print("Development server started at http://localhost:8000")
        while True:
            time.sleep(1)
    if any(map(lambda x: x == "--watch", sys.argv)):
        startDevServer()

        while True:
            doTheProcessing(verbose=False)
            time.sleep(1)
    doTheProcessing(release=True)
