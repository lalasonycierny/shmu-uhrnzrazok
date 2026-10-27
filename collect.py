#!/usr/bin/env python3
"""
Hodinový zber úhrnov zrážok zo SHMÚ.

Zdroj : https://www.shmu.sk/sk/?page=838  (Hydrologické spravodajstvo -> Zrážkomerné stanice)
Výstup: data/raw/YYYY-MM.csv   (jeden riadok = stanica x hodina merania, v UTC)
        data/stations.csv      (číselník staníc: názov, typ, povodie)

Spúšťanie:
    python collect.py                      # stiahne stránku a uloží údaje
    python collect.py --from-file page.html   # offline test na uloženom HTML
    python collect.py --dry-run            # nič nezapíše, len vypíše súhrn
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from bs4 import BeautifulSoup

URL = "https://www.shmu.sk/sk/?page=838"
TZ = ZoneInfo("Europe/Bratislava")          # SHMÚ uvádza lokálny čas
ROOT = Path(__file__).resolve().parent
RAW_DIR = ROOT / "data" / "raw"
STATIONS_CSV = ROOT / "data" / "stations.csv"

VALUE_COLS = ["p1h", "p3h", "p6h", "p12h", "p24h"]
KEY = ["obs_time", "station_id"]
USER_AGENT = "shmu-uhrnzrazok/1.0 (+https://github.com/lalasonycierny/shmu-uhrnzrazok)"

TIME_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})\s+(\d{1,2}):(\d{2})")
NUM_RE = re.compile(r"^-?\d+(?:[.,]\d+)?$")
BASIN_RE = re.compile(r"^\s*Povodie\s+\S+")

# Na stránke je názov povodia UNDER tabuľkou (overené na živej stránke:
# tabuľka so stanicami Myjava/Skalica/... je nasledovaná nadpisom "Povodie Morava").
# Ak by to SHMÚ zmenilo, stačí prepnúť na False (názov je nad tabuľkou).
BASIN_LABEL_AFTER_TABLE = True

log = logging.getLogger("collect")


# --------------------------------------------------------------------------- #
# Sťahovanie
# --------------------------------------------------------------------------- #
def fetch_html(url: str = URL, retries: int = 4, timeout: int = 40) -> bytes:
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
            r.raise_for_status()
            if len(r.content) < 10_000:
                raise RuntimeError(f"Podozrivo krátka odpoveď ({len(r.content)} B)")
            return r.content
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            wait = 5 * attempt
            log.warning("Pokus %d/%d zlyhal: %s (čakám %ds)", attempt, retries, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Stránku sa nepodarilo stiahnuť: {last_err}")


# --------------------------------------------------------------------------- #
# Parsovanie
# --------------------------------------------------------------------------- #
def parse_value(text: str) -> float | None:
    text = text.strip().replace("\xa0", " ").strip()
    if not NUM_RE.match(text):
        return None
    return float(text.replace(",", "."))


def parse_time_utc(text: str) -> str | None:
    m = TIME_RE.search(text)
    if not m:
        return None
    d, mo, y, h, mi = map(int, m.groups())
    local = datetime(y, mo, d, h, mi, tzinfo=TZ)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def station_id_from_href(href: str) -> str | None:
    q = parse_qs(urlparse(href).query)
    ids = q.get("stationId")
    return ids[0] if ids else None


def parse_page(html: bytes | str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Vráti (merania, stanice)."""
    soup = BeautifulSoup(html, "lxml")
    order = {id(n): i for i, n in enumerate(soup.descendants)}
    tables = [
        t for t in soup.find_all("table")
        if "Stanica" in t.get_text(" ") and "Zrážky 1h" in t.get_text(" ")
    ]
    if not tables:
        raise RuntimeError("Na stránke som nenašiel tabuľky so zrážkami – zmenila sa štruktúra?")

    basin_nodes = [n for n in soup.find_all(string=BASIN_RE)]
    all_tables = soup.find_all("table")

    def basin_for(table) -> str:
        t_pos = order[id(table)]
        idx = all_tables.index(table)
        if BASIN_LABEL_AFTER_TABLE:
            lo = t_pos
            hi = order[id(all_tables[idx + 1])] if idx + 1 < len(all_tables) else float("inf")
        else:
            lo = order[id(all_tables[idx - 1])] if idx > 0 else -1
            hi = t_pos
        cand = [n for n in basin_nodes if lo < order[id(n)] < hi]
        if not cand:
            return ""
        node = cand[0] if BASIN_LABEL_AFTER_TABLE else cand[-1]
        return re.sub(r"^\s*Povodie\s+", "", node.strip()).strip()

    rows, stations = [], {}
    for table in tables:
        basin = basin_for(table)
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 3 + len(VALUE_COLS):
                continue
            link = tds[0].find("a", href=True)
            sid = station_id_from_href(link["href"]) if link else None
            if not sid:                       # riadok "Priemery" a pod.
                continue
            obs = parse_time_utc(tds[2].get_text(" ", strip=True))
            if not obs:
                continue
            vals = [parse_value(td.get_text(" ", strip=True)) for td in tds[3:3 + len(VALUE_COLS)]]
            rows.append({"obs_time": obs, "station_id": sid, **dict(zip(VALUE_COLS, vals))})
            stations[sid] = {
                "station_id": sid,
                "name": link.get_text(" ", strip=True),
                "type": tds[1].get_text(strip=True),
                "basin": basin,
            }

    df = pd.DataFrame(rows, columns=KEY + VALUE_COLS)
    if df.empty:
        raise RuntimeError("Tabuľky sa našli, ale nepodarilo sa z nich prečítať žiadny riadok.")
    # ak sa stanica objaví vo viacerých tabuľkách, necháme poslednú
    df = df.drop_duplicates(KEY, keep="last")
    st = pd.DataFrame(stations.values())
    return df, st


# --------------------------------------------------------------------------- #
# Ukladanie
# --------------------------------------------------------------------------- #
def _atomic_write_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_csv(tmp, index=False)
    os.replace(tmp, path)


def save_measurements(df: pd.DataFrame) -> int:
    """Upsert do mesačných súborov. Vráti počet nových riadkov."""
    df = df.dropna(subset=VALUE_COLS, how="all")        # riadky bez akejkoľvek hodnoty nemá zmysel držať
    added = 0
    for month, part in df.groupby(df["obs_time"].str[:7]):
        path = RAW_DIR / f"{month}.csv"
        if path.exists():
            old = pd.read_csv(path, dtype={"station_id": str})
            merged = pd.concat([part, old], ignore_index=True)
            # nový záznam má prednosť (operatívne údaje SHMÚ sa môžu dodatočne spresniť)
            merged = merged.drop_duplicates(KEY, keep="first")
            added += len(merged) - len(old)
        else:
            merged = part
            added += len(part)
        # triedenie podľa času => nové riadky pribúdajú na koniec súboru (malé git diffy)
        merged = merged.sort_values(KEY)
        _atomic_write_csv(merged[KEY + VALUE_COLS], path)
    return added


def save_stations(st: pd.DataFrame) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    st = st.copy()
    st["last_seen"] = now
    if STATIONS_CSV.exists():
        old = pd.read_csv(STATIONS_CSV, dtype=str).fillna("")
        first = old.set_index("station_id")["first_seen"].to_dict()
        st["first_seen"] = st["station_id"].map(first).fillna(now)
        # stanice, ktoré dnes na stránke nie sú, necháme v číselníku
        keep = old[~old["station_id"].isin(st["station_id"])]
        st = pd.concat([st, keep], ignore_index=True)
    else:
        st["first_seen"] = now
    st = st.sort_values("station_id")[["station_id", "name", "type", "basin", "first_seen", "last_seen"]]
    _atomic_write_csv(st, STATIONS_CSV)


# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from-file", help="použiť uložený HTML súbor namiesto sťahovania")
    ap.add_argument("--dry-run", action="store_true", help="nič nezapisovať")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    html = Path(args.from_file).read_bytes() if args.from_file else fetch_html()
    df, st = parse_page(html)

    latest = df["obs_time"].max()
    age_h = (datetime.now(timezone.utc) - datetime.strptime(latest, "%Y-%m-%dT%H:%MZ").replace(tzinfo=timezone.utc)
             ).total_seconds() / 3600
    log.info("Načítaných %d staníc, čas merania %s UTC (pred %.1f h)", len(df), latest, age_h)
    if not args.from_file and age_h > 3:
        log.warning("Údaje na SHMÚ sú staršie ako 3 hodiny – zdroj sa pravdepodobne neaktualizuje.")

    if args.dry_run:
        print(df.head(10).to_string(index=False))
        return 0

    n = save_measurements(df)
    save_stations(st)
    log.info("Uložené. Nových riadkov: %d", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
