"""
Konverterer vegnettet (og rutenettet) til Esri JSON, som ArcGIS Pro kan lese
med verktøyet "JSON To Features". Bruker bare standardbiblioteket i Python,
så det fungerer selv om GDAL/PROJ er blokkert på maskinen.

Kjøres fra repo-roten:
    python scripts/omkjoring/til_arcgis.py
    python scripts/omkjoring/til_arcgis.py --fylke 55   # rutenett bare for Troms (standard)

Utdata (i scripts/omkjoring/data/arcgis/):
    vegnett.json      veglenkesegmenter (linjer)
    rutenett.json     1x1 km-rutene (polygoner)

I ArcGIS Pro: Geoprocessing -> "JSON To Features" -> velg fila -> Run.
Koordinatsystem er ETRS89 / UTM 33N (EPSG:25833).
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path

SKRIPT_DIR = Path(__file__).resolve().parent
REPO = SKRIPT_DIR.parent.parent
DATA_DIR = SKRIPT_DIR / "data"
UT_DIR = DATA_DIR / "arcgis"
VEGNETT_CSV = DATA_DIR / "vegnett.csv"
RUTENETT_GEOJSON = REPO / "scripts" / "source_geometry.geojson"

SPATIAL_REF = {"wkid": 25833}

# Felter fra vegnett.csv som tas med, og Esri-feltype.
VEG_FELTER = {
    "segment_id": "esriFieldTypeString",
    "veglenkesekvensid": "esriFieldTypeDouble",
    "startposisjon": "esriFieldTypeDouble",
    "sluttposisjon": "esriFieldTypeDouble",
    "startnode": "esriFieldTypeString",
    "sluttnode": "esriFieldTypeString",
    "lengde": "esriFieldTypeDouble",
    "typeVeg": "esriFieldTypeString",
    "detaljnivaa": "esriFieldTypeString",
    "veglenketype": "esriFieldTypeString",
    "vegkategori": "esriFieldTypeString",
    "fase": "esriFieldTypeString",
    "vegnummer": "esriFieldTypeDouble",
    "kortform": "esriFieldTypeString",
    "kommune": "esriFieldTypeDouble",
}

RUTE_FELTER = {
    "ssbid": "esriFieldTypeString",
    "txtKomNr": "esriFieldTypeString",
    "KomNavn": "esriFieldTypeString",
    "txtFylkNr": "esriFieldTypeString",
}

TALL = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def wkt_til_paths(wkt: str) -> list:
    """LINESTRING [Z] / MULTILINESTRING [Z] -> Esri 'paths' (bare x, y)."""
    wkt = wkt.strip()
    hode = wkt.split("(", 1)[0].upper()
    har_z = " Z" in hode or hode.endswith("Z")
    dim = 3 if har_z else 2
    kropp = wkt[len(wkt.split("(", 1)[0]):]

    if hode.startswith("MULTILINESTRING"):
        deler = re.findall(r"\(([^()]*)\)", kropp)
    elif hode.startswith("LINESTRING"):
        deler = [kropp.strip()[1:-1]]
    else:
        raise ValueError(f"Ukjent geometritype: {hode}")

    paths = []
    for del_ in deler:
        tall = [float(t) for t in TALL.findall(del_)]
        if len(tall) % dim:
            raise ValueError(f"Uventet antall koordinater i {hode}")
        paths.append([[tall[i], tall[i + 1]] for i in range(0, len(tall), dim)])
    return paths


def felt_defs(felter: dict) -> list:
    return [
        {"name": n, "type": t, "alias": n, **({"length": 255} if t == "esriFieldTypeString" else {})}
        for n, t in felter.items()
    ]


def verdi(v, t):
    if v in (None, ""):
        return None
    if t == "esriFieldTypeDouble":
        return float(v)
    return str(v)


def konverter_vegnett():
    if not VEGNETT_CSV.exists():
        sys.exit(f"Finner ikke {VEGNETT_CSV}. Kjør 01_hent_vegnett.py først.")

    features = []
    with open(VEGNETT_CSV, encoding="utf-8") as f:
        for rad in csv.DictReader(f):
            features.append({
                "attributes": {n: verdi(rad.get(n), t) for n, t in VEG_FELTER.items()},
                "geometry": {"paths": wkt_til_paths(rad["wkt"])},
            })

    ut = UT_DIR / "vegnett.json"
    skriv(ut, "esriGeometryPolyline", VEG_FELTER, features)
    print(f"Skrev {len(features)} veglinjer til {ut}")


def konverter_rutenett(fylke: str | None):
    with open(RUTENETT_GEOJSON, encoding="utf-8") as f:
        gj = json.load(f)

    crs = ((gj.get("crs") or {}).get("properties") or {}).get("name", "")
    if crs and "25833" not in crs:
        print(f"Advarsel: rutenettet har {crs}, ikke EPSG:25833")

    features = []
    for ft in gj["features"]:
        p = ft["properties"]
        if fylke and str(p.get("txtFylkNr")) != fylke:
            continue
        g = ft["geometry"]
        if g["type"] == "Polygon":
            rings = g["coordinates"]
        elif g["type"] == "MultiPolygon":
            rings = [r for poly in g["coordinates"] for r in poly]
        else:
            continue
        # Esri: ytre ring med klokka. Rutene fra GeoJSON går mot klokka -> snu.
        rings = [[pt[:2] for pt in reversed(r)] for r in rings]
        features.append({
            "attributes": {n: verdi(p.get(n), t) for n, t in RUTE_FELTER.items()},
            "geometry": {"rings": rings},
        })

    navn = f"rutenett_fylke{fylke}.json" if fylke else "rutenett.json"
    ut = UT_DIR / navn
    skriv(ut, "esriGeometryPolygon", RUTE_FELTER, features)
    print(f"Skrev {len(features)} ruter til {ut}")


def skriv(sti: Path, geomtype: str, felter: dict, features: list):
    UT_DIR.mkdir(parents=True, exist_ok=True)
    with open(sti, "w", encoding="utf-8") as f:
        json.dump({
            "geometryType": geomtype,
            "spatialReference": SPATIAL_REF,
            "fields": felt_defs(felter),
            "features": features,
        }, f, ensure_ascii=False)


def main():
    p = argparse.ArgumentParser(description="Lag Esri JSON for ArcGIS Pro.")
    p.add_argument("--fylke", default="55", help="Fylke for rutenettet (standard 55). Bruk 'alle' for hele landet.")
    args = p.parse_args()

    konverter_vegnett()
    konverter_rutenett(None if args.fylke == "alle" else args.fylke)
    print("\nI ArcGIS Pro: Analysis -> Tools -> 'JSON To Features' -> velg .json-fila -> Run.")


if __name__ == "__main__":
    main()