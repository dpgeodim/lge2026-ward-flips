#!/usr/bin/env python3
"""
LGE 2026 ward-flip poller.

Reads the IEC live results feed and writes compact JSON for the tracker page:

  out/data.json      summary: progress, councils, declared wards, early-warning wards
  out/m/<id>.json    per-municipality ward detail (vote-share swing vs 2021)
  out/baseline.json  cached 2021 results per municipality (built lazily, reused)
  out/map.json       simplified municipality outlines (built once)

Usage: python3 poll.py <out_dir> [--base URL] [--full] [--no-map]
Only Python 3.8+ standard library is needed.
"""
import argparse, gzip, json, os, sys, threading, time, urllib.request, urllib.error, zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE = "https://results.elections.org.za/dashboards/lge/MapsJason"
MAP_URL = "https://results.elections.org.za/ElectionMap/MapData/2026/nationalMunic.js"
E26, E21 = "EEID1887", "EEID1091"
METROS = ("JHB ", "TSH ", "EKU ", "ETH ", "CPT ", "NMA ", "BUF ", "MAN ")
SAST = timezone(timedelta(hours=2))


def now_sast():
    return datetime.now(SAST).strftime("%Y-%m-%d %H:%M")


def fetch(url, timeout=90):
    req = urllib.request.Request(url, headers={"User-Agent": "WardFlipTracker/2.0 (+github pages)",
                                               "Accept-Encoding": "gzip, deflate", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(); enc = (r.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip": raw = gzip.decompress(raw)
    elif enc == "deflate": raw = zlib.decompress(raw)
    return raw.decode("utf-8-sig")


def fetch_json(url):
    return json.loads(fetch(url).strip())


def bust(url):
    return f"{url}?r={int(time.time())}"


def short(name):
    return name.split(" - ", 1)[1] if " - " in (name or "") else (name or "")


def r1(x):
    return None if x is None else round(x, 1)


# ---------------------------------------------------------------- baseline (2021)
def build_baseline(base, mid):
    try:
        j = fetch_json(f"{base}/2021/{E21}Munic{mid}.js")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"ok": False, "wards": {}, "seats": {}, "avail": 0, "cast": 0, "reg": 0}
        raise
    b = {"ok": True, "wards": {}, "seats": {}, "avail": 0, "cast": 0, "reg": 0}
    for w in j.get("WardResults", []):
        ps = w.get("PartyBallotResults", [])
        win = next((p for p in ps if (p.get("WardSeatsWon") or 0) > 0 or (p.get("TotalSeatsWon") or 0) > 0), None) \
            or next((p for p in ps if p.get("IsLeadingPartyVotes")), None)
        if win:
            b["wards"][str(w["WardID"])] = {
                "id": win["ID"], "c": (win.get("CandidateName") or "-").strip(), "pc": win.get("PercOfVotes"),
                "p": {str(p["ID"]): p["PercOfVotes"] for p in ps if p.get("PercOfVotes")}}
    for p in j.get("PartyBallotResults", []):
        if p.get("TotalSeatsWon"): b["seats"][str(p["ID"])] = p["TotalSeatsWon"]
        b["avail"] = max(b["avail"], p.get("AvailableSeats") or 0)
        b["cast"] = p.get("TotalVotesCast") or b["cast"]; b["reg"] = p.get("RegisteredVoters") or b["reg"]
    return b


# ---------------------------------------------------------------- one municipality
LOCK = threading.Lock()


def process(mid, j, b, natm, parties, prev_declared, declared, early, details, now, odd):
    with LOCK:
        for k in [k for k, v in declared.items() if v["mid"] == mid]: del declared[k]
    name = j.get("Municipality") or natm.get("name") or str(mid)
    metro = name.startswith(METROS)
    pb = j.get("PartyBallotResults", [])
    for p in pb:
        parties.setdefault(str(p["ID"]), p.get("Name"))
    plist = [{"id": p["ID"], "pc": p.get("PercOfVotes") or 0, "v": p.get("TotalValidVotes") or 0, "s": p.get("TotalSeatsWon") or 0} for p in pb]
    plist.sort(key=lambda p: (-p["s"], -p["v"]))
    avail = max([p.get("AvailableSeats") or 0 for p in pb] + [j.get("SeatsExpected") or 0])
    cast = (pb[0].get("TotalVotesCast") or 0) if pb else 0
    reg = (pb[0].get("RegisteredVoters") or 0) if pb else (j.get("RegPop") or 0)
    need = avail // 2 + 1
    sd, se = j.get("SeatsComplete") or 0, j.get("SeatsExpected") or 0
    vd, ve = j.get("VDsComplete") or 0, j.get("VDExpected") or 0
    top = plist[0] if plist else None
    if se > 0 and sd >= se:
        status = "maj" if top and top["s"] >= need else "hung"
    else:
        status = "count" if vd > 0 else "wait"
    vote_lead = max(plist, key=lambda p: p["v"]) if plist else None
    ballots = 2 if metro else 3
    turn = turn21 = None
    if ve and vd >= ve and reg:
        turn = r1(100 * cast / reg / ballots)
        if b.get("reg"): turn21 = r1(100 * b["cast"] / b["reg"] / ballots)
    m = {"id": mid, "name": short(name), "prov": j.get("Province") or natm.get("prov") or "", "metro": metro,
         "status": status, "wd": j.get("WardsComplete") or 0, "we": j.get("WardExpected") or 0, "vd": vd, "ve": ve,
         "sd": sd, "se": se, "avail": avail, "need": need,
         "parties": [{"id": p["id"], "s": p["s"], "pc": r1(p["pc"]), "v": p["v"]} for p in plist if p["s"] > 0 or p["pc"] >= 1][:12],
         "seats21": b.get("seats", {}), "turn": turn, "turn21": turn21,
         "top": top["id"] if top else None, "lead": {"id": vote_lead["id"], "pc": r1(vote_lead["pc"])} if vote_lead else None,
         "flips": 0, "early": 0, "ok21": b.get("ok", False)}
    wards = []
    for w in j.get("WardResults", []):
        wid = str(w["WardID"]); no = w["WardID"] % 1000
        ps = [p for p in w.get("PartyBallotResults", []) if (p.get("TotalValidVotes") or 0) > 0]
        for p in ps: parties.setdefault(str(p["ID"]), p.get("Name"))
        wv = lambda p: (p.get("WardVotes") if p.get("WardVotes") is not None else p.get("TotalValidVotes")) or 0
        ps.sort(key=lambda p: (-wv(p), -(p.get("TotalValidVotes") or 0)))
        wtot = sum(wv(p) for p in ps)
        wpc = lambda p: (100 * wv(p) / wtot) if wtot else 0
        vdl = w.get("VotingDistrictResults", [])
        cnt = sum(1 for v in vdl if any((p.get("TotalValidVotes") or 0) > 0 for p in v.get("PartyBallotResults", [])))
        winner = next((p for p in w.get("PartyBallotResults", []) if (p.get("WardSeatsWon") or 0) > 0), None)
        conflict = bool(winner and (not ps or ps[0]["ID"] != winner["ID"]) and cnt < len(vdl))
        if conflict:
            with LOCK:
                odd.append({"w": wid, "no": no, "mid": mid, "flag": winner["ID"], "lead": ps[0]["ID"] if ps else None, "cnt": cnt, "tot": len(vdl)})
            winner = None
        lead = winner or (ps[0] if ps else None)
        second = next((p for p in ps if lead and p["ID"] != lead["ID"]), None)
        pv = b.get("wards", {}).get(wid)
        rec = {"w": wid, "no": no, "st": "dec" if winner else ("count" if ps else "wait"),
               "lead": lead["ID"] if lead else None, "pc": r1(wpc(lead)) if lead else None,
               "cpc": r1(lead.get("PercOfVotes")) if lead else None,
               "c": (lead.get("CandidateName") or "-").strip() if lead else "",
               "mg": r1(wpc(lead) - wpc(second)) if (lead and second) else (r1(wpc(lead)) if lead else None),
               "cnt": cnt, "tot": len(vdl),
               "prev": pv["id"] if pv else None, "ppc": r1(pv["pc"]) if pv else None, "pc_": pv["c"] if pv else None,
               "p": [{"id": p["ID"], "pc": r1(p.get("PercOfVotes")), "pc21": (r1(pv["p"].get(str(p["ID"]), 0)) if pv else None)} for p in ps[:6]]}
        if not b.get("ok"): rec["ch"] = "unk"
        elif pv is None: rec["ch"] = "new"
        elif pv["id"] != rec["lead"] or (rec["lead"] == 0 and pv["c"].upper() != rec["c"].upper()): rec["ch"] = "flip"
        else: rec["ch"] = "hold"
        wards.append(rec)
        if rec["st"] == "dec":
            t = prev_declared.get(wid, {}).get("t") or now
            d = {"w": wid, "no": no, "mid": mid, "k": rec["ch"], "o": rec["prev"], "n": rec["lead"], "c": rec["c"],
                 "pc": rec["pc"], "cpc": rec["cpc"], "opc": rec["ppc"], "mg": rec["mg"], "t": t,
                 "seq": prev_declared.get(wid, {}).get("seq") or 0}
            with LOCK: declared[wid] = d
            if rec["ch"] == "flip": m["flips"] += 1
        elif rec["st"] == "count" and rec["ch"] == "flip":
            with LOCK: early.append({"w": wid, "no": no, "mid": mid, "prev": rec["prev"], "lead": rec["lead"], "pc": rec["pc"],
                                     "mg": rec["mg"], "cnt": cnt, "tot": len(vdl)})
            m["early"] += 1
    details[mid] = {"id": mid, "name": m["name"], "prov": m["prov"], "updated": now, "wards": wards}
    return m


# ---------------------------------------------------------------- map simplification
def dp(points, eps):
    """Douglas–Peucker on a list of (x, y)."""
    if len(points) < 3: return points
    (x1, y1), (x2, y2) = points[0], points[-1]
    dx, dy = x2 - x1, y2 - y1; L = (dx * dx + dy * dy) ** .5 or 1e-12
    imax, dmax = 0, 0
    for i in range(1, len(points) - 1):
        x, y = points[i]
        d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / L
        if d > dmax: imax, dmax = i, d
    if dmax > eps:
        return dp(points[:imax + 1], eps)[:-1] + dp(points[imax:], eps)
    return [points[0], points[-1]]


def dp_ring(ring, eps):
    """DP for a closed ring: split at the point farthest from the start, simplify both halves."""
    if len(ring) < 4: return ring
    x0, y0 = ring[0]
    k = max(range(1, len(ring) - 1), key=lambda i: (ring[i][0] - x0) ** 2 + (ring[i][1] - y0) ** 2)
    return dp(ring[:k + 1], eps)[:-1] + dp(ring[k:], eps)


def build_map(out):
    path = os.path.join(out, "map.json")
    if os.path.exists(path): return
    try:
        txt = fetch(MAP_URL, timeout=180).strip()
        geo = json.loads(txt[txt.index("{"):])
    except Exception as e:
        print("map: not built:", e); return
    feats = []
    for f in geo.get("features", []):
        pr = f.get("properties", {}); g = f.get("geometry") or {}
        polys = [g["coordinates"]] if g.get("type") == "Polygon" else (g.get("coordinates", []) if g.get("type") == "MultiPolygon" else [])
        rings = []
        for poly in polys:
            if not poly: continue
            ring = dp_ring([tuple(p[:2]) for p in poly[0]], 0.012)
            if len(ring) >= 4: rings.append([[round(x, 3), round(y, 3)] for x, y in ring])
        if rings:
            feats.append({"id": pr.get("MunicId"), "n": short(pr.get("Municipali") or ""), "r": rings})
    with open(path, "w") as fh: json.dump({"f": feats}, fh, separators=(",", ":"))
    print(f"map: {len(feats)} municipalities, {os.path.getsize(path) // 1024} KB")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(); ap.add_argument("out"); ap.add_argument("--base", default=BASE)
    ap.add_argument("--full", action="store_true", help="re-fetch every municipality"); ap.add_argument("--no-map", action="store_true")
    a = ap.parse_args(); out = a.out; base = a.base.rstrip("/")
    os.makedirs(os.path.join(out, "m"), exist_ok=True)
    now = now_sast()

    def load(name, default):
        p = os.path.join(out, name)
        try:
            with open(p, encoding="utf-8") as fh: return json.load(fh)
        except Exception: return default
    prev = load("data.json", {})
    prev_declared = {d["w"]: d for d in prev.get("declared", [])}
    prev_munis = {m["id"]: m for m in prev.get("munis", [])}
    baseline = load("baseline.json", {})
    parties = dict(prev.get("parties", {}))
    for yr in ("2021", "2026"):
        try:
            for p in fetch_json(bust(f"{base}/{yr}/PartyList.js")):
                parties[str(p["ID"])] = {"n": p.get("Name") or p.get("PartyFullName"), "c": p.get("sWebColour") or "#9aa5b1"}
        except Exception as e: print("party list", yr, e)

    nat = fetch_json(bust(f"{base}/2026/{E26}LiteMapsNational.js"))
    prog = {"wd": nat.get("WardsComplete"), "we": nat.get("WardExpected"), "vd": nat.get("VDsComplete"), "ve": nat.get("VDExpected"),
            "sd": nat.get("SeatsComplete"), "se": nat.get("SeatsExpected"), "iec": nat.get("ReportDate")}
    natm, todo = {}, []
    for pr in nat.get("ProvinceResults", []):
        for x in pr.get("MunicipalityResults", []):
            mid = x["MunicipalityID"]
            natm[mid] = {"name": x.get("Municipality"), "prov": x.get("Province") or pr.get("Province"),
                         "wd": x.get("WardsComplete") or 0, "we": x.get("WardExpected") or 0, "vd": x.get("VDsComplete") or 0,
                         "ve": x.get("VDExpected") or 0, "sd": x.get("SeatsComplete") or 0}
            pm = prev_munis.get(mid)
            if a.full or not pm or (pm["wd"], pm["vd"], pm["sd"]) != (natm[mid]["wd"], natm[mid]["vd"], natm[mid]["sd"]) or (natm[mid]["vd"] > 0 and not pm.get("ok21")):
                todo.append(mid)
    print(f"{len(natm)} municipalities, {len(todo)} to refresh")

    declared = dict(prev_declared); early = []; odd = []; details = {}; munis = {}; errors = []
    keep_odd = [e for e in prev.get("odd", []) if e["mid"] not in todo and e["mid"] in natm]
    # keep early-warning entries for municipalities we are not refreshing
    keep_early = [e for e in prev.get("early", []) if e["mid"] not in todo and e["mid"] in natm]

    def work(mid):
        try:
            if str(mid) not in baseline:
                baseline[str(mid)] = build_baseline(base, mid)
            j = fetch_json(bust(f"{base}/2026/{E26}Munic{mid}.js"))
            munis[mid] = process(mid, j, baseline[str(mid)], natm[mid], parties, prev_declared, declared, early, details, now, odd)
        except Exception as e:
            errors.append(f"{mid}: {e}")
    with ThreadPoolExecutor(max_workers=6) as ex: list(ex.map(work, todo))

    # municipalities not refreshed this run: carry over previous summary; those never seen: placeholder
    for mid, nm in natm.items():
        if mid in munis: continue
        pm = prev_munis.get(mid)
        if pm: munis[mid] = pm
        else: munis[mid] = {"id": mid, "name": short(nm["name"]), "prov": nm["prov"], "metro": (nm["name"] or "").startswith(METROS),
                            "status": "count" if nm["vd"] else "wait", "wd": nm["wd"], "we": nm["we"], "vd": nm["vd"], "ve": nm["ve"],
                            "sd": nm["sd"], "se": 0, "avail": 0, "need": 0, "parties": [], "seats21": {}, "turn": None, "turn21": None,
                            "top": None, "lead": None, "flips": 0, "early": 0, "ok21": False}
    # sequence numbers so the page can sort by declaration order
    seq = max([d.get("seq") or 0 for d in declared.values()] + [0])
    for d in sorted(declared.values(), key=lambda d: (d.get("seq") or 0, d["w"])):
        if not d.get("seq"):
            seq += 1; d["seq"] = seq
    data = {"generated": now, "ok": not errors, "errors": errors[:5], "prog": prog,
            "parties": {k: (v if isinstance(v, dict) else {"n": v, "c": "#9aa5b1"}) for k, v in parties.items() if v},
            "odd": odd + keep_odd,
            "munis": sorted(munis.values(), key=lambda m: m["name"]),
            "declared": sorted(declared.values(), key=lambda d: -d["seq"]),
            "early": sorted(early + keep_early, key=lambda e: -(e["cnt"] / (e["tot"] or 1)))}
    tmp = os.path.join(out, "data.json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh: json.dump(data, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, os.path.join(out, "data.json"))
    for mid, d in details.items():
        with open(os.path.join(out, "m", f"{mid}.json"), "w", encoding="utf-8") as fh: json.dump(d, fh, separators=(",", ":"), ensure_ascii=False)
    with open(os.path.join(out, "baseline.json"), "w", encoding="utf-8") as fh: json.dump(baseline, fh, separators=(",", ":"), ensure_ascii=False)
    if not a.no_map: build_map(out)
    flips = sum(1 for d in declared.values() if d["k"] == "flip")
    print(f"{now}: declared {len(declared)} (flips {flips}), early {len(data['early'])}, errors {len(errors)}")
    for e in errors[:5]: print("  ", e)


if __name__ == "__main__":
    main()
