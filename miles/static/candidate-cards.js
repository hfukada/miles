// Route-candidate cards shared by candidates.html and create-route.html: stats,
// score breakdown, a lazily drawn map and elevation profile, and Save as route.
// Include after nav.js, charts.js and map.js; a card is a CandidateCard from
// /api/candidate-sets or /api/searches.

const cardCharts = [];
const cardMaps = [];

const miles = (m) => (m / MILE_METERS).toFixed(1);
const shapeLabel = (shape) => shape.replace(/_/g, ' ');

const RANKING_LABELS = {
  score: 'Score',
  value_per_mile: 'Value per mile',
  connector_cost_per_mile: 'Connector cost per mile',
  repeats_effect: 'Repeats effect',
  distance_fit: 'Distance fit',
  gain_fit: 'Gain fit',
  shape: 'Shape',
};

function rankingValue(v) {
  if (typeof v !== 'number') return String(v);
  return Number.isInteger(v) ? String(v) : v.toFixed(Math.abs(v) < 10 ? 2 : 1);
}

function rankingHtml(ranking) {
  const terms = Object.entries(ranking ?? {}).filter(([, v]) => v != null && typeof v !== 'object');
  if (!terms.length) return '';
  const rows = terms.map(([k, v]) => `<dt>${esc(RANKING_LABELS[k] ?? k)}</dt><dd>${esc(rankingValue(v))}</dd>`).join('');
  return `<details class="cand-why"><summary>Why this score</summary><dl>${rows}</dl></details>`;
}

function cardHtml(c, i) {
  const gain = c.gain_m != null ? ` · ${Math.round(c.gain_m * FT_PER_METER)} ft gain` : '';
  const drive = c.start.drive_minutes != null ? ` · ${Math.round(c.start.drive_minutes)} min drive` : '';
  const sections = c.sections_used.map(s => `${esc(s.name ?? 'unnamed')} (${miles(s.length_m)} mi)`).join(', ');
  const missed = c.missed_constraints.map(m => esc(m.detail)).join('; ');
  return `
    <section class="chart-panel cand-card" data-id="${esc(c.candidate_id)}">
      <h2>${i + 1}. ${esc(shapeLabel(c.shape))}</h2>
      <div class="cand-sub">${miles(c.distance_m)} mi${gain}</div>
      ${missed ? `<div class="cand-miss">Near miss: ${missed}</div>` : ''}
      ${c.note ? `<div class="cand-note">${esc(c.note)}</div>` : ''}
      <div class="stat-strip">
        ${statTile('Run', c.run_pct.toFixed(0), '%')}
        ${statTile('Grind', c.grind_pct.toFixed(0), '%')}
        ${statTile('Hike', c.hike_pct.toFixed(0), '%')}
        ${statTile('Singletrack', miles(c.singletrack_m), 'mi')}
        ${statTile('Track', miles(c.track_m), 'mi')}
        ${statTile('Paved', miles(c.paved_m), 'mi')}
      </div>
      <div class="cand-start">Start: ${esc(c.start.label ?? `${c.start.lat.toFixed(4)}, ${c.start.lng.toFixed(4)}`)}${drive}</div>
      ${sections ? `<div class="cand-start">Runs: ${sections}</div>` : ''}
      <div class="cand-map skeleton"></div>
      <div class="legend"><span class="skeleton"></span></div>
      <div class="cand-elev skeleton"></div>
      ${rankingHtml(c.ranking)}
      <div class="cand-actions"><button type="button">Save as route</button><span class="cand-result"></span></div>
    </section>`;
}

function panelError(el, message) {
  el.classList.remove('skeleton');
  el.style.height = 'auto';
  el.innerHTML = `<div class="error">${esc(message)}</div>`;
}

async function loadCard(card) {
  const mapEl = card.querySelector('.cand-map');
  const elevEl = card.querySelector('.cand-elev');
  const legendEl = card.querySelector('.legend');
  const res = await getJson(`/api/candidates/${encodeURIComponent(card.dataset.id)}`);
  if (!card.isConnected) return;
  if (res.error) {
    panelError(mapEl, `Could not load the route line: ${res.error}`);
    panelError(elevEl, `Could not load the elevation profile: ${res.error}`);
    legendEl.remove();
    return;
  }
  const points = res.data;
  const theme = chartTheme();

  mapEl.classList.remove('skeleton');
  const map = L.map(mapEl, { scrollWheelZoom: false });
  cardMaps.push(map);
  addBaseLayer(map, { withTerrainData: true });
  const bounds = L.latLngBounds(drawClassedRoute(L.layerGroup().addTo(map), points, theme));
  map.fitBounds(bounds, { padding: [16, 16] });

  if (points.some(p => p.cls)) legendEl.innerHTML = classLegendHtml(theme);
  else legendEl.textContent = 'No elevation data: runnability colors unavailable.';

  if (points.filter(p => p.ele_m != null).length < 2) {
    panelError(elevEl, 'No elevation profile available.');
    return;
  }
  elevEl.classList.remove('skeleton');
  const chart = echarts.init(elevEl);
  cardCharts.push(chart);
  chart.setOption(classedElevationOption(theme, points));
  linkChartToMap(chart, () => map, () => points);
}

async function saveCandidate(card, button) {
  const result = card.querySelector('.cand-result');
  button.disabled = true;
  button.textContent = 'Saving…';
  result.innerHTML = '';
  try {
    const res = await fetch(`/api/candidates/${encodeURIComponent(card.dataset.id)}/save`, { method: 'POST' });
    if (!res.ok) throw new Error(await failure(res));
    const saved = await res.json();
    button.textContent = 'Saved';
    result.innerHTML = `<a href="/route.html?id=${saved.route_id}">Open route</a>`;
  } catch (e) {
    button.disabled = false;
    button.textContent = 'Save as route';
    result.innerHTML = `<span class="error">${esc(e.message || 'Could not save.')}</span>`;
  }
}

// Each card draws its map and profile when it nears the viewport.
const cardObserver = new IntersectionObserver((entries) => {
  for (const entry of entries) {
    if (!entry.isIntersecting) continue;
    cardObserver.unobserve(entry.target);
    loadCard(entry.target);
  }
}, { rootMargin: '400px' });

// Wires up the cards under `root` that are not wired yet.
function mountCards(root) {
  root.querySelectorAll('.cand-card:not([data-mounted])').forEach(card => {
    card.dataset.mounted = '1';
    cardObserver.observe(card);
    card.querySelector('button').addEventListener('click', (e) => saveCandidate(card, e.currentTarget));
  });
}

// Frees the maps and charts of cards about to be removed from the page.
function disposeCards() {
  cardMaps.splice(0).forEach(m => m.remove());
  cardCharts.splice(0).forEach(c => c.dispose());
}

window.addEventListener('resize', () => {
  cardCharts.forEach(c => c.resize());
  cardMaps.forEach(m => m.invalidateSize());
});
