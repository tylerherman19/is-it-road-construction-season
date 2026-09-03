# Is It Road Construction Season?

Live counts of closed / under-construction / starting-soon roads within 5, 10, 20
and 50 miles of a particular driveway in Plymouth, MN 55446, plus an interactive
map of every project the agencies will admit to.

- Static site, no build step. MapLibre GL + OpenFreeMap (no tokens, no API keys).
- `scripts/build_data.py` pulls MnDOT 511 (CARS events REST, with WZDx and an
  Iowa DOT mirror behind it), MnDOT State Aid, and the Anoka / Carver / Dakota /
  Hennepin / St. Paul / Minneapolis / Minnetonka / Golden Valley ArcGIS feeds,
  normalizes them into one event schema, computes ring distances (EPSG:26915),
  folds duplicate reports of the same work together, and writes `data.json`.
- `.github/workflows/update-data.yml` reruns that on a `*/15` cron and commits the
  result. GitHub queues scheduled workflows on a best-effort basis, so that is a
  ceiling rather than a promise — the page reports the real age of the data and
  flags itself as stale past 90 minutes.
- The page reads `data.json` only. Browser-side fetching of the source feeds is
  not used: CARS REST sends no CORS header, and polling undocumented endpoints
  from page views is how they stop being public.

## Front end

`index.html` / `styles.css` / `app.js`, no framework. One piece of shared state —
a radius, a set of enabled statuses, a search string — drives the stat tiles, the
map layers and the list panel together, so the three can never disagree.

Light and dark are both explicitly designed; the theme follows the OS and can be
overridden with the toggle (remembered in `localStorage`). The four map statuses
carry a second encoding besides color — line weight, dash pattern, and a text
label in the legend, the list and the popup — so nothing depends on hue alone.

## Data notes

- **Closed** means an agency says the road, ramp or bridge is shut. Undated
  closures from a closure layer count too; an agency that omits its dates is
  still telling you the road is closed.
- **Under construction** means work is active or on the books for the season.
  County and city layers promise a project, not a blocked lane this morning.
- **Starting soon** has a published start date in the future.
- Counts are cumulative by ring, measured to the nearest point of each road
  segment, and de-duplicated across sources — Anoka publishes the same closure
  in two layers, Carver in a feature layer and its related table, and a county
  project often reappears in State Aid.
- Several agencies publish a bare calendar year, prose ("Spring 2026"), or a
  yes/no flag where a date belongs. Years are read as years; prose is read as no
  date at all rather than guessed at.
