#!/usr/bin/env python3
"""Turn the extracted daily traces (raw/<day>/traces/xx/trace_full_<hex>.json, gzip JSON from adsb.lol's
globe_history archives) into one compact season file for the Season map:

  season-data.json = {
    from, through, days, generated,
    aircraft: [{hex, reg, type, nick, unit, cat, cafId, roster}],   # the whole fleet, in fleet.json order
    flights:  [[ai, t0, t1, from, to, nm, maxAlt, estSec, parts, airSec]]   # ai = index into aircraft
                                     # airSec = hours counted (t1 - t0, less any gap time the distance doesn't explain)
  }
  parts = [[kind, encodedPolyline], ...]  kind 0 = received track, 1 = estimated straight leg (no coverage)

How flights are found (same basis as the live tracker, plus coverage-gap handling for season totals):
  * a point is airborne if it isn't flagged "ground" and is >400 ft above that day's lowest altitude or >= 40 kt;
  * airborne points closer than 10 minutes apart form a received segment;
  * when coverage drops out, the flight is joined up with a straight, estimated leg — and its time and distance
    counted — only when both ends are known: the aircraft was clearly in the air (or sitting on the ground at the
    departure/arrival field) on each side, nothing was seen on the ground in between, the gap is under 6 hours, and
    the straight-line speed it implies is believable for that aircraft (60 kt up to 1.25x the fastest it was seen);
  * an estimated leg counts its whole gap as flying time when the distance explains it; when the gap is longer
    (most likely a fuel stop or time parked out of range) only distance / (85% of the aircraft's typical cruise) counts.
"""
import gzip, json, math, os, sys, datetime as dt
from pathlib import Path

HERE = Path(__file__).resolve().parent
RAW = HERE / 'raw'
DATA = next(d for d in (HERE.parent / 'data', HERE.parent / 'caf-warbird-tracker' / 'data') if (d / 'fleet.json').exists())
fleet = json.load(open(DATA / 'fleet.json'))
airports = json.load(open(DATA / 'airports.json'))
BY_HEX = {a['hex']: a for a in fleet}

AIRBORNE_GS = 40
SEG_GAP = 600            # s: points further apart than this start a new received segment
MAX_BRIDGE = 6 * 3600    # s: longest coverage gap we will join with an estimated leg
MIN_BRIDGE_KT = 60
MIN_DUR = 180
NM_PER_RAD = 3440.065

def dist_nm(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * NM_PER_RAD * math.asin(min(1, math.sqrt(h)))

GRID = {}
for ap in airports:
    GRID.setdefault((int(math.floor(ap[2])), int(math.floor(ap[3]))), []).append(ap)
def nearest(lat, lon):
    best, bd = None, 1e9
    gy, gx = int(math.floor(lat)), int(math.floor(lon))
    for r in (1, 2):
        for y in range(gy - r, gy + r + 1):
            for x in range(gx - r, gx + r + 1):
                for ap in GRID.get((y, x), ()):
                    d = dist_nm((lat, lon), (ap[2], ap[3]))
                    if d < bd: best, bd = ap, d
        if best and bd < 60: break
    return best[0] if best else ''

class P:
    __slots__ = ('t', 'lat', 'lon', 'alt', 'gs', 'air', 'up')
    def __init__(s, t, lat, lon, alt, gs, air, up): s.t, s.lat, s.lon, s.alt, s.gs, s.air, s.up = t, lat, lon, alt, gs, air, up
    @property
    def ll(s): return (s.lat, s.lon)

def load_points(path):
    j = json.loads(gzip.open(path).read())
    base = j.get('timestamp', 0)
    raw = [p for p in j.get('trace', []) if p[1] is not None and p[2] is not None]
    nums = [p[3] for p in raw if isinstance(p[3], (int, float))]
    ground = min(nums) if nums else 0
    out = []
    for p in raw:
        alt, gs = p[3], p[4]
        geo = p[10] if len(p) > 10 and isinstance(p[10], (int, float)) else None
        isnum = isinstance(alt, (int, float))
        air = alt != 'ground' and ((isnum and alt > ground + 400) or (isinstance(gs, (int, float)) and gs >= AIRBORNE_GS))
        up = air and ((isnum and alt > ground + 1000) or (isinstance(gs, (int, float)) and gs >= 80))
        out.append(P(base + p[0], p[1], p[2], alt if isnum else geo, gs if isinstance(gs, (int, float)) else None, air, up))
    return out

def build_flights(pts):
    """pts: all observations of one aircraft, sorted by time. Returns flight dicts with 'parts'."""
    pts = [p for i, p in enumerate(pts) if i == 0 or p.t > pts[i - 1].t]
    # drop position glitches: implied speed > 550 kt between points less than two minutes apart
    clean = []
    for p in pts:
        if clean and 0 < p.t - clean[-1].t < 120 and dist_nm(clean[-1].ll, p.ll) / ((p.t - clean[-1].t) / 3600) > 550: continue
        clean.append(p)
    pts = clean
    maxgs = max((p.gs for p in pts if p.air and p.gs), default=0)
    cap = max(120, maxgs * 1.25)
    # typical cruise: median ground speed while well clear of the ground. An estimated leg counts its whole gap
    # only when the straight-line speed it implies is near that; a longer gap (most likely an unheard fuel stop
    # or time parked) counts just the flying time the distance needs at 85% of cruise.
    ups = sorted(p.gs for p in pts if p.up and p.gs)
    cruise = max(70, ups[len(ups) // 2]) if len(ups) >= 20 else max(90, maxgs * 0.8)
    def counted(a, b): return min(b.t - a.t, dist_nm(a.ll, b.ll) / (0.85 * cruise) * 3600)

    # received airborne segments, each with the index range into pts
    segs, cur = [], None
    for i, p in enumerate(pts):
        if not p.air:
            if cur: segs.append(cur); cur = None
            continue
        if cur and p.t - pts[cur[1]].t > SEG_GAP:
            segs.append(cur); cur = None
        if cur is None: cur = [i, i]
        else: cur[1] = i
    if cur: segs.append(cur)

    def plausible(a, b):
        gap = b.t - a.t
        if gap <= 0 or gap > MAX_BRIDGE: return False
        kt = dist_nm(a.ll, b.ll) / (gap / 3600)
        return MIN_BRIDGE_KT <= kt <= cap

    flights, used_ground = [], set()
    si = 0
    while si < len(segs):
        a, b = segs[si]
        parts = [('rx', pts[a:b + 1])]
        est = lost = 0
        start_i, end_i = a, b
        # join following segments across a coverage gap when both sides are clearly in the air
        while si + 1 < len(segs):
            na, nb = segs[si + 1]
            last, nxt = pts[end_i], pts[na]
            between = pts[end_i + 1:na]
            if last.up and nxt.up and not any(not q.air for q in between) and plausible(last, nxt):
                parts.append(('est', [last, nxt])); est += counted(last, nxt); lost += nxt.t - last.t - counted(last, nxt)
                parts.append(('rx', pts[na:nb + 1])); end_i = nb; si += 1
            elif nxt.t - last.t <= SEG_GAP and not between:
                parts[-1] = ('rx', parts[-1][1] + pts[na:nb + 1]); end_i = nb; si += 1
            else:
                break
        # departure: lost on the ground at the field, first heard already in the air somewhere else
        first = pts[start_i]
        if first.up and start_i > 0 and not pts[start_i - 1].air and (start_i - 1) not in used_ground:
            g = pts[start_i - 1]
            if dist_nm(g.ll, first.ll) > 3 and plausible(g, first):
                parts.insert(0, ('est', [g, first])); est += counted(g, first); lost += first.t - g.t - counted(g, first); start_i -= 1
        # arrival: last heard in the air, next heard on the ground at the destination
        last = pts[end_i]
        if last.up and end_i + 1 < len(pts) and not pts[end_i + 1].air:
            g = pts[end_i + 1]
            if dist_nm(last.ll, g.ll) > 3 and plausible(last, g):
                parts.append(('est', [last, g])); est += counted(last, g); lost += g.t - last.t - counted(last, g); end_i += 1; used_ground.add(end_i)
        t0, t1 = pts[start_i].t, pts[end_i].t
        rx_pts = sum(len(pp) for k, pp in parts if k == 'rx')
        if t1 - t0 >= MIN_DUR and (rx_pts >= 5 or est):
            flights.append({'t0': t0, 't1': t1, 'air': t1 - t0 - lost, 'parts': parts, 'est': est, 'from': pts[start_i], 'to': pts[end_i]})
        si += 1

    # ground-to-ground: seen parked at one field, then next seen parked at another far away, nothing in between
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        if a.air or b.air: continue
        d = dist_nm(a.ll, b.ll)
        if d >= 10 and plausible(a, b) and not any(f['t0'] <= a.t <= f['t1'] or f['t0'] <= b.t <= f['t1'] for f in flights):
            flights.append({'t0': a.t, 't1': b.t, 'air': counted(a, b), 'parts': [('est', [a, b])], 'est': counted(a, b), 'from': a, 'to': b})
    flights.sort(key=lambda f: f['t0'])
    return flights

def rdp(points, eps):
    if len(points) < 3: return points
    keep = [False] * len(points); keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        la, lo = points[a]; lb, lob = points[b]
        k = math.cos(math.radians((la + lb) / 2))
        dx, dy = (lob - lo) * k, lb - la
        L = dx * dx + dy * dy
        best, bi = -1, -1
        for i in range(a + 1, b):
            px, py = (points[i][1] - lo) * k, points[i][0] - la
            if L == 0: d = px * px + py * py
            else:
                t = max(0, min(1, (px * dx + py * dy) / L))
                ex, ey = px - t * dx, py - t * dy
                d = ex * ex + ey * ey
            if d > best: best, bi = d, i
        if best > eps * eps:
            keep[bi] = True; stack.append((a, bi)); stack.append((bi, b))
    return [p for p, k in zip(points, keep) if k]

def encode(points, prec=1e4):
    out, plat, plon = [], 0, 0
    for lat, lon in points:
        ilat, ilon = int(round(lat * prec)), int(round(lon * prec))
        for v in (ilat - plat, ilon - plon):
            v = ~(v << 1) if v < 0 else (v << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1f)) + 63)); v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return ''.join(out)

def missing_days(days):
    """days between the first and last archive read that have no complete archive"""
    if not days: return []
    have, out = set(days), []
    d, end = dt.date.fromisoformat(days[0]), dt.date.fromisoformat(days[-1])
    while d <= end:
        if d.isoformat() not in have: out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out

def main(base=None):
    """base: an earlier season-data.json to extend — its flights are kept and only raw days after its last day are
    read, so a refresh only needs the new days' archives."""
    days = sorted(d.name for d in RAW.iterdir() if (d / '.ok').exists()) if RAW.exists() else []
    if base:
        days = [d for d in days if d > base['through']]
        # a day whose archive isn't up yet (or failed to download) leaves a hole: stop just before a recent hole so the
        # next run can fill it in order; a hole more than 3 days old is given up on and listed under "missing"
        today, d, keep = dt.datetime.now(dt.timezone.utc).date(), dt.date.fromisoformat(base['through']) + dt.timedelta(days=1), []
        while days and d.isoformat() <= days[-1]:
            if d.isoformat() in days: keep.append(d.isoformat())
            elif (today - d).days <= 3: break
            d += dt.timedelta(days=1)
        days = keep
    per_hex = {}
    for day in days:
        for f in (RAW / day / 'traces').glob('*/*.json'):
            hexid = f.name[len('trace_full_'):-5]
            if hexid not in BY_HEX: continue
            try: per_hex.setdefault(hexid, []).extend(load_points(f))
            except Exception as e: print('bad trace', f, e, file=sys.stderr)
    aircraft = [{k: a.get(k) for k in ('hex', 'reg', 'type', 'nick', 'unit', 'cat', 'cafId', 'roster')} for a in fleet]
    AI = {a['hex']: i for i, a in enumerate(fleet)}
    flights, flown = [], set()
    stats = {'est_legs': 0, 'est_sec': 0, 'g2g': 0}
    for hexid in per_hex:
        pts = sorted(per_hex[hexid], key=lambda p: p.t)
        fl = build_flights(pts)
        if not fl: continue
        ai = AI[hexid]; flown.add(ai)
        for f in fl:
            nm, max_alt, parts = 0.0, 0, []
            for kind, pp in f['parts']:
                ll = [p.ll for p in pp]
                nm += sum(dist_nm(ll[i - 1], ll[i]) for i in range(1, len(ll)))
                max_alt = max([max_alt] + [int(p.alt) for p in pp if isinstance(p.alt, (int, float)) and p.air])
                parts.append([1 if kind == 'est' else 0, encode(rdp(ll, 0.002) if kind == 'rx' else ll)])
                if kind == 'est': stats['est_legs'] += 1
            if len(f['parts']) == 1 and f['parts'][0][0] == 'est': stats['g2g'] += 1
            stats['est_sec'] += f['est']
            flights.append([ai, int(f['t0']), int(f['t1']), nearest(*f['from'].ll), nearest(*f['to'].ll), round(nm, 1), max_alt, int(f['est']), parts, int(f['air'])])
    all_days, missing = days, missing_days(days)
    if base:
        cut = dt.datetime.fromisoformat(base['through'] + 'T00:00:00+00:00').timestamp() + 86400
        remap = {i: AI.get(a['hex']) for i, a in enumerate(base['aircraft'])}
        for f in base['flights']:
            if f[1] < cut and remap.get(f[0]) is not None: flights.append([remap[f[0]]] + f[1:]); flown.add(remap[f[0]])
        missing = list(base.get('missing', []))
        d = dt.date.fromisoformat(base['through']) + dt.timedelta(days=1)
        while days and d.isoformat() <= days[-1]:
            if d.isoformat() not in days: missing.append(d.isoformat())
            d += dt.timedelta(days=1)
        all_days = [base['from'], days[-1] if days else base['through']]
        n_days = base['days'] + len(days)
    else:
        n_days = len(days)
    flights.sort(key=lambda f: f[1])
    data = {'from': all_days[0] if all_days else None, 'through': all_days[-1] if all_days else None, 'days': n_days,
            'missing': sorted(set(missing)),
            'generated': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'), 'aircraft': aircraft, 'flights': flights}
    Path(HERE / 'season-data.json').write_text(json.dumps(data, separators=(',', ':')))
    hrs = sum(f[9] for f in flights) / 3600
    print(f"{n_days} days, {len(flown)} of {len(aircraft)} aircraft flew, {len(flights)} flights, {hrs:.0f} h, {sum(f[5] for f in flights) * 1.15078:,.0f} mi, "
          f"{os.path.getsize(HERE / 'season-data.json') / 1e6:.2f} MB | estimated legs {stats['est_legs']} "
          f"({stats['est_sec'] / 3600:.1f} h), ground-to-ground {stats['g2g']}")

if __name__ == '__main__':
    if '--names' in sys.argv:   # the list of trace files fetch_day.sh pulls out of each day's archive
        (HERE / 'names.txt').write_text(''.join(f"./traces/{a['hex'][-2:]}/trace_full_{a['hex']}.json\n" for a in fleet))
    elif '--extend' in sys.argv:   # python3 process.py --extend path/to/season-data.json
        main(json.load(open(sys.argv[sys.argv.index('--extend') + 1])))
    else:
        main()
