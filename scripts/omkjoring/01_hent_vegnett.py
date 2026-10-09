"""
Steg 3: Hent vegnett med topologi fra NVDB API Les v4.

Henter segmenterte veglenker (med startnode/sluttnode og geometri) for valgte
fylker, og lagrer dem som en flat tabell som steg 4 bygger graf av.

Kjøres fra repo-roten:
    python scripts/omkjoring/01_hent_vegnett.py
    python scripts/omkjoring/01_hent_vegnett.py --maks-sider 2     # prøvekjøring

Utdata (i scripts/omkjoring/data/, som er ignorert av git):
    raw/fylke<NR>.jsonl     råobjekter fra API-et, én linje per segment
    raw/fylke<NR>.state     hvor nedlastingen kom til (for å kunne fortsette)
    vegnett.csv             flat tabell med én rad per segment, geometri som WKT
    vegnett.gpkg            samme som over, til QGIS (bare hvis geopandas finnes)

Nedlastingen kan avbrytes (Ctrl+C) og startes på nytt; den fortsetter der den
slapp. Bruk --paa-nytt for å laste ned alt fra start.
"""

import argparse
import csv
import json
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Innstillinger
# ---------------------------------------------------------------------------

API_URL = "https://nvdbapiles.atlas.vegvesen.no/vegnett/api/v4/veglenkesekvenser/segmentert"

# NVDB ber alle klienter identifisere seg. Skriv gjerne inn e-posten din.
X_CLIENT = "klimarisk-omkjoring (prosjektoppgave NTNU)"

# Bare Troms (55). Omkjøringer som går via Nordland, Finnmark, Sverige eller
# Finland fanges derfor ikke opp; nabofylker kan legges til med --fylker 55 18 56.
STANDARD_FYLKER = [55]

# Hvilke vegkategorier som tas med i vegnett.csv:
#   E = europaveg, R = riksveg, F = fylkesveg  (de store vegene)
#   K = kommunal veg, P = privat veg, S = skogsbilveg
# Alt lastes ned fra NVDB; filtreringen skjer når tabellen bygges, så du kan
# endre dette (eller bruke --vegkategorier) uten å laste ned på nytt.
STANDARD_VEGKATEGORIER = ["E", "R", "F", "K"]

# Bare veg for motorkjøretøy. Ferjer tas med fordi de er en del av
# omkjøringsmulighetene langs kysten (de kan få en tidsstraff i steg 4).
TYPEVEG = ["enkelBilveg", "kanalisertVeg", "rampe", "rundkjøring", "bilferje"]

# Vegtrasé-nivå: én linje per veg, ikke én per kjørebane/kjørefelt.
DETALJNIVA = ["VT", "VTKB"]

# EPSG:5973 = ETRS89 / UTM 33N (+ NN2000-høyde). Høyden fjernes ved lagring,
# slik at koordinatene passer med rutenettet (EPSG:25833).
SRID = 5973

SIDESTORRELSE = 1000
MAKS_FORSOK = 6
PAUSE_MELLOM_KALL = 0.2  # sekunder; vær snill mot API-et

DATA_DIR = Path(__file__).resolve().parent / "data"
RAW_DIR = DATA_DIR / "raw"

CSV_KOLONNER = [
    "segment_id",
    "veglenkesekvensid",
    "startposisjon",
    "sluttposisjon",
    "veglenkenummer",
    "segmentnummer",
    "startnode",
    "sluttnode",
    "lengde",
    "typeVeg",
    "detaljnivaa",
    "veglenketype",
    "topologinivaa",
    "vegkategori",
    "fase",
    "vegnummer",
    "trafikantgruppe",
    "kortform",
    "fylke",
    "kommune",
    "wkt",
]


# ---------------------------------------------------------------------------
# API-kall
# ---------------------------------------------------------------------------

def lag_params(fylke: int, start: str | None) -> dict:
    params = {
        "fylke": fylke,
        "typeveg": ",".join(TYPEVEG),
        "detaljniva": ",".join(DETALJNIVA),
        "trafikantgruppe": "K",
        "srid": SRID,
        "antall": SIDESTORRELSE,
    }
    if start:
        params["start"] = start
    else:
        params["inkluderAntall"] = "true"
    return params


def hent_side(session: requests.Session, params: dict) -> dict:
    """Ett API-kall med gjentatte forsøk ved nettverksfeil og 429/5xx."""
    for forsok in range(1, MAKS_FORSOK + 1):
        try:
            r = session.get(API_URL, params=params, timeout=120)
        except requests.RequestException as e:
            feil = str(e)
        else:
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                feil = f"HTTP {r.status_code}"
            else:
                # 4xx som ikke går over av seg selv: vis svaret og stopp.
                sys.exit(
                    f"\nNVDB svarte {r.status_code} for {r.url}\n{r.text[:1000]}"
                )
        vent = min(2 ** forsok, 60)
        print(f"  ! {feil} - prøver igjen om {vent} s (forsøk {forsok}/{MAKS_FORSOK})")
        time.sleep(vent)
    sys.exit("\nGa opp etter gjentatte feil. Kjør skriptet på nytt for å fortsette.")


def neste_start(side: dict) -> str | None:
    meta = side.get("metadata") or {}
    if not side.get("objekter") or meta.get("returnert", 0) == 0:
        return None
    neste = meta.get("neste") or {}
    return neste.get("start")


def last_ned_fylke(session: requests.Session, fylke: int, maks_sider: int | None, paa_nytt: bool):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    jsonl = RAW_DIR / f"fylke{fylke}.jsonl"
    state = RAW_DIR / f"fylke{fylke}.state"

    if paa_nytt:
        jsonl.unlink(missing_ok=True)
        state.unlink(missing_ok=True)

    st = json.loads(state.read_text()) if state.exists() else {}
    if st.get("ferdig"):
        print(f"Fylke {fylke}: allerede lastet ned ({st['antall']} segmenter). Hopper over.")
        return

    start = st.get("start")
    antall = st.get("antall", 0)
    side_nr = st.get("sider", 0)
    totalt = st.get("totalt")
    print(f"Fylke {fylke}: " + ("fortsetter" if start else "starter") + " nedlasting ...")

    with open(jsonl, "a", encoding="utf-8") as f:
        while True:
            if maks_sider is not None and side_nr >= maks_sider:
                print(f"  stoppet etter {side_nr} sider (--maks-sider)")
                break

            side = hent_side(session, lag_params(fylke, start))
            objekter = side.get("objekter") or []
            if totalt is None:
                totalt = (side.get("metadata") or {}).get("antall")

            for obj in objekter:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()

            antall += len(objekter)
            side_nr += 1
            nytt_start = neste_start(side)
            ferdig = nytt_start is None or nytt_start == start or len(objekter) == 0

            state.write_text(json.dumps({
                "start": nytt_start, "antall": antall, "sider": side_nr,
                "totalt": totalt, "ferdig": ferdig,
            }))

            av = f" av {totalt}" if totalt else ""
            print(f"  side {side_nr}: {antall}{av} segmenter")

            if ferdig:
                print(f"Fylke {fylke}: ferdig ({antall} segmenter).")
                break
            start = nytt_start
            time.sleep(PAUSE_MELLOM_KALL)


# ---------------------------------------------------------------------------
# Flat tabell
# ---------------------------------------------------------------------------

def flat_rad(obj: dict) -> dict:
    """Plukker ut feltene steg 4 trenger fra ett segment-objekt."""
    vsr = obj.get("vegsystemreferanse") or {}
    vegsystem = vsr.get("vegsystem") or {}
    strekning = vsr.get("strekning") or {}
    geom = obj.get("geometri") or {}

    vlsid = obj.get("veglenkesekvensid")
    fra, til = obj.get("startposisjon"), obj.get("sluttposisjon")

    return {
        # Unik nøkkel for segmentet (samme segment kan komme fra to fylker).
        "segment_id": f"{fra}-{til}@{vlsid}",
        "veglenkesekvensid": vlsid,
        "startposisjon": fra,
        "sluttposisjon": til,
        "veglenkenummer": obj.get("veglenkenummer"),
        "segmentnummer": obj.get("segmentnummer"),
        "startnode": obj.get("startnode"),
        "sluttnode": obj.get("sluttnode"),
        "lengde": obj.get("lengde"),
        "typeVeg": obj.get("typeVeg"),
        "detaljnivaa": obj.get("detaljnivå"),
        "veglenketype": obj.get("type"),
        "topologinivaa": obj.get("topologinivå"),
        "vegkategori": vegsystem.get("vegkategori"),
        "fase": vegsystem.get("fase"),
        "vegnummer": vegsystem.get("nummer"),
        "trafikantgruppe": strekning.get("trafikantgruppe"),
        "kortform": vsr.get("kortform"),
        "fylke": obj.get("fylke"),
        "kommune": obj.get("kommune"),
        "wkt": geom.get("wkt"),
    }


def bygg_tabell(fylker: list[int], vegkategorier: list[str]) -> Path:
    ut = DATA_DIR / "vegnett.csv"
    sett = set()
    n_inn = n_ut = n_uten_geom = n_annen_kat = 0
    tillatt = {k.upper() for k in vegkategorier}

    with open(ut, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_KOLONNER)
        w.writeheader()
        for fylke in fylker:
            jsonl = RAW_DIR / f"fylke{fylke}.jsonl"
            if not jsonl.exists():
                print(f"  (ingen data for fylke {fylke})")
                continue
            with open(jsonl, encoding="utf-8") as inn:
                for linje in inn:
                    n_inn += 1
                    rad = flat_rad(json.loads(linje))
                    if rad["segment_id"] in sett:
                        continue  # duplikat over fylkesgrense
                    if not rad["wkt"]:
                        n_uten_geom += 1
                        continue
                    if (rad["vegkategori"] or "").upper() not in tillatt:
                        n_annen_kat += 1
                        continue
                    sett.add(rad["segment_id"])
                    w.writerow(rad)
                    n_ut += 1

    print(f"\nSkrev {n_ut} segmenter til {ut}")
    print(f"  vegkategorier: {', '.join(sorted(tillatt))}")
    print(f"  fjernet: {n_annen_kat} på andre vegkategorier, "
          f"{n_inn - n_ut - n_uten_geom - n_annen_kat} duplikater, {n_uten_geom} uten geometri")
    return ut


def skriv_geopackage(csv_fil: Path):
    """Valgfritt: GeoPackage for å se på vegnettet i QGIS."""
    try:
        import geopandas as gpd
        import pandas as pd
        from shapely import force_2d, wkt
    except ImportError:
        print("geopandas er ikke installert - hopper over vegnett.gpkg (trengs ikke før steg 4).")
        return

    try:
        df = pd.read_csv(csv_fil, dtype={"startnode": str, "sluttnode": str})
        geom = [force_2d(wkt.loads(s)) for s in df.pop("wkt")]
        gdf = gpd.GeoDataFrame(df, geometry=geom, crs="EPSG:25833")
        ut = csv_fil.with_suffix(".gpkg")
        gdf.to_file(ut, layer="vegnett", driver="GPKG")
        print(f"Skrev {ut} (åpne i QGIS for å sjekke)")
    except Exception as e:  # noqa: BLE001 - valgfritt steg, CSV-en er allerede skrevet
        print(f"\nAdvarsel: klarte ikke å skrive vegnett.gpkg ({type(e).__name__}: {e})")
        print("vegnett.csv er skrevet og er det steg 4 bruker. Fiks geopandas-installasjonen før steg 4.")


def oppsummer(csv_fil: Path):
    from collections import Counter

    with open(csv_fil, encoding="utf-8") as f:
        rader = list(csv.DictReader(f))
    if not rader:
        return
    km = sum(float(r["lengde"] or 0) for r in rader) / 1000
    noder = {r["startnode"] for r in rader} | {r["sluttnode"] for r in rader}
    print("\nOppsummering")
    print(f"  segmenter: {len(rader)}   veglengde: {km:,.0f} km   noder: {len(noder)}")
    for felt in ["vegkategori", "typeVeg", "fase", "veglenketype", "detaljnivaa"]:
        c = Counter(r[felt] or "(tom)" for r in rader)
        print(f"  {felt}: " + ", ".join(f"{k}={v}" for k, v in c.most_common()))


# ---------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(description="Hent vegnett fra NVDB API Les v4.")
    p.add_argument("--fylker", nargs="+", type=int, default=STANDARD_FYLKER,
                   help="Fylkesnummer (standard: 55 = Troms)")
    p.add_argument("--vegkategorier", nargs="+", default=STANDARD_VEGKATEGORIER,
                   help="Vegkategorier som tas med (standard: E R F K). F.eks. --vegkategorier E R F K P")
    p.add_argument("--maks-sider", type=int, default=None,
                   help="Stopp etter så mange sider per fylke (for testing)")
    p.add_argument("--paa-nytt", dest="paa_nytt", action="store_true",
                   help="Slett tidligere nedlasting og start på nytt")
    args = p.parse_args()

    session = requests.Session()
    session.headers.update({"X-Client": X_CLIENT, "Accept": "application/json"})

    for fylke in args.fylker:
        last_ned_fylke(session, fylke, args.maks_sider, args.paa_nytt)

    csv_fil = bygg_tabell(args.fylker, args.vegkategorier)
    oppsummer(csv_fil)
    skriv_geopackage(csv_fil)


if __name__ == "__main__":
    main()