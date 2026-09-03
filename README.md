# Is It Road Construction Season?

A daily reckoning for the northwest metro. One-word answer, live counts of
closed / under-construction / starting-soon roads within 5, 10, 20 and 50
miles of a particular driveway in Plymouth, MN 55446, and an interactive map.

- Static site, no build step. MapLibre GL + OpenFreeMap (no tokens).
- `scripts/build_data.py` pulls MnDOT 511 (CARS events REST, WZDx fallback),
  MnDOT State Aid, and the Anoka / Carver / Dakota / Hennepin / St. Paul /
  Minneapolis / Minnetonka / Golden Valley ArcGIS feeds, normalizes them,
  computes ring distances (EPSG:26915), and writes `data.json`.
- `.github/workflows/update-data.yml` reruns that every 15 minutes and commits
  the result. The page reads `data.json` — browser-side fetching of the source
  feeds is not used (CARS REST sends no CORS header; polling undocumented
  endpoints from page views is how they stop being public).
- Source analysis and field maps: see the build memo (12 pp., 3 Sep 2026) this
  repo was built from. Counts are cumulative by ring and measured to the
  nearest point of each road segment.
