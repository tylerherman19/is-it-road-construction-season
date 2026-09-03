/* Is It Road Construction Season? — front end. Reads data.json (built by the
   unpaid robot in scripts/build_data.py) and draws the verdict, the tally and the map. */
(function () {
  "use strict";

  var CT = "America/Chicago";
  var $ = function (id) { return document.getElementById(id); };

  function fmtStamp(iso) {
    var d = new Date(iso);
    var date = d.toLocaleDateString("en-US", { timeZone: CT, month: "short", day: "numeric" });
    var time = d.toLocaleTimeString("en-US", { timeZone: CT, hour: "numeric", minute: "2-digit" });
    return date + ", " + time.replace(/\s/g, " ") + " CT";
  }
  function fmtDay(iso) {
    return new Date(iso).toLocaleDateString("en-US", { timeZone: CT, month: "short", day: "numeric", year: "numeric" });
  }

  // Dateline in the masthead
  $("dateline-date").textContent = new Date().toLocaleDateString("en-US", {
    timeZone: CT, weekday: "long", month: "long", day: "numeric", year: "numeric"
  });

  fetch("data.json?cb=" + Date.now())
    .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
    .then(render)
    .catch(function (err) {
      $("answer").textContent = "MAYBE.";
      $("lede").textContent = "The data feed did not answer. Like the city of Plymouth, it publishes nothing a machine can read.";
      $("stamp").textContent = "Fetch failed — " + err.message;
    });

  function render(data) {
    var counts = data.counts || {};
    var c5 = counts["5"] || { closed: 0, construction: 0, starting_soon: 0 };
    var c50 = counts["50"] || { closed: 0, construction: 0, starting_soon: 0 };

    // verdict
    $("answer").textContent = (data.answer || "YES") + ".";
    if (c5.closed > 0) {
      $("lede").textContent = "Minnesota has two seasons: winter and road construction. " +
        c5.closed + (c5.closed === 1 ? " road is" : " roads are") +
        " closed within five miles of the house. Condolences.";
    } else if (data.answer === "YES") {
      $("lede").textContent = "Minnesota has two seasons: winter and road construction. The math says you are in the orange one.";
    } else {
      $("lede").textContent = "Nothing on the books within fifty miles. Enjoy it. It will not last.";
    }

    // stamp + staleness
    var gen = new Date(data.generated_at);
    var ageMin = Math.round((Date.now() - gen.getTime()) / 60000);
    $("stamp").textContent = "Last checked " + fmtStamp(data.generated_at) + " · refreshed every 15 minutes";
    if (ageMin > 90) {
      var b = $("stale-banner");
      b.hidden = false;
      b.innerHTML = "The robot is on break. Figures below are as of <strong>" + fmtStamp(data.generated_at) + "</strong>.";
    }

    // tally table
    var rows = "";
    (data.rings_miles || [5, 10, 20, 50]).forEach(function (r) {
      var c = counts[String(r)] || { closed: 0, construction: 0, starting_soon: 0 };
      rows += "<tr>" +
        '<td class="radius">' + r + " miles<small>from the house</small></td>" +
        '<td class="num closed">' + c.closed + "</td>" +
        '<td class="num">' + c.construction + "</td>" +
        '<td class="num">' + (c.starting_soon || 0) + "</td>" +
        "</tr>";
    });
    $("tally-body").innerHTML = rows;

    // footer source roll
    var ok = (data.sources || []).filter(function (s) { return s.ok; });
    $("footer-sources").textContent = ok.length + " feeds reporting";

    drawMap(data);
  }

  // ---------- map ----------
  function circle(origin, radiusMi, steps) {
    var coords = [];
    var latR = radiusMi / 69.0;
    var lonR = radiusMi / (69.0 * Math.cos(origin[1] * Math.PI / 180));
    for (var i = 0; i <= steps; i++) {
      var a = (i / steps) * 2 * Math.PI;
      coords.push([origin[0] + lonR * Math.cos(a), origin[1] + latR * Math.sin(a)]);
    }
    return coords;
  }

  function classLabel(e) {
    if (e.temporal === "upcoming") return "Starting soon";
    if (e.event_class === "closed") return "Closed now";
    if (e.temporal === "season") return "On the books this season";
    return "Under construction";
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (ch) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch];
    });
  }

  function popupHtml(p) {
    var dates = "";
    if (p.start || p.end) {
      var s = p.start ? fmtDay(p.start) : "…";
      var e = p.end ? fmtDay(p.end) : "until further notice";
      dates = '<p class="pop-dates">' + esc(s) + " → " + esc(e) + "</p>";
    }
    var src = '<p class="pop-src">' + esc(p.source) +
      (p.url ? ' · <a href="' + esc(p.url) + '" target="_blank" rel="noopener">agency record</a>' : "") +
      " · " + esc(String(p.distance_mi)) + " mi away</p>";
    return '<p class="pop-road">' + esc(p.road) + "</p>" +
      '<span class="pop-chip ' + (p.event_class === "closed" ? "closed" : "") + '">' + esc(classLabel(p)) + "</span>" +
      dates +
      (p.description ? '<p class="pop-desc">' + esc(p.description) + "</p>" : "") +
      src;
  }

  function drawMap(data) {
    var origin = [data.origin.lon, data.origin.lat];

    var features = [];
    (data.events || []).forEach(function (e) {
      if (e.event_class !== "closed" && e.event_class !== "construction") return;
      if (!e.geometry || !e.geometry.type) return;
      var cls = e.event_class === "closed"
        ? (e.temporal === "upcoming" ? "soon" : "closed")
        : (e.temporal === "upcoming" ? "soon" : (e.temporal === "season" ? "season" : "constr"));
      features.push({
        type: "Feature",
        properties: {
          cls: cls,
          road: e.road, description: e.description, source: e.source,
          start: e.start, end: e.end, url: e.url,
          distance_mi: e.distance_mi,
          event_class: e.event_class, temporal: e.temporal
        },
        geometry: e.geometry
      });
    });

    var ringFeats = [], ringLabels = [];
    (data.rings_miles || [5, 10, 20, 50]).forEach(function (r) {
      ringFeats.push({ type: "Feature", properties: { r: r }, geometry: { type: "LineString", coordinates: circle(origin, r, 128) } });
      var latR = r / 69.0;
      ringLabels.push({ type: "Feature", properties: { label: r + " MI" }, geometry: { type: "Point", coordinates: [origin[0], origin[1] + latR] } });
    });

    var map = new maplibregl.Map({
      container: "map",
      style: "https://tiles.openfreemap.org/styles/positron",
      center: origin,
      zoom: 8.6,
      attributionControl: { compact: true }
    });
    map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");
    map.scrollZoom.enable();

    map.on("load", function () {
      // cream the paper under the map
      try { map.setPaintProperty("background", "background-color", "#efe8d5"); } catch (e) {}

      map.addSource("events", { type: "geojson", data: { type: "FeatureCollection", features: features } });
      map.addSource("rings", { type: "geojson", data: { type: "FeatureCollection", features: ringFeats } });
      map.addSource("ringLabels", { type: "geojson", data: { type: "FeatureCollection", features: ringLabels } });

      map.addLayer({
        id: "rings-line", type: "line", source: "rings",
        paint: { "line-color": "#6e6654", "line-width": 1, "line-opacity": 0.55 }
      });
      map.addLayer({
        id: "rings-label", type: "symbol", source: "ringLabels",
        layout: {
          "text-field": ["get", "label"],
          "text-font": ["Noto Sans Regular"],
          "text-size": 10,
          "text-offset": [0, -0.9]
        },
        paint: { "text-color": "#6e6654" }
      });

      var lineFilter = ["any", ["==", ["geometry-type"], "LineString"], ["==", ["geometry-type"], "MultiLineString"]];
      var ptFilter = ["any", ["==", ["geometry-type"], "Point"], ["==", ["geometry-type"], "MultiPoint"]];
      var polyFilter = ["any", ["==", ["geometry-type"], "Polygon"], ["==", ["geometry-type"], "MultiPolygon"]];

      // season projects (thin, grey) — lines, polygons, points
      map.addLayer({ id: "season-line", type: "line", source: "events", filter: ["all", lineFilter, ["==", ["get", "cls"], "season"]],
        paint: { "line-color": "#97907c", "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.2, 13, 3], "line-opacity": 0.8 } });
      map.addLayer({ id: "season-fill", type: "fill", source: "events", filter: ["all", polyFilter, ["==", ["get", "cls"], "season"]],
        paint: { "fill-color": "#97907c", "fill-opacity": 0.18 } });
      map.addLayer({ id: "season-pt", type: "circle", source: "events", filter: ["all", ptFilter, ["==", ["get", "cls"], "season"]],
        paint: { "circle-color": "#97907c", "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 2.5, 13, 5], "circle-opacity": 0.75 } });

      // active construction (ink)
      map.addLayer({ id: "constr-line", type: "line", source: "events", filter: ["all", lineFilter, ["==", ["get", "cls"], "constr"]],
        paint: { "line-color": "#1b1812", "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.8, 13, 4.5], "line-opacity": 0.85 } });
      map.addLayer({ id: "constr-fill", type: "fill", source: "events", filter: ["all", polyFilter, ["==", ["get", "cls"], "constr"]],
        paint: { "fill-color": "#1b1812", "fill-opacity": 0.2 } });
      map.addLayer({ id: "constr-pt", type: "circle", source: "events", filter: ["all", ptFilter, ["==", ["get", "cls"], "constr"]],
        paint: { "circle-color": "#1b1812", "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 3, 13, 6], "circle-opacity": 0.85 } });

      // starting soon (dashed orange)
      map.addLayer({ id: "soon-line", type: "line", source: "events", filter: ["all", lineFilter, ["==", ["get", "cls"], "soon"]],
        paint: { "line-color": "#e4570a", "line-width": ["interpolate", ["linear"], ["zoom"], 8, 1.6, 13, 4], "line-dasharray": [2, 1.6], "line-opacity": 0.9 } });
      map.addLayer({ id: "soon-pt", type: "circle", source: "events", filter: ["all", ptFilter, ["==", ["get", "cls"], "soon"]],
        paint: { "circle-color": "#e4570a", "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 3, 13, 6], "circle-opacity": 0.55 } });

      // closed now (safety orange, thickest)
      map.addLayer({ id: "closed-line", type: "line", source: "events", filter: ["all", lineFilter, ["==", ["get", "cls"], "closed"]],
        paint: { "line-color": "#e4570a", "line-width": ["interpolate", ["linear"], ["zoom"], 8, 2.6, 13, 6.5], "line-opacity": 0.95 } });
      map.addLayer({ id: "closed-fill", type: "fill", source: "events", filter: ["all", polyFilter, ["==", ["get", "cls"], "closed"]],
        paint: { "fill-color": "#e4570a", "fill-opacity": 0.28 } });
      map.addLayer({ id: "closed-pt", type: "circle", source: "events", filter: ["all", ptFilter, ["==", ["get", "cls"], "closed"]],
        paint: { "circle-color": "#e4570a", "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 3.5, 13, 7], "circle-stroke-width": 1.5, "circle-stroke-color": "#f6f1e4" } });

      // home marker
      var el = document.createElement("div");
      el.className = "home-marker";
      el.textContent = "HOME";
      new maplibregl.Marker({ element: el, anchor: "bottom" }).setLngLat(origin).addTo(map);

      // open on the 20-mile ring
      var b20 = 20 / 69.0, b20lon = 20 / (69.0 * Math.cos(origin[1] * Math.PI / 180));
      map.fitBounds([[origin[0] - b20lon, origin[1] - b20], [origin[0] + b20lon, origin[1] + b20]], { padding: 30, duration: 0 });

      // popups
      var popup = new maplibregl.Popup({ closeButton: true, closeOnClick: true, maxWidth: "320px" });
      var clickable = ["closed-line", "closed-pt", "closed-fill", "constr-line", "constr-pt", "constr-fill", "soon-line", "soon-pt", "season-line", "season-pt", "season-fill"];
      clickable.forEach(function (layer) {
        map.on("click", layer, function (ev) {
          var f = ev.features && ev.features[0];
          if (!f) return;
          popup.setLngLat(ev.lngLat).setHTML(popupHtml(f.properties)).addTo(map);
        });
        map.on("mouseenter", layer, function () { map.getCanvas().style.cursor = "pointer"; });
        map.on("mouseleave", layer, function () { map.getCanvas().style.cursor = ""; });
      });
    });
  }
})();
