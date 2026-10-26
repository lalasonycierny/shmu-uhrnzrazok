# shmu-uhrnzrazok

Hodinový zber údajov z https://www.shmu.sk/sk/?page=838 (úhrny zrážok 1/3/6/12/24 h),
štatistiky a mapy (deň / týždeň / mesiac / rok). Beží iba v GitHub Actions, výstup je na GitHub Pages.

## Nastavenie (raz)
1. Nahraj súbory do repozitára (`shmu.py`, `requirements.txt`, `.gitignore`, `.github/workflows/shmu.yml`).
2. *Settings → Pages → Source:* **GitHub Actions**.
3. *Settings → Actions → General → Workflow permissions:* **Read and write**.
4. *Actions → SHMU hourly → Run workflow* (prvý beh). Potom beží sám každú hodinu.

## Dáta
- `data/raw/YYYY/MM/DD/HH.csv` – hodinový snímok (čas v UTC)
- `data/daily/YYYY/YYYY-MM-DD.csv` – denné súhrny (lokálny čas)
- `data/stations.csv` – stanice a súradnice (lat/lon sa dopĺňajú automaticky podľa názvu obce
  cez Nominatim, približne; môžeš ich ručne opraviť, neprepíšu sa)

Denný úhrn = súčet hodinových `p1` hodnôt; ak hodiny chýbajú, použije sa 24h hodnota z polnoci.
Lokálne: `pip install -r requirements.txt && python shmu.py run`, výstup v `site/`.
