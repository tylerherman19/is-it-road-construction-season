/* Is It Road Construction Season? — front end.
   Reads data.json (built by scripts/build_data.py) and drives one piece of shared
   state: a radius, a set of enabled statuses, and a search string. The stat tiles,
   the map layers and the list panel are all renderings of that state, so the three
   can never disagree with each other. */
(function () {
  "use strict";

  var CT = "America/Chicago";
  var MAX_LIST = 300;

  var CLASSES = {
    closed: { label: "Closed now",   varName: "--closed" },
    constr: { label: "Under construction", varName: "--constr" },
    soon:   { label: "Starting soon", varName: "--soon" },
    season: { label: "On the books",  varName: "--season" }
  };
  var ORDER = ["closed", "constr", "soon", "season"];

  var $ = function (id) { return document.getElementById(id); };

  var state = {
    data: null,
    radius: 20,
    enabled: new Set(ORDER),
    query: "",
    visible: [],       // events passing the current filters, nearest first
    selected: null,    // event id
    map: null,
    mapReady: false,
    popup: null
  };

  // ---------------------------------------------------------------- theme
  var THEME_KEY = "ircs-theme";

  function storedTheme() {
    try { return localStorage.getItem(THEME_KEY); } catch (e) { return null; }
  }
  function isDark() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t) return t === "dark";
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function applyTheme(t) {
    if (t) document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
  }

  applyTheme(storedTheme());

  $("theme-toggle").addEventListener("click", function () {
    var next = isDark() ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
    restyleMap();
  });

  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
      if (!storedTheme()) restyleMap();
    });
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888888";
  }

  // ---------------------------------------------------------------- format
  function fmtStamp(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return "unknown";
    var date = d.toLocaleDateString("en-US", { timeZone: CT, month: "short", day: "numeric" });
    var time = d.toLocaleTimeString("en-US", { timeZone: CT, hour: "numeric", minute: "2-digit" });
    return date + ", " + time + " CT";
  }

  // Two kinds of values arrive here. Date-only agency fields are stored at exactly
  // UTC midnight - they mean the calendar day, so show the stored date (rendering
  // those in Central shifts every one back a day). Real instants (511 feed times,
  // refresh stamps) render in Central, like every other timestamp on the page.
  function fmtDay(iso) {
    var d = new Date(iso);
    if (isNaN(d)) return null;
    var dateOnly = d.getUTCHours() === 0 && d.getUTCMinutes() === 0 &&
                   d.getUTCSeconds() === 0 && d.getUTCMilliseconds() === 0;
    return d.toLocaleDateString("en-US", {
      timeZone: dateOnly ? "UTC" : CT, month: "short", day: "numeric", year: "numeric"
    });
  }

  function ago(iso) {
    var mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
    if (!isFinite(mins) || mins < 0) return "just now";
    if (mins < 2) return "just now";
    if (mins < 60) return mins + " min ago";
    var hrs = Math.round(mins / 60);
    if (hrs < 24) return hrs + (hrs === 1 ? " hour ago" : " hours ago");
    var days = Math.round(hrs / 24);
    return days + (days === 1 ? " day ago" : " days ago");
  }

  function plural(n, one, many) { return n === 1 ? one : many; }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  // Which of the four display buckets an event belongs to. `event_class` says what
  // kind of work it is, `temporal` says when — the map cares about the combination.
  function bucket(e) {
    if (e.temporal === "upcoming") return "soon";
    if (e.event_class === "closed") return "closed";
    if (e.temporal === "season") return "season";
    return "constr";
  }

  // ---------------------------------------------------------------- load
  $("dateline").textContent = new Date().toLocaleDateString("en-US", {
    timeZone: CT, weekday: "long", month: "long", day: "numeric"
  });

  fetch("data.json?cb=" + Date.now())
    .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(boot)
    .catch(function (err) {
      $("verdict-badge").innerHTML = "<span>Unknown</span>";
      $("verdict-badge").dataset.state = "error";
      $("verdict-text").textContent =
        "The data feed did not answer, so this page has nothing to report. Like the city of Plymouth, it publishes nothing a machine can read.";
      var p = $("pulse");
      p.dataset.state = "error";
      $("pulse-text").textContent = "Feed offline";
      $("stat-updated").textContent = String(err.message);
      $("tally-body").innerHTML = '<tr><td colspan="4" class="loading">No data.</td></tr>';
      $("results").innerHTML = '<li class="results-empty">Nothing to show — the feed is offline.</li>';
    });

  function boot(data) {
    state.data = data;
    data.events = (data.events || []).filter(function (e) {
      return e.geometry && (e.event_class === "closed" || e.event_class === "construction");
    });
    data.events.forEach(function (e, i) {
      e._id = i;
      e._b = bucket(e);
      e._hay = ((e.road || "") + " " + (e.description || "") + " " + (e.source || "")).toLowerCase();
    });

    buildRadiusControl();
    buildChips();
    buildLegend();
    renderFreshness();
    renderSources();
    renderTally();
    initMap();
    apply();

    $("search").addEventListener("input", debounce(function (ev) {
      state.query = ev.target.value.trim().toLowerCase();
      apply();
    }, 140));

    $("panel-grip").addEventListener("click", function () {
      var panel = this.closest(".panel");
      var open = panel.dataset.open !== "true";
      panel.dataset.open = open ? "true" : "false";
      this.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  function debounce(fn, ms) {
    var t;
    return function () {
      var args = arguments, self = this;
      clearTimeout(t);
      t = setTimeout(function () { fn.apply(self, args); }, ms);
    };
  }

  // ---------------------------------------------------------------- controls
  function buildRadiusControl() {
    var rings = state.data.rings_miles || [5, 10, 20, 50];
    if (rings.indexOf(state.radius) === -1) state.radius = rings[rings.length - 1];
    var group = $("radius-group");
    group.innerHTML = "";
    rings.forEach(function (r) {
      var b = document.createElement("button");
      b.type = "button";
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", r === state.radius ? "true" : "false");
      b.textContent = r + " mi";
      b.addEventListener("click", function () {
        state.radius = r;
        Array.prototype.forEach.call(group.children, function (c) {
          c.setAttribute("aria-checked", c === b ? "true" : "false");
        });
        apply();
        frameRadius();
      });
      group.appendChild(b);
    });
  }

  function buildChips() {
    var chips = $("chips");
    chips.innerHTML = "";
    ORDER.forEach(function (key) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.dataset.cls = key;
      b.setAttribute("aria-pressed", "true");
      b.innerHTML = '<span class="dot" aria-hidden="true"></span>' +
        esc(CLASSES[key].label) + ' <span class="chip-n" data-n="' + key + '"></span>';
      b.addEventListener("click", function () {
        if (state.enabled.has(key)) {
          // Never let the last chip off — an empty map reads as a broken map.
          if (state.enabled.size === 1) return;
          state.enabled.delete(key);
        } else {
          state.enabled.add(key);
        }
        b.setAttribute("aria-pressed", state.enabled.has(key) ? "true" : "false");
        apply();
      });
      chips.appendChild(b);
    });
  }

  function buildLegend() {
    var rows = ORDER.map(function (key) {
      var w = key === "closed" ? 4 : 2.5;
      var style = key === "soon" ? "dashed" : "solid";
      return '<span class="legend-row"><span class="legend-key" style="border-top-width:' + w +
        "px;border-top-style:" + style + ";border-top-color:var(" + CLASSES[key].varName + ')"></span>' +
        esc(CLASSES[key].label) + "</span>";
    });
    rows.push('<span class="legend-row"><span class="legend-key" style="border-top-width:1px;border-top-color:var(--text-3)"></span>Distance rings</span>');
    $("legend").innerHTML = rows.join("");
  }

  // ---------------------------------------------------------------- render
  function apply() {
    var q = state.query;
    state.visible = state.data.events.filter(function (e) {
      if (e.distance_mi > state.radius) return false;
      if (!state.enabled.has(e._b)) return false;
      if (q && e._hay.indexOf(q) === -1) return false;
      return true;
    });

    renderStats();
    renderChipCounts();
    renderList();
    renderTally();
    syncMap();
  }

  // Counts come from the pre-computed rings so the headline figures match the
  // tally exactly; the search box narrows the list, not the verdict.
  function countsFor(r) {
    var c = (state.data.counts || {})[String(r)];
    return c || { closed: 0, construction: 0, starting_soon: 0 };
  }

  function renderStats() {
    var c = countsFor(state.radius);
    var r = state.radius;
    $("stat-closed").textContent = c.closed;
    $("stat-constr").textContent = c.construction;
    $("stat-soon").textContent = c.starting_soon;
    $("cms-head").textContent = "WITHIN " + r + " MI OF HOME";

    var badge = $("verdict-badge"), text = $("verdict-text");
    var total = c.closed + c.construction;
    if (total === 0) {
      badge.innerHTML = "<span>No</span>";
      badge.dataset.state = "no";
      text.textContent = "Nothing on the books within " + r + " miles of the house. Enjoy it. It will not last.";
    } else {
      badge.innerHTML = "<span>Yes</span>";
      badge.dataset.state = "yes";
      if (c.closed > 0) {
        text.textContent = c.closed + " " + plural(c.closed, "road is", "roads are") + " closed and " +
          c.construction + " " + plural(c.construction, "project is", "projects are") +
          " under way within " + r + " miles. Minnesota has two seasons, and this is the orange one.";
      } else {
        text.textContent = "Nothing is fully closed within " + r + " miles, but " + c.construction + " " +
          plural(c.construction, "project is", "projects are") +
          " under way. Minnesota has two seasons, and this is the orange one.";
      }
    }
  }

  function renderChipCounts() {
    var tally = { closed: 0, constr: 0, soon: 0, season: 0 };
    var q = state.query;
    state.data.events.forEach(function (e) {
      if (e.distance_mi > state.radius) return;
      if (q && e._hay.indexOf(q) === -1) return;
      tally[e._b]++;
    });
    ORDER.forEach(function (key) {
      var el = document.querySelector('[data-n="' + key + '"]');
      if (el) el.textContent = tally[key];
    });
  }

  function renderFreshness() {
    var d = state.data;
    var gen = new Date(d.generated_at);
    var ageMin = (Date.now() - gen.getTime()) / 60000;
    var ok = (d.sources || []).filter(function (s) { return s.ok; }).length;

    $("stat-feeds").textContent = ok + " / " + (d.sources || []).length;
    $("stat-updated").textContent = "Updated " + ago(d.generated_at);

    var pulse = $("pulse");
    if (ageMin > 90) {
      pulse.dataset.state = "stale";
      $("pulse-text").textContent = "Stale · " + ago(d.generated_at);
      var n = $("notice");
      n.hidden = false;
      n.innerHTML = "The robot that refreshes this page is behind. These figures are from <strong>" +
        esc(fmtStamp(d.generated_at)) + "</strong>.";
    } else {
      pulse.dataset.state = "live";
      $("pulse-text").textContent = "Live · " + ago(d.generated_at);
    }

    var degraded = d.degraded || [];
    if (degraded.length) {
      var note = $("notice");
      note.hidden = false;
      note.innerHTML = (note.innerHTML ? note.innerHTML + " " : "") +
        "MnDOT&rsquo;s primary feed did not answer this round, so state highways are coming from a backup with coarser geometry.";
    }
  }

  function renderSources() {
    var list = $("source-list");
    var html = (state.data.sources || []).map(function (s) {
      var cls = !s.ok ? "s-bad" : (s.degraded || s.records === 0 ? "s-warn" : "s-ok");
      var right = !s.ok ? "failed" : (s.records === 0 ? "none" : s.records);
      return '<li><span class="s-dot ' + cls + '" aria-hidden="true"></span>' +
        '<span class="s-name">' + esc(s.name) + "</span>" +
        '<span class="s-n">' + esc(String(right)) + "</span></li>";
    }).join("");
    if (state.data.deduped) {
      html += '<li><span class="s-dot s-ok" aria-hidden="true"></span><span class="s-name">' +
        "Duplicates folded together</span>" +
        '<span class="s-n">' + esc(String(state.data.deduped)) + "</span></li>";
    }
    list.innerHTML = html;
  }

  function renderTally() {
    var rings = state.data.rings_miles || [5, 10, 20, 50];
    $("tally-body").innerHTML = rings.map(function (r) {
      var c = countsFor(r);
      return '<tr data-selected="' + (r === state.radius) + '">' +
        '<td class="radius">Within ' + r + " miles</td>" +
        '<td class="num" data-cls="closed" data-label="Closed now">' + c.closed + "</td>" +
        '<td class="num" data-label="Under construction">' + c.construction + "</td>" +
        '<td class="num" data-label="Starting soon">' + (c.starting_soon || 0) + "</td>" +
        "</tr>";
    }).join("");
  }

  function renderList() {
    var list = $("results");
    var more = $("results-more");
    var items = state.visible;

    $("panel-count").textContent = items.length;
    $("panel-sub").textContent = items.length
      ? items.length + " " + plural(items.length, "project", "projects") + " within " + state.radius + " miles"
      : "Nothing matches";

    if (!items.length) {
      list.innerHTML = '<li class="results-empty">No projects match. Try a wider radius or a different search.</li>';
      more.hidden = true;
      return;
    }

    var shown = items.slice(0, MAX_LIST);
    list.innerHTML = shown.map(function (e) {
      var dates = dateLine(e);
      return "<li>" +
        '<button type="button" class="result" data-cls="' + e._b + '" data-id="' + e._id + '">' +
          '<span class="result-top">' +
            '<span class="result-road">' + esc(e.road || "Unnamed road") + "</span>" +
            '<span class="result-dist">' + e.distance_mi.toFixed(1) + " mi</span>" +
          "</span>" +
          '<span class="result-meta">' +
            '<span class="result-tag"><span class="dot" aria-hidden="true"></span>' +
              esc(CLASSES[e._b].label) + "</span>" +
            (dates ? '<span class="result-dist">' + esc(dates) + "</span>" : "") +
          "</span>" +
          (e.description ? '<span class="result-desc">' + esc(e.description) + "</span>" : "") +
        "</button></li>";
    }).join("");

    more.hidden = items.length <= MAX_LIST;
    if (!more.hidden) {
      more.textContent = "Showing the " + MAX_LIST + " closest of " + items.length +
        ". Narrow the radius or search to see the rest.";
    }

    Array.prototype.forEach.call(list.querySelectorAll(".result"), function (btn) {
      btn.addEventListener("click", function () {
        select(Number(btn.dataset.id), true);
      });
    });

    if (state.selected != null) markSelected();
  }

  function dateLine(e) {
    var s = e.start ? fmtDay(e.start) : null;
    var en = e.end ? fmtDay(e.end) : null;
    if (s && en) return s + " – " + en;
    if (en) return "through " + en;
    if (s) return "from " + s;
    return null;
  }

  function markSelected() {
    Array.prototype.forEach.call(document.querySelectorAll(".result"), function (b) {
      b.dataset.active = Number(b.dataset.id) === state.selected ? "true" : "false";
    });
  }

  // ---------------------------------------------------------------- map
  function ringCoords(origin, radiusMi, steps) {
    var coords = [];
    var latR = radiusMi / 69.0;
    var lonR = radiusMi / (69.0 * Math.cos(origin[1] * Math.PI / 180));
    for (var i = 0; i <= steps; i++) {
      var a = (i / steps) * 2 * Math.PI;
      coords.push([origin[0] + lonR * Math.cos(a), origin[1] + latR * Math.sin(a)]);
    }
    return coords;
  }

  function styleUrl() {
    return "https://tiles.openfreemap.org/styles/" + (isDark() ? "dark" : "positron");
  }

  function origin() {
    return [state.data.origin.lon, state.data.origin.lat];
  }

  function initMap() {
    var o = origin();
    var map = new maplibregl.Map({
      container: "map",
      style: styleUrl(),
      center: o,
      zoom: 8.4,
      minZoom: 6,
      maxZoom: 16,
      attributionControl: { compact: true },
      cooperativeGestures: window.matchMedia("(max-width: 820px)").matches
    });
    state.map = map;

    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.GeolocateControl({ trackUserLocation: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "imperial" }), "bottom-right");

    state.popup = new maplibregl.Popup({
      closeButton: true, closeOnClick: true, maxWidth: "320px", offset: 12
    });
    state.popup.on("close", function () {
      state.selected = null;
      markSelected();
    });

    map.on("load", function () { addLayers(); });
  }

  function featureCollection(events) {
    return {
      type: "FeatureCollection",
      features: events.map(function (e) {
        return {
          type: "Feature",
          id: e._id,
          properties: {
            id: e._id, cls: e._b,
            road: e.road || "Unnamed road",
            description: e.description || "",
            source: e.source || "",
            start: e.start || "", end: e.end || "",
            url: e.url || "",
            distance_mi: e.distance_mi
          },
          geometry: e.geometry
        };
      })
    };
  }

  // [width at z8, width at z13]. A zoom expression has to sit at the top level of a
  // paint property, so the hover/selected lift is folded into each stop's output
  // rather than multiplying the interpolation from outside.
  var LINE_W = { closed: [2.6, 7], constr: [1.9, 5], soon: [1.8, 4.5], season: [1.3, 3.2] };
  var PT_R   = { closed: [4, 8],   constr: [3.2, 6.5], soon: [3.2, 6.5], season: [2.6, 5] };
  var LIFT = 1.7;

  function zoomSize(stops, cond) {
    return ["interpolate", ["linear"], ["zoom"],
      8,  ["case", cond, stops[0] * LIFT, stops[0]],
      13, ["case", cond, stops[1] * LIFT, stops[1]]];
  }

  function addLayers() {
    var map = state.map, o = origin();
    var rings = state.data.rings_miles || [5, 10, 20, 50];

    map.addSource("rings", {
      type: "geojson",
      data: {
        type: "FeatureCollection",
        features: rings.map(function (r) {
          return { type: "Feature", properties: { r: r, label: r + " MI" },
                   geometry: { type: "LineString", coordinates: ringCoords(o, r, 180) } };
        })
      }
    });
    map.addSource("ringLabels", {
      type: "geojson",
      data: {
        type: "FeatureCollection",
        features: rings.map(function (r) {
          return { type: "Feature", properties: { label: r + " MI" },
                   geometry: { type: "Point", coordinates: [o[0], o[1] + r / 69.0] } };
        })
      }
    });
    map.addSource("events", { type: "geojson", data: featureCollection(state.data.events) });

    map.addLayer({
      id: "rings-line", type: "line", source: "rings",
      paint: {
        "line-color": cssVar("--text-3"),
        "line-width": 1,
        "line-opacity": ["case", ["==", ["get", "r"], state.radius], 0.85, 0.3],
        "line-dasharray": [3, 3]
      }
    });
    map.addLayer({
      id: "rings-label", type: "symbol", source: "ringLabels",
      layout: {
        "text-field": ["get", "label"],
        "text-font": ["Noto Sans Regular"],
        "text-size": 10, "text-offset": [0, -0.7], "text-allow-overlap": false
      },
      paint: {
        "text-color": cssVar("--text-3"),
        "text-halo-color": cssVar("--bg"), "text-halo-width": 1.2
      }
    });

    // Painted back to front: planned work underneath, hard closures on top, so the
    // thing most likely to ruin a commute is never buried under a resurfacing job.
    ["season", "soon", "constr", "closed"].forEach(function (key) {
      var color = cssVar(CLASSES[key].varName);
      var isCls = ["==", ["get", "cls"], key];
      var hovered = ["boolean", ["feature-state", "hover"], false];
      var chosen = ["boolean", ["feature-state", "selected"], false];
      var lit = ["any", hovered, chosen];

      map.addLayer({
        id: key + "-fill", type: "fill", source: "events",
        filter: ["all", isCls, ["in", ["geometry-type"], ["literal", ["Polygon", "MultiPolygon"]]]],
        paint: { "fill-color": color, "fill-opacity": ["case", lit, 0.42, 0.22] }
      });
      map.addLayer({
        id: key + "-line", type: "line", source: "events",
        filter: ["all", isCls, ["in", ["geometry-type"], ["literal", ["LineString", "MultiLineString"]]]],
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": color,
          "line-width": zoomSize(LINE_W[key], lit),
          "line-opacity": key === "season" ? 0.75 : 0.92,
          "line-dasharray": key === "soon" ? [2, 1.5] : [1, 0]
        }
      });
      map.addLayer({
        id: key + "-pt", type: "circle", source: "events",
        filter: ["all", isCls, ["in", ["geometry-type"], ["literal", ["Point", "MultiPoint"]]]],
        paint: {
          "circle-color": color,
          "circle-radius": zoomSize(PT_R[key], lit),
          "circle-opacity": key === "season" ? 0.75 : 0.95,
          "circle-stroke-width": 1.5,
          "circle-stroke-color": cssVar("--bg")
        }
      });
    });

    // Markers live outside the style, so they survive a theme swap; adding one per
    // addLayers() call would stack a new pin on the house every toggle.
    if (!state.homeMarker) {
      var el = document.createElement("div");
      el.className = "home-marker";
      el.textContent = "Home";
      state.homeMarker = new maplibregl.Marker({ element: el, anchor: "bottom" })
        .setLngLat(o).addTo(state.map);
    }

    // Layer-scoped handlers outlive a style swap because the layer ids are rebuilt
    // under the same names; wiring them again would double every click.
    if (!state.wired) { wireInteraction(); state.wired = true; }
    state.mapReady = true;
    syncMap();
    frameRadius();
  }

  function interactiveLayers() {
    var ids = [];
    ORDER.forEach(function (k) { ids.push(k + "-line", k + "-pt", k + "-fill"); });
    return ids.filter(function (id) { return state.map.getLayer(id); });
  }

  function wireInteraction() {
    var map = state.map;
    var hovered = null;

    function setHover(id) {
      if (hovered === id) return;
      if (hovered != null) map.setFeatureState({ source: "events", id: hovered }, { hover: false });
      hovered = id;
      if (hovered != null) map.setFeatureState({ source: "events", id: hovered }, { hover: true });
    }

    interactiveLayers().forEach(function (layer) {
      map.on("mousemove", layer, function (ev) {
        map.getCanvas().style.cursor = "pointer";
        if (ev.features && ev.features[0]) setHover(ev.features[0].id);
      });
      map.on("mouseleave", layer, function () {
        map.getCanvas().style.cursor = "";
        setHover(null);
      });
      map.on("click", layer, function (ev) {
        var f = ev.features && ev.features[0];
        if (!f) return;
        select(f.properties.id, false, ev.lngLat);
      });
    });
  }

  // Selecting from either side — a list row or a map click — runs through here, so
  // the highlight, the popup and the list scroll position always agree.
  function select(id, fromList, lngLat) {
    var e = state.data.events[id];
    if (!e) return;

    if (state.selected != null && state.mapReady) {
      state.map.setFeatureState({ source: "events", id: state.selected }, { selected: false });
    }
    state.selected = id;
    if (state.mapReady) {
      state.map.setFeatureState({ source: "events", id: id }, { selected: true });
    }
    markSelected();

    var at = lngLat || centroid(e.geometry);
    if (!at) return;

    if (state.mapReady) {
      state.popup.setLngLat(at).setHTML(popupHtml(e)).addTo(state.map);
      keepPopupInView(state.popup);
      if (fromList) {
        state.map.easeTo({ center: at, zoom: Math.max(state.map.getZoom(), 11), duration: 600 });
        window.setTimeout(function () { keepPopupInView(state.popup); }, 700);
      }
    }
  }

  // MapLibre anchors the popup on the tapped point; near a screen edge half the
  // card lands outside the viewport (the mobile screenshot bug). Pan the map just
  // enough to bring the whole card back inside.
  function keepPopupInView(popup) {
    window.requestAnimationFrame(function () {
      if (!state.map) return;
      var el = popup.getElement && popup.getElement();
      if (!el) return;
      var r = el.getBoundingClientRect();
      var m = state.map.getContainer().getBoundingClientRect();
      var pad = 10;
      var shiftX = 0, shiftY = 0;
      if (r.left < m.left + pad) shiftX = (m.left + pad) - r.left;
      else if (r.right > m.right - pad) shiftX = (m.right - pad) - r.right;
      if (r.top < m.top + pad) shiftY = (m.top + pad) - r.top;
      else if (r.bottom > m.bottom - pad) shiftY = (m.bottom - pad) - r.bottom;
      if (shiftX || shiftY) state.map.panBy([shiftX, shiftY], { duration: 200 });
    });
  }

  function centroid(geom) {
    if (!geom) return null;
    var pts = [];
    (function walk(c, depth) {
      if (!Array.isArray(c)) return;
      if (typeof c[0] === "number") { pts.push(c); return; }
      c.forEach(function (x) { walk(x, depth + 1); });
    })(geom.coordinates, 0);
    if (!pts.length) return null;
    // Midpoint of the drawn shape reads better than an average for long corridors.
    return pts[Math.floor(pts.length / 2)];
  }

  function popupHtml(e) {
    var rows = "";
    var dl = dateLine(e);
    if (dl) rows += "<dt>Dates</dt><dd>" + esc(dl) + "</dd>";
    rows += "<dt>Distance</dt><dd>" + e.distance_mi.toFixed(1) + " mi from home</dd>";
    rows += "<dt>Source</dt><dd>" + esc(e.source || "unknown") + "</dd>";

    var link = "";
    if (e.url && /^https?:\/\//i.test(e.url)) {
      link = '<a class="pop-link" href="' + esc(e.url) + '" target="_blank" rel="noopener noreferrer">Agency record &rarr;</a>';
    }

    return '<div class="pop" data-cls="' + e._b + '">' +
      '<p class="pop-tag"><span class="dot" aria-hidden="true"></span>' + esc(CLASSES[e._b].label) + "</p>" +
      '<p class="pop-road">' + esc(e.road || "Unnamed road") + "</p>" +
      (e.description ? '<p class="pop-desc">' + esc(e.description) + "</p>" : "") +
      '<dl class="pop-rows">' + rows + "</dl>" +
      link +
      "</div>";
  }

  function syncMap() {
    if (!state.mapReady) return;
    var map = state.map;
    var ids = new Set(state.visible.map(function (e) { return e._id; }));

    // Filtering the source rather than toggling layer visibility keeps the radius,
    // the chips and the search box all expressible in one filter.
    var filter = ["in", ["get", "id"], ["literal", Array.from(ids)]];
    ORDER.forEach(function (key) {
      ["-line", "-pt", "-fill"].forEach(function (suffix) {
        var id = key + suffix;
        if (!map.getLayer(id)) return;
        var base = map.getFilter(id);
        // base[1] is the class test, base[2] the geometry-type test; keep both.
        map.setFilter(id, ["all", base[1], base[2], filter]);
      });
    });

    map.setPaintProperty("rings-line", "line-opacity",
      ["case", ["==", ["get", "r"], state.radius], 0.85, 0.3]);
  }

  function frameRadius() {
    if (!state.mapReady) return;
    var o = origin(), r = state.radius;
    var dLat = r / 69.0;
    var dLon = r / (69.0 * Math.cos(o[1] * Math.PI / 180));
    state.map.fitBounds(
      [[o[0] - dLon, o[1] - dLat], [o[0] + dLon, o[1] + dLat]],
      { padding: { top: 40, bottom: 40, left: 40, right: 40 }, duration: 600 }
    );
  }

  // Re-colour the marks after a theme change. The basemap style is swapped wholesale,
  // which discards our sources and layers, so they are rebuilt on the new style.
  function restyleMap() {
    if (!state.map || !state.mapReady) return;
    state.mapReady = false;
    state.map.setStyle(styleUrl());
    state.map.once("styledata", function () {
      addLayers();
    });
  }
})();
