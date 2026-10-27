#!/usr/bin/env python3
"""
Krátkodobé a dlhodobé štatistiky úhrnov zrážok zo SHMÚ.

    python stats.py short   # posledných 72 h  -> stats/short_term.{md,json}, stats/short_term_stations.csv, ...
    python stats.py long    # celá história    -> stats/long_term.md, stats/long_term/*.csv
    python stats.py all

Vstup: data/raw/*.csv (a *.csv.gz) a data/stations.csv, ktoré vytvára collect.py.

Definície
---------
* hodinový úhrn (p1h)  = zrážky v mm za hodinu končiacu v čase merania (UTC v dátach)
* kalendárny deň       = 00:00-24:00 lokálneho času (Europe/Bratislava)
* denný úhrn           = 24h úhrn nahlásený SHMÚ o polnoci; ak chýba, súčet hodinových hodnôt
                         (len ak máme aspoň MIN_HOURS_DAY hodín z 24)
* mokrá hodina         = p1h >= WET_HOUR (0.1 mm)
* mokrý / suchý deň    = denný úhrn >= / < WET_DAY (1.0 mm, definícia WMO)
* úplný mesiac / rok   = aspoň 90 % dní s platným denným úhrnom
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"
STATIONS_CSV = ROOT / "data" / "stations.csv"
OUT = ROOT / "stats"
LONG_OUT = OUT / "long_term"

TZ = ZoneInfo("Europe/Bratislava")
WET_HOUR = 0.1        # mm/h
WET_DAY = 1.0         # mm/deň
MIN_HOURS_DAY = 22    # minimum hodinových hodnôt pre denný súčet
COVERAGE = 0.9        # minimálne pokrytie okna dátami
SHORT_WINDOW_H = 72
SUM_WINDOWS = (1, 3, 6, 12, 24, 48, 72)
TOP_N = 10


# --------------------------------------------------------------------------- #
# Načítanie
# --------------------------------------------------------------------------- #
def load() -> tuple[pd.DataFrame, pd.DataFrame]:
    files = sorted(RAW_DIR.glob("*.csv")) + sorted(RAW_DIR.glob("*.csv.gz"))
    if not files:
        sys.exit(f"V {RAW_DIR} nie sú žiadne dáta. Najprv spusti collect.py.")
    df = pd.concat((pd.read_csv(f, dtype={"station_id": str}) for f in files), ignore_index=True)
    df["obs"] = pd.to_datetime(df["obs_time"], utc=True)
    df = (df.drop_duplicates(["obs", "station_id"], keep="last")
            .sort_values(["obs", "station_id"]).reset_index(drop=True))
    if STATIONS_CSV.exists():
        st = pd.read_csv(STATIONS_CSV, dtype=str).fillna("")
    else:
        st = pd.DataFrame({"station_id": df["station_id"].unique(), "name": "", "type": "", "basin": ""})
    return df, st.set_index("station_id")


# --------------------------------------------------------------------------- #
# Pomocné funkcie
# --------------------------------------------------------------------------- #
def local_str(ts) -> str | None:
    if ts is None or pd.isna(ts):
        return None
    return pd.Timestamp(ts).tz_convert(TZ).strftime("%d.%m.%Y %H:%M")


def r1(x):
    return None if x is None or pd.isna(x) else round(float(x), 1)


def describe(s: pd.Series) -> dict:
    s = s.dropna()
    if s.empty:
        return {"n": 0, "mean": None, "median": None, "p90": None, "max": None}
    return {"n": int(s.size), "mean": r1(s.mean()), "median": r1(s.median()),
            "p90": r1(s.quantile(0.9)), "max": r1(s.max())}


def md_table(df: pd.DataFrame, floatfmt: str = "{:.1f}") -> str:
    if df.empty:
        return "_žiadne dáta_\n"
    def fmt(v):
        if v is None or (isinstance(v, float) and np.isnan(v)):
            return "–"
        if isinstance(v, (float, np.floating)):
            return floatfmt.format(v)
        return str(v)
    head = "| " + " | ".join(df.columns) + " |"
    sep = "|" + "|".join("---" for _ in df.columns) + "|"
    body = ["| " + " | ".join(fmt(v) for v in row) + " |" for row in df.itertuples(index=False)]
    return "\n".join([head, sep, *body]) + "\n"


def jsonable(o):
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, (np.floating, float)):
        return None if np.isnan(o) else round(float(o), 2)
    if isinstance(o, np.integer):
        return int(o)
    return o


def longest_run(mask: np.ndarray) -> int:
    best = cur = 0
    for v in mask:
        cur = cur + 1 if v else 0
        best = max(best, cur)
    return best


def trailing_run(mask: np.ndarray) -> int:
    n = 0
    for v in mask[::-1]:
        if not v:
            break
        n += 1
    return n


def name_of(st: pd.DataFrame, sid: str) -> str:
    return st["name"].get(sid, "") or sid


# --------------------------------------------------------------------------- #
# KRÁTKODOBÉ ŠTATISTIKY
# --------------------------------------------------------------------------- #
def short_term(df: pd.DataFrame, st: pd.DataFrame) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    last = df["obs"].max()
    idx = pd.date_range(end=last, periods=SHORT_WINDOW_H, freq="h")
    win = df[df["obs"] >= idx[0]]

    piv = win.pivot(index="obs", columns="station_id", values="p1h").reindex(idx)
    cur = win[win["obs"] == last].set_index("station_id")
    stations = [s for s in piv.columns if s in cur.index]
    piv = piv[stations]

    # --- po staniciach ----------------------------------------------------- #
    t = pd.DataFrame(index=stations)
    t["name"] = [name_of(st, s) for s in stations]
    t["basin"] = [st["basin"].get(s, "") for s in stations]
    for col in ("p1h", "p3h", "p6h", "p12h", "p24h"):
        t[f"shmu_{col}"] = cur.loc[stations, col]
    # Nahlásené 24h úhrny SHMÚ v čase t, t-24h, t-48h (nezávisia od dier v hodinovom zbere)
    p24 = win.pivot(index="obs", columns="station_id", values="p24h").reindex(idx).reindex(columns=stations)
    for h in SUM_WINDOWS:
        sub = piv.iloc[-h:]
        # 1) súčet hodinových hodnôt, ak je okno pokryté aspoň na COVERAGE
        comp = sub.sum(min_count=1).where(sub.notna().mean() >= COVERAGE)
        if h == 24:
            # 2) 24h: priamo hodnota od SHMÚ
            comp = comp.combine_first(cur.loc[stations, "p24h"])
        elif h > 24 and h % 24 == 0:
            # 2) 48h/72h: reťaz nahlásených 24h hodnôt (t, t-24h, ...) – chýba aspoň jedna => NaN
            k = h // 24
            parts = [p24.iloc[-1 - 24 * i] for i in range(k)]
            chain = pd.concat(parts, axis=1).sum(axis=1, min_count=k)
            comp = comp.combine_first(chain)
        t[f"sum_{h}h"] = comp
    day = piv.iloc[-24:]
    t["max_1h_24h"] = day.max()
    t["max_1h_24h_at"] = [local_str(day[s].idxmax()) if day[s].notna().any() else None for s in stations]
    t["wet_hours_24h"] = (day >= WET_HOUR).sum()
    wet = (piv >= WET_HOUR)
    known = piv.notna()
    t["dry_hours"] = [
        trailing_run(((~wet[s]) & known[s]).values) for s in stations   # strop = dĺžka okna (72)
    ]
    t["raining_now"] = (piv.iloc[-1] >= WET_HOUR)

    t = t.round(1)
    t.index.name = "station_id"
    t.sort_values("shmu_p24h", ascending=False).to_csv(OUT / "short_term_stations.csv")

    # --- národný hodinový priebeh ---------------------------------------- #
    nat = pd.DataFrame({
        "obs_local": [local_str(i) for i in piv.index],
        "stations": piv.notna().sum(axis=1),
        "mean_mm": piv.mean(axis=1),
        "max_mm": piv.max(axis=1),
        "wet_share_pct": (piv >= WET_HOUR).sum(axis=1) / piv.notna().sum(axis=1).replace(0, np.nan) * 100,
    }).round(2)
    nat.index = piv.index.strftime("%Y-%m-%dT%H:%MZ")
    nat.index.name = "obs_time"
    nat.to_csv(OUT / "hourly_national_72h.csv")

    # --- súhrn ------------------------------------------------------------ #
    prev_mean = piv.iloc[-2].mean() if len(piv) > 1 else np.nan
    cur_mean = piv.iloc[-1].mean()
    thresholds = {f">={x} mm / 24 h": int((t["shmu_p24h"] >= x).sum()) for x in (1, 10, 20, 50, 100)}
    by_basin = (t.groupby("basin")[["shmu_p1h", "shmu_p24h", "sum_72h"]].mean().round(1)
                if t["basin"].astype(bool).any() else pd.DataFrame())

    def top(col: str, n: int = TOP_N) -> pd.DataFrame:
        x = t[["name", "basin", col]].dropna().sort_values(col, ascending=False).head(n)
        return x.reset_index(drop=True)

    summary = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
        "latest_obs_utc": last.strftime("%Y-%m-%dT%H:%MZ"),
        "latest_obs_local": local_str(last),
        "stations_reporting": len(stations),
        "national": {
            "1h": describe(t["shmu_p1h"]),
            "3h": describe(t["shmu_p3h"]),
            "6h": describe(t["shmu_p6h"]),
            "12h": describe(t["shmu_p12h"]),
            "24h": describe(t["shmu_p24h"]),
            "48h_computed": describe(t["sum_48h"]),
            "72h_computed": describe(t["sum_72h"]),
            "mean_1h_change_vs_prev_hour": r1(cur_mean - prev_mean),
            "stations_raining_now": int(t["raining_now"].sum()),
            "stations_by_24h_threshold": thresholds,
        },
        "hours_in_db_last_72h": int(piv.notna().any(axis=1).sum()),
        "top_1h": top("shmu_p1h").to_dict("records"),
        "top_24h": top("shmu_p24h").to_dict("records"),
        "top_72h_computed": top("sum_72h").to_dict("records"),
        "by_basin": by_basin.reset_index().to_dict("records") if not by_basin.empty else [],
    }
    (OUT / "short_term.json").write_text(
        json.dumps(jsonable(summary), ensure_ascii=False, indent=2), encoding="utf-8")

    # --- markdown --------------------------------------------------------- #
    n = summary["national"]
    ren = {"name": "Stanica", "basin": "Povodie"}
    md = [
        "# Krátkodobé štatistiky zrážok (SHMÚ)\n",
        f"Posledné meranie: **{summary['latest_obs_local']}** (lokálny čas) · "
        f"staníc: **{len(stations)}** · vygenerované {summary['generated_utc']} UTC\n",
        f"Hodín s dátami v poslednom 72h okne: {summary['hours_in_db_last_72h']}/{SHORT_WINDOW_H}"
        + (f" · história zbieraná od {local_str(df['obs'].min())}" if (last - df['obs'].min()) < pd.Timedelta(hours=SHORT_WINDOW_H - 1) else "")
        + "\n",
        "## Slovensko – priemer cez stanice\n",
        md_table(pd.DataFrame({
            "Okno": ["1 h", "3 h", "6 h", "12 h", "24 h", "48 h", "72 h"],
            "Staníc": [n[k]["n"] for k in ("1h", "3h", "6h", "12h", "24h", "48h_computed", "72h_computed")],
            "Priemer mm": [n[k]["mean"] for k in ("1h", "3h", "6h", "12h", "24h", "48h_computed", "72h_computed")],
            "Medián mm": [n[k]["median"] for k in ("1h", "3h", "6h", "12h", "24h", "48h_computed", "72h_computed")],
            "P90 mm": [n[k]["p90"] for k in ("1h", "3h", "6h", "12h", "24h", "48h_computed", "72h_computed")],
            "Max mm": [n[k]["max"] for k in ("1h", "3h", "6h", "12h", "24h", "48h_computed", "72h_computed")],
        })),
        f"Prší na **{n['stations_raining_now']}** staniciach, zmena priemeru oproti predošlej hodine: "
        f"**{n['mean_1h_change_vs_prev_hour']} mm**.\n",
        "Počet staníc podľa 24h úhrnu: " + ", ".join(f"{k}: **{v}**" for k, v in thresholds.items()) + "\n",
        "## Top stanice – 1 h\n", md_table(top("shmu_p1h").rename(columns={**ren, "shmu_p1h": "mm / 1 h"})),
        "## Top stanice – 24 h\n", md_table(top("shmu_p24h").rename(columns={**ren, "shmu_p24h": "mm / 24 h"})),
        "## Top stanice – 72 h (vypočítané z hodinových hodnôt)\n",
        md_table(top("sum_72h").rename(columns={**ren, "sum_72h": "mm / 72 h"})),
    ]
    if not by_basin.empty:
        md += ["## Priemer podľa povodia\n",
               md_table(by_basin.reset_index().rename(columns={
                   "basin": "Povodie", "shmu_p1h": "1 h", "shmu_p24h": "24 h", "sum_72h": "72 h"}))]
    md += ["\nÚplné dáta po staniciach: [`short_term_stations.csv`](short_term_stations.csv) · "
           "hodinový priebeh: [`hourly_national_72h.csv`](hourly_national_72h.csv)\n"]
    (OUT / "short_term.md").write_text("\n".join(md), encoding="utf-8")
    print(f"[short] {summary['latest_obs_local']}, {len(stations)} staníc -> {OUT}")


# --------------------------------------------------------------------------- #
# DLHODOBÉ ŠTATISTIKY
# --------------------------------------------------------------------------- #
def build_daily(df: pd.DataFrame) -> pd.DataFrame:
    """Denné úhrny po staniciach (kalendárny deň v lokálnom čase)."""
    d = df[["obs", "station_id", "p1h", "p24h"]].copy()
    start_local = (d["obs"] - pd.Timedelta(hours=1)).dt.tz_convert(TZ)   # hodina končiaca 00:00 patrí predošlému dňu
    d["day"] = start_local.dt.strftime("%Y-%m-%d")
    d["hour_local"] = start_local.dt.hour

    g = d.groupby(["station_id", "day"])
    daily = pd.DataFrame({"sum_1h": g["p1h"].sum(min_count=1), "hours": g["p1h"].count()})

    last_hour = d[d["hour_local"] == 23].drop_duplicates(["station_id", "day"], keep="last")
    daily["shmu_24h"] = last_hour.set_index(["station_id", "day"])["p24h"].reindex(daily.index)

    use_shmu = daily["shmu_24h"].notna()
    use_sum = ~use_shmu & (daily["hours"] >= MIN_HOURS_DAY) & daily["sum_1h"].notna()
    daily["daily_mm"] = np.where(use_shmu, daily["shmu_24h"], np.where(use_sum, daily["sum_1h"], np.nan))
    daily["source"] = np.where(use_shmu, "shmu24h", np.where(use_sum, "sum1h", ""))

    daily = daily.reset_index()
    daily["month"] = daily["day"].str[:7]
    daily["year"] = daily["day"].str[:4]
    return daily


def aggregate_period(valid: pd.DataFrame, key: str, days_in_period) -> pd.DataFrame:
    g = valid.groupby(["station_id", key])
    out = pd.DataFrame({
        "total_mm": g["daily_mm"].sum(),
        "days_valid": g["daily_mm"].count(),
        "wet_days": g["daily_mm"].apply(lambda s: int((s >= WET_DAY).sum())),
        "max_daily_mm": g["daily_mm"].max(),
    }).reset_index()
    out["days_in_period"] = out[key].map(days_in_period)
    out["complete"] = out["days_valid"] >= 0.9 * out["days_in_period"]
    return out


def long_term(df: pd.DataFrame, st: pd.DataFrame) -> None:
    LONG_OUT.mkdir(parents=True, exist_ok=True)
    daily = build_daily(df)
    valid = daily[daily["daily_mm"].notna()].copy()
    if valid.empty:
        print("[long] zatiaľ nie je ani jeden platný deň – skús neskôr")
        return

    # --- denné ------------------------------------------------------------ #
    daily_out = (daily[["day", "station_id", "daily_mm", "source", "hours"]]
                 .sort_values(["day", "station_id"]))
    daily_out.round(2).to_csv(LONG_OUT / "daily_station.csv", index=False)

    # --- mesačné a ročné po staniciach ------------------------------------ #
    months = sorted(valid["month"].unique())
    dim = {m: pd.Period(m).days_in_month for m in months}
    monthly = aggregate_period(valid, "month", dim)
    monthly.round(1).sort_values(["month", "station_id"]).to_csv(LONG_OUT / "monthly_station.csv", index=False)

    years = sorted(valid["year"].unique())
    diy = {y: 366 if pd.Period(f"{y}-01").is_leap_year else 365 for y in years}
    yearly = aggregate_period(valid, "year", diy)
    yearly.round(1).sort_values(["year", "station_id"]).to_csv(LONG_OUT / "yearly_station.csv", index=False)

    # --- národné rady ------------------------------------------------------ #
    gd = valid.groupby("day")["daily_mm"]
    nat_daily = pd.DataFrame({
        "stations": gd.count(), "mean_mm": gd.mean(), "median_mm": gd.median(), "max_mm": gd.max(),
        "wet_share_pct": gd.apply(lambda s: (s >= WET_DAY).mean() * 100),
    })
    nat_daily["max_station"] = valid.loc[gd.idxmax(), ["day", "station_id"]].set_index("day")["station_id"]
    nat_daily["max_station"] = nat_daily["max_station"].map(lambda s: name_of(st, s))
    nat_daily.round(2).to_csv(LONG_OUT / "national_daily.csv")

    mc = monthly[monthly["complete"]]
    gm = mc.groupby("month")["total_mm"]
    nat_monthly = pd.DataFrame({"stations": gm.count(), "mean_mm": gm.mean(),
                                "median_mm": gm.median(), "max_mm": gm.max(), "min_mm": gm.min()})
    nat_monthly.round(1).to_csv(LONG_OUT / "national_monthly.csv")

    yc = yearly[yearly["complete"]]
    gy = yc.groupby("year")["total_mm"]
    nat_yearly = pd.DataFrame({"stations": gy.count(), "mean_mm": gy.mean(),
                               "median_mm": gy.median(), "max_mm": gy.max(), "min_mm": gy.min()})
    nat_yearly.round(1).to_csv(LONG_OUT / "national_yearly.csv")

    # --- klimatológia podľa kalendárneho mesiaca (priemer cez roky) -------- #
    if not mc.empty:
        mc = mc.assign(m=mc["month"].str[5:7].astype(int))
        clim = mc.pivot_table(index="station_id", columns="m", values="total_mm", aggfunc="mean").round(1)
        clim.columns = [f"m{c:02d}" for c in clim.columns]
        clim["n_months"] = mc.groupby("station_id")["month"].count()
        clim.to_csv(LONG_OUT / "station_monthly_climatology.csv")

    # --- rekordy a súhrn po staniciach ------------------------------------ #
    d1 = df.dropna(subset=["p1h"])
    imax = d1.loc[d1.groupby("station_id")["p1h"].idxmax()].set_index("station_id")
    rows = []
    for sid, s in valid.groupby("station_id"):
        s = s.set_index(pd.to_datetime(s["day"]))["daily_mm"].sort_index()
        full = s.reindex(pd.date_range(s.index.min(), s.index.max(), freq="D"))
        known = full.notna().values
        wet = (full.values >= WET_DAY) & known
        dry = (full.values < WET_DAY) & known
        rows.append({
            "station_id": sid,
            "name": name_of(st, sid),
            "basin": st["basin"].get(sid, ""),
            "first_day": s.index.min().strftime("%Y-%m-%d"),
            "last_day": s.index.max().strftime("%Y-%m-%d"),
            "days_valid": int(s.size),
            "total_mm": s.sum(),
            "mean_daily_mm": s.mean(),
            "wet_days": int((s >= WET_DAY).sum()),
            "wet_days_pct": (s >= WET_DAY).mean() * 100,
            "max_daily_mm": s.max(),
            "max_daily_date": s.idxmax().strftime("%Y-%m-%d"),
            "max_hourly_mm": imax["p1h"].get(sid, np.nan),
            "max_hourly_at": local_str(imax["obs"].get(sid)),
            "longest_dry_spell_days": longest_run(dry),
            "longest_wet_spell_days": longest_run(wet),
        })
    summ = pd.DataFrame(rows).sort_values("total_mm", ascending=False)
    summ.round(1).to_csv(LONG_OUT / "station_summary.csv", index=False)

    # --- markdown ---------------------------------------------------------- #
    first, lastd = valid["day"].min(), valid["day"].max()
    rec = (nat_daily.reset_index().sort_values("max_mm", ascending=False).head(TOP_N)
           [["day", "max_mm", "max_station", "mean_mm"]]
           .rename(columns={"day": "Deň", "max_mm": "Max mm", "max_station": "Stanica", "mean_mm": "Priemer SR mm"}))
    hr = (summ.sort_values("max_hourly_mm", ascending=False).head(TOP_N)
          [["name", "max_hourly_mm", "max_hourly_at"]]
          .rename(columns={"name": "Stanica", "max_hourly_mm": "Max mm / 1 h", "max_hourly_at": "Kedy"}))
    sp = (summ.sort_values("longest_dry_spell_days", ascending=False).head(TOP_N)
          [["name", "longest_dry_spell_days", "longest_wet_spell_days"]]
          .rename(columns={"name": "Stanica", "longest_dry_spell_days": "Najdlhšie suché obdobie (dni)",
                           "longest_wet_spell_days": "Najdlhšie daždivé obdobie (dni)"}))
    last_months = nat_monthly.reset_index().tail(12).rename(columns={
        "month": "Mesiac", "stations": "Staníc", "mean_mm": "Priemer mm", "median_mm": "Medián mm",
        "max_mm": "Max mm", "min_mm": "Min mm"})
    md = [
        "# Dlhodobé štatistiky zrážok (SHMÚ)\n",
        f"Obdobie: **{first} – {lastd}** · dní s platnými údajmi: **{valid['day'].nunique()}** · "
        f"staníc: **{valid['station_id'].nunique()}** · vygenerované "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n",
        "_Mesačné a ročné štatistiky sú len z úplných období (≥ 90 % dní)._\n",
        "## Priemerný mesačný úhrn cez stanice (posledných 12 úplných mesiacov)\n",
        md_table(last_months),
    ]
    if not nat_yearly.empty:
        md += ["## Priemerný ročný úhrn cez stanice\n",
               md_table(nat_yearly.reset_index().rename(columns={
                   "year": "Rok", "stations": "Staníc", "mean_mm": "Priemer mm", "median_mm": "Medián mm",
                   "max_mm": "Max mm", "min_mm": "Min mm"}))]
    md += [
        "## Najvyššie denné úhrny (stanica s maximom v daný deň)\n", md_table(rec),
        "## Najvyššie hodinové úhrny\n", md_table(hr),
        "## Najdlhšie suché obdobia (deň < 1 mm)\n", md_table(sp),
        "\nSúbory: [`long_term/`](long_term/) – `daily_station`, `monthly_station`, `yearly_station`, "
        "`national_daily|monthly|yearly`, `station_summary`, `station_monthly_climatology`.\n",
    ]
    (OUT / "long_term.md").write_text("\n".join(md), encoding="utf-8")
    print(f"[long] {first} – {lastd}, {valid['station_id'].nunique()} staníc -> {LONG_OUT}")


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=["short", "long", "all"], nargs="?", default="all")
    args = ap.parse_args()
    df, st = load()
    if args.what in ("short", "all"):
        short_term(df, st)
    if args.what in ("long", "all"):
        long_term(df, st)
    return 0


if __name__ == "__main__":
    sys.exit(main())
