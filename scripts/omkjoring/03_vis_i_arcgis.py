"""
Steg 5b: Visualiser omkjøringsresultatet i ArcGIS Pro.

Kjøres i ArcGIS Pro sitt Python-vindu (Analysis -> Python -> Python Window),
med et prosjekt og et kart åpent:

    exec(open(r"C:\\Users\\aasne\\Prosjektoppgave\\klimarisk-grid\\klimarisk-grid\\scripts\\omkjoring\\03_vis_i_arcgis.py", encoding="utf-8").read())

Skriptet
  1. importerer omkjoring_fylke55.json og vegnett.json til prosjektets geodatabase
  2. lager feltet "klasse" (omkjøringsklasse) på rutene
  3. legger begge lagene i kartet med ferdig symbologi
  4. zoomer til Troms

Kjør 02_beregn_omkjoring.py (og til_arcgis.py for vegnettet) først.
"""

import os

import arcpy

# ---------------------------------------------------------------------------
# Innstillinger
# ---------------------------------------------------------------------------

REPO = r"C:\Users\aasne\Prosjektoppgave\klimarisk-grid\klimarisk-grid"
JSON_DIR = os.path.join(REPO, "scripts", "omkjoring", "data", "arcgis")
FYLKE = "55"

# Klassegrenser for ekstra kjørelengde (km). Endre om du vil.
GRENSER_KM = [10, 50, 100, 200]

# Farger (RGB). Gul -> mørk rød for økende omkjøring, lilla for ingen omkjøring.
FARGE_KLASSER = [
    [255, 255, 178],
    [254, 204, 92],
    [253, 141, 60],
    [240, 59, 32],
    [189, 0, 38],
]
FARGE_INGEN_OMKJORING = [84, 39, 143]
FARGE_IKKE_VURDERT = [200, 200, 200]

FARGE_VEG = {
    "E": ([40, 40, 40], 1.6),
    "R": ([40, 40, 40], 1.3),
    "F": ([90, 90, 90], 1.0),
    "K": ([150, 150, 150], 0.5),
    "P": ([200, 200, 200], 0.4),
}


# ---------------------------------------------------------------------------

def klassenavn():
    navn = []
    nedre = 0
    for i, g in enumerate(GRENSER_KM, 1):
        navn.append(f"{i} {nedre}–{g} km ekstra")
        nedre = g
    navn.append(f"{len(GRENSER_KM) + 1} Over {nedre} km ekstra")
    navn.append(f"{len(GRENSER_KM) + 2} Ingen omkjøring")
    navn.append(f"{len(GRENSER_KM) + 3} Ikke vurdert")
    return navn


KLASSER = klassenavn()

KODEBLOKK = f"""
GRENSER = {GRENSER_KM!r}
KLASSER = {KLASSER!r}

def klasse(status, km):
    if status == "ingen_omkjoring":
        return KLASSER[-2]
    if status != "ok" or km is None:
        return KLASSER[-1]
    for i, g in enumerate(GRENSER):
        if km < g:
            return KLASSER[i]
    return KLASSER[len(GRENSER)]
"""


def importer(json_navn, fc_navn):
    kilde = os.path.join(JSON_DIR, json_navn)
    if not os.path.exists(kilde):
        raise FileNotFoundError(f"Finner ikke {kilde}")
    ut = os.path.join(aprx.defaultGeodatabase, fc_navn)
    arcpy.conversion.JSONToFeatures(kilde, ut)
    print(f"Importerte {json_navn} -> {ut}")
    return ut


def rgb(c):
    return {"RGB": list(c) + [100]}


def sett_rute_symbologi(lyr):
    sym = lyr.symbology
    sym.updateRenderer("UniqueValueRenderer")
    sym.renderer.fields = ["klasse"]
    farger = dict(zip(KLASSER, FARGE_KLASSER + [FARGE_INGEN_OMKJORING, FARGE_IKKE_VURDERT]))
    for grp in sym.renderer.groups:
        for itm in grp.items:
            verdi = itm.values[0][0]
            if verdi in farger:
                itm.symbol.color = rgb(farger[verdi])
                itm.symbol.outlineColor = {"RGB": [0, 0, 0, 0]}
                itm.label = verdi.split(" ", 1)[1]  # fjern sorteringsnummeret i forklaringen
    lyr.symbology = sym


def sett_veg_symbologi(lyr):
    sym = lyr.symbology
    sym.updateRenderer("UniqueValueRenderer")
    sym.renderer.fields = ["vegkategori"]
    for grp in sym.renderer.groups:
        for itm in grp.items:
            kat = itm.values[0][0]
            farge, bredde = FARGE_VEG.get(kat, ([180, 180, 180], 0.5))
            itm.symbol.color = rgb(farge)
            itm.symbol.width = bredde
    lyr.symbology = sym


def enkel_veg_symbologi(lyr):
    sym = lyr.symbology
    sym.updateRenderer("SimpleRenderer")
    sym.renderer.symbol.color = rgb([90, 90, 90])
    sym.renderer.symbol.width = 0.6
    lyr.symbology = sym


def med_nye_forsok(funksjon, lyr, navn, reserve=None):
    """arcpy klarer av og til ikke å endre symbologi rett etter at laget er lagt til."""
    for forsok in range(3):
        try:
            funksjon(lyr)
            return True
        except Exception as e:  # noqa: BLE001
            siste_feil = e
    if reserve:
        try:
            reserve(lyr)
            print(f"Merk: brukte enkel symbologi for '{navn}' ({siste_feil}).")
            return False
        except Exception:  # noqa: BLE001
            pass
    print(f"Merk: klarte ikke å sette symbologi for '{navn}' ({siste_feil}). "
          "Laget ligger i kartet; sett symbologi manuelt.")
    return False


# ---------------------------------------------------------------------------

arcpy.env.overwriteOutput = True
aprx = arcpy.mp.ArcGISProject("CURRENT")
kart = aprx.activeMap or aprx.listMaps()[0]

# Fjern lag fra en tidligere kjøring, så de ikke legges inn to ganger
for gammelt in kart.listLayers():
    if gammelt.name in (f"omkjoring_fylke{FYLKE}", "vegnett"):
        kart.removeLayer(gammelt)

ruter_fc = importer(f"omkjoring_fylke{FYLKE}.json", f"omkjoring_fylke{FYLKE}")
arcpy.management.AddField(ruter_fc, "klasse", "TEXT", field_length=40)
arcpy.management.CalculateField(
    ruter_fc, "klasse", "klasse(!status!, !sOmkj_2050!)", "PYTHON3", KODEBLOKK
)

veg_fc = importer("vegnett.json", "vegnett")

ruter_lyr = kart.addDataFromPath(ruter_fc)
med_nye_forsok(sett_rute_symbologi, ruter_lyr, f"omkjoring_fylke{FYLKE}")

veg_lyr = kart.addDataFromPath(veg_fc)   # legges øverst, over rutene
med_nye_forsok(sett_veg_symbologi, veg_lyr, "vegnett", reserve=enkel_veg_symbologi)
try:
    veg_lyr.transparency = 20
except Exception:  # noqa: BLE001
    pass

try:
    aprx.activeView.camera.setExtent(arcpy.Describe(ruter_fc).extent)
except Exception:  # noqa: BLE001 - zoom er bare til hjelp
    pass

# Oppsummering per klasse
teller = {}
with arcpy.da.SearchCursor(ruter_fc, ["klasse"]) as cur:
    for (k,) in cur:
        teller[k] = teller.get(k, 0) + 1
print("\nRuter per klasse:")
for k in KLASSER:
    print(f"  {k.split(' ', 1)[1]}: {teller.get(k, 0)}")
print("\nFerdig. Lagene 'omkjoring_fylke55' og 'vegnett' er lagt til i kartet.")
