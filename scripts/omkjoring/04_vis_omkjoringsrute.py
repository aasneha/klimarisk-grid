"""
Vis omkjøringsruta for valgte ruter i ArcGIS Pro.

Bruk (i ArcGIS Pro sitt Python-vindu, med laget "omkjoring_fylke55" i kartet):

  1. Kjør én gang per økt (definerer funksjonen vis()):
       exec(open(r"C:\\Users\\aasne\\Prosjektoppgave\\klimarisk-grid\\klimarisk-grid\\scripts\\omkjoring\\04_vis_omkjoringsrute.py", encoding="utf-8").read())

  2. Velg én eller flere ruter med Select-verktøyet (Map -> Select).
  3. Skriv  vis()  og trykk Enter. (Neste gang holder det med pil opp + Enter.)

Da tegnes laget "omkjoringsrute" med
  - Omkjøring:         korteste veg rundt den stengte ruta (tykk magenta)
  - Stengt strekning:  den normale vegen gjennom ruta (tykk svart)
og kartet zoomer til ruta. Første kall laster vegnettet (15-30 s).
"""

import importlib.util
import os
import time

import arcpy

REPO = r"C:\Users\aasne\Prosjektoppgave\klimarisk-grid\klimarisk-grid"
BEREGNING = os.path.join(REPO, "scripts", "omkjoring", "02_beregn_omkjoring.py")
RUTELAG = "omkjoring_fylke55"
UT_NAVN = "omkjoringsrute"
SR = arcpy.SpatialReference(25833)

FARGER = {
    "Omkjøring": ([230, 0, 169], 3.5),
    "Stengt strekning": ([0, 0, 0], 3.5),
}


def _last_vegnett():
    """Laster beregningsskriptet og vegnettet én gang per økt."""
    global _OMKJ_CACHE
    if globals().get("_OMKJ_CACHE"):
        return _OMKJ_CACHE
    print("Laster vegnettet (første gang tar litt tid) ...")
    t0 = time.time()
    spec = importlib.util.spec_from_file_location("omkj", BEREGNING)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    g, biter = m.bygg_graf(lagre_geometri=True)
    _OMKJ_CACHE = (m, g, biter)
    print(f"Vegnettet er lastet ({time.time() - t0:.0f} s).")
    return _OMKJ_CACHE


def _linje(g, kanter):
    deler = arcpy.Array()
    for e in kanter:
        deler.add(arcpy.Array([arcpy.Point(x, y) for x, y in g.kant_geom[e]]))
    return arcpy.Polyline(deler, SR)


def _klargjor_featureclass(aprx):
    fc = os.path.join(aprx.defaultGeodatabase, UT_NAVN)
    if arcpy.Exists(fc):
        arcpy.management.TruncateTable(fc)
    else:
        arcpy.management.CreateFeatureclass(aprx.defaultGeodatabase, UT_NAVN, "POLYLINE",
                                            spatial_reference=SR)
        arcpy.management.AddField(fc, "ssbid", "TEXT", field_length=20)
        arcpy.management.AddField(fc, "type", "TEXT", field_length=30)
        arcpy.management.AddField(fc, "km", "DOUBLE")
        arcpy.management.AddField(fc, "status", "TEXT", field_length=40)
    return fc


def _sett_symbologi(lyr):
    for _ in range(3):
        try:
            sym = lyr.symbology
            sym.updateRenderer("UniqueValueRenderer")
            sym.renderer.fields = ["type"]
            for grp in sym.renderer.groups:
                for itm in grp.items:
                    farge, bredde = FARGER.get(itm.values[0][0], ([255, 0, 0], 3))
                    itm.symbol.color = {"RGB": farge + [100]}
                    itm.symbol.width = bredde
            lyr.symbology = sym
            return
        except Exception:  # noqa: BLE001 - arcpy er av og til ustabil her
            continue
    print("Merk: klarte ikke å sette farger automatisk; sett symbologi på feltet 'type' manuelt.")


def vis():
    m, g, biter = _last_vegnett()
    aprx = arcpy.mp.ArcGISProject("CURRENT")
    kart = aprx.activeMap or aprx.listMaps()[0]

    lagliste = kart.listLayers(RUTELAG)
    if not lagliste:
        print(f"Finner ikke laget '{RUTELAG}' i kartet. Kjør 03_vis_i_arcgis.py først.")
        return
    rutelag = lagliste[0]
    if not rutelag.getSelectionSet():
        print("Ingen rute er valgt. Velg en rute med Select-verktøyet (Map -> Select) og kjør vis() igjen.")
        return

    valgte = []
    with arcpy.da.SearchCursor(rutelag, ["ssbid", "SHAPE@"]) as cur:   # følger utvalget
        for ssbid, shp in cur:
            ext = shp.extent
            valgte.append((ssbid, (round(ext.XMin / 1000), round(ext.YMin / 1000))))

    fc = _klargjor_featureclass(aprx)
    utstrekning = None
    with arcpy.da.InsertCursor(fc, ["SHAPE@", "ssbid", "type", "km", "status"]) as ic:
        for ssbid, rute in valgte:
            u = m.omkjoringsrute(g, biter, rute)
            res = u["res"]
            status = res["status"]
            if u["omkjoring"]:
                omk = _linje(g, u["omkjoring"])
                ic.insertRow((omk, ssbid, "Omkjøring", res["ekstra_valgt_m"] / 1000, status))
                ic.insertRow((_linje(g, u["normal"]), ssbid, "Stengt strekning",
                              res["normal_m"] / 1000, status))
                ext = omk.extent
                utstrekning = ext if utstrekning is None else arcpy.Extent(
                    min(utstrekning.XMin, ext.XMin), min(utstrekning.YMin, ext.YMin),
                    max(utstrekning.XMax, ext.XMax), max(utstrekning.YMax, ext.YMax),
                    spatial_reference=SR)
                ferje = " (via ferje)" if res.get("omkjoring_via_ferje") else ""
                print(f"{ssbid}: omkjøring {res['ekstra_valgt_m'] / 1000:.1f} km ekstra{ferje}")
            elif status == "ingen_omkjoring":
                print(f"{ssbid}: ingen omkjøring - stenging av ruta isolerer en del av vegnettet")
            else:
                print(f"{ssbid}: {status.replace('_', ' ')} - ingen omkjøring å vise")

    if not kart.listLayers(UT_NAVN):
        lyr = kart.addDataFromPath(fc)
        _sett_symbologi(lyr)

    if utstrekning is not None:
        try:
            buffer = max(utstrekning.width, utstrekning.height) * 0.1
            aprx.activeView.camera.setExtent(arcpy.Extent(
                utstrekning.XMin - buffer, utstrekning.YMin - buffer,
                utstrekning.XMax + buffer, utstrekning.YMax + buffer, spatial_reference=SR))
        except Exception:  # noqa: BLE001 - zoom er bare til hjelp
            pass


print("Klar. Velg en rute med Select-verktøyet og skriv  vis()  for å vise omkjøringen.")
