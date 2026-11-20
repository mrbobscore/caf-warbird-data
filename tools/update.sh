#!/bin/bash
# Nightly update for the CAF Warbird Tracker's data repository. Run by GitHub Actions (.github/workflows/update.yml):
#
#   bash tools/update.sh https://clever-gumdrop-466aec.netlify.app
#
# What it does, in the repository it runs in:
#   1. refreshes its helper scripts and the fleet list from the tracker site (keeps the repository's copies if the
#      site can't be reached), so a change to the roster or the rules only needs the site redeploying;
#   2. on the very first run, copies the site's history files and season data in as a starting point;
#   3. fetches each day of the last 31 that has no history file yet from adsb.lol's open daily archive (one ~3 GB
#      download per day, streamed; only the fleet's own traces are kept);
#   4. writes history/<day>.json + history/index.json (newest 31 days) and extends season-data.json;
#   5. commits and pushes when something changed.
# The tracker and the Season map read these files straight from the repository, so nothing needs redeploying.
set -u
SITE="${1:-https://clever-gumdrop-466aec.netlify.app}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"; cd "$ROOT"
mkdir -p tools data history

valid() {   # a downloaded file must at least parse before it replaces the repository's copy
  case "$1" in
    *.json) python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$1" ;;
    *.py)   python3 -m py_compile "$1" ;;
    *.sh)   bash -n "$1" ;;
    *)      [ -s "$1" ] ;;
  esac
}
valid_json() { python3 -c "import json,sys; json.load(open(sys.argv[1]))" "$1" 2>/dev/null; }
get() {     # refresh one file from the tracker site, falling back to the copy already in the repository
  if curl -fsSL --retry 2 -m 180 "$SITE/$1" -o "$1.new" && valid "$1.new" 2>/dev/null; then mv "$1.new" "$1"
  else
    rm -f "$1.new"
    if [ -s "$1" ]; then echo "Could not refresh $1 from the site; using the repository's copy."
    else echo "Cannot get $1 from $SITE and the repository has no copy."; return 1; fi
  fi
}
for f in tools/fetch_day.sh tools/history.py tools/process.py data/fleet.json data/airports.json; do get "$f" || exit 1; done
rm -rf tools/__pycache__

if [ ! -s history/index.json ]; then
  echo "First run: copying the history files from the tracker site."
  IDX="$(mktemp)"
  if curl -fsSL -m 60 "$SITE/history/index.json" -o "$IDX" && valid_json "$IDX"; then
    for d in $(python3 -c "import json,sys; print(' '.join(json.load(open(sys.argv[1]))['days']))" "$IDX"); do
      curl -fsSL -m 180 "$SITE/history/$d.json" -o "history/$d.json" && valid "history/$d.json" 2>/dev/null || rm -f "history/$d.json"
    done
  fi
  rm -f "$IDX"
fi
if [ ! -s season-data.json ]; then
  echo "First run: copying the season data from the tracker site."
  curl -fsSL -m 300 "$SITE/season-data.json" -o season-data.json && valid season-data.json 2>/dev/null || rm -f season-data.json
fi
printf 'tools/raw/\ntools/logs/\ntools/names.txt\ntools/days.txt\ntools/season-data.json\n__pycache__/\n*.new\n' > .gitignore

# Days to fetch: any of the last 31 complete UTC days with no history file, or not yet in the season data.
python3 - > tools/days.txt <<'PY'
import datetime as dt, json, os
end = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
try: through = json.load(open('season-data.json'))['through']
except Exception: through = None
d = end - dt.timedelta(days=30)
while d <= end:
    k = d.isoformat()
    if not os.path.exists(f'history/{k}.json') or (through and k > through): print(k)
    d += dt.timedelta(days=1)
PY

cd tools
rm -rf raw logs
if [ -s days.txt ]; then
  echo "Fetching $(wc -l < days.txt | tr -d ' ') day(s): $(tr '\n' ' ' < days.txt)"
  python3 process.py --names
  xargs -P 2 -n 1 bash ./fetch_day.sh < days.txt
  xargs -n 1 bash ./fetch_day.sh < days.txt        # second pass retries a day whose download broke
  cat logs/*.log 2>/dev/null | grep -v '^curl' || true
fi
python3 history.py --out ../history --days 31 || exit 1      # leaves index.json alone when nothing changed
if ls raw/*/.ok >/dev/null 2>&1; then
  if [ -s ../season-data.json ]; then
    python3 process.py --extend ../season-data.json && cp season-data.json ../season-data.json
  else
    echo "No season data to extend (the tracker site had none); history files only."
  fi
fi
cd "$ROOT"
rm -rf tools/raw tools/logs tools/__pycache__

LATEST="$(python3 -c "import json; print(json.load(open('history/index.json')).get('latest') or '')" 2>/dev/null)"
echo "History through ${LATEST:-nothing yet}."
if [ -d .git ]; then
  git config user.name "CAF tracker update"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git add -A
  if git diff --cached --quiet; then echo "Nothing new to save."
  else git commit -q -m "Flight history through ${LATEST:-?}" && git push -q && echo "Saved."; fi
fi
