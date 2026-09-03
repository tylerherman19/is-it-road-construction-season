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
DEDUPED = 0

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

def parse_dt(v, end_of=False):
    """Esri epoch ms, epoch s, bare calendar year, or ISO string -> aware datetime or None.

    Several county layers publish a plain year ("2026", or the integer 2026) where a
    date belongs. Read those as a year rather than handing 2026 to fromtimestamp(),
    which silently yields 1 Jan 1970 and marks every such project expired.
    Values that are neither are None: Dakota's "Spring 2026" and Golden Valley's
    CONSTRUCTEND of "Yes" are prose, and prose is not a date.
    """
    if v is None: return None
    if isinstance(v, bool): return None
    if isinstance(v, (int, float)):
        if v <= 0: return None
        if 1900 <= v <= 2100: return year_dt(int(v), end_of)
        if v > 1e12: v = v / 1000.0
        return dt.datetime.fromtimestamp(v, tz=dt.timezone.utc)
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"\d{4}", s):
            y = int(s)
            if 1900 <= y <= 2100: return year_dt(y, end_of)
        if re.fullmatch(r"-?\d{9,14}", s):  # epoch handed over as a string
            return parse_dt(float(s), end_of)
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%m/%d/%Y"):
            try: return dt.datetime.strptime(s[:19], fmt).replace(tzinfo=dt.timezone.utc)
            except ValueError: pass
    return None

def year_dt(y, end_of=False):
    return (dt.datetime(y, 12, 31, 23, 59, tzinfo=dt.timezone.utc) if end_of
            else dt.datetime(y, 1, 1, tzinfo=dt.timezone.utc))

# Fields that carry a real clock time rather than a calendar date. Everything else
# an agency publishes as a date lands on midnight UTC, and the difference decides
# which zone the page may format it in: an instant is the user's local moment, a
# calendar date is the same day everywhere. Both kinds arrive as Esri epoch
# milliseconds, so only the field name tells them apart.
TIMED_DATE_FIELDS = {
    "Srt_Date", "EndDate", "Startdt", "EndDt",   # Anoka closures
    "StartDate",                                  # St. Paul closures (EndDate shared)
    "CloseDateTime", "OpenDateTime",              # Carver closures
}

def field_dt(p, names, end_of=False):
    """First parseable date among candidate field names, plus whether it is timed."""
    if not names: return None, False
    if isinstance(names, str): names = [names]
    for n in names:
        d = parse_dt(p.get(n), end_of=end_of)
        if d is not None: return d, n in TIMED_DATE_FIELDS
    return None, False

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

def merge_into(ev, geom):
    """Union `geom` into an already-stored event and recompute its distance ring.

    simplify() returns either a coordinates-bearing geometry or a
    GeometryCollection carrying `geometries`; only round the former, or the
    result is a geometry with both keys and no valid GeoJSON reading.
    """
    merged = shape(ev["geometry"]).union(geom)
    g = simplify(merged)
    if "coordinates" in g:
        g["coordinates"] = round_coords(g["coordinates"])
    ev["geometry"] = g
    d = dist_mi(shape(g))
    if d is None: return
    ev["distance_mi"] = round(d, 2)
    ev["ring"] = next((r for r in RINGS_MI if d <= r), None)

def add_event(**kw):
    geom = kw.pop("geometry", None)
    if geom is None or geom.is_empty: return
    # Calendar dates are the common case, so only the exceptions ride in the payload.
    if not kw.pop("timed", False): kw.pop("timed", None)
    else: kw["timed"] = True
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
    return None  # incidents, obstructions, mdss-conditions: not construction, skip

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
    # The CARS events payload is ~9 MB, so give it room; note why we fell down the
    # ladder rather than swallowing it, otherwise a silent demotion to the coarser
    # point-geometry mirror looks identical to a healthy run.
    try:
        data = get("https://mn.carsprogram.org/carsapi_v1/api/events", timeout=240, retries=2)
        src, via = data, "cars_rest"
    except Exception as ex:
        why = str(ex)[:160]
        try:
            # Fallback 1: WZDx (CC0)
            data = get("https://mn.carsprogram.org/carsapi_v1/api/wzdx", timeout=120, retries=1)
            return load_wzdx(data, degraded_from=why)
        except Exception as ex2:
            # Fallback 2: Iowa DOT ArcGIS mirror of the same CARS database
            # (point geometry, 10-minute cycle - memo section 1d / fallback ladder)
            return load_mirror(degraded_from=f"{why}; wzdx: {str(ex2)[:80]}")
    n_out = 0
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
        if key in seen:
            merge_into(EVENTS[seen[key]], geom)
            continue
        before = len(EVENTS)
        add_event(id=f"cars:{e.get('event-id')}", source="MnDOT 511", event_class=cls,
                  temporal=tmp, road=route or "State highway", description=desc,
                  start=start.isoformat() if start else None,
                  end=end.isoformat() if end else None, timed=True,
                  url=f"https://511mn.org/event/{e.get('event-id')}", geometry=geom)
        if len(EVENTS) > before:
            seen[key] = len(EVENTS) - 1
            n_out += 1
    SOURCES_STATUS.append({"name": "MnDOT 511 (CARS events)", "ok": True, "records": n_out, "via": via})
    return True

def load_wzdx(data, degraded_from=None):
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
                  end=end.isoformat() if end else None, timed=True,
                  url="https://511mn.org", geometry=geom)
        if len(EVENTS) > before: n += 1
    SOURCES_STATUS.append({"name": "MnDOT 511 (WZDx fallback)", "ok": True, "records": n,
                           "via": "wzdx", "degraded": True, "degraded_from": degraded_from})
    return True

MIRROR_URL = "https://services.arcgis.com/8lRhdTsQyJpO52F1/arcgis/rest/services/CARS511_MN_Events_View/FeatureServer/0"
MIRROR_CLOSED = ("closed", "road closed", "bridge closed", "intersection closed",
                 "exit ramp closed", "entrance ramp closed")
MIRROR_CONSTR = ("construction", "maintenance", "roadwork", "paving", "utility work",
                 "lane closed", "reduced to one lane", "reduced to two lanes",
                 "shoulder closed", "intermittent")

def mirror_class(phrase):
    ph = (phrase or "").lower()
    if any(k in ph for k in ("weight limit", "height limit", "width limit", "length limit",
                             "roundabout", "oversize")):
        return "restriction"
    if any(k in ph for k in MIRROR_CLOSED): return "closed"
    if any(k in ph for k in MIRROR_CONSTR): return "construction"
    return None

def mirror_date(dstr, tstr, end_of_day=False):
    """-> (datetime or None, whether it carries a real clock time)."""
    if not dstr: return None, False
    try:
        base = dt.datetime.strptime(str(dstr), "%Y%m%d").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None, False
    if tstr:
        try:
            t = dt.datetime.strptime(str(tstr).strip(), "%I:%M %p")
            # feed times are America/Chicago (UTC-5 in Sept, -6 in winter; approximate with -5/-6 by dst)
            return base.replace(hour=t.hour, minute=t.minute) + dt.timedelta(hours=5), True
        except ValueError:
            pass
    return base + dt.timedelta(days=1 if end_of_day else 0), False

def load_mirror(degraded_from=None):
    feats = arcgis_query(MIRROR_URL, buffer_m=BUF50)
    n = 0
    seen = set()
    for f in feats:
        p = props(f)
        phrase = p.get("phrase") or ""
        cls = mirror_class(phrase)
        if cls is None: continue
        key = (p.get("ID"), phrase)
        if key in seen: continue
        seen.add(key)
        start, t1 = mirror_date(p.get("IssueDate"), p.get("StartTime"))
        end, t2 = mirror_date(p.get("ExpireDate"), p.get("EndTime"), end_of_day=True)
        tmp = "upcoming" if p.get("STYLE") == "future_event" else temporal(start, end, undated="active")
        geom = feat_geom(f)
        if geom is None: continue
        before = len(EVENTS)
        add_event(id=f"mirror:{p.get('ID')}", source="MnDOT 511 (Iowa DOT mirror)",
                  event_class=cls, temporal=tmp,
                  road=str(p.get("Route") or "State highway"),
                  description=str(p.get("headline") or ""),
                  start=start.isoformat() if start else None,
                  end=end.isoformat() if end else None, timed=t1 or t2,
                  url=p.get("linktxt") if str(p.get("linktxt") or "").startswith("http") else None,
                  geometry=geom)
        if len(EVENTS) > before: n += 1
    SOURCES_STATUS.append({"name": "MnDOT 511 (Iowa DOT mirror)", "ok": True, "records": n,
                           "via": "iowa_mirror", "degraded": True, "degraded_from": degraded_from})
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
        start, t1 = field_dt(p, start_f)
        end, t2 = field_dt(p, end_f, end_of=True)
        timed = t1 or t2
        tmp = temporal(start, end, undated=undated)
        geom = feat_geom(f)
        if geom is None: continue
        oid = p.get("OBJECTID") or p.get("GlobalID") or p.get("OBJECTID_1") or n
        named_road = first_field(p, road_fields)
        road = named_road or name
        desc = first_field(p, desc_fields) or ""
        loc = first_field(p, ["LOCATION", "Location", "Project_Location", "Clse_From"])
        if loc and loc.lower() not in desc.lower(): desc = (desc + " " + loc).strip()
        link = p.get(link_f) if link_f else None
        # Same-source dedupe: same road + same date window = one project, union geometry.
        # Only collapse when the road name came out of the data. Where it didn't, every
        # feature would share the fallback key (source name, None, None) and a whole
        # layer would fuse into a single event, which is how fifteen Minneapolis layers
        # were reporting one project apiece.
        if named_road:
            dkey = (named_road.lower().strip(), str(start), str(end))
        else:
            dkey = ("#oid", str(oid))
        if dkey in seen and len(EVENTS) > seen[dkey]:
            merge_into(EVENTS[seen[dkey]], geom)
            continue
        cls = klass(p) if callable(klass) else klass
        if cls is None: continue
        before = len(EVENTS)
        add_event(id=f"{name}:{oid}", source=name, event_class=cls, temporal=tmp,
                  road=road, description=desc,
                  start=start.isoformat() if start else None,
                  end=end.isoformat() if end else None,
                  timed=timed,
                  url=link if isinstance(link, str) and link.startswith("http") else None,
                  geometry=geom)
        if len(EVENTS) > before:
            seen[dkey] = len(EVENTS) - 1
            n += 1
    SOURCES_STATUS.append({"name": name, "ok": True, "records": n})

def stpaul_class(p):
    """St. Paul publishes closures and lane restrictions in one layer."""
    rt = str(p.get("RestrictionType") or "").lower()
    if "lane" in rt or "sidewalk" in rt or "parking" in rt: return "construction"
    if "clos" in rt: return "closed"
    return "construction"

DIR_RE = re.compile(r"\b(north|south|east|west|n|s|e|w)\s?bound\b", re.I)
NOISE_RE = re.compile(r"[^a-z0-9]+")

def norm_key(s):
    s = DIR_RE.sub("", str(s or "").lower())
    s = re.sub(r"\b(county|road|rd|street|st|avenue|ave|highway|hwy|closure|project)\b", "", s)
    return NOISE_RE.sub("", s)[:60]

def dedupe_across_sources():
    """Fold records that several agencies publish about the same work into one event.

    Anoka ships the same closure in two layers, Carver in a feature layer and its
    related table, and a county project often reappears in State Aid. Without this
    the tally double-counts them. Keeps the nearest copy and unions the geometry.
    """
    keep, index = [], {}
    for ev in sorted(EVENTS, key=lambda e: e["distance_mi"]):
        rk, dk = norm_key(ev["road"]), norm_key(ev["description"])
        if not rk and not dk:
            keep.append(ev)
            continue
        key = (ev["event_class"], rk, dk)
        prev = index.get(key)
        # Same name at opposite ends of the metro is two projects, not one; only fold
        # copies that sit at a comparable distance from the origin.
        if prev is not None and abs(prev["distance_mi"] - ev["distance_mi"]) <= 3.0:
            merge_into(prev, shape(ev["geometry"]))
            if not prev.get("url") and ev.get("url"): prev["url"] = ev["url"]
            prev.setdefault("also_reported_by", [])
            if ev["source"] not in prev["also_reported_by"] and ev["source"] != prev["source"]:
                prev["also_reported_by"].append(ev["source"])
            continue
        index[key] = ev
        keep.append(ev)
    global DEDUPED
    DEDUPED = len(EVENTS) - len(keep)
    EVENTS[:] = keep

def run():
    # Tier 1
    try:
        load_511()
    except Exception as ex:
        SOURCES_STATUS.append({"name": "MnDOT 511", "ok": False, "error": str(ex)[:160]})

    # Tier 2: State Aid (current-year program = season projects)
    add_arcgis("MnDOT State Aid", "https://dotapp9.dot.state.mn.us/egis12/rest/services/state_aid/sa_projects/MapServer/0",
               "construction", ["ROAD_NAME", "LOCATION"], ["PROJTOW", "PROJECT_TYPE"],
               ["DIST_ACTUAL_WORK_START_DATE", "DIST_EST_WORK_START_DATE", "FIRSTCONSTRUCTIONYEAR"],
               ["DIST_EST_WORK_COMPLETE_DATE", "LASTCONSTRUCTIONYEAR"])

    # Tier 3: counties
    anoka = "https://gisservices.co.anoka.mn.us/anoka_gis/rest/services/Highway_ConstructionFinder_2/FeatureServer"
    add_arcgis("Anoka County", f"{anoka}/3", "closed", ["Road"], ["Description"],
               "Srt_Date", "EndDate", link_f="Link")
    # Layer 2 mirrors layer 3 with its own date field names; the cross-source pass at
    # the end folds the overlap back together.
    add_arcgis("Anoka County", f"{anoka}/2", "closed", ["Road"], ["Description"],
               ["Startdt", "Srt_Date"], ["EndDt", "EndDate"], link_f="Link")

    carver = "https://gis.co.carver.mn.us/arcgis_ea/rest/services/OpenAccess/CC_PW_ConstructionAndClosures/MapServer"
    # Closure layers key off BLOCKNM/COMMENTS/ANTICIPATED*DATE, not the StreetName and
    # CompleteDate this used to ask for - none of those fields exist here, so every
    # closure arrived nameless and undated and the layer collapsed to one event.
    add_arcgis("Carver County", f"{carver}/11", "closed", ["BLOCKNM"],
               ["COMMENTS", "ABBREVDESC", "LOCDESC"],
               ["ANTICIPATEDSTARTDATE", "CloseDateTime"], ["ANTICIPATEDENDDATE", "OpenDateTime"],
               link_f="WEBLINK")
    add_arcgis("Carver County", f"{carver}/12", "closed", ["BLOCKNM"],
               ["COMMENTS", "ABBREVDESC", "LOCDESC"],
               ["ANTICIPATEDSTARTDATE", "CloseDateTime"], ["ANTICIPATEDENDDATE", "OpenDateTime"],
               link_f="WEBLINK")
    add_arcgis("Carver County", f"{carver}/119", "construction",
               ["ProjectName", "StreetName", "ProjectLocation"],
               ["PublicProjectDescription", "ProjectDescription", "ActivityNarrative"],
               "ConstructionYearStart", "ConstructionYearEnd", link_f="Activity_URL")

    add_arcgis("Hennepin County", "https://gis.hennepin.us/arcgis/rest/services/Maps/TRANSPORTATION_PROJECTS/FeatureServer/1",
               "construction", ["Local_Name", "Roadway", "Road_Num"], ["Proj_Type", "Description"],
               ["Start_CRN_Dt", "Start_CRN_Yr"], "End_CRN_Dt",
               where="PD_Status='Construction - Active'", link_f="Website")
    # CONST_START/CONST_FINISH are prose ("Spring 2026"); the CIP years are the real dates.
    add_arcgis("Dakota County", "https://gis2.co.dakota.mn.us/arcgis/rest/services/Transportation/DC_OL_TRANS_TransportationProjects/MapServer/0",
               "construction", ["ROADNAME"], ["PROJECTWORK", "LOCATIONDESCRIPTION"],
               "CIP_CONSTRUCTIONYR1", ["CIP_CONSTRUCTIONYR2", "CIP_CONSTRUCTIONYR1"],
               where="PUBLIC_WEB_CATEGORY='Current'", link_f="CONST_URL")

    # Tier 4: cities
    # One pass over St. Paul, classified by RestrictionType. This used to be two passes -
    # every closure, then every lane restriction again as construction - which counted
    # each lane restriction twice.
    add_arcgis("City of St. Paul", "https://services1.arcgis.com/9meaaHE3uiba0zr8/arcgis/rest/services/Public_Works_Road_Closures_(Public_View)/FeatureServer/0",
               stpaul_class, ["Road"], ["Description", "RestrictionDetails"], "StartDate", "EndDate",
               where="Status='Current'", buffer_m=None, link_f="Website",
               skip_fn=lambda p: bool(re.search(r"mn/?dot", str(p.get("Owner") or ""), re.I)))

    # Minneapolis 2026 program layers 1-17 (exclude 21-26: other agencies, dupes of Tiers 1/3)
    mpls_base = "https://services.arcgis.com/afSMGVsC7QlRK1kZ/arcgis/rest/services/2026_Mpls_Construction_Map/FeatureServer"
    try:
        meta = get(mpls_base, params={"f": "json"})
        layer_names = {l["id"]: l["name"] for l in meta.get("layers", [])}
    except Exception:
        layer_names = {}
    for lid in list(range(1, 18)):
        lname = layer_names.get(lid, f"layer {lid}")
        add_arcgis(f"Minneapolis ({lname})", f"{mpls_base}/{lid}",
                   "construction", ["PROJECT_DESCRIPTION"], ["PROJECT_TYPE"],
                   "PROJECT_START_YEAR", "PROJECT_END_YEAR", link_f="WEBPAGE")

    mtka = "https://services.arcgis.com/vAmq2qjze38HN5HF/arcgis/rest/services/Project_Locations/FeatureServer"
    for lid in (0, 1):
        add_arcgis("City of Minnetonka", f"{mtka}/{lid}", "construction",
                   ["ProjectName"], ["ProjectType", "Description", "Location"], None, None,
                   where="Status='Under Construction'", link_f="Website")
    # CONSTRUCTSTART is a bare year and CONSTRUCTEND is a yes/no flag, so the CIP year
    # filter carries the date logic and these projects stand as season work.
    add_arcgis("City of Golden Valley", "https://gis.goldenvalleymn.gov/server/rest/services/GoldenMap/Golden_Misc/FeatureServer/6",
               "construction", ["PROJNAME"], ["NOTES", "ASSETTYP"], "CONSTRUCTSTART", None,
               where=f"CIPYEAR={NOW.year}", link_f="LINK")

    dedupe_across_sources()

    # ---- counts ----
    # "Closed" counts undated closures alongside dated ones. A county closure layer
    # that omits its dates is still telling us the road is shut; counting only the
    # dated ones dropped them from every ring while the equivalent construction
    # records were counted.
    counts = {}
    for r in RINGS_MI:
        within = [e for e in EVENTS if e["ring"] is not None and e["ring"] <= r]
        counts[str(r)] = {
            "closed": sum(1 for e in within if e["event_class"] == "closed" and e["temporal"] in ("active", "season")),
            "construction": sum(1 for e in within if e["event_class"] == "construction" and e["temporal"] in ("active", "season")),
            "starting_soon": sum(1 for e in within if e["temporal"] == "upcoming" and e["event_class"] in ("closed", "construction")),
        }
    c50 = counts["50"]
    answer = "YES" if (c50["closed"] + c50["construction"]) > 0 else "NO"

    # Only closures and construction reach the page; restrictions (weight, height and
    # width limits) are not roadwork and nothing renders them.
    events = sorted((e for e in EVENTS if e["event_class"] in ("closed", "construction")),
                    key=lambda e: e["distance_mi"])
    out = {
        "generated_at": NOW.isoformat(),
        "origin": {"lon": ORIGIN[0], "lat": ORIGIN[1], "label": "Home, Plymouth MN"},
        "rings_miles": RINGS_MI,
        "answer": answer,
        "counts": counts,
        "deduped": DEDUPED,
        "degraded": [s["name"] for s in SOURCES_STATUS if s.get("degraded")],
        "sources": SOURCES_STATUS,
        "events": events,
    }
    dest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data.json")
    with open(dest, "w") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(json.dumps({"answer": answer, "counts": counts,
                      "events": len(EVENTS), "deduped": DEDUPED,
                      "sources_ok": sum(1 for s in SOURCES_STATUS if s.get("ok")),
                      "sources_total": len(SOURCES_STATUS)}, indent=1))
    for s in SOURCES_STATUS:
        if not s.get("ok"): print("FAILED:", s["name"], s.get("error"))
        elif s.get("degraded"): print("DEGRADED:", s["name"], "-", s.get("degraded_from"))
        elif s.get("records") == 0: print("EMPTY:", s["name"])

if __name__ == "__main__":
    run()
