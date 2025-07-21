#!/usr/bin/env bash
git pull --rebase
source venv/bin/activate
python3 -m pip install -r requirements.txt
python3 prepare.py movie.mkv
deactivate
