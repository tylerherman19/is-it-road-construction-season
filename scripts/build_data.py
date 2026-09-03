#!/usr/bin/env python3
"""Build data.json for is-it-road-construction-season.

Pulls MnDOT 511 (CARS), State Aid, and county/city ArcGIS feeds,
normalizes into one event schema, computes distance rings around
the origin, and writes data.json. Fail-soft per source.
Sources and field maps: see BUILD_MEMO.md (research memo, 3 Sep 2026).
"""
import json, math, os, re, sys, time, datetime as dt
import requests
from shapely.geometry import shape, Point, LineString, MultiLineString, mapping
from shapely.ops import transform as shp_transform
from pyproj import Transformer

ORIGIN = (-93.4594, 45.0394)  # lon, lat - Plymouth MN 55446 (the house)
RINGS_MI = [5, 10, 20, 50]
MAX_MI = 50.0
UA = "is-it-road-construction-season/1.0 (+https://github.com/tylerherman19/is-it-road-construction-season)"
NOW = dt.datetime.now(dt.timezone.utc)

to26915 = Transformer.from_crs("EPSG:4326", "EPSG:26915", always_xy=True).transform
ORIGIN_M = shp_transform(to26915, Point(ORIGIN))

SOURCES_STATUS = []

def get(url, params=None, timeout=60, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout,
                             headers={"User-Agent": UA, "Accept": "application/json"})
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as ex:
            last = str(ex)
        time.sleep(2 + i * 3)
    raise RuntimeError(f"{url}: {last}")

def parse_dt(v):
    """Esri epoch ms, epoch s, or ISO string -> aware datetime or None."""
    if v is None: return None
    if isinstance(v, (int, float)):
        if v <= 0: return None
        if v > 1e12: v = v / 1000.0
        return dt.datetime.fromtimestamp(v, tz=dt.timezone.utc)
    if isinstance(v, str):
        s = v.strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
            try: return dt.datetime.strptime(s[:19], fmt).replace(tzinfo=dt.timezone.utc)
            except ValueError: pass
    return None

def temporal(start, end, undated="season"):
    if end and end < NOW: return "expired"
    if start and start > NOW: return "upcoming"
    if start or end: return "active"
    return undated

def dist_mi(geom):
    if geom is None or geom.is_empty: return None
    g = shp_transform(to26915, geom)
    return ORIGIN_M.distance(g) / 1609.344

def simplify(geom):
    try:
        g = geom.simplify(0.0001, preserve_topology=True)
        return json.loads(json.dumps(mapping(g), default=float))
    except Exception:
        return mapping(geom)

def round_coords(obj, n=5):
    if isinstance(obj, (list, tuple)):
        return [round_coords(x, n) for x in obj]
    if isinstance(obj, float):
        return round(obj, n)
    return obj

EVENTS = []
def add_event(**kw):
    geom = kw.pop("geometry", None)
    if geom is None or geom.is_empty: return
    d = dist_mi(geom)
    if d is None or d > MAX_MI: return
    if kw.get("temporal") == "expired": return
    g = simplify(geom)
    if "coordinates" in g: g["coordinates"] = round_coords(g["coordinates"])
    ring = next((r for r in RINGS_MI if d <= r), None)
    EVENTS.append({**kw, "distance_mi": round(d, 2), "ring": ring, "geometry": g})

# ---------------- Tier 1: MnDOT 511 via CARS ----------------
CLOSED_CODES = {"closed", "road closure", "bridge is closed", "intersection closed",
                "exit ramp closed", "entrance ramp closed"}
CONSTR_CODES = {"construction work", "road construction", "bridge construction",
                "road maintenance operations", "bridge maintenance operations",
                "utility work", "night time construction work", "paving operations",
                "head to head traffic on the roadway", "portable traffic signals in use",
                "lane closed", "reduced to one lane", "reduced to two lanes",
                "intermittent lane closure", "shoulder closed"}

def cars_class(cat, code):
    if cat == "closure" and code in CLOSED_CODES: return "closed"
    if cat == "closure": return "construction"
    if cat == "roadwork": return "construction"
    if cat == "mobile-situation": return "construction"
    if cat == "restriction": return "restriction"
    if cat in ("incident", "obstruction"): return "incident"
    return None  # mdss-conditions etc: skip

def cars_geometry(details):
    segs = []
    for det in details:
        for loc in det.get("locations", []):
            p = loc.get("primary-location", {}).get("geo-location", {})
            s = (loc.get("secondary-location") or {}).get("geo-location", {})
            if "latitude" not in p: continue
            a = (p["longitude"], p["latitude"])
            if s and "latitude" in s:
                b = (s["longitude"], s["latitude"])
                if a != b: segs.append((a, b))
                else: segs.append(a)
            else:
                segs.append(a)
    if not segs: return None
    lines = [sg for sg in segs if isinstance(sg, tuple) and len(sg) == 2 and isinstance(sg[0], tuple)]
    pts = [sg for sg in segs if not (isinstance(sg, tuple) and len(sg) == 2 and isinstance(sg[0], tuple))]
    geoms = []
    if len(lines) == 1: geoms.append(LineString(lines[0]))
    elif lines: geoms.append(MultiLineString(lines))
    geoms += [Point(p) for p in pts]
    if not geoms: return None
    if len(geoms) == 1: return geoms[0]
    from shapely.geometry import GeometryCollection
    return GeometryCollection(geoms)

def load_511():
    try:
        data = get("https://mn.carsprogram.org/carsapi_v1/api/events")
        src, via = data, "cars_rest"
    except Exception as ex:
        # Fallback: WZDx (CC0)
        data = get("https://mn.carsprogram.org/carsapi_v1/api/wzdx")
        src, via = data, "wzdx"
        return load_wzdx(src)
    n_in = n_out = 0
    seen = {}
    for e in src:
        h = e.get("headline", {})
        cls = cars_class(h.get("category"), h.get("code"))
        if cls is None: continue
        desc = e.get("description") or ""
        # cross-tier dedupe: same category + direction-stripped description = one event
        # (directional splits like "I-394 eastbound"/"westbound" are one closure)
        norm = re.sub(r"\b(north|south|east|west)bound\b", "", desc.lower())
        norm = re.sub(r"\bin both directions\b", "", norm)
        key = (h.get("category"), norm[:80])
        start = end = None
        route = None
        for det in e.get("details", []):
            st = parse_dt((det.get("start-time") or {}).get("time"))
            en = parse_dt((det.get("end-time") or {}).get("time"))
            if st and (start is None or st < start): start = st
            if en and (end is None or en > end): end = en
            if not route:
                for loc in det.get("locations", []):
                    route = loc.get("route-designator")
                    if route: break
        geom = cars_geometry(e.get("details", []))
        if geom is None: continue
        tmp = temporal(start, end, undated="active")
        n_in += 1
        if key in seen:
            old = EVENTS[seen[key]]
            from shapely.geometry import shape as shp_shape, GeometryCollection
            merged = shp_shape(old["geometry"]).union(geom)
            old["geometry"] = simplify(merged)
            old["geometry"]["coordinates"] = round_coords(old["geometry"].get("coordinates", [])) if "coordinates" in old["geometry"] else old["geometry"].get("geometries")
            d = dist_mi(shp_shape(old["geometry"]))
            old["distance_mi"] = round(d, 2); old["ring"] = next((r for r in RINGS_MI if d <= r), None)
            continue
        before = len(EVENTS)
        add_event(id=f"cars:{e.get('event-id')}", source="MnDOT 511", event_class=cls,
                  temporal=tmp, road=route or "State highway", description=desc,
                  start=start.isoformat() if start else None,
                  end=end.isoformat() if end else None,
                  url=f"https://511mn.org/event/{e.get('event-id')}", geometry=geom)
        if len(EVENTS) > before:
            seen[key] = len(EVENTS) - 1
            n_out += 1
    SOURCES_STATUS.append({"name": "MnDOT 511 (CARS events)", "ok": True, "records": n_out, "via": via})
    return True

def load_wzdx(data):
    feats = data.get("features", [])
    n = 0
    for f in feats:
        p = f.get("properties", {})
        core = p.get("core_details", {})
        vt = core.get("vehicle_impact", "")
        et = core.get("event_type", "")
        cls = "closed" if vt == "all-lanes-closed" else ("construction" if et == "work-zone" else "restriction")
        start, end = parse_dt(core.get("start_date")), parse_dt(core.get("end_date"))
        tmp = temporal(start, end, undated="active")
        try: geom = shape(f.get("geometry"))
        except Exception: continue
        roads = core.get("road_names") or []
        before = len(EVENTS)
        add_event(id=f"wzdx:{f.get('id')}", source="MnDOT WZDx", event_class=cls,
                  temporal=tmp, road=roads[0] if roads else "State highway",
                  description=core.get("description") or "",
                  start=start.isoformat() if start else None,
                  end=end.isoformat() if end else None,
                  url="https://511mn.org", geometry=geom)
        if len(EVENTS) > before: n += 1
    SOURCES_STATUS.append({"name": "MnDOT 511 (WZDx fallback)", "ok": True, "records": n, "via": "wzdx"})
    return True

# ---------------- ArcGIS helpers ----------------
def arcgis_query(layer_url, where="1=1", buffer_m=None, out_fields="*"):
    params = {"where": where, "outFields": out_fields, "returnGeometry": "true",
              "outSR": 4326, "f": "geojson", "resultRecordCount": 2000}
    if buffer_m:
        params.update({"geometry": f"{ORIGIN[0]},{ORIGIN[1]}",
                       "geometryType": "esriGeometryPoint", "inSR": 4326,
                       "distance": buffer_m, "units": "esriSRUnit_Meter",
                       "spatialRel": "esriSpatialRelIntersects"})
    feats, offset = [], 0
    while True:
        params["resultOffset"] = offset
        data = get(f"{layer_url}/query", params=params)
        if "error" in data: raise RuntimeError(str(data["error"])[:200])
        batch = data.get("features", [])
        feats += batch
        if data.get("exceededTransferLimit") and batch:
            offset += len(batch)
        else:
            break
    return feats

def feat_geom(f):
    g = f.get("geometry")
    if not g: return None
    try: return shape(g)
    except Exception: return None

def props(f): return f.get("properties", {}) or {}

def first_field(p, names):
    for n in names:
        v = p.get(n)
        if v not in (None, "", " "): return str(v)
    return None

BUF50 = 80467.20
def add_arcgis(name, url, klass, road_fields, desc_fields, start_f, end_f,
               where="1=1", buffer_m=BUF50, skip_fn=None, link_f=None, undated="season"):
    try:
        feats = arcgis_query(url, where=where, buffer_m=buffer_m)
    except Exception as ex:
        SOURCES_STATUS.append({"name": name, "ok": False, "error": str(ex)[:160]})
        return
    n = 0
    seen = {}
    for f in feats:
        p = props(f)
        if skip_fn and skip_fn(p): continue
        start, end = parse_dt(p.get(start_f)), parse_dt(p.get(end_f))
        tmp = temporal(start, end, undated=undated)
        geom = feat_geom(f)
        if geom is None: continue
        oid = p.get("OBJECTID") or p.get("GlobalID") or p.get("OBJECTID_1") or n
        road = first_field(p, road_fields) or name
        desc = first_field(p, desc_fields) or ""
        loc = first_field(p, ["LOCATION", "Location", "Project_Location", "Clse_From"])
        if loc and loc.lower() not in desc.lower(): desc = (desc + " " + loc).strip()
        link = p.get(link_f) if link_f else None
        # same-source dedupe: same road + same date window = one project, union geometry
        dkey = (road.lower().strip(), str(start), str(end))
        if dkey in seen and len(EVENTS) > seen[dkey]:
            from shapely.geometry import shape as shp_shape
            old_ev = EVENTS[seen[dkey]]
            merged = shp_shape(old_ev["geometry"]).union(geom)
            g = simplify(merged)
            if "coordinates" in g: g["coordinates"] = round_coords(g["coordinates"])
            old_ev["geometry"] = g
            d = dist_mi(shp_shape(g))
            old_ev["distance_mi"] = round(d, 2)
            old_ev["ring"] = next((r for r in RINGS_MI if d <= r), None)
            continue
        before = len(EVENTS)
        add_event(id=f"{name}:{oid}", source=name, event_class=klass, temporal=tmp,
                  road=road, description=desc,
                  start=start.isoformat() if start else None,
                  end=end.isoformat() if end else None,
                  url=link if isinstance(link, str) and link.startswith("http") else None,
                  geometry=geom)
        if len(EVENTS) > before:
            seen[dkey] = len(EVENTS) - 1
            n += 1
    SOURCES_STATUS.append({"name": name, "ok": True, "records": n})

def run():
    # Tier 1
    try:
        load_511()
    except Exception as ex:
        SOURCES_STATUS.append({"name": "MnDOT 511", "ok": False, "error": str(ex)[:160]})

    # Tier 2: State Aid (current-year program = season projects)
    add_arcgis("MnDOT State Aid", "https://dotapp9.dot.state.mn.us/egis12/rest/services/state_aid/sa_projects/MapServer/0",
               "construction", ["ROAD_NAME"], ["LOCATION", "PROJECT_TYPE"],
               "DIST_EST_WORK_START_DATE", "DIST_EST_WORK_COMPLETE_DATE")

    # Tier 3: counties
    add_arcgis("Anoka County", "https://gisservices.co.anoka.mn.us/anoka_gis/rest/services/Highway_ConstructionFinder_2/FeatureServer/3",
               "closed", ["Road"], ["Description"], "Srt_Date", "EndDate")
    add_arcgis("Anoka County", "https://gisservices.co.anoka.mn.us/anoka_gis/rest/services/Highway_ConstructionFinder_2/FeatureServer/2",
               "closed", ["Road"], ["Description"], "Srt_Date", "EndDate")
    add_arcgis("Carver County", "https://gis.co.carver.mn.us/arcgis_ea/rest/services/OpenAccess/CC_PW_ConstructionAndClosures/MapServer/11",
               "closed", ["StreetName"], ["Project_Description", "Project_Description", "Activity_Narrative"],
               "created_date", "CompleteDate")
    add_arcgis("Carver County", "https://gis.co.carver.mn.us/arcgis_ea/rest/services/OpenAccess/CC_PW_ConstructionAndClosures/MapServer/12",
               "closed", ["StreetName"], ["Activity_Narrative"], "created_date", "CompleteDate")
    add_arcgis("Carver County", "https://gis.co.carver.mn.us/arcgis_ea/rest/services/OpenAccess/CC_PW_ConstructionAndClosures/MapServer/119",
               "construction", ["StreetName"], ["Public_Project_Description", "Project_Description", "Activity_Narrative"],
               "created_date", "CompleteDate")
    add_arcgis("Hennepin County", "https://gis.hennepin.us/arcgis/rest/services/Maps/TRANSPORTATION_PROJECTS/FeatureServer/1",
               "construction", ["Local_Name"], ["Proj_Type", "Location"], "Start_CRN_Dt", "End_CRN_Dt",
               where="PD_Status='Construction - Active'")
    add_arcgis("Dakota County", "https://gis2.co.dakota.mn.us/arcgis/rest/services/Transportation/DC_OL_TRANS_TransportationProjects/MapServer/0",
               "construction", ["ROADNAME"], ["PROJECTWORK", "PROJECT"], "CONST_START", "CONST_FINISH")

    # Tier 4: cities
    add_arcgis("City of St. Paul", "https://services1.arcgis.com/9meaaHE3uiba0zr8/arcgis/rest/services/Public_Works_Road_Closures_(Public_View)/FeatureServer/0",
               "closed", ["Road"], ["Description", "RestrictionDetails"], "StartDate", "EndDate",
               buffer_m=None,
               skip_fn=lambda p: bool(re.search(r"mn/?dot", str(p.get("Owner") or ""), re.I)))
    # St. Paul lane restrictions count as construction
    try:
        feats = arcgis_query("https://services1.arcgis.com/9meaaHE3uiba0zr8/arcgis/rest/services/Public_Works_Road_Closures_(Public_View)/FeatureServer/0")
        n = 0
        for f in feats:
            p = props(f)
            if "lane" not in str(p.get("RestrictionType") or "").lower(): continue
            if re.search(r"mn/?dot", str(p.get("Owner") or ""), re.I): continue
            start, end = parse_dt(p.get("StartDate")), parse_dt(p.get("EndDate"))
            tmp = temporal(start, end)
            geom = feat_geom(f)
            if geom is None: continue
            before = len(EVENTS)
            add_event(id=f"stpaul-lr:{p.get('OBJECTID')}", source="City of St. Paul",
                      event_class="construction", temporal=tmp,
                      road=str(p.get("Road") or "St. Paul street"),
                      description=str(p.get("Description") or p.get("RestrictionDetails") or ""),
                      start=start.isoformat() if start else None,
                      end=end.isoformat() if end else None, url=None, geometry=geom)
            if len(EVENTS) > before: n += 1
    except Exception:
        pass
    # Minneapolis 2026 program layers 1-17 (exclude 21-26: other agencies, dupes of Tiers 1/3)
    mpls_base = "https://services.arcgis.com/afSMGVsC7QlRK1kZ/arcgis/rest/services/2026_Mpls_Construction_Map/FeatureServer"
    try:
        meta = get(mpls_base, params={"f": "json"})
        layer_names = {l["id"]: l["name"] for l in meta.get("layers", [])}
    except Exception:
        layer_names = {}
    for lid in list(range(1, 18)):
        lname = layer_names.get(lid, f"layer {lid}")
        add_arcgis(f"Minneapolis 2026 ({lname})", f"{mpls_base}/{lid}",
                   "construction", ["Project_Location", "PROJECT_LOCATION", "Location", "LOCATION", "Name", "NAME"],
                   ["Project_Type", "PROJECT_TYPE", "Description", "DESCRIPTION", "Scope", "SCOPE"],
                   None, None)
    add_arcgis("City of Minnetonka", "https://services.arcgis.com/vAmq2qjze38HN5HF/arcgis/rest/services/Project_Locations/FeatureServer/1",
               "construction", ["ProjectName", "Name", "NAME"], ["Description", "Location"], None, None,
               where="Status='Under Construction'")
    add_arcgis("City of Minnetonka", "https://services.arcgis.com/vAmq2qjze38HN5HF/arcgis/rest/services/Project_Locations/FeatureServer/0",
               "construction", ["ProjectName", "Name", "NAME"], ["Description", "Location"], None, None,
               where="Status='Under Construction'")
    add_arcgis("City of Golden Valley", "https://gis.goldenvalleymn.gov/server/rest/services/GoldenMap/Golden_Misc/FeatureServer/6",
               "construction", ["PROJNAME"], ["NOTES"], "CONSTRUCTSTART", "CONSTRUCTEND",
               where=f"CIPYEAR={NOW.year}")

    # ---- counts ----
    counts = {}
    for r in RINGS_MI:
        within = [e for e in EVENTS if e["ring"] is not None and e["ring"] <= r]
        counts[str(r)] = {
            "closed": sum(1 for e in within if e["event_class"] == "closed" and e["temporal"] == "active"),
            "construction": sum(1 for e in within if e["event_class"] == "construction" and e["temporal"] in ("active", "season")),
            "starting_soon": sum(1 for e in within if e["temporal"] == "upcoming" and e["event_class"] in ("closed", "construction")),
        }
    c50 = counts["50"]
    answer = "YES" if (c50["closed"] + c50["construction"]) > 0 else "NO"

    out = {
        "generated_at": NOW.isoformat(),
        "origin": {"lon": ORIGIN[0], "lat": ORIGIN[1], "label": "Home, Plymouth MN"},
        "rings_miles": RINGS_MI,
        "answer": answer,
        "counts": counts,
        "sources": SOURCES_STATUS,
        "events": sorted(EVENTS, key=lambda e: e["distance_mi"]),
    }
    dest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.json")
    with open(dest, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(json.dumps({"answer": answer, "counts": counts,
                      "events": len(EVENTS),
                      "sources_ok": sum(1 for s in SOURCES_STATUS if s.get("ok")),
                      "sources_total": len(SOURCES_STATUS)}, indent=1))
    for s in SOURCES_STATUS:
        if not s.get("ok"): print("FAILED:", s["name"], s.get("error"))

if __name__ == "__main__":
    run()
