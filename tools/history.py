#!/usr/bin/env python3
"""Write the tracker's daily history files from the fetched archive traces (raw/<day>/traces/xx/trace_full_<hex>.json).

  python3 history.py [--days 31] [--out ../dist/history]

Day files already in the output folder are kept (only days present in raw/ are rewritten), so it can run every day with just
the newest archive fetched; the folder is trimmed to the newest --days files.

  history/<YYYY-MM-DD>.json = {"day", "generated", "traces": {hex: {"timestamp": t0, "trace": [[dt, lat, lon, alt, gs], …]}}}
  history/index.json        = {"days": [...], "latest": "YYYY-MM-DD", "generated"}

Why: adsb.lol now puts a browser check in front of its per-aircraft history files, so the tracker can't read them through
Netlify any more. These files hold the same traces (from adsb.lol's open daily archive on GitHub), one file per UTC day for
the whole fleet, thinned to a point every ~8 s while moving — the tracker fetches a week in 8 requests instead of ~1,500.
alt is the barometric altitude, "ground", or the GPS altitude when no barometric one was sent; gs is ground speed (kt).
"""
import argparse, datetime as dt, gzip, json, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / 'raw'
DATA = next(d for d in (HERE.parent / 'data', HERE.parent / 'caf-warbird-tracker' / 'data') if (d / 'fleet.json').exists())
FLEET = {a['hex'] for a in json.load(open(DATA / 'fleet.json'))}
STEP = 8        # s: keep at most one point per this many seconds while moving
PARKED = 300    # s: and one per this many while sitting still on the ground


def read_trace(path):
    raw = path.read_bytes()
    return json.loads(gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw)


def thin(j):
    base = j.get('timestamp', 0)
    out, last_t, last_state = [], None, None
    pts = [p for p in j.get('trace', []) if p[1] is not None and p[2] is not None]
    for i, p in enumerate(pts):
        alt = p[3]
        if not isinstance(alt, (int, float)) and alt != 'ground':
            alt = p[10] if len(p) > 10 and isinstance(p[10], (int, float)) else None
        gs = p[4] if isinstance(p[4], (int, float)) else None
        still = alt == 'ground' and (gs is None or gs < 5)
        state = ('g' if alt == 'ground' else 'a')
        keep = (last_t is None or i == len(pts) - 1 or state != last_state
                or p[0] - last_t >= (PARKED if still else STEP))
        if keep:
            out.append([round(p[0], 1), round(p[1], 5), round(p[2], 5),
                        alt if alt == 'ground' or alt is None else int(alt), None if gs is None else round(gs, 1)])
            last_t, last_state = p[0], state
    return {'timestamp': base, 'trace': out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--days', type=int, default=31)
    ap.add_argument('--out', default=str(HERE.parent / 'dist' / 'history'))
    a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    # every archive day fetched into raw/ is (re)written; day files already in the folder are kept, so a daily run only
    # needs the new day's archive. Then the folder is trimmed to the newest --days files and the index rebuilt from it.
    days = sorted(d.name for d in RAW.iterdir() if (d / '.ok').exists()) if RAW.exists() else []
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    for day in days:
        traces = {}
        for f in sorted((RAW / day / 'traces').glob('*/*.json')):
            hexid = f.name[len('trace_full_'):-5]
            if hexid not in FLEET: continue
            try: t = thin(read_trace(f))
            except Exception as e: print('bad trace', f, e, file=sys.stderr); continue
            if t['trace']: traces[hexid] = t
        (out / f'{day}.json').write_text(json.dumps({'day': day, 'generated': now, 'traces': traces}, separators=(',', ':')))
    have = sorted(f.stem for f in out.glob('20??-??-??.json'))
    for old in have[:-a.days]: (out / f'{old}.json').unlink()
    have = have[-a.days:]
    try: unchanged = not days and json.loads((out / 'index.json').read_text()).get('days') == have
    except Exception: unchanged = False
    if not unchanged:       # leave the index alone when nothing changed, so a run with no new archive changes no file
        (out / 'index.json').write_text(json.dumps({'days': have, 'latest': have[-1] if have else None, 'generated': now}))
    total = sum((out / f'{d}.json').stat().st_size for d in have)
    print(f"{len(days)} day file(s) written; {len(have)} days in the folder ({have[0] if have else '-'} to {have[-1] if have else '-'}), {total / 1e6:.1f} MB -> {out}")


if __name__ == '__main__':
    main()
