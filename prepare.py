import csv
from string import Template
import os
import shutil
import sys
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


def parseData() -> [dict]:
    data = []
    with open('data.csv', 'r', encoding="utf8") as file:
        reader = csv.reader(file)

        next(reader)  # skip headers

        for row in reader:
            if row[1] == '':
                continue
            timecode_parsed = row[1].split(':')  # hh:mm:ss

            if len(timecode_parsed) == 3:
                hour, minute, second = map(int, timecode_parsed)
                timecode_seconds: int = hour * 3600 + minute * 60 + second
            else:
                raise ValueError(f"Invalid timecode format: {row[1]} (expected hh:mm:ss or mm:ss)")

            tags = [tag.strip() for tag in row[4].split(',') if tag.strip() != '']

            data.append({
                'id': row[0],  # ''
                'timecode': row[1],  # 'hh:mm:ss'
                'timecode_seconds': timecode_seconds,  # 1234
                'title': row[2],  # 'title'
                'long_text': row[3],  # 'long text'
                'tags': tags,  # ['tag1', 'tag2']
                'explanation': row[5],  # 'explanation',
                'interesting': row[6]  # 'TRUE/FALSE'
            })

    return data


def genQuotes(data: [dict]):
    quote_template = Template(open('front/quote_template.html', 'r', encoding="utf8").read())

    quotes: str = ""
    for datum in data:
        timecode_seconds = datum['timecode_seconds']
        image = f'images/{timecode_seconds}.jpg'
        image_preview = f'images/{timecode_seconds}_preview.jpg'

        quote = quote_template.substitute(
            id=datum['id'],
            imagefull=image,
            imagepreview=image_preview,
            timecode=datum['timecode'],
            timecode_seconds=timecode_seconds,
            title=prepare_html(datum['title']),
            longtext=genLongText(datum['long_text']),
            tags=genTags(datum['tags']),
            explanation=genExplanation(datum['explanation']),
            interesting=datum['interesting'],
        )

        quotes += quote
    return quotes


def genQuotesPages(data: [dict]):
    quote_template = Template(open('front/quote_page_template.html', 'r', encoding="utf8").read())
    footer = open('front/footer.html', 'r', encoding="utf8").read()

    for datum in data:
        id = datum['id']
        timecode_seconds = datum['timecode_seconds']
        image = f'images/{timecode_seconds}.jpg'
        image_preview = f'images/{timecode_seconds}_preview.jpg'

        quote = quote_template.substitute(
            id=id,
            imagefull=image,
            imagepreview=image_preview,
            timecode=datum['timecode'],
            title=prepare_html(datum['title']),
            metatitle=datum['title'].replace('\n', ' ').strip(),
            longtext=genLongText(datum['long_text']),
            tags=genTags(datum['tags']),
            explanation=genExplanation(datum['explanation']),
            footer=footer,
            style="style.css?r=" + str(time.time())
        )

        with open(f'build/quotes/{id}.html', 'w', encoding="utf8") as quote_file:
            quote_file.write(quote)


def genImages(data: [dict], videoFilePath: str):
    import cv2
    video_file = cv2.VideoCapture(videoFilePath)

    for datum in data:
        timecode_seconds = datum['timecode_seconds']
        if os.path.exists(f'build/images/{timecode_seconds}.jpg'):
            continue
        video_file.set(cv2.CAP_PROP_POS_MSEC, timecode_seconds * 1000)
        success, image = video_file.read()
        if success:
            cv2.imwrite(f'build/images/{timecode_seconds}.jpg', image)
            cv2.imwrite(f'build/images/{timecode_seconds}_preview.jpg', cv2.resize(image, (1280 // 8, 544 // 8)))
            print(f"build/images/{timecode_seconds}.jpg generated")
        else:
            print(f"Error while generating image for {timecode_seconds}")


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


def doTheProcessing(release=False, verbose=True):
    os.makedirs('build/images', exist_ok=True)
    os.makedirs('build/quotes', exist_ok=True)

    printv = print if verbose else lambda *a, **k: None

    data = parseData()
    printv("data parsed")

    quotes = genQuotes(data)
    printv("quotes generated")

    genIndex(quotes)
    printv("build/index.html generated")

    genQuotesPages(data)
    printv("build/quotes generated")

    shutil.copytree('front/assets', 'build/assets', dirs_exist_ok=True)
    printv("build/assets copied from front/assets")

    if release:
        compressFront(verbose)

    if len(sys.argv) > 1:
        genImages(data, sys.argv[1])
        printv("images generated")
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
