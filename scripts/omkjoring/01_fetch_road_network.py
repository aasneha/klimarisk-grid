import argparse
import csv
import json
import sys
import time
from collections import Counter
from pathlib import Path

import requests


API_URL = "https://nvdbapiles.atlas.vegvesen.no/vegnett/api/v4/veglenkesekvenser/segmentert"
X_CLIENT = "klimarisk-omkjoring, NTNU" # NVDB asks all clients to identify themselves.
COUNTIES = [55] # Troms
DEFAULT_ROAD_CATEGORIES = ["E", "R", "F", "K"]
ROAD_TYPES = ["enkelBilveg", "kanalisertVeg", "rampe", "rundkjøring", "bilferje"]
DETAIL_LEVELS = ["VT", "VTKB"]

SRID = 5973 # = ETRS89 / UTM 33N (+ NN2000 height)

PAGE_SIZE = 1000
MAX_ATTEMPTS = 6
PAUSE_BETWEEN_CALLS = 0.2 

DATA_DIR = Path(__file__).resolve().parent / "data"
RAW_DIR = DATA_DIR / "raw"

CSV_COLUMNS = [
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

def make_params(county: int, start: str | None) -> dict:
    params = {
        "fylke": county,
        "typeveg": ",".join(ROAD_TYPES),
        "detaljniva": ",".join(DETAIL_LEVELS),
        "trafikantgruppe": "K",          
        "srid": SRID,
        "antall": PAGE_SIZE,
    }
    if start:
        params["start"] = start
    else:
        params["inkluderAntall"] = "true"
    return params


def fetch_page(session: requests.Session, params: dict) -> dict:
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            r = session.get(API_URL, params=params, timeout=120)
        except requests.RequestException as e:
            error = str(e)
        else:
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503, 504):
                error = f"HTTP {r.status_code}"
            else:
                sys.exit(
                    f"\nNVDB returned {r.status_code} for {r.url}\n{r.text[:1000]}"
                )
        wait = min(2 ** attempt, 60)
        print(f"  ! {error} - retrying in {wait} s (attempt {attempt}/{MAX_ATTEMPTS})")
        time.sleep(wait)
    sys.exit("\nGave up after repeated errors. Run the script again to resume.")


def next_start(page: dict) -> str | None:
    return ((page.get("metadata") or {}).get("neste") or {}).get("start")


def download_county(session: requests.Session, county: int, restart: bool):
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    jsonl = RAW_DIR / f"fylke{county}.jsonl"
    state_file = RAW_DIR / f"fylke{county}.state"

    if restart:
        jsonl.unlink(missing_ok=True)
        state_file.unlink(missing_ok=True)

    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    if state.get("done"):
        print(f"County {county}: already downloaded ({state['count']} segments). Skipping.")
        return

    start = state.get("start")
    count = state.get("count", 0)
    print(f"County {county}: " + ("resuming" if start else "starting") + " download ...")

    with open(jsonl, "a", encoding="utf-8") as f:
        while True:
            page = fetch_page(session, make_params(county, start))
            objects = page.get("objekter") or []
            for obj in objects:
                f.write(json.dumps(obj, ensure_ascii=False) + "\n")
            f.flush()

            count += len(objects)
            new_start = next_start(page)
            done = new_start is None or new_start == start or not objects
            state_file.write_text(json.dumps({"start": new_start, "count": count, "done": done}))
            print(f"  {count} segments")

            if done:
                print(f"County {county}: finished ({count} segments).")
                break
            start = new_start
            time.sleep(PAUSE_BETWEEN_CALLS)


def segment_to_row(obj: dict) -> dict:
    road_system_reference = obj.get("vegsystemreferanse") or {}
    road_system = road_system_reference.get("vegsystem") or {}
    section = road_system_reference.get("strekning") or {}
    geom = obj.get("geometri") or {}

    seq_id = obj.get("veglenkesekvensid")
    start_pos, end_pos = obj.get("startposisjon"), obj.get("sluttposisjon")

    return {
        "segment_id": f"{start_pos}-{end_pos}@{seq_id}",
        "veglenkesekvensid": seq_id,
        "startposisjon": start_pos,
        "sluttposisjon": end_pos,
        "veglenkenummer": obj.get("veglenkenummer"),
        "segmentnummer": obj.get("segmentnummer"),
        "startnode": obj.get("startnode"),
        "sluttnode": obj.get("sluttnode"),
        "lengde": obj.get("lengde"),
        "typeVeg": obj.get("typeVeg"),
        "detaljnivaa": obj.get("detaljnivå"),
        "veglenketype": obj.get("type"),
        "topologinivaa": obj.get("topologinivå"),
        "vegkategori": road_system.get("vegkategori"),
        "fase": road_system.get("fase"),
        "vegnummer": road_system.get("nummer"),
        "trafikantgruppe": section.get("trafikantgruppe"),
        "kortform": road_system_reference.get("kortform"),
        "fylke": obj.get("fylke"),
        "kommune": obj.get("kommune"),
        "wkt": geom.get("wkt"),
    }


def build_table(counties: list[int], road_categories: list[str]) -> Path:
    out = DATA_DIR / "vegnett.csv"
    seen = set()
    n_in = n_out = n_no_geom = n_other_cat = 0
    allowed = {c.upper() for c in road_categories}

    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        w.writeheader()
        for county in counties:
            jsonl = RAW_DIR / f"fylke{county}.jsonl"
            if not jsonl.exists():
                print(f"  (no data for county {county})")
                continue
            with open(jsonl, encoding="utf-8") as src:
                for line in src:
                    n_in += 1
                    row = segment_to_row(json.loads(line))
                    if row["segment_id"] in seen:
                        continue  
                    if not row["wkt"]:
                        n_no_geom += 1
                        continue
                    if (row["vegkategori"] or "").upper() not in allowed:
                        n_other_cat += 1
                        continue
                    seen.add(row["segment_id"])
                    w.writerow(row)
                    n_out += 1

    print(f"\nWrote {n_out} segments to {out}")
    print(f"  road categories: {', '.join(sorted(allowed))}")
    print(f"  removed: {n_other_cat} in other road categories, "
          f"{n_in - n_out - n_no_geom - n_other_cat} duplicates, {n_no_geom} without geometry")
    return out


def summarise(csv_file: Path):
    with open(csv_file, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return
    km = sum(float(r["lengde"] or 0) for r in rows) / 1000
    nodes = {r["startnode"] for r in rows} | {r["sluttnode"] for r in rows}
    print("\nSummary")
    print(f"  segments: {len(rows)}   road length: {km:,.0f} km   nodes: {len(nodes)}")
    for field in ["vegkategori", "typeVeg", "fase", "veglenketype", "detaljnivaa"]:
        c = Counter(r[field] or "(empty)" for r in rows)
        print(f"  {field}: " + ", ".join(f"{k}={v}" for k, v in c.most_common()))


def main():
    p = argparse.ArgumentParser(description="Download the road network from NVDB API Les v4.")
    p.add_argument("--counties", nargs="+", type=int, default=COUNTIES,
                   help="County numbers (default: 55 = Troms)")
    p.add_argument("--road-categories", nargs="+", default=DEFAULT_ROAD_CATEGORIES,
                   help="Road categories to include (default: E R F K), e.g. --road-categories E R F K P")
    p.add_argument("--restart", action="store_true",
                   help="Delete the previous download and start again")
    args = p.parse_args()

    session = requests.Session()
    session.headers.update({"X-Client": X_CLIENT, "Accept": "application/json"})

    for county in args.counties:
        download_county(session, county, args.restart)

    csv_file = build_table(args.counties, args.road_categories)
    summarise(csv_file)


if __name__ == "__main__":
    main()
