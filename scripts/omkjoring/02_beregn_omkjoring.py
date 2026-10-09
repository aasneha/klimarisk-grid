"""
Steg 4 og 5: Bygg vegnettsgraf og beregn omkjøringsindikator per 1x1 km-rute.

Scenario: hele ruta stenges (alle veger i ruta, også kommunale). For hvert par
av punkter der en stor veg (E/R/F) går inn eller ut av ruta, måles hvor mye
lengre det blir å kjøre mellom punktene på resten av det offentlige vegnettet
(E, R, F og K). Kommunale veger gir ikke egne inngangspunkter, men brukes til å
finne omkjøring.

I et kryss der én gren er blindveg (ingen omkjøring) og de andre har
omkjøring, er det grenene med omkjøring som avgjør: ruta får den korteste
omkjøringen. Ruta er bare "uten omkjøring" hvis ingen par har en veg rundt.

Metode
  1. Vegsegmentene fra vegnett.csv deles der de krysser rutenettlinjene
     (hver 1000 m i UTM 33), slik at hver bit ligger i nøyaktig én rute.
  2. Bitene blir kanter i en uretta graf (vekt = lengde i meter). Noder er
     endepunktene, identifisert med koordinat (avrundet til 0,1 m).
     Ferjestrekninger er med som omkjøringsmulighet, men stenges aldri.
     Der vegen slynger seg inn og ut av ruta langs kanten, regnes de korte
     bitene i naboruta som en del av stengingen (se SLYNG_MAKS_M).
  3. For hver rute i fylket:
       - inngangspunkter = der en E-, R- eller F-veg krysser rutekanten
         (ferjer gir ikke inngangspunkter)
       - punkter som når hverandre på under PORT_M utenfor den stengte ruta,
         slås sammen til én "port" (lommer/sløyfer ved kanten)
       - normal avstand d0 mellom hvert par (hele vegnettet åpent)
       - stengt avstand d1 mellom samme par (alle biter i ruta fjernet)
       - ekstra = d1 - d0
  4. Indikator sOmkj = korteste ekstra kjørelengde (km) blant parene som
     påvirkes av stengingen (se AGGREGERING).
     Ruter uten omkjøring (innen SOK_GRENSE_KM) får verdien til den lengste
     reelle omkjøringen som ble funnet i fylket (taket settes automatisk).

Kjøres fra repo-roten (etter 01_hent_vegnett.py):
    python scripts/omkjoring/02_beregn_omkjoring.py
    python scripts/omkjoring/02_beregn_omkjoring.py --maks-ruter 50     # rask test

Utdata (scripts/omkjoring/data/):
    omkjoring_fylke55.csv            én rad per rute, med indikator og detaljer
    arcgis/omkjoring_fylke55.json    samme som polygoner, for "JSON To Features"

Bruker bare Python sitt standardbibliotek.
"""

import argparse
import csv
import heapq
import json
import math
import re
import sys
import time
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Innstillinger
# ---------------------------------------------------------------------------

FYLKE = "55"

# Vegkategorier som definerer inngangspunktene (de store vegene).
HOVEDVEG_KATEGORIER = {"E", "R", "F"}

# Vegkategorier i omkjøringsnettet (hovedvariant: offentlig veg som er åpen for
# alle). Legg til "P" for private veger som følsomhetsanalyse.
OMKJORINGSNETT = {"E", "R", "F", "K"}

# Hvor langt (km ekstra) det letes etter omkjøring før ruta regnes som
# "uten omkjøring".
SOK_GRENSE_KM = 500.0

# Verdien ruter uten omkjøring får. None = automatisk: lik den lengste reelle
# omkjøringen som ble funnet i fylket. Sett et tall (km) for å overstyre.
TAK_KM = None

# Ekstra "lengde" (meter) for å kjøre en ferjestrekning. 0 = bare faktisk
# seilingsdistanse. Sett f.eks. 20000 for å la en ferje "koste" 20 km ekstra.
FERJE_STRAFF_M = 0.0

# Slyngbiter: der en veg slynger seg inn og ut av ruta langs rutekanten, blir
# biten mellom to kryssinger liggende i naboruta uten annen forbindelse enn
# gjennom den stengte ruta. Slike biter (kortere enn dette, med minst to
# kryssinger med ruta) regnes som en del av stengingen, og kryssingene deres
# er ikke egne inngangspunkter.
SLYNG_MAKS_M = 2000.0

# Når ruta har flere par av inngangspunkter (f.eks. i kryss):
#   "korteste" = bruk den korteste omkjøringen blant parene som påvirkes;
#                ruta er bare "uten omkjøring" hvis INGEN av parene har omkjøring.
#   "lengste"  = verste tilfelle; ruta er "uten omkjøring" hvis ett par mangler det.
AGGREGERING = "korteste"

# Inngangspunkter som når hverandre på en kortere veg enn dette (m) utenfor den
# stengte ruta, regnes som samme "port" (f.eks. en lomme eller gammel vegtrasé
# som går ut og inn igjen like ved rutekanten). Bare par mellom ulike porter
# brukes når det finnes slike.
PORT_M = 1000.0

RUTE_M = 1000.0         # rutestørrelse
KANT_TOL = 0.01         # m: hvor nær en rutelinje et punkt må være for å regnes som på kanten
NODE_PRESISJON = 1      # desimaler i nodenøkkel (0,1 m)

SKRIPT_DIR = Path(__file__).resolve().parent
REPO = SKRIPT_DIR.parent.parent
DATA_DIR = SKRIPT_DIR / "data"
VEGNETT_CSV = DATA_DIR / "vegnett.csv"
RUTENETT_GEOJSON = REPO / "scripts" / "source_geometry.geojson"

TALL = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


# ---------------------------------------------------------------------------
# Geometri
# ---------------------------------------------------------------------------

def les_wkt(wkt: str) -> list[list[tuple[float, float]]]:
    """LINESTRING [Z] / MULTILINESTRING [Z] -> liste av linjer med (x, y)."""
    hode = wkt.split("(", 1)[0].strip().upper()
    dim = 3 if (" Z" in hode or hode.endswith("Z")) else 2
    kropp = wkt[wkt.index("("):]
    if hode.startswith("MULTILINESTRING"):
        deler = re.findall(r"\(([^()]*)\)", kropp)
    elif hode.startswith("LINESTRING"):
        deler = [kropp.strip()[1:-1]]
    else:
        raise ValueError(f"Ukjent geometritype: {hode}")
    linjer = []
    for d in deler:
        t = [float(v) for v in TALL.findall(d)]
        linjer.append([(t[i], t[i + 1]) for i in range(0, len(t), dim)])
    return linjer


def del_ved_rutenett(punkter: list[tuple[float, float]]):
    """
    Deler en linje der den krysser x = k*1000 eller y = k*1000.
    Returnerer liste av (rute_ix, rute_iy, [punkter]) - én bit per rute.
    """
    # Sett inn krysningspunkter mellom hvert par av punkter
    alle = [punkter[0]]
    for (x1, y1), (x2, y2) in zip(punkter, punkter[1:]):
        t_verdier = []
        for a, b, i in ((x1, x2, 0), (y1, y2, 1)):
            if a == b:
                continue
            lo, hi = min(a, b), max(a, b)
            k = math.floor(lo / RUTE_M) + 1
            while k * RUTE_M < hi:
                t_verdier.append((k * RUTE_M - a) / (b - a))
                k += 1
        for t in sorted(t_verdier):
            if 0 < t < 1:
                alle.append((x1 + t * (x2 - x1), y1 + t * (y2 - y1)))
        alle.append((x2, y2))

    # Fjern duplikatpunkter
    rene = [alle[0]]
    for p in alle[1:]:
        if abs(p[0] - rene[-1][0]) > 1e-6 or abs(p[1] - rene[-1][1]) > 1e-6:
            rene.append(p)
    if len(rene) < 2:
        return []

    # Grupper sammenhengende delstrekninger etter hvilken rute midtpunktet ligger i
    biter = []
    gjeldende = None
    for p, q in zip(rene, rene[1:]):
        mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
        rute = (math.floor(mx / RUTE_M), math.floor(my / RUTE_M))
        if gjeldende and gjeldende[0] == rute:
            gjeldende[1].append(q)
        else:
            gjeldende = (rute, [p, q])
            biter.append(gjeldende)
    return [(r[0], r[1], pts) for r, pts in biter]


def lengde(pts):
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:]))


def nodenokkel(p):
    return (round(p[0], NODE_PRESISJON), round(p[1], NODE_PRESISJON))


def paa_rutekant(p, ix, iy):
    x0, y0 = ix * RUTE_M, iy * RUTE_M
    return (abs(p[0] - x0) < KANT_TOL or abs(p[0] - x0 - RUTE_M) < KANT_TOL
            or abs(p[1] - y0) < KANT_TOL or abs(p[1] - y0 - RUTE_M) < KANT_TOL)


# ---------------------------------------------------------------------------
# Graf
# ---------------------------------------------------------------------------

class Graf:
    def __init__(self):
        self.node_id = {}      # nodenøkkel -> heltall
        self.koord = []        # heltall -> (x, y)
        self.nabo = []         # heltall -> [(nabo, vekt, kant_id)]
        self.kant_ferje = []   # kant_id -> bool
        self.kant_hoved = []   # kant_id -> bool (E/R/F)
        self.kant_ender = []   # kant_id -> (node_a, node_b)
        self.kant_geom = None  # kant_id -> [(x, y), ...] (bare når geometri lagres)

    def node(self, p):
        k = nodenokkel(p)
        i = self.node_id.get(k)
        if i is None:
            i = len(self.koord)
            self.node_id[k] = i
            self.koord.append(k)
            self.nabo.append([])
        return i

    def legg_til_kant(self, a, b, vekt, ferje, hoved=False, geom=None):
        e = len(self.kant_ferje)
        if self.kant_geom is not None:
            self.kant_geom.append(geom)
        self.kant_ferje.append(ferje)
        self.kant_hoved.append(hoved and not ferje)
        self.kant_ender.append((a, b))
        self.nabo[a].append((b, vekt, e))
        self.nabo[b].append((a, vekt, e))
        return e

    def korteste(self, s, t, sperret=frozenset(), grense=math.inf, med_sti=False):
        """
        Toveis Dijkstra fra s til t uten kantene i `sperret`.
        Returnerer (avstand, brukte_ferje), eller (avstand, brukte_ferje,
        [kant_id]) med med_sti=True. avstand = inf hvis ingen vei kortere
        enn `grense`.
        """
        if s == t:
            return (0.0, False, []) if med_sti else (0.0, False)
        dist = ({s: 0.0}, {t: 0.0})
        forr = ({s: None}, {t: None})       # node -> (forrige node, kant_id)
        ferdig = (set(), set())
        koer = ([(0.0, s)], [(0.0, t)])
        beste, moete = math.inf, None

        while koer[0] and koer[1]:
            if koer[0][0][0] + koer[1][0][0] >= min(beste, grense):
                break
            r = 0 if koer[0][0][0] <= koer[1][0][0] else 1
            d, u = heapq.heappop(koer[r])
            if u in ferdig[r]:
                continue
            ferdig[r].add(u)
            for v, w, e in self.nabo[u]:
                if e in sperret:
                    continue
                nd = d + w
                if nd < dist[r].get(v, math.inf):
                    dist[r][v] = nd
                    forr[r][v] = (u, e)
                    heapq.heappush(koer[r], (nd, v))
                if v in dist[1 - r] and nd + dist[1 - r][v] < beste:
                    beste = nd + dist[1 - r][v]
                    moete = v

        if beste >= grense or moete is None:
            return (math.inf, False, []) if med_sti else (math.inf, False)

        # Følg stien tilbake fra møtepunktet (sjekker også om den bruker ferje)
        ferje, sti = False, []
        for r in (0, 1):
            n = moete
            while forr[r].get(n):
                n, e = forr[r][n]
                ferje = ferje or self.kant_ferje[e]
                sti.append(e)
        return (beste, ferje, sti) if med_sti else (beste, ferje)


# ---------------------------------------------------------------------------
# Innlesing
# ---------------------------------------------------------------------------

def les_rutenett(fylke):
    with open(RUTENETT_GEOJSON, encoding="utf-8") as f:
        gj = json.load(f)
    ruter = {}
    for ft in gj["features"]:
        p = ft["properties"]
        if str(p.get("txtFylkNr")) != fylke:
            continue
        ring = ft["geometry"]["coordinates"][0]
        minx = min(c[0] for c in ring)
        miny = min(c[1] for c in ring)
        ix, iy = round(minx / RUTE_M), round(miny / RUTE_M)
        ruter[(ix, iy)] = {
            "ssbid": str(p["ssbid"]),
            "txtKomNr": str(p.get("txtKomNr", "")),
            "KomNavn": p.get("KomNavn", ""),
            "ring": [c[:2] for c in ring],
        }
    return ruter


def bygg_graf(lagre_geometri=False):
    if not VEGNETT_CSV.exists():
        sys.exit(f"Finner ikke {VEGNETT_CSV}. Kjør 01_hent_vegnett.py først.")

    g = Graf()
    if lagre_geometri:
        g.kant_geom = []
    biter_i_rute = defaultdict(list)          # (ix, iy) -> [kant_id] (stenges)
    kategorier = defaultdict(int)
    n_seg = 0

    with open(VEGNETT_CSV, encoding="utf-8") as f:
        for rad in csv.DictReader(f):
            if (rad.get("fase") or "V").upper() != "V":
                continue  # bare eksisterende veg (ikke under bygging / fiktiv)
            kat = (rad.get("vegkategori") or "").upper()
            if kat not in OMKJORINGSNETT:
                continue
            n_seg += 1
            kategorier[kat] += 1
            er_hovedveg = kat in HOVEDVEG_KATEGORIER
            er_ferje = (rad.get("typeVeg") or "").lower().startswith("bilferje")
            for linje in les_wkt(rad["wkt"]):
                for ix, iy, pts in del_ved_rutenett(linje):
                    a, b = g.node(pts[0]), g.node(pts[-1])
                    if a == b:
                        continue
                    vekt = lengde(pts) + (FERJE_STRAFF_M if er_ferje else 0.0)
                    e = g.legg_til_kant(a, b, vekt, er_ferje, er_hovedveg,
                                        pts if lagre_geometri else None)
                    if er_ferje:
                        # Ferjestrekninger går over sjø: de stenges ikke når en
                        # rute stenges, og gir ikke inngangspunkter. De kan
                        # brukes som omkjøring (men en ferjekai i en stengt rute
                        # blir uten vegforbindelse, fordi landvegen dit stenges).
                        continue
                    biter_i_rute[(ix, iy)].append(e)

    print(f"Leste {n_seg} vegsegmenter (" +
          ", ".join(f"{k or '?'}={v}" for k, v in sorted(kategorier.items())) + ")")
    print(f"Graf: {len(g.koord)} noder, {len(g.kant_ferje)} kanter")
    return g, biter_i_rute


def finn_inngangspunkter(g, rute, kanter):
    """Punkter der en E-, R- eller F-veg krysser kanten av ruta."""
    ix, iy = rute
    return {n for e in kanter if g.kant_hoved[e]
            for n in g.kant_ender[e] if paa_rutekant(g.koord[n], ix, iy)}


# ---------------------------------------------------------------------------
# Beregning
# ---------------------------------------------------------------------------

def utforsk(g, start, sperret, maks_m):
    """
    Finner sammenhengende vegnett fra `start` uten kantene i `sperret`.
    Stopper når samlet veglengde passerer `maks_m`.
    Returnerer (noder, kanter, lengde, fullstendig).
    """
    noder, kanter = {start}, set()
    stakk, total = [start], 0.0
    while stakk:
        u = stakk.pop()
        for v, w, e in g.nabo[u]:
            if e in sperret or e in kanter:
                continue
            kanter.add(e)
            total += w
            if total > maks_m:
                return noder, kanter, total, False
            if v not in noder:
                noder.add(v)
                stakk.append(v)
    return noder, kanter, total, True


def fjern_slyngbiter(g, punkter, sperret):
    """
    Utvider stengingen med slyngbiter og fjerner kryssingspunktene deres.
    Endrer `sperret` direkte. Returnerer (gjenværende punkter, antall fjernet).
    """
    punkter = list(punkter)
    fjernet = 0
    sjekket = set()
    for p in list(punkter):
        if p in sjekket or p not in punkter:
            continue
        noder, kanter, total, fullstendig = utforsk(g, p, sperret, SLYNG_MAKS_M)
        sjekket |= noder
        inne = [q for q in punkter if q in noder]
        if fullstendig and len(inne) >= 2:
            sperret |= kanter
            for q in inne:
                punkter.remove(q)
            fjernet += len(inne)
    return sorted(punkter), fjernet


def analyser_rute(g, kanter, punkter):
    """Returnerer dict med resultat for én rute."""
    punkter = sorted(punkter)
    res = {
        "antall_inngangspunkt": len(punkter),
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
    if not kanter:
        res["status"] = "ingen_veg_i_ruta"
        return res
    if len(punkter) == 0:
        res["status"] = "ingen_hovedveg"
        return res
    if len(punkter) == 1:
        res["status"] = "hovedveg_ender_i_ruta"
        return res

    sperret = set(kanter)
    punkter, n_slyng = fjern_slyngbiter(g, punkter, sperret)
    res["antall_slyngpunkt"] = n_slyng
    res["antall_inngangspunkt"] = len(punkter)
    if len(punkter) < 2:
        res["status"] = "hovedveg_ender_i_ruta"
        return res
    sperret = frozenset(sperret)
    grense_m = SOK_GRENSE_KM * 1000

    # Porter: inngangspunkter som fortsatt når hverandre innen PORT_M når ruta
    # er stengt (f.eks. en lomme/sløyfe som går ut og inn igjen like ved kanten),
    # er i praksis samme vei inn i ruta og slås sammen.
    port = list(range(len(punkter)))

    def portrot(i):
        while port[i] != i:
            port[i] = port[port[i]]
            i = port[i]
        return i

    for i in range(len(punkter)):
        for j in range(i + 1, len(punkter)):
            d, _ = g.korteste(punkter[i], punkter[j], sperret, grense=PORT_M)
            if not math.isinf(d):
                port[portrot(i)] = portrot(j)
    res["antall_porter"] = len({portrot(i) for i in range(len(punkter))})

    alle_par = []  # (ulike_porter, (ekstra_m eller inf, normal_m, ferje))
    for i in range(len(punkter)):
        for j in range(i + 1, len(punkter)):
            s, t = punkter[i], punkter[j]
            d0, _ = g.korteste(s, t)
            if math.isinf(d0):
                continue  # aldri forbundet (f.eks. to ulike vegnett) - ikke relevant
            d1, ferje = g.korteste(s, t, sperret, grense=d0 + grense_m)
            if not math.isinf(d1) and d1 <= d0 + 0.5:
                continue  # paret påvirkes ikke av stengingen (kjører utenom ruta)
            alle_par.append((portrot(i) != portrot(j),
                             (d1 - d0 if not math.isinf(d1) else math.inf, d0, ferje, (s, t))))

    # Bruk bare par mellom ulike porter, om det finnes noen
    mellom = [p for ulike, p in alle_par if ulike]
    par = mellom if mellom else [p for _, p in alle_par]

    res["antall_par"] = len(par)
    res["par_uten_omkjoring"] = sum(math.isinf(e) for e, *_ in par)
    if not par:
        res["status"] = "ikke_paavirket"
        return res

    funnet = [p for p in par if not math.isinf(p[0])]
    if funnet:
        res["ekstra_min_m"] = round(min(e for e, *_ in funnet), 1)
        res["ekstra_maks_m"] = round(max(e for e, *_ in funnet), 1)

    if AGGREGERING == "korteste":
        # Ruta har omkjøring hvis minst ett par har det; bruk den korteste.
        if funnet:
            valgt = min(funnet)
            res["status"] = "ok"
        else:
            valgt = None
            res["status"] = "ingen_omkjoring"
    else:  # "lengste": verste tilfelle; ingen omkjøring hvis ett par mangler
        if res["par_uten_omkjoring"]:
            valgt = None
            res["status"] = "ingen_omkjoring"
        else:
            valgt = max(funnet)
            res["status"] = "ok"

    if valgt:
        res["ekstra_valgt_m"] = round(valgt[0], 1)
        res["normal_m"] = round(valgt[1], 1)
        res["omkjoring_via_ferje"] = valgt[2]
        res["_par"] = valgt[3]          # internt: valgt par (brukes til visning)
    res["_sperret"] = sperret           # internt: stengte kanter
    return res


def omkjoringsrute(g, biter_i_rute, rute):
    """
    Beregner omkjøringen for én rute og returnerer stiene, til visning.
    Krever en graf bygd med bygg_graf(lagre_geometri=True).
    Returnerer dict med resultat, "omkjoring" (kant_id-er), "normal"
    (kant_id-er) og "punkter" ((x, y) for start og slutt).
    """
    kanter = biter_i_rute.get(rute, [])
    res = analyser_rute(g, kanter, finn_inngangspunkter(g, rute, kanter))
    ut = {"res": res, "omkjoring": [], "normal": [], "punkter": []}
    if "_par" in res:
        s, t = res["_par"]
        _, _, ut["omkjoring"] = g.korteste(s, t, res["_sperret"], med_sti=True)
        _, _, ut["normal"] = g.korteste(s, t, med_sti=True)
        ut["punkter"] = [g.koord[s], g.koord[t]]
    return ut


KOLONNER = [
    "ssbid", "txtKomNr", "KomNavn", "status", "sOmkj_2050",
    "ekstra_valgt_m", "ekstra_min_m", "ekstra_maks_m", "normal_m",
    "antall_inngangspunkt", "antall_porter", "antall_slyngpunkt", "antall_par", "par_uten_omkjoring",
    "omkjoring_via_ferje",
]


def main():
    p = argparse.ArgumentParser(description="Beregn omkjøringsindikator per rute.")
    p.add_argument("--fylke", default=FYLKE)
    p.add_argument("--maks-ruter", type=int, default=None, help="Bare de N første rutene (testing)")
    p.add_argument("--vegkategorier", nargs="+", default=None,
                   help="Overstyr omkjøringsnettet, f.eks. --vegkategorier E R F K P")
    args = p.parse_args()
    if args.vegkategorier:
        OMKJORINGSNETT.clear()
        OMKJORINGSNETT.update(k.upper() for k in args.vegkategorier)
    print(f"Omkjøringsnett: {', '.join(sorted(OMKJORINGSNETT))}   "
          f"Inngangspunkter: {', '.join(sorted(HOVEDVEG_KATEGORIER))}")

    t0 = time.time()
    ruter = les_rutenett(args.fylke)
    print(f"Rutenett: {len(ruter)} ruter i fylke {args.fylke}")
    g, biter_i_rute = bygg_graf()

    nokler = sorted(ruter, key=lambda k: ruter[k]["ssbid"])
    if args.maks_ruter:
        nokler = nokler[: args.maks_ruter]

    resultater = []
    t1 = time.time()
    for n, k in enumerate(nokler, 1):
        kanter = biter_i_rute.get(k, [])
        punkter = finn_inngangspunkter(g, k, kanter)
        res = analyser_rute(g, kanter, punkter)
        resultater.append({**{c: ruter[k][c] for c in ("ssbid", "txtKomNr", "KomNavn")}, **res})

        if n % 200 == 0 or n == len(nokler):
            brukt = time.time() - t1
            igjen = brukt / n * (len(nokler) - n)
            print(f"  {n}/{len(nokler)} ruter  ({brukt:.0f} s, ca. {igjen:.0f} s igjen)")

    # Sett indikatorverdi. Taket = lengste reelle omkjøring (eller TAK_KM).
    reelle = [r["ekstra_valgt_m"] / 1000 for r in resultater if r["status"] == "ok"]
    tak = TAK_KM if TAK_KM is not None else (max(reelle) if reelle else SOK_GRENSE_KM)
    for r in resultater:
        if r["status"] == "ok":
            r["sOmkj_2050"] = round(min(r["ekstra_valgt_m"] / 1000, tak), 3)
        elif r["status"] == "ingen_omkjoring":
            r["sOmkj_2050"] = round(tak, 3)
        elif r["status"] == "ikke_paavirket":
            r["sOmkj_2050"] = 0.0  # stengingen forlenger ingen reise
        else:
            r["sOmkj_2050"] = None
    print(f"\nTak for ruter uten omkjøring: {tak:.1f} km"
          + (" (lengste reelle omkjøring)" if TAK_KM is None else " (satt manuelt)"))

    ut = DATA_DIR / f"omkjoring_fylke{args.fylke}.csv"
    with open(ut, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=KOLONNER, extrasaction="ignore")
        w.writeheader()
        w.writerows(resultater)
    print(f"\nSkrev {ut}")

    skriv_esri_json(resultater, ruter, nokler, args.fylke)
    oppsummer(resultater, tak)
    print(f"\nFerdig på {time.time() - t0:.0f} s")


def skriv_esri_json(resultater, ruter, nokler, fylke):
    typer = {
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
    for res, k in zip(resultater, nokler):
        attr = {c: res.get(c) for c in typer}
        attr["omkjoring_via_ferje"] = int(bool(attr["omkjoring_via_ferje"]))
        features.append({
            "attributes": attr,
            "geometry": {"rings": [list(reversed(ruter[k]["ring"]))]},  # Esri: med klokka
        })
    ut_dir = DATA_DIR / "arcgis"
    ut_dir.mkdir(parents=True, exist_ok=True)
    ut = ut_dir / f"omkjoring_fylke{fylke}.json"
    with open(ut, "w", encoding="utf-8") as f:
        json.dump({
            "geometryType": "esriGeometryPolygon",
            "spatialReference": {"wkid": 25833},
            "fields": [{"name": n, "type": t, "alias": n, **({"length": 100} if t.endswith("String") else {})}
                       for n, t in typer.items()],
            "features": features,
        }, f, ensure_ascii=False)
    print(f"Skrev {ut} (ArcGIS Pro: JSON To Features)")


def oppsummer(resultater, tak):
    from collections import Counter
    print("\nOppsummering")
    for s, n in Counter(r["status"] for r in resultater).most_common():
        print(f"  {s}: {n}")
    verdier = sorted(r["sOmkj_2050"] for r in resultater if r["sOmkj_2050"] is not None)
    if verdier:
        def q(p):
            return verdier[min(len(verdier) - 1, int(p * len(verdier)))]
        print(f"  sOmkj_2050 (km): median {q(0.5):.1f}, 75 % {q(0.75):.1f}, "
              f"90 % {q(0.9):.1f}, maks {verdier[-1]:.1f}")
        print(f"  ruter uten omkjøring (= taket, {tak:.1f} km): "
              f"{sum(r['status'] == 'ingen_omkjoring' for r in resultater)}")
        print(f"  omkjøring via ferje: {sum(bool(r['omkjoring_via_ferje']) for r in resultater)}")


if __name__ == "__main__":
    main()