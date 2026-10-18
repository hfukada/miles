// Shared map chrome for the Leaflet pages: the base layers and the data
// credits their usage terms ask for. Include after leaflet.js and nav.js.
//
// The default base layer is OpenTopoMap, with OpenStreetMap's standard tiles
// as a toggleable alternative. A server that sets MILES_TILE_URL (exposed by
// /api/config) replaces the default layer with its own.

const OPENTOPOMAP = {
  name: 'OpenTopoMap',
  url: 'https://{s}.tile.opentopomap.org/{z}/{x}/{y}.png',
  options: {
    subdomains: 'abc',
    maxZoom: 17,
    attribution:
      'Map data: &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors, ' +
      '<a href="http://viewfinderpanoramas.org">SRTM</a> | ' +
      'Map style: &copy; <a href="https://opentopomap.org">OpenTopoMap</a> ' +
      '(<a href="https://creativecommons.org/licenses/by-sa/3.0/">CC-BY-SA</a>)',
  },
};

const OSM_STANDARD = {
  name: 'OpenStreetMap',
  url: 'https://tile.openstreetmap.org/{z}/{x}/{y}.png',
  options: {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  },
};

const TERRAIN_DATA_CREDIT =
  'Terrain data: <a href="https://registry.opendata.aws/terrain-tiles/">Terrain Tiles</a> ' +
  '(Mapzen/Tilezen Joerd), via the AWS Open Data Registry; ' +
  '<a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md">source attributions</a>';

let mapConfigPromise = null;
function loadMapConfig() {
  mapConfigPromise ??= fetch('/api/config')
    .then(r => (r.ok ? r.json() : {}))
    .catch(() => ({}));
  return mapConfigPromise;
}

function defaultBaseLayer(config) {
  if (!config || !config.tile_url) return OPENTOPOMAP;
  const options = {};
  if (config.tile_attribution) options.attribution = config.tile_attribution;
  if (config.tile_max_zoom) options.maxZoom = config.tile_max_zoom;
  return { name: 'Map', url: config.tile_url, options };
}

// OpenTopoMap is a volunteer-run server that can be slow or unreachable. When
// the default layer racks up this many failed tiles (more than it has loaded),
// or loads nothing within the timeout, the map switches once to OpenStreetMap
// and says so. It never switches back by itself.
const FALLBACK_TILE_ERRORS = 6;
const FALLBACK_TIMEOUT_MS = 8000;
const DEFAULT_MAX_ZOOM = OPENTOPOMAP.options.maxZoom;

function watchDefaultLayer(map, primary, fallback, isRemoved) {
  let errors = 0;
  let loaded = 0;
  let switched = false;
  const failOver = () => {
    if (switched || isRemoved() || !map.hasLayer(primary)) return;
    switched = true;
    map.removeLayer(primary);
    fallback.addTo(map);
    map.setMaxZoom(fallback.options.maxZoom);
    const note = L.control({ position: 'bottomleft' });
    note.onAdd = () => {
      const div = L.DomUtil.create('div', 'leaflet-control-attribution');
      div.textContent = 'OpenTopoMap is not responding; showing OpenStreetMap.';
      return div;
    };
    note.addTo(map);
  };
  primary.on('tileload', () => { loaded += 1; });
  primary.on('tileerror', () => {
    errors += 1;
    if (errors >= FALLBACK_TILE_ERRORS && errors > loaded) failOver();
  });
  setTimeout(() => { if (loaded === 0) failOver(); }, FALLBACK_TIMEOUT_MS);
}

// Adds the base layers and a control to switch between them once the server's
// tile settings are known. withTerrainData adds the terrain-tile credit to the
// map's attribution control, for maps showing elevation-derived data. The
// map's maximum zoom is set at once (so a fitBounds right after this call
// cannot zoom past the tiles) and follows the configured layer when known.
function addBaseLayer(map, { withTerrainData = false } = {}) {
  if (withTerrainData) map.attributionControl.addAttribution(TERRAIN_DATA_CREDIT);
  map.setMaxZoom(DEFAULT_MAX_ZOOM);
  let removed = false;
  map.on('unload', () => { removed = true; });
  loadMapConfig().then(config => {
    if (removed) return;
    const primary = defaultBaseLayer(config);
    const layers = {};
    for (const def of [primary, OSM_STANDARD]) {
      layers[def.name] = L.tileLayer(def.url, def.options);
    }
    layers[primary.name].addTo(map);
    map.setMaxZoom(primary.options.maxZoom ?? OSM_STANDARD.options.maxZoom);
    L.control.layers(layers, null, { position: 'topright' }).addTo(map);
    if (primary === OPENTOPOMAP) {
      watchDefaultLayer(map, layers[primary.name], layers[OSM_STANDARD.name], () => removed);
    }
  });
}

// One polyline segment per class run, repeating each boundary point at the
// start of the next segment so the drawn line stays continuous across a class
// change.
function groupByClass(runs) {
  return runs
    .map((run, i) => {
      const bridge = i ? [runs[i - 1].points[runs[i - 1].points.length - 1]] : [];
      return { cls: run.cls, latlngs: [...bridge, ...run.points].map(p => [p.lat, p.lng]) };
    })
    .filter(g => g.latlngs.length > 1);
}

// Draws points ({lat, lng, cls}) into `layer` as a class-colored line over a
// white casing, with a green start and a dark end marker. Returns the line's
// [lat, lng] pairs.
function drawClassedRoute(layer, points, theme) {
  const groups = groupByClass(classRuns(points));
  const allLatLngs = groups.flatMap(g => g.latlngs);
  if (!allLatLngs.length) return allLatLngs;

  // Casings first, so a neighboring segment never covers another's color.
  for (const g of groups) L.polyline(g.latlngs, { color: '#ffffff', weight: 7, opacity: 0.9, lineCap: 'round' }).addTo(layer);
  for (const g of groups) L.polyline(g.latlngs, { color: classColor(theme, g.cls), weight: 4, opacity: 1, lineCap: 'round' }).addTo(layer);

  const dotIcon = (fill) => L.divIcon({
    className: '',
    html: `<svg width="16" height="16" viewBox="0 0 16 16"><circle cx="8" cy="8" r="6" fill="${fill}" stroke="white" stroke-width="2"/></svg>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
  L.marker(allLatLngs[0], { icon: dotIcon('#22c55e'), zIndexOffset: 1000 }).addTo(layer);
  L.marker(allLatLngs[allLatLngs.length - 1], { icon: dotIcon('#111'), zIndexOffset: 1000 }).addTo(layer);
  return allLatLngs;
}
