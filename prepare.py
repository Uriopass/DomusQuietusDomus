import csv
from string import Template
import os
import shutil
import sys


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
        tag = tag.strip()
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
            timecode_parsed = row[0].split(':')  # hh:mm:ss

            if len(timecode_parsed) == 3:
                timecode_seconds: int = int(timecode_parsed[0]) * 3600 + int(timecode_parsed[1]) * 60 + int(
                    timecode_parsed[2])
            else:
                raise ValueError(f"Invalid timecode format: {row[0]} (expected hh:mm:ss or mm:ss)")

            tags = row[3].split(',')

            data.append({
                'timecode': row[0],  # 'hh:mm:ss'
                'timecode_seconds': timecode_seconds,  # 1234
                'title': row[1],  # 'title'
                'long_text': row[2],  # 'long text'
                'tags': tags,  # ['tag1', 'tag2']
                'explanation': row[4]  # 'explanation'
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
            imagefull=image,
            imagepreview=image_preview,
            timecode=datum['timecode'],
            title=prepare_html(datum['title']),
            longtext=genLongText(datum['long_text']),
            tags=genTags(datum['tags']),
            explanation=genExplanation(datum['explanation']),
        )

        quotes += quote
    return quotes


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

    printv = print if verbose else lambda *a, **k: None

    data = parseData()
    printv("data parsed")

    quotes = genQuotes(data)
    printv("quotes generated")

    index = genIndex(quotes)
    printv("build/index.html generated")

    shutil.copytree('front/assets', 'build/assets', dirs_exist_ok=True)
    printv("build/assets copied from front/assets")

    if release:
        compressFront(verbose)

    if len(sys.argv) > 1:
        genImages(data, sys.argv[1])
        printv("images generated")
    else:
        printv("No video file provided, skipping image generation")


if __name__ == '__main__':
    if sys.argv[1] == 'watch':
        import time

        while True:
            doTheProcessing(verbose=False)
            time.sleep(1)
    doTheProcessing(release=True)