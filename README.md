# Domus Quietus Domus

A website exhaustively listing all jokes from "Asterix & Obelix: Mission Cleopatra (2002)".

### Features

- [x] List all jokes from the movie
- [x] Textual search for jokes + tags
- [x] Timecode of jokes visualizer
- [x] Each joke's clip, with word-by-word subtitles, as a video or a GIF

### Clip subtitles

The subtitles' text comes from the DVD subtitles (`subtitles.json`), fixed in `corrections_sous_titres.csv`,
and the jokes' quotes in `data.csv`; each word is timed on the audio (`tools/`). The audio model's output is
computed once, on a machine with torch, and placed next to `movie.mkv`; `prepare.py` does the rest:

    python3 tools/align_words.py --emissions audio.ogg emissions.npz

## Copyright

I do not own the rights to the movie "Asterix & Obelix: Mission Cleopatra (2002)".
Copyrights belong to the respective owners.
All the code and assets are under the AGPL-3.0 license as specified in the LICENSE file except for the images in the front/assets folder which are not .