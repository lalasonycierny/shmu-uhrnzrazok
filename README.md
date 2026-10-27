# shmu-uhrnzrazok

Hodinový zber úhrnov zrážok zo SHMÚ ([Zrážkomerné stanice](https://www.shmu.sk/sk/?page=838))
a výpočet krátkodobých a dlhodobých štatistík. Údaje SHMÚ sú operatívne, nekorigované.

| Súbor | Čo robí |
|---|---|
| `collect.py` | stiahne stránku, uloží `data/raw/YYYY-MM.csv` (UTC) a `data/stations.csv` |
| `stats.py short` | posledných 72 h → `stats/short_term.md`, `.json`, `short_term_stations.csv`, `hourly_national_72h.csv` |
| `stats.py long` | celá história → `stats/long_term.md`, `stats/long_term/*.csv` |
| `.github/workflows/hourly.yml` | spúšťa všetko každú hodinu a commitne výsledok |

## Spustenie lokálne
```bash
pip install -r requirements.txt
python collect.py
python stats.py all
```
Bez GitHub Actions (vlastný server): `10 * * * * cd /cesta/k/repo && python collect.py && python stats.py short`

## Ako sa počítajú okná
* **1h–24h**: hodnoty, ktoré SHMÚ uvádza priamo (nezávisia od vynechaných behov).
* **48h, 72h**: súčet hodinových hodnôt, ak je okno pokryté aspoň na 90 %; inak súčet nahlásených 24h hodnôt (t, t−24h, t−48h).
* **Denný úhrn**: 24h hodnota o polnoci lokálneho času; ak chýba, súčet hodín (aspoň 22 z 24).
* Mesačné/ročné štatistiky len z úplných období (≥ 90 % dní). Mokrý deň ≥ 1 mm, mokrá hodina ≥ 0,1 mm.

Čas v dátach je UTC (`obs_time`), SHMÚ zverejňuje lokálny čas (Europe/Bratislava).
