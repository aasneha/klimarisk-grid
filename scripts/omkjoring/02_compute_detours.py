
import argparse
import csv
import heapq
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

COUNTY = "55" # Troms 
MAIN_ROAD_CATEGORIES = {"E", "R", "F"} # Road categories that define the entry points
DETOUR_NETWORK = {"E", "R", "F", "K"} # Road categories in the detour network
SEARCH_LIMIT_KM = 500.0
FERRY_PENALTY_M = 0.0 # Extra length (metres) per ferry crossing. 
WIGGLE_MAX_M = 1000.0 # max length of a piece that wiggles out and back into the cell
AGGREGATION = "shortest" # "shortest" or "longest" detour when a cell has several road pairs
PORT_M = 1000.0

CELL_M = 1000.0         
EDGE_TOL = 0.01         
NODE_PRECISION = 1      

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parent.parent
DATA_DIR = SCRIPT_DIR / "data"
ROAD_NETWORK_CSV = DATA_DIR / "vegnett.csv"
GRID_GEOJSON = REPO / "scripts" / "source_geometry.geojson"

NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

# Output columns 
COLUMNS = [
    "ssbid", "txtKomNr", "KomNavn", "status", "sOmkj_2050",
    "ekstra_valgt_m", "ekstra_min_m", "ekstra_maks_m", "normal_m",
    "antall_inngangspunkt", "antall_porter", "antall_slyngpunkt", "antall_par", "par_uten_omkjoring",
    "omkjoring_via_ferje",
]


def read_wkt(wkt: str) -> list[list[tuple[float, float]]]: # WKT line -> list of (x, y) lines
    head = wkt.split("(", 1)[0].strip().upper()
    dim = 3 if (" Z" in head or head.endswith("Z")) else 2
    body = wkt[wkt.index("("):]
    if head.startswith("MULTILINESTRING"):
        parts = re.findall(r"\(([^()]*)\)", body)
    elif head.startswith("LINESTRING"):
        parts = [body.strip()[1:-1]]
    else:
        raise ValueError(f"Unknown geometry type: {head}")
    lines = []
    for part in parts:
        t = [float(v) for v in NUMBER.findall(part)]
        lines.append([(t[i], t[i + 1]) for i in range(0, len(t), dim)])
    return lines


def split_road_segment_at_grid(points: list[tuple[float, float]]):
    all_pts = [points[0]]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        t_values = []
        for a, b in ((x1, x2), (y1, y2)):
            if a == b:
                continue
            lo, hi = min(a, b), max(a, b)
            k = math.floor(lo / CELL_M) + 1
            while k * CELL_M < hi:
                t_values.append((k * CELL_M - a) / (b - a))
                k += 1
        for t in sorted(t_values):
            if 0 < t < 1:
                all_pts.append((x1 + t * (x2 - x1), y1 + t * (y2 - y1)))
        all_pts.append((x2, y2))

    # Remove duplicate points
    clean = [all_pts[0]]
    for p in all_pts[1:]:
        if abs(p[0] - clean[-1][0]) > 1e-6 or abs(p[1] - clean[-1][1]) > 1e-6:
            clean.append(p)
    if len(clean) < 2:
        return []

    # Group consecutive sub-segments by the cell their midpoint lies in
    pieces = []
    current = None
    for p, q in zip(clean, clean[1:]):
        mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
        cell = (math.floor(mx / CELL_M), math.floor(my / CELL_M))
        if current and current[0] == cell:
            current[1].append(q)
        else:
            current = (cell, [p, q])
            pieces.append(current)
    return [(c[0], c[1], pts) for c, pts in pieces]


def length(pts):
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def node_key(p):
    return (round(p[0], NODE_PRECISION), round(p[1], NODE_PRECISION))


def on_cell_edge(p, ix, iy):
    x0, y0 = ix * CELL_M, iy * CELL_M
    return (abs(p[0] - x0) < EDGE_TOL or abs(p[0] - x0 - CELL_M) < EDGE_TOL
            or abs(p[1] - y0) < EDGE_TOL or abs(p[1] - y0 - CELL_M) < EDGE_TOL)


class Graph:
    def __init__(self):
        self.node_id = {}        
        self.coords = []         
        self.adjacent = []       
        self.edge_ferry = []     
        self.edge_main_road = []      
        self.edge_endpoints = [] 
        self.edge_geometry = None   

    def node(self, p):
        k = node_key(p)
        i = self.node_id.get(k)
        if i is None:
            i = len(self.coords)
            self.node_id[k] = i
            self.coords.append(k)
            self.adjacent.append([])
        return i

    def add_edge(self, a, b, weight, ferry, main=False, geom=None):
        e = len(self.edge_ferry)
        if self.edge_geometry is not None:
            self.edge_geometry.append(geom)
        self.edge_ferry.append(ferry)
        self.edge_main_road.append(main and not ferry)
        self.edge_endpoints.append((a, b))
        self.adjacent[a].append((b, weight, e))
        self.adjacent[b].append((a, weight, e))
        return e

    def find_shortest_path(self, s, t, blocked=frozenset(), limit=math.inf, with_path=False): # Shortest path from s to t, avoiding blocked edges (bidirectional Dijkstra)
        if s == t:
            return (0.0, False, []) if with_path else (0.0, False)
        dist = ({s: 0.0}, {t: 0.0})
        prev = ({s: None}, {t: None})      
        done = (set(), set())
        queues = ([(0.0, s)], [(0.0, t)])
        best, meet = math.inf, None

        while queues[0] and queues[1]:
            if queues[0][0][0] + queues[1][0][0] >= min(best, limit):
                break
            r = 0 if queues[0][0][0] <= queues[1][0][0] else 1
            d, u = heapq.heappop(queues[r])
            if u in done[r]:
                continue
            done[r].add(u)
            for v, w, e in self.adjacent[u]:
                if e in blocked:
                    continue
                nd = d + w
                if nd < dist[r].get(v, math.inf):
                    dist[r][v] = nd
                    prev[r][v] = (u, e)
                    heapq.heappush(queues[r], (nd, v))
                if v in dist[1 - r] and nd + dist[1 - r][v] < best:
                    best = nd + dist[1 - r][v]
                    meet = v

        if best >= limit or meet is None:
            return (math.inf, False, []) if with_path else (math.inf, False)

        # Follow the path back from the meeting point (also checks for ferry use)
        ferry, path = False, []
        for r in (0, 1):
            n = meet
            while prev[r].get(n):
                n, e = prev[r][n]
                ferry = ferry or self.edge_ferry[e]
                path.append(e)
        return (best, ferry, path) if with_path else (best, ferry)


def read_grid(county):
    with open(GRID_GEOJSON, encoding="utf-8") as f:
        gj = json.load(f)
    cells = {}
    for ft in gj["features"]:
        p = ft["properties"]
        if str(p.get("txtFylkNr")) != county:
            continue
        ring = ft["geometry"]["coordinates"][0]
        minx = min(c[0] for c in ring)
        miny = min(c[1] for c in ring)
        ix, iy = round(minx / CELL_M), round(miny / CELL_M)
        cells[(ix, iy)] = {
            "ssbid": str(p["ssbid"]),
            "txtKomNr": str(p.get("txtKomNr", "")),
            "KomNavn": p.get("KomNavn", ""),
            "ring": [c[:2] for c in ring],
        }
    return cells


def build_graph(store_geometry=False):
    if not ROAD_NETWORK_CSV.exists():
        sys.exit(f"Cannot find {ROAD_NETWORK_CSV}. Run 01_fetch_road_network.py first.")

    graf = Graph()
    if store_geometry:
        graf.edge_geometry = []
    pieces_in_cell = defaultdict(list)        
    categories = defaultdict(int)
    n_seg = 0

    with open(ROAD_NETWORK_CSV, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("fase") or "V").upper() != "V":
                continue  
            cat = (row.get("vegkategori") or "").upper()
            if cat not in DETOUR_NETWORK:
                continue
            n_seg += 1
            categories[cat] += 1
            is_main = cat in MAIN_ROAD_CATEGORIES
            is_ferry = (row.get("typeVeg") or "").lower().startswith("bilferje")
            for line in read_wkt(row["wkt"]):
                for ix, iy, pts in split_road_segment_at_grid(line):
                    a, b = graf.node(pts[0]), graf.node(pts[-1])
                    if a == b:
                        continue
                    e = graf.add_edge(a, b, length(pts), is_ferry, is_main,
                                   pts if store_geometry else None)
                    if is_ferry:
                        continue
                    pieces_in_cell[(ix, iy)].append(e)

    if FERRY_PENALTY_M:
        add_ferry_penalty(graf, FERRY_PENALTY_M)

    print(f"Read {n_seg} road segments (" +
          ", ".join(f"{k or '?'}={v}" for k, v in sorted(categories.items())) + ")")
    print(f"Graph: {len(graf.coords)} nodes, {len(graf.edge_ferry)} edges")
    return graf, pieces_in_cell


def add_ferry_penalty(graf, penalty_m): # Add penalty_m once per ferry crossing (half on each landing edge).
    on_land = [any(not graf.edge_ferry[e] for _, _, e in nbrs) for nbrs in graf.adjacent]
    extra = {}
    for e, (a, b) in enumerate(graf.edge_endpoints):
        if graf.edge_ferry[e]:
            x = penalty_m / 2 * (on_land[a] + on_land[b])
            if x:
                extra[e] = x
    for u, nbrs in enumerate(graf.adjacent):
        graf.adjacent[u] = [(v, w + extra.get(e, 0.0), e) for v, w, e in nbrs]


def find_entry_points(graf, cell, edges):
    """Points where an E, R or F road crosses the edge of the cell."""
    ix, iy = cell
    return {n for e in edges if graf.edge_main_road[e]
            for n in graf.edge_endpoints[e] if on_cell_edge(graf.coords[n], ix, iy)}


# Computation

def find_reachable_road_network(graf, start, blocked, max_m): # Find the road network reachable from start without blocked edges, up to max_m in total.
    nodes, edges = {start}, set()
    stack, total = [start], 0.0
    while stack:
        u = stack.pop()
        for v, w, e in graf.adjacent[u]:
            if e in blocked or e in edges:
                continue
            edges.add(e)
            total += w
            if total > max_m:
                return nodes, edges, False
            if v not in nodes:
                nodes.add(v)
                stack.append(v)
    return nodes, edges, True


def remove_wiggles(graf, points, blocked): # Close small road pieces that leave and re-enter the cell, and drop their entry points
    points = list(points)
    removed = 0
    checked = set()
    for p in list(points):
        if p in checked:
            continue
        nodes, edges, complete = find_reachable_road_network(graf, p, blocked, WIGGLE_MAX_M)
        checked |= nodes
        inside = [q for q in points if q in nodes]
        if complete and len(inside) >= 2:
            blocked |= edges
            for q in inside:
                points.remove(q)
            removed += len(inside)
    return sorted(points), removed


def analyse_cell(graf, edges, points): # Close one cell and compute the extra driving distance between its entry points
    points = sorted(points)
    res = {
        "antall_inngangspunkt": len(points),
        "antall_par": 0,
        "normal_m": None,
        "ekstra_valgt_m": None,
        "ekstra_min_m": None,
        "ekstra_maks_m": None,
        "par_uten_omkjoring": 0,
        "omkjoring_via_ferje": False,
        "antall_slyngpunkt": 0,
        "antall_porter": None,
    }
    # Status values
    if not edges:
        res["status"] = "ingen_veg_i_ruta"
        return res
    if len(points) == 0:
        res["status"] = "ingen_hovedveg"
        return res
    if len(points) == 1:
        res["status"] = "hovedveg_ender_i_ruta"
        return res

    blocked = set(edges)
    points, n_wiggle = remove_wiggles(graf, points, blocked)
    res["antall_slyngpunkt"] = n_wiggle
    res["antall_inngangspunkt"] = len(points)
    if len(points) < 2:
        res["status"] = "hovedveg_ender_i_ruta"
        return res
    blocked = frozenset(blocked)
    limit_m = SEARCH_LIMIT_KM * 1000

    port = list(range(len(points)))

    def port_root(i):
        while port[i] != i:
            port[i] = port[port[i]]
            i = port[i]
        return i

    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            d, _ = graf.find_shortest_path(points[i], points[j], blocked, limit=PORT_M)
            if not math.isinf(d):
                port[port_root(i)] = port_root(j)
    res["antall_porter"] = len({port_root(i) for i in range(len(points))})

    all_pairs = [] 
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            s, t = points[i], points[j]
            d0, _ = graf.find_shortest_path(s, t, blocked=frozenset(), limit=math.inf)
            if math.isinf(d0):
                continue  
            d1, ferry = graf.find_shortest_path(s, t, blocked, limit=d0 + limit_m)
            if not math.isinf(d1) and d1 <= d0 + 0.5:
                continue  
            all_pairs.append((port_root(i) != port_root(j),
                              (d1 - d0 if not math.isinf(d1) else math.inf, d0, ferry, (s, t))))

    between = [p for different, p in all_pairs if different]
    pairs = between if between else [p for _, p in all_pairs]

    res["antall_par"] = len(pairs)
    res["par_uten_omkjoring"] = sum(math.isinf(e) for e, *_ in pairs)
    if not pairs:
        res["status"] = "ikke_paavirket"
        return res

    found = [p for p in pairs if not math.isinf(p[0])]
    if found:
        res["ekstra_min_m"] = round(min(e for e, *_ in found), 1)
        res["ekstra_maks_m"] = round(max(e for e, *_ in found), 1)

    if AGGREGATION == "shortest":
        if found:
            chosen = min(found)
            res["status"] = "ok"
        else:
            chosen = None
            res["status"] = "ingen_omkjoring"
    else:  
        if res["par_uten_omkjoring"]:
            chosen = None
            res["status"] = "ingen_omkjoring"
        else:
            chosen = max(found)
            res["status"] = "ok"

    if chosen:
        res["ekstra_valgt_m"] = round(chosen[0], 1)
        res["normal_m"] = round(chosen[1], 1)
        res["omkjoring_via_ferje"] = chosen[2]
        res["_pair"] = chosen[3]        # internal: chosen pair (used for display)
    res["_blocked"] = blocked           # internal: closed edges
    return res


def detour_route(graf, pieces_in_cell, cell): # Compute the detour for one cell and return its edges, for display in ArcGIS.
    edges = pieces_in_cell.get(cell, [])
    res = analyse_cell(graf, edges, find_entry_points(graf, cell, edges))
    out = {"res": res, "detour": [], "normal": []}
    if "_pair" in res:
        s, t = res["_pair"]
        _, _, out["detour"] = graf.find_shortest_path(s, t, res["_blocked"], with_path=True)
        _, _, out["normal"] = graf.find_shortest_path(s, t, with_path=True)
    return out


def write_esri_json(results, cells, keys, county): # Write the cell results as Esri JSON polygons for ArcGIS Pro (JSON To Features)
    types = {
        "ssbid": "esriFieldTypeString", "txtKomNr": "esriFieldTypeString",
        "KomNavn": "esriFieldTypeString", "status": "esriFieldTypeString",
        "sOmkj_2050": "esriFieldTypeDouble", "ekstra_valgt_m": "esriFieldTypeDouble",
        "ekstra_min_m": "esriFieldTypeDouble", "ekstra_maks_m": "esriFieldTypeDouble",
        "normal_m": "esriFieldTypeDouble",
        "antall_inngangspunkt": "esriFieldTypeInteger", "antall_par": "esriFieldTypeInteger",
        "par_uten_omkjoring": "esriFieldTypeInteger", "omkjoring_via_ferje": "esriFieldTypeInteger",
        "antall_slyngpunkt": "esriFieldTypeInteger", "antall_porter": "esriFieldTypeInteger",
    }
    features = []
    for res, k in zip(results, keys):
        attr = {c: res.get(c) for c in types}
        attr["omkjoring_via_ferje"] = int(bool(attr["omkjoring_via_ferje"]))
        features.append({
            "attributes": attr,
            "geometry": {"rings": [list(reversed(cells[k]["ring"]))]},  
        })
    out_dir = DATA_DIR / "arcgis"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"omkjoring_fylke{county}.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({
            "geometryType": "esriGeometryPolygon",
            "spatialReference": {"wkid": 25833},
            "fields": [{"name": n, "type": t, "alias": n, **({"length": 100} if t.endswith("String") else {})}
                       for n, t in types.items()],
            "features": features,
        }, f, ensure_ascii=False)
    print(f"Wrote {out} (ArcGIS Pro: JSON To Features)")


def summarise(results, cap):
    print("\nSummary")
    for s, n in Counter(r["status"] for r in results).most_common():
        print(f"  {s}: {n}")
    values = sorted(r["sOmkj_2050"] for r in results if r["sOmkj_2050"] is not None)
    if values:
        def q(p):
            return values[min(len(values) - 1, int(p * len(values)))]
        print(f"  sOmkj_2050 (km): median {q(0.5):.1f}, 75 % {q(0.75):.1f}, "
              f"90 % {q(0.9):.1f}, max {values[-1]:.1f}")
        print(f"  cells without detour (= cap, {cap:.1f} km): "
              f"{sum(r['status'] == 'ingen_omkjoring' for r in results)}")
        print(f"  detour via ferry: {sum(bool(r['omkjoring_via_ferje']) for r in results)}")



def main():
    p = argparse.ArgumentParser(description="Compute the detour indicator per grid cell.")
    p.add_argument("--county", default=COUNTY)
    args = p.parse_args()
    print(f"Detour network: {', '.join(sorted(DETOUR_NETWORK))}   "
          f"Entry points: {', '.join(sorted(MAIN_ROAD_CATEGORIES))}")

    t0 = time.time()
    cells = read_grid(args.county)
    print(f"Grid: {len(cells)} cells in county {args.county}")
    graf, pieces_in_cell = build_graph()

    keys = sorted(cells, key=lambda k: cells[k]["ssbid"])

    results = []
    t1 = time.time()
    for n, k in enumerate(keys, 1):
        edges = pieces_in_cell.get(k, [])
        points = find_entry_points(graf, k, edges)
        res = analyse_cell(graf, edges, points)
        results.append({**{c: cells[k][c] for c in ("ssbid", "txtKomNr", "KomNavn")}, **res})

        if n % 200 == 0 or n == len(keys):
            used = time.time() - t1
            left = used / n * (len(keys) - n)
            print(f"  {n}/{len(keys)} cells  ({used:.0f} s, approx. {left:.0f} s left)")

    # Set the indicator value. Cap = longest real detour in the county.
    real = [r["ekstra_valgt_m"] / 1000 for r in results if r["status"] == "ok"]
    cap = max(real) if real else SEARCH_LIMIT_KM
    for r in results:
        if r["status"] == "ok":
            r["sOmkj_2050"] = round(r["ekstra_valgt_m"] / 1000, 3)
        elif r["status"] == "ingen_omkjoring":
            r["sOmkj_2050"] = round(cap, 3)
        elif r["status"] == "ikke_paavirket":
            r["sOmkj_2050"] = 0.0  
        else:
            r["sOmkj_2050"] = None
    print(f"\nCap for cells without detour: {cap:.1f} km (longest real detour)")

    out = DATA_DIR / f"omkjoring_fylke{args.county}.csv"
    with open(out, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        w.writerows(results)
    print(f"\nWrote {out}")

    write_esri_json(results, cells, keys, args.county)
    summarise(results, cap)
    print(f"\nFinished in {time.time() - t0:.0f} s")



if __name__ == "__main__":
    main()
