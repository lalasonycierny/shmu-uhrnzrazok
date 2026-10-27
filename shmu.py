#!/usr/bin/env python3
"""SHMÚ – úhrn zrážok: zber (hodinový), štatistiky a mapy.

Príkazy:
  python shmu.py collect   stiahne stránku, uloží hodinový snímok a prepočíta denný súhrn
  python shmu.py geocode   doplní súradnice staníc (Nominatim), max --limit staníc za beh
  python shmu.py build     vytvorí web (site/): štatistiky + mapy deň/týždeň/mesiac/rok
  python shmu.py rebuild   prepočíta všetky denné súhrny z hodinových snímok
  python shmu.py run       collect + geocode + build (to volá GitHub Actions)

Dáta (hodinové súbory sú v UTC):
  data/raw/YYYY/MM/DD/HH.csv     station_id,p1,p3,p6,p12,p24
  data/daily/YYYY/YYYY-MM-DD.csv denné súhrny po staniciach (lokálny čas Europe/Bratislava)
  data/stations.csv              station_id,name,type,basin,lat,lon,geo_tried
"""
from __future__ import annotations

import argparse
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
from bs4 import BeautifulSoup

URL = "https://www.shmu.sk/sk/?page=838"
UA = "shmu-uhrnzrazok/1.0 (github.com/lalasonycierny/shmu-uhrnzrazok)"
TZ = "Europe/Bratislava"

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "data" / "raw"
DAILY = ROOT / "data" / "daily"
STATIONS = ROOT / "data" / "stations.csv"
SITE = ROOT / "site"

COLS = ["p1", "p3", "p6", "p12", "p24"]
ST_COLS = ["station_id", "name", "type", "basin", "lat", "lon", "geo_tried"]

# Farebné triedy (9 tried ako v legende SHMÚ) – hranice podľa obdobia, mm
BINS = {
    "day": [0, 1, 5, 10, 20, 35, 50, 70, 90],
    "week": [0, 5, 10, 20, 40, 70, 100, 150, 200],
    "month": [0, 10, 25, 50, 80, 120, 170, 230, 300],
    "year": [0, 300, 500, 650, 800, 1000, 1250, 1500, 2000],
}
COLORS = ["#f2f2f2", "#c7e9c0", "#74c476", "#41b6c4", "#1d91c0",
          "#225ea8", "#6a3d9a", "#cb181d", "#7f0000"]
KIND_SK = {"day": "Deň", "week": "Týždeň", "month": "Mesiac", "year": "Rok"}


# ───────────────────────── sťahovanie a parsovanie ─────────────────────────

def fetch_html(retries: int = 3) -> bytes:
    for i in range(retries):
        try:
            r = requests.get(URL, headers={"User-Agent": UA}, timeout=60)
            r.raise_for_status()
            return r.content
        except requests.RequestException as e:
            print(f"[fetch] pokus {i + 1} zlyhal: {e}", file=sys.stderr)
            time.sleep(5 * (i + 1))
    raise SystemExit("Stránku SHMÚ sa nepodarilo stiahnuť.")


def _num(txt: str) -> float:
    try:
        return float(txt.strip().replace(",", "."))
    except ValueError:
        return float("nan")


def local_to_utc(naive: pd.Timestamp, now: pd.Timestamp) -> pd.Timestamp:
    """Lokálny čas zo stránky -> UTC. Pri nejednoznačnej hodine (zmena času)
    sa vyberie variant, ktorý je najbližšie pred 'teraz'."""
    cands = set()
    for amb in (True, False):
        try:
            cands.add(naive.tz_localize(TZ, ambiguous=amb, nonexistent="shift_forward")
                      .tz_convert("UTC"))
        except Exception:
            pass
    ok = [c for c in cands if c <= now + pd.Timedelta(minutes=5)]
    return max(ok) if ok else min(cands)


def parse(html: bytes, now_utc: pd.Timestamp | None = None) -> pd.DataFrame:
    now_utc = now_utc or pd.Timestamp.now(tz="UTC")
    soup = BeautifulSoup(html, "lxml")
    rows, ts_cache = [], {}
    for table in soup.find_all("table"):
        # názov povodia je na stránke uvedený pod tabuľkou
        h = table.find_next(string=re.compile(r"^\s*Povodie\s"))
        basin = re.sub(r"^\s*Povodie\s+", "", h).strip() if h else ""
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 8:
                continue
            a = tds[0].find("a", href=True)
            m = re.search(r"stationId=([A-Za-z0-9]+)", a["href"]) if a else None
            if not m:
                continue  # riadok "Priemery"
            tstr = tds[2].get_text(strip=True)
            if tstr not in ts_cache:
                try:
                    naive = pd.to_datetime(tstr, format="%d.%m.%Y %H:%M")
                    ts_cache[tstr] = local_to_utc(naive, now_utc)
                except ValueError:
                    ts_cache[tstr] = pd.NaT
            rows.append({
                "station_id": m.group(1),
                "name": a.get_text(strip=True),
                "type": tds[1].get_text(strip=True),
                "basin": basin,
                "ts": ts_cache[tstr],
                **{c: _num(td.get_text()) for c, td in zip(COLS, tds[3:8])},
            })
    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("Parser nenašiel žiadne stanice – zmenila sa štruktúra stránky?")
    return df.dropna(subset=["ts"]).drop_duplicates("station_id")


# ───────────────────────────── ukladanie ─────────────────────────────

def raw_path(h: pd.Timestamp) -> Path:
    return RAW / f"{h:%Y/%m/%d/%H}.csv"


def save_raw(df: pd.DataFrame) -> pd.Timestamp:
    hour = df["ts"].mode().iloc[0].floor("h")
    # stanice so starším časom merania by sa pripísali dvakrát -> vynechať
    cur = df[df["ts"].dt.floor("h") == hour]
    p = raw_path(hour)
    p.parent.mkdir(parents=True, exist_ok=True)
    cur[["station_id", *COLS]].to_csv(p, index=False)
    print(f"[collect] {hour:%Y-%m-%d %H:%M} UTC: {len(cur)}/{len(df)} staníc -> {p}")
    return hour


def load_stations() -> pd.DataFrame:
    if STATIONS.exists():
        return pd.read_csv(STATIONS, dtype={"station_id": str})
    return pd.DataFrame(columns=ST_COLS)


def update_stations(df: pd.DataFrame) -> None:
    cur = load_stations()
    new = df[~df.station_id.isin(cur.station_id)][["station_id", "name", "type", "basin"]]
    if new.empty:
        return
    new = new.assign(lat=float("nan"), lon=float("nan"), geo_tried=0)
    out = new if cur.empty else pd.concat([cur, new], ignore_index=True)
    STATIONS.parent.mkdir(parents=True, exist_ok=True)
    out[ST_COLS].to_csv(STATIONS, index=False)
    print(f"[stations] nové stanice: {len(new)}")


# ───────────────────────────── súradnice ─────────────────────────────

def _queries(name: str) -> list[str]:
    base = re.sub(r"[()]", " ", name.split(" - ")[0]).strip()
    qs = [base, base.split("-")[0].strip()]
    return list(dict.fromkeys(q for q in qs if q))


def _nominatim(q: str):
    r = requests.get("https://nominatim.openstreetmap.org/search",
                     params={"q": q, "format": "jsonv2", "limit": 1, "countrycodes": "sk"},
                     headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if j:
        lat, lon = float(j[0]["lat"]), float(j[0]["lon"])
        if 47.6 < lat < 49.7 and 16.8 < lon < 22.7:
            return lat, lon
    return None


def geocode(limit: int = 150) -> None:
    """Súradnice podľa názvu obce (približné). Hodnoty v data/stations.csv
    môžeš kedykoľvek opraviť ručne – existujúce sa neprepisujú."""
    st = load_stations()
    todo = st[st.lat.isna() & (st.geo_tried == 0)]
    cache, calls = {}, 0
    for idx, r in todo.iterrows():
        res = None
        for q in _queries(r["name"]):
            if q not in cache:
                if calls >= limit:
                    break
                try:
                    cache[q] = _nominatim(q)
                except requests.RequestException as e:
                    print(f"[geocode] {q}: {e}", file=sys.stderr)
                    cache[q] = None
                calls += 1
                time.sleep(1.1)  # limit Nominatim: 1 req/s
            res = cache.get(q)
            if res:
                break
        if res:
            st.loc[idx, ["lat", "lon"]] = res
            st.loc[idx, "geo_tried"] = 1
        elif all(q in cache for q in _queries(r["name"])):
            st.loc[idx, "geo_tried"] = 1  # neúspech – neskúšať znova
    st[ST_COLS].to_csv(STATIONS, index=False)
    print(f"[geocode] volaní: {calls}, bez súradníc: {int(st.lat.isna().sum())}/{len(st)}")


# ───────────────────────────── denné súhrny ─────────────────────────────

def day_range(d: date):
    """Hodinové záznamy dňa D (lokálne): čas merania T patrí do D, ak T ∈ (D 00:00, D+1 00:00]
    – hodnota p1 v čase T je úhrn za hodinu *pred* T."""
    s = pd.Timestamp(d).tz_localize(TZ).tz_convert("UTC")
    e = (pd.Timestamp(d) + pd.Timedelta(days=1)).tz_localize(TZ).tz_convert("UTC")
    return s, e, pd.date_range(s + pd.Timedelta(hours=1), e, freq="h")


def build_day(d: date) -> bool:
    _, e, hrs = day_range(d)
    frames = []
    for h in hrs:
        p = raw_path(h)
        if p.exists():
            f = pd.read_csv(p, dtype={"station_id": str})
            f["h"] = h
            frames.append(f)
    if not frames:
        return False
    x = pd.concat(frames)
    g = x.groupby("station_id")
    out = pd.DataFrame({"sum_1h": g.p1.sum(min_count=1), "max_1h": g.p1.max(),
                        "n_hours": g.p1.count()})
    out["p24_end"] = x[x.h == e].set_index("station_id").p24
    out["expected"] = len(hrs)
    # chýbajúce hodiny -> použiť 24h úhrn z polnoci (ak existuje)
    use24 = (out.n_hours < out.expected) & out.p24_end.notna() & (len(hrs) == 24)
    out["total_mm"] = out.sum_1h.where(~use24, out.p24_end)
    out["complete"] = (out.n_hours >= out.expected) | use24
    out.insert(0, "date", d.isoformat())
    p = DAILY / f"{d:%Y}" / f"{d.isoformat()}.csv"
    p.parent.mkdir(parents=True, exist_ok=True)
    out.reset_index().to_csv(p, index=False)
    return True


def load_daily(start: date, end: date) -> pd.DataFrame:
    fr = []
    for p in sorted(DAILY.glob("*/*.csv")):
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if start <= d <= end:
            fr.append(pd.read_csv(p, dtype={"station_id": str}))
    if not fr:
        return pd.DataFrame()
    df = pd.concat(fr, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"]).dt.date
    return df


def rebuild_all() -> None:
    hours = sorted(RAW.glob("*/*/*/*.csv"))
    if not hours:
        return
    days = set()
    for p in hours:
        h = pd.Timestamp(f"{p.parts[-4]}-{p.parts[-3]}-{p.parts[-2]} {p.stem}:00", tz="UTC")
        days.add((h - pd.Timedelta(hours=1)).tz_convert(TZ).date())
    for d in sorted(days):
        build_day(d)
    print(f"[rebuild] prepočítaných dní: {len(days)}")


# ───────────────────────────── štatistiky ─────────────────────────────

def latest_hour() -> pd.Timestamp | None:
    files = sorted(RAW.glob("*/*/*/*.csv"))
    if not files:
        return None
    p = files[-1]
    return pd.Timestamp(f"{p.parts[-4]}-{p.parts[-3]}-{p.parts[-2]} {p.stem}:00", tz="UTC")


def short_term(windows=(1, 3, 6, 12, 24, 48, 72)):
    """Krátkodobé úhrny po staniciach.

    1–24 h : hodnoty, ktoré SHMÚ nahlásilo v poslednom snímku (p1, p3, p6, p12, p24) –
             nezávisia od histórie ani od dier v zbere. Ak chýbajú, súčet hodinových p1
             (aspoň 90 % hodín okna).
    48, 72 h: súčet hodinových p1 (aspoň 90 % hodín okna); inak súčet nahlásených
              24h úhrnov v časoch T, T-24h, (T-48h) – všetky musia existovať.
    """
    last = latest_hour()
    if last is None:
        return None, None
    rep_cols = {1: "p1", 3: "p3", 6: "p6", 12: "p12", 24: "p24"}
    hrs = pd.date_range(last - pd.Timedelta(hours=max(windows) - 1), last, freq="h")
    fr = []
    for h in hrs:
        p = raw_path(h)
        if p.exists():
            f = pd.read_csv(p, dtype={"station_id": str},
                            usecols=["station_id", "p1", "p3", "p6", "p12", "p24"])
            f["h"] = h
            fr.append(f)
    if not fr:
        return None, None
    x = pd.concat(fr, ignore_index=True)
    cur = x[x.h == last].set_index("station_id")
    tab = pd.DataFrame(index=sorted(x.station_id.unique()))
    for w in windows:
        sub = x[x.h > last - pd.Timedelta(hours=w)]
        g = sub.groupby("station_id").p1
        comp = g.sum(min_count=1).where(g.count() >= math.ceil(0.9 * w)).reindex(tab.index)
        if w in rep_cols:
            val = cur[rep_cols[w]].reindex(tab.index).combine_first(comp)
        elif w % 24 == 0:
            k = w // 24
            parts = [x[x.h == last - pd.Timedelta(hours=24 * i)].set_index("station_id").p24
                     .reindex(tab.index) for i in range(k)]
            chain = pd.concat(parts, axis=1).sum(axis=1, min_count=k)
            val = comp.combine_first(chain)
        else:
            val = comp
        tab[f"{w}h"] = val
    return tab, last


def longest_dry(s: pd.Series) -> int:
    run = best = 0
    for v in s.sort_index():
        run = run + 1 if v < 1 else 0
        best = max(best, run)
    return best


def period_table(sub: pd.DataFrame, expected_days: int, dry: bool = False) -> pd.DataFrame:
    g = sub.groupby("station_id")
    t = pd.DataFrame({
        "total_mm": g.total_mm.sum(min_count=1).round(1),
        "max_day_mm": g.total_mm.max(),
        "max_1h_mm": g.max_1h.max(),
        "wet_days": g.total_mm.apply(lambda s: int((s >= 1).sum())),
        "days": g.total_mm.count(),
    })
    t["coverage"] = (t.days / max(expected_days, 1)).clip(upper=1).round(2)
    if dry:
        t["longest_dry_days"] = sub.set_index("date").groupby("station_id").total_mm.apply(longest_dry)
    return t


@dataclass
class Period:
    kind: str
    label: str
    start: date
    end: date
    title: str


def periods(today: date) -> list[Period]:
    P = []
    for i in range(31):
        d = today - timedelta(days=i)
        P.append(Period("day", d.isoformat(), d, d, f"Deň {d:%d.%m.%Y}"))
    mon = today - timedelta(days=today.weekday())
    for i in range(12):
        s = mon - timedelta(weeks=i)
        e = s + timedelta(days=6)
        y, w, _ = s.isocalendar()
        P.append(Period("week", f"{y}-W{w:02d}", s, e, f"Týždeň {w}/{y} ({s:%d.%m.}–{e:%d.%m.%Y})"))
    y, m = today.year, today.month
    for _ in range(12):
        s = date(y, m, 1)
        e = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
        P.append(Period("month", f"{y}-{m:02d}", s, e, f"Mesiac {m:02d}/{y}"))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    years = sorted(int(p.name) for p in DAILY.glob("*") if p.name.isdigit()) or [today.year]
    for yy in range(years[0], today.year + 1):
        P.append(Period("year", str(yy), date(yy, 1, 1), date(yy, 12, 31), f"Rok {yy}"))
    return P


# ───────────────────────────── mapy ─────────────────────────────

def cls(v: float, kind: str) -> int:
    return sum(v >= b for b in BINS[kind][1:])


def make_map(tab: pd.DataFrame, st: pd.DataFrame, p: Period, partial: bool, path: Path) -> None:
    import folium  # import až tu, aby ostatné príkazy fungovali bez neho

    d = tab.join(st.set_index("station_id")[["name", "lat", "lon"]], how="inner")
    d = d.dropna(subset=["lat", "lon", "total_mm"])
    m = folium.Map(location=[48.7, 19.5], zoom_start=8, control_scale=True)
    circles = folium.FeatureGroup("Stanice", show=True)
    labels = folium.FeatureGroup("Hodnoty (mm)", show=p.kind == "day")
    for r in d.itertuples():
        c = COLORS[cls(r.total_mm, p.kind)]
        tip = f"<b>{r.name}</b><br>{r.total_mm:.1f} mm<br>pokrytie {r.coverage:.0%}"
        folium.CircleMarker([r.lat, r.lon], radius=7, color="#333" if r.coverage >= .8 else "#e00",
                            weight=1, fill=True, fill_color=c, fill_opacity=.85,
                            tooltip=tip).add_to(circles)
        folium.Marker([r.lat, r.lon], icon=folium.DivIcon(
            html=f'<div style="font:600 10px sans-serif;margin:-6px 0 0 10px;'
                 f'text-shadow:0 0 2px #fff,0 0 2px #fff">{r.total_mm:.0f}</div>')).add_to(labels)
    circles.add_to(m)
    labels.add_to(m)
    folium.LayerControl().add_to(m)

    b = BINS[p.kind]
    names = [f"{b[i]}–{b[i + 1]}" for i in range(8)] + [f"&gt; {b[8]}"]
    leg = "".join(f'<div><span style="display:inline-block;width:14px;height:14px;'
                  f'background:{COLORS[i]};border:1px solid #666;margin-right:6px"></span>{n} mm</div>'
                  for i, n in enumerate(names))
    title = p.title + (" (zatiaľ neukončené)" if partial else "")
    m.get_root().html.add_child(folium.Element(
        f'<div style="position:fixed;bottom:25px;left:10px;z-index:9999;background:#fff;'
        f'padding:8px 10px;font:12px sans-serif;border:1px solid #888;border-radius:4px">{leg}</div>'
        f'<div style="position:fixed;top:10px;left:50px;z-index:9999;background:#fff;'
        f'padding:6px 12px;font:600 14px sans-serif;border:1px solid #888;border-radius:4px">'
        f'{title} – úhrn zrážok (mm); červený okraj = nízke pokrytie dát</div>'))
    path.parent.mkdir(parents=True, exist_ok=True)
    m.save(str(path))


# ───────────────────────────── web ─────────────────────────────

CSS = ("body{font:15px/1.5 system-ui,sans-serif;max-width:1000px;margin:20px auto;padding:0 12px}"
       "table{border-collapse:collapse;margin:8px 0}td,th{border:1px solid #ccc;padding:3px 8px;"
       "text-align:right}th{background:#f3f3f3}td:first-child,th:first-child{text-align:left}"
       "a{margin-right:10px}")


def html_table(df: pd.DataFrame, **kw) -> str:
    return df.to_html(na_rep="–", border=0, **kw)


def build_site() -> None:
    SITE.mkdir(exist_ok=True)
    (SITE / "data").mkdir(exist_ok=True)
    st = load_stations()
    now = pd.Timestamp.now(tz=TZ)
    today = now.date()
    parts = [f"<h1>SHMÚ – úhrn zrážok: štatistiky a mapy</h1>"
             f"<p>Aktualizované: {now:%d.%m.%Y %H:%M}. Zdroj: "
             f'<a href="{URL}">SHMÚ</a> (operatívne, nekorigované údaje).</p>']
    names = st.set_index("station_id").name

    # krátkodobé
    tab, last = short_term()
    if tab is not None:
        tab.join(names).to_csv(SITE / "data" / "short_term.csv", index_label="station_id")
        summ = pd.DataFrame({"priemer (mm)": tab.mean().round(1), "max (mm)": tab.max().round(1),
                             "stanica s max": [names.get(tab[c].idxmax(), "") if tab[c].notna().any()
                                               else "" for c in tab.columns],
                             "počet staníc": tab.count()})
        top = tab["24h"].dropna().nlargest(10).round(1).to_frame("24h (mm)")
        top.insert(0, "stanica", [names.get(i, i) for i in top.index])
        parts += [f"<h2>Krátkodobé (k {last.tz_convert(TZ):%d.%m.%Y %H:%M})</h2>",
                  html_table(summ), "<h3>Top 10 staníc – 24 h</h3>",
                  html_table(top.reset_index(drop=True), index=False),
                  '<p><a href="data/short_term.csv">short_term.csv</a></p>']

    # dlhodobé + mapy
    plist = periods(today)
    daily = load_daily(min(p.start for p in plist), today)
    links: dict[str, list[tuple[str, str]]] = {k: [] for k in KIND_SK}
    summaries = []
    if not daily.empty:
        nat = daily.groupby("date").total_mm.mean().round(2).rename("priemer_mm")
        nat.to_csv(SITE / "data" / "national_daily_mean.csv")
        for p in plist:
            sub = daily[(daily.date >= p.start) & (daily.date <= p.end)]
            if sub.empty:
                continue
            exp = (min(p.end, today) - p.start).days + 1
            t = period_table(sub, exp, dry=p.kind in ("month", "year"))
            t.join(names).to_csv(SITE / "data" / f"{p.kind}_{p.label}.csv", index_label="station_id")
            partial = p.end >= today
            try:
                make_map(t, st, p, partial, SITE / "maps" / p.kind / f"{p.label}.html")
                links[p.kind].append((p.label, f"maps/{p.kind}/{p.label}.html"))
            except ModuleNotFoundError:
                print("[build] chýba folium – mapy sa preskakujú", file=sys.stderr)
            if p.kind in ("month", "year"):
                tt = t.dropna(subset=["total_mm"])
                if len(tt):
                    summaries.append({
                        "obdobie": f"{KIND_SK[p.kind]} {p.label}" + (" (zatiaľ)" if partial else ""),
                        "priemer (mm)": round(tt.total_mm.mean(), 1),
                        "max (mm)": tt.total_mm.max(),
                        "stanica s max": names.get(tt.total_mm.idxmax(), ""),
                        "max denný (mm)": tt.max_day_mm.max(),
                        "max hodinový (mm)": tt.max_1h_mm.max(),
                        "najdlhšie sucho (dni)": int(tt.longest_dry_days.max()),
                    })
        parts.append("<h2>Dlhodobé (mesiace, roky)</h2>")
        if summaries:
            parts.append(html_table(pd.DataFrame(summaries), index=False))
        parts.append('<p><a href="data/national_daily_mean.csv">priemer SR po dňoch (CSV)</a></p>')
    parts.append("<h2>Mapy</h2>")
    for k, lab in KIND_SK.items():
        if links[k]:
            parts.append(f"<p><b>{lab}:</b> " + " ".join(f'<a href="{u}">{l}</a>' for l, u in links[k]) + "</p>")
    (SITE / "index.html").write_text(
        f'<!doctype html><html lang="sk"><meta charset="utf-8"><title>SHMÚ zrážky</title>'
        f"<style>{CSS}</style>" + "\n".join(parts), encoding="utf-8")
    print(f"[build] hotovo -> {SITE}/")


# ───────────────────────────── CLI ─────────────────────────────

def collect() -> None:
    df = parse(fetch_html())
    hour = save_raw(df)
    update_stations(df)
    build_day((hour - pd.Timedelta(hours=1)).tz_convert(TZ).date())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("cmd", choices=["collect", "geocode", "build", "rebuild", "run"])
    ap.add_argument("--limit", type=int, default=150, help="max. volaní geokódovania za beh")
    a = ap.parse_args()
    if a.cmd in ("collect", "run"):
        collect()
    if a.cmd in ("geocode", "run"):
        geocode(a.limit)
    if a.cmd == "rebuild":
        rebuild_all()
    if a.cmd in ("build", "run"):
        build_site()


if __name__ == "__main__":
    main()
