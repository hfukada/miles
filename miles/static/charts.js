// Shared chart/formatting helpers for races.html: fmt (time/pace/name
// formatting, from design-lab.html), gradientColor/recentColor (build-line
// highlight palettes, from index.html), staggerEndLabels (endLabel
// de-collision, from design-lab.html), and sparklineSVG (dense-table
// sparkline, generalized from lab/variant-c.js's fixed 12-week version).
// Plain global script — load after nav.js.

const fmt = {
  // seconds -> "2:54:00"
  time(s) {
    if (s == null) return null;
    const h = Math.floor(s / 3600);
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
    const sec = String(Math.round(s % 60)).padStart(2, '0');
    return `${h}:${m}:${sec}`;
  },
  // seconds -> "2:54" (h:mm, for tight spots)
  timeShort(s) {
    if (s == null) return null;
    return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}`;
  },
  // decimal minutes per mile (7.82) -> "7:49"
  pace(dec) {
    if (dec == null) return null;
    const m = Math.floor(dec);
    const s = String(Math.round((dec - m) * 60)).padStart(2, '0');
    return `${m}:${s}`;
  },
  // "Grandma's Marathon 2023" -> "Grandma's '23"; virtual -> "(v)"
  shortName(name, date) {
    const nameYear = (name.match(/\d{4}/) ?? [])[0];
    const year = nameYear ?? (date ? date.slice(0, 4) : null);
    const yr = year ? `'${year.slice(2)}` : '';
    let short = name
      .replace("Grandma's Marathon", "Grandma's")
      .replace('Marathon', '')
      .replace('Virtual', '(v)')
      .replace(/\s+/g, ' ').trim();
    if (nameYear) short = short.replace(nameYear, yr).replace(/\s+/g, ' ').trim();
    else if (year) short = `${short} ${yr}`;
    return short;
  },
  isVirtual(m) { return m.name?.includes('Virtual') ?? false; },
};

// Fastest-N highlight gradient (blue -> teal).
function gradientColor(rank, total, dark) {
  const t = total > 1 ? rank / (total - 1) : 0;
  const l = dark ? 58 + t * 10 : 42 + t * 6;
  return `hsl(${Math.round(215 + t * (15 - 215))}, 82%, ${l}%)`;
}

// Recent-N highlight gradient (gold -> green).
function recentColor(rank, total, dark) {
  const t = total > 1 ? rank / (total - 1) : 0;
  const l = dark ? 60 : 45;
  return `hsl(${Math.round(48 + t * (20 - 48))}, 90%, ${l}%)`;
}

// ECharts labelLayout.moveOverlap ignores endLabels, so converging lines
// stack their labels. Re-space them in pixel space after setOption: sort by
// final-value y, push apart to minGap, apply per-series offsets.
function staggerEndLabels(chart, minGap = 15) {
  const series = chart.getOption().series ?? [];
  const items = [];
  series.forEach((s, i) => {
    if (!s.endLabel?.show) return;
    const v = s.data[s.data.length - 1];
    const y = chart.convertToPixel({ yAxisIndex: 0 }, v);
    if (Number.isFinite(y)) items.push({ i, y0: y, y });
  });
  if (items.length < 2) return;
  items.sort((a, b) => a.y - b.y);
  for (let k = 1; k < items.length; k++) {
    if (items[k].y - items[k - 1].y < minGap) items[k].y = items[k - 1].y + minGap;
  }
  const patch = series.map(() => ({}));
  for (const it of items) {
    patch[it.i] = { endLabel: { offset: [4, it.y - it.y0] } };
  }
  chart.setOption({ series: patch });
}

// nWeeks+1 bars for offsets -nWeeks..0, scaled to a shared globalMax so rows
// stay visually comparable within a bucket's table.
function sparklineSVG(weeksArr, nWeeks, globalMax, isPR) {
  const map = Object.fromEntries((weeksArr ?? []).map(w => [w.offset, w.miles]));
  const vals = Array.from({ length: nWeeks + 1 }, (_, i) => map[i - nWeeks] ?? 0);
  const peak = Math.max(0, ...vals);
  const barW = 5, gap = 2;
  const width = vals.length * (barW + gap) - gap;
  const bars = vals.map((v, i) => {
    const h = globalMax > 0 ? (v / globalMax) * 20 : 0;
    const x = i * (barW + gap);
    return `<rect x="${x}" y="${(20 - h).toFixed(1)}" width="${barW}" height="${h.toFixed(1)}" />`;
  }).join('');
  const opacity = isPR ? 0.9 : 0.45;
  return `<svg class="spark" width="${width}" height="20" viewBox="0 0 ${width} 20" style="fill:var(--series-1);fill-opacity:${opacity}"><title>${peak.toFixed(1)} mi peak</title>${bars}</svg>`;
}
