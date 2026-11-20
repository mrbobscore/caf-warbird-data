#!/bin/bash
# Pull one day of CAF fleet traces out of adsb.lol's daily archive on GitHub (globe_history_2026 releases).
# usage: ./fetch_day.sh 2026-10-01   -> raw/2026-10-01/traces/xx/trace_full_<hex>.json (+ .ok marker when complete)
# Each day's archive is about 3 GB; it is streamed and only the fleet's ~200 trace files are kept (a few MB).
# Run several days at once with:  xargs -P 3 -n 1 bash ./fetch_day.sh < days.txt
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
DAY="$1"; TAG="v${DAY//-/.}-planes-readsb-prod-0"
BASE="https://github.com/adsblol/globe_history_${DAY:0:4}/releases/download/$TAG/$TAG.tar"
OUT="$HERE/raw/$DAY"; LOG="$HERE/logs/$DAY.log"
mkdir -p "$OUT" "$HERE/logs"
[ -f "$OUT/.ok" ] && exit 0
[ -f "$HERE/names.txt" ] || python3 "$HERE/process.py" --names
rm -f "$LOG" "$LOG.tar"
parts=()
for s in aa ab ac ad ae af; do
  code=$(curl -sS -m 30 -o /dev/null -w "%{http_code}" -r 0-0 -L "$BASE.$s")
  [ "$code" = "206" ] || [ "$code" = "200" ] || break
  parts+=("$s")
done
if [ ${#parts[@]} -eq 0 ]; then echo "$DAY no-archive" > "$LOG"; touch "$OUT/.missing"; exit 0; fi
start=$(date +%s)
( for s in "${parts[@]}"; do curl -sSfL --retry 3 --retry-delay 5 "$BASE.$s" || { echo "curl-fail $s" >&2; exit 9; }; done ) 2>>"$LOG" \
  | tar -x -C "$OUT" -f - -T "$HERE/names.txt" 2>>"$LOG.tar"
n=$(find "$OUT/traces" -name '*.json' 2>/dev/null | wc -l)
if grep -q "curl-fail" "$LOG" 2>/dev/null || grep -qi "unexpected EOF\|short read" "$LOG.tar" 2>/dev/null; then
  echo "$DAY FAILED parts=${parts[*]} found=$n secs=$(( $(date +%s) - start ))" >> "$LOG"; rm -rf "$OUT"; exit 1
fi
rm -f "$OUT/.missing"
echo "$DAY ok parts=${parts[*]} found=$n secs=$(( $(date +%s) - start ))" >> "$LOG"
touch "$OUT/.ok"
