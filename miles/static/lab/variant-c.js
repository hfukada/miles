// Variant C — Dense instrument: stat strip + compact chart + sparkline table,
// all on one screen. Adapts chart logic (gradientColor, fastest-5 highlight,
// tooltip) from index.html's 'fastest' mode.
(function () {
  const CSS = `
    .v-c main { max-width: 1600px; padding: 1rem 2rem; }
    .v-c section { margin-bottom: 1rem; }

    .v-c .stat-strip {
      display: flex;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    .v-c .stat { padding: 0.625rem 1.25rem; }
    .v-c .stat + .stat { border-left: 1px solid var(--border); }
    .v-c .stat-label {
      font-size: 0.625rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
      margin-bottom: 0.1875rem;
    }
    .v-c .stat-value {
      display: flex;
      align-items: baseline;
      gap: 0.375rem;
      font-size: 1.0625rem;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      color: var(--text);
    }
    .v-c .stat-sub { font-size: 0.6875rem; font-weight: 400; color: var(--muted); }

    .v-c .chart-wrap { height: 360px; }

    .v-c tbody td { padding: 0.25rem 0.75rem; font-size: 0.75rem; }
    .v-c thead th { padding: 0.375rem 0.75rem; font-size: 0.625rem; }
    .v-c .spark { display: block; }
  `;
  const style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);

  // Same gradient used by index.html's fastest-5 highlight.
  function gradientColor(rank, total, dark) {
    const t = total > 1 ? rank / (total - 1) : 0;
    const l = dark ? 58 + t * 10 : 42 + t * 6;
    return `hsl(${Math.round(215 + t * (15 - 215))}, 82%, ${l}%)`;
  }

  // seconds -> "+M:SS" (or "+H:MM:SS" past an hour); negative deltas get a
  // leading "-" instead.
  function fmtDelta(s) {
    if (s == null) return null;
    const sign = s < 0 ? '-' : '+';
    const abs = Math.abs(s);
    const h = Math.floor(abs / 3600);
    const m = String(Math.floor((abs % 3600) / 60)).padStart(h ? 2 : 1, '0');
    const sec = String(Math.round(abs % 60)).padStart(2, '0');
    return h ? `${sign}${h}:${m}:${sec}` : `${sign}${m}:${sec}`;
  }

  // 96x20 sparkline of weeks -12..0, bars scaled to a shared global max so
  // rows are visually comparable.
  function sparkline(weeksArr, globalMax, isPR) {
    const map = Object.fromEntries((weeksArr ?? []).map(w => [w.offset, w.miles]));
    const vals = Array.from({ length: 13 }, (_, i) => map[i - 12] ?? 0);
    const peak = Math.max(0, ...vals);
    const barW = 5, gap = 2;
    const bars = vals.map((v, i) => {
      const h = globalMax > 0 ? (v / globalMax) * 20 : 0;
      const x = i * (barW + gap);
      return `<rect x="${x}" y="${(20 - h).toFixed(1)}" width="${barW}" height="${h.toFixed(1)}" />`;
    }).join('');
    const opacity = isPR ? 0.9 : 0.45;
    return `<svg class="spark" width="96" height="20" viewBox="0 0 96 20" style="fill:var(--series-1);fill-opacity:${opacity}"><title>${peak.toFixed(1)} mi peak</title>${bars}</svg>`;
  }

  function td(val, cls = 'r') {
    if (val == null) return `<td class="dash">—</td>`;
    return `<td class="${cls}">${val}</td>`;
  }

  function rowClass(r, isVirtual) {
    if (isVirtual) return 'virt';
    if (r.finish_time_s != null && r.finish_time_s < 10800) return 'sub3';
    return '';
  }

  LAB.register({
    id: 'c',
    name: 'Dense instrument',
    blurb: 'Cockpit density: stat strip, compact chart, sparkline table — one screen, no scrolling to think.',
    render(root, data, theme) {
      const marathons = data.marathons ?? [];
      const weeksData = data.weeks ?? [];

      const real = marathons.filter(m => !fmt.isVirtual(m));
      const virt = marathons.filter(m => fmt.isVirtual(m));
      const sortedReal = [...real].sort((a, b) => (a.finish_time_s ?? Infinity) - (b.finish_time_s ?? Infinity));
      const sorted = [...sortedReal, ...virt];
      const pr = sortedReal[0];
      const latest = [...real].sort((a, b) => (b.date ?? '').localeCompare(a.date ?? ''))[0];

      const avgVals = real.map(m => m.build?.avg_mpw).filter(v => v != null);
      const peakVals = real.map(m => m.build?.peak_week).filter(v => v != null);
      const bestAvg = avgVals.length ? Math.max(...avgVals) : null;
      const bestPeak = peakVals.length ? Math.max(...peakVals) : null;
      const sub3Count = real.filter(m => m.finish_time_s != null && m.finish_time_s < 10800).length;

      const weeksByDate = new Map(weeksData.map(w => [w.date, w.weeks]));
      let globalMax = 0;
      for (const w of weeksData) {
        for (const wk of w.weeks ?? []) {
          if (wk.offset >= -12 && wk.offset <= 0 && wk.miles > globalMax) globalMax = wk.miles;
        }
      }

      root.innerHTML = `
        <main>
          <section>
            <div class="stat-strip">
              <div class="stat">
                <div class="stat-label">PR</div>
                <div class="stat-value">${pr ? fmt.time(pr.finish_time_s) : '—'}<span class="stat-sub">${pr ? fmt.shortName(pr.name ?? '', pr.date) : ''}</span></div>
              </div>
              <div class="stat">
                <div class="stat-label">Latest</div>
                <div class="stat-value">${latest ? fmt.time(latest.finish_time_s) : '—'}<span class="stat-sub">${latest ? fmt.shortName(latest.name ?? '', latest.date) : ''}</span></div>
              </div>
              <div class="stat">
                <div class="stat-label">Builds</div>
                <div class="stat-value">${real.length}<span class="stat-sub">${virt.length ? `+${virt.length} virtual` : ''}</span></div>
              </div>
              <div class="stat">
                <div class="stat-label">Best avg</div>
                <div class="stat-value">${bestAvg != null ? bestAvg.toFixed(1) : '—'}<span class="stat-sub">mpw</span></div>
              </div>
              <div class="stat">
                <div class="stat-label">Peak week</div>
                <div class="stat-value">${bestPeak != null ? bestPeak.toFixed(1) : '—'}<span class="stat-sub">mi</span></div>
              </div>
              <div class="stat">
                <div class="stat-label">Sub-3</div>
                <div class="stat-value">${sub3Count}</div>
              </div>
            </div>
          </section>

          <section>
            <div class="chart-wrap"><div id="v-c-chart" style="width:100%;height:100%"></div></div>
          </section>

          <section>
            <div class="scroll">
              <table>
                <thead>
                  <tr>
                    <th>Race</th>
                    <th>Date</th>
                    <th class="r">Time</th>
                    <th class="r">Δ</th>
                    <th class="r">12-wk shape</th>
                    <th class="r">Avg</th>
                    <th class="r">Peak</th>
                    <th class="r">Peak 3wk</th>
                    <th class="r">WOs</th>
                    <th class="r">WO pace</th>
                    <th class="r">LRs</th>
                    <th class="r">LR pace</th>
                    <th class="r">HR</th>
                  </tr>
                </thead>
                <tbody id="v-c-tbody"></tbody>
              </table>
            </div>
          </section>
        </main>
      `;

      const tbody = root.querySelector('#v-c-tbody');
      tbody.innerHTML = sorted.map((r, i) => {
        const isVirtual = fmt.isVirtual(r);
        const isPR = !isVirtual && i === 0;
        const rc = isPR ? 'pr' : rowClass(r, isVirtual);
        const bt = r.build?.by_type ?? {};
        const wko = bt.workout, lr = bt.long_run, easy = bt.easy;
        const delta = !isPR && r.finish_time_s != null && pr?.finish_time_s != null
          ? r.finish_time_s - pr.finish_time_s : null;

        return `<tr class="${rc}">
          ${td(r.name, '')}
          ${td(r.date, 'dim')}
          ${td(r.finish_time, 'r bold')}
          ${isPR ? `<td class="r dim">—</td>` : td(fmtDelta(delta), 'r dim')}
          <td class="r">${sparkline(weeksByDate.get(r.date), globalMax, isPR)}</td>
          ${td(r.build?.avg_mpw?.toFixed(1))}
          ${td(r.build?.peak_week?.toFixed(1))}
          ${td(r.build?.peak_3wk_avg?.toFixed(1))}
          ${td(wko?.runs)}
          ${td(fmt.pace(wko?.avg_pace_min_per_mile))}
          ${td(lr?.runs)}
          ${td(fmt.pace(lr?.avg_pace_min_per_mile))}
          ${td(easy?.avg_hr != null ? Math.round(easy.avg_hr) : null)}
        </tr>`;
      }).join('');

      // --- Chart: fixed fastest-5 highlight, adapted from index.html ---
      const BUILD_WEEKS = 12;
      const offsets = Array.from({ length: BUILD_WEEKS + 1 }, (_, i) => i - BUILD_WEEKS);
      const labels = offsets.map(o => o === 0 ? 'Race wk' : String(Math.abs(o)));

      // Draw only the union of the 5 fastest real builds and the 5 most
      // recent real builds (virtual never qualifies). Rank is fixed from
      // sortedReal so highlight/color assignment matches the full-field
      // version even though fewer series are drawn.
      const rankMap = new Map(sortedReal.map((m, idx) => [m.date, idx]));
      const fastestSet = new Set(sortedReal.slice(0, 5).map(m => m.date));
      const recentFive = [...real].sort((a, b) => (b.date ?? '').localeCompare(a.date ?? '')).slice(0, 5);
      const recentSet = new Set(recentFive.map(m => m.date));
      const drawn = sortedReal.filter(m => fastestSet.has(m.date) || recentSet.has(m.date));

      const chartSeries = drawn.map((m) => {
        const rank = rankMap.get(m.date);
        const isPR = rank === 0;
        const highlighted = rank < 5;
        const weekMap = Object.fromEntries((weeksByDate.get(m.date) ?? []).map(w => [w.offset, w.miles]));
        const shortName = fmt.shortName(m.name ?? '', m.date);

        const color = highlighted ? gradientColor(rank, 5, theme.isDark) : theme.baseline;
        const lineWidth = isPR ? 3 : highlighted ? 2 : 1;
        const lineOpacity = highlighted ? 1 : 0.35;
        // Natural color across the full spectrum — emphasis color so hovering
        // a gray line reveals its identity, mirroring index.html.
        const naturalColor = gradientColor(rank, real.length, theme.isDark);
        const emphasisColor = highlighted ? color : naturalColor;

        const prMark = {
          markPoint: {
            symbol: 'roundRect',
            symbolSize: [20, 12],
            symbolOffset: [0, -16],
            itemStyle: { color: theme.accent, borderRadius: 2 },
            data: [{ type: 'max', label: { show: true, formatter: 'PR', color: theme.surface, fontSize: 9, fontWeight: 700 } }],
          },
        };
        const taper = {
          markArea: {
            silent: true,
            itemStyle: { color: theme.withAlpha(theme.muted, theme.isDark ? 0.12 : 0.07) },
            label: { show: true, position: 'insideTopLeft', formatter: 'Taper', fontSize: 10, color: theme.muted },
            data: [[{ xAxis: '2' }, { xAxis: 'Race wk' }]],
          },
        };

        return {
          name: shortName,
          type: 'line',
          color,
          z: isPR ? 10 : highlighted ? 2 : 1,
          data: offsets.map(o => weekMap[o] ?? 0),
          connectNulls: false,
          symbol: 'none',
          lineStyle: { color, width: lineWidth, opacity: lineOpacity, type: 'solid' },
          emphasis: { focus: 'series', lineStyle: { color: emphasisColor, width: lineWidth + 1.5, opacity: 1 } },
          blur: { lineStyle: { opacity: 0.12 } },
          ...(highlighted ? { endLabel: { show: true, formatter: () => shortName, fontSize: 10, color: theme.textSecondary } } : {}),
          ...(isPR ? prMark : {}),
          ...(isPR ? taper : {}),
        };
      });

      const chart = echarts.init(root.querySelector('#v-c-chart'));
      chart.setOption({
        animation: false,
        grid: { top: 12, right: 135, bottom: 22, left: 40 },
        labelLayout: { moveOverlap: 'shiftY' },
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'line', lineStyle: { color: theme.gridline, width: 1 } },
          backgroundColor: theme.surface,
          borderColor: theme.gridline,
          textStyle: { fontSize: 12, color: theme.text },
          formatter(params) {
            const lbl = params[0].axisValue;
            const title = lbl === 'Race wk' ? 'Race week'
                        : `${lbl} week${lbl === '1' ? '' : 's'} before race`;
            const rows = params
              .filter(p => p.value > 0)
              .sort((a, b) => b.value - a.value)
              .map(p => `<div style="display:flex;justify-content:space-between;gap:1rem">
                           <span>${p.marker}${p.seriesName}</span>
                           <b>${p.value.toFixed(1)} mi</b>
                         </div>`);
            const visible = rows.slice(0, 8);
            const extra = rows.length - visible.length;
            const more = extra > 0
              ? `<div style="color:${theme.muted};font-size:11px;margin-top:4px">… ${extra} more</div>`
              : '';
            return `<div style="font-weight:600;margin-bottom:6px;color:${theme.text}">${title}</div>${visible.join('')}${more}`;
          },
        },
        xAxis: {
          type: 'category',
          data: labels,
          axisLine: { lineStyle: { color: theme.gridline } },
          axisTick: { show: false },
          axisLabel: { fontSize: 10, color: theme.muted },
        },
        yAxis: {
          type: 'value',
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { lineStyle: { color: theme.gridline } },
          axisLabel: { fontSize: 10, color: theme.muted },
          min: val => Math.max(0, Math.floor((val.min - 5) / 5) * 5),
        },
        series: chartSeries,
      });
      staggerEndLabels(chart);

      function onResize() { chart.resize(); staggerEndLabels(chart); }
      window.addEventListener('resize', onResize);

      return () => {
        window.removeEventListener('resize', onResize);
        chart.dispose();
      };
    },
  });
})();
