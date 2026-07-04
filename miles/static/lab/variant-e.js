// Variant E — Small multiples: one panel per build, focal line against a
// shared gray field, shared y-scale. Kills the 14-item legend problem by
// giving every build its own frame instead of overlaying all of them once.
(function () {
  const CSS = `
    .v-e .panel-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(330px, 1fr));
      gap: 1rem;
    }
    .v-e .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.75rem 1rem;
    }
    .v-e .panel-header {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      margin-bottom: 0.5rem;
    }
    .v-e .panel-name { font-size: 0.8125rem; font-weight: 600; color: var(--text); }
    .v-e .panel-name.virt { color: var(--muted); font-style: italic; font-weight: 500; }
    .v-e .panel-time { font-size: 0.8125rem; font-weight: 700; font-variant-numeric: tabular-nums; color: var(--text); }
    .v-e .panel-chart { height: 150px; }
  `;
  const style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);

  function td(val, cls = 'r') {
    if (val == null) return `<td class="dash">—</td>`;
    return `<td class="${cls}">${val}</td>`;
  }

  // Precomputes the shared arrays (per-build series data, gray context
  // series, y max) once, and returns a factory that builds each panel's
  // option by slotting in that panel's focal series — avoids rebuilding the
  // 13-line gray field 14 times over.
  function buildShared(sortedWeeks, theme) {
    const offsets = Array.from({ length: 13 }, (_, i) => i - 12);
    const labels = offsets.map(o => o === 0 ? 'Race' : String(Math.abs(o)));

    const seriesData = sortedWeeks.map(w => {
      const map = Object.fromEntries((w.weeks ?? []).map(p => [p.offset, p.miles]));
      return offsets.map(o => map[o] ?? 0);
    });

    let globalMax = 0;
    for (const arr of seriesData) for (const v of arr) if (v > globalMax) globalMax = v;
    const yMax = Math.max(0, Math.ceil(globalMax / 20) * 20);

    // Built once; each panel reuses these objects via filter (not rebuilt).
    const contextSeries = sortedWeeks.map((w, idx) => ({
      type: 'line',
      data: seriesData[idx],
      symbol: 'none',
      silent: true,
      lineStyle: { color: theme.gridline, width: 1, opacity: 0.55 },
      z: 1,
    }));

    function axisLabelFormatter(value) {
      return (value === '12' || value === '6' || value === 'Race') ? value : '';
    }

    function makeOption(idx) {
      const w = sortedWeeks[idx];
      const isVirtual = fmt.isVirtual(w);
      const focal = {
        type: 'line',
        data: seriesData[idx],
        symbol: 'none',
        lineStyle: {
          color: isVirtual ? theme.muted : theme.accent,
          width: 2,
          type: isVirtual ? 'dashed' : 'solid',
        },
        z: 10,
      };
      const series = [...contextSeries.filter((_, i) => i !== idx), focal];
      const focalIndex = series.length - 1;

      return {
        animation: false,
        grid: { top: 6, right: 8, bottom: 18, left: 26 },
        xAxis: {
          type: 'category',
          data: labels,
          axisLine: { lineStyle: { color: theme.gridline } },
          axisTick: { show: false },
          axisLabel: { formatter: axisLabelFormatter, fontSize: 9, color: theme.muted },
        },
        yAxis: {
          type: 'value',
          min: 0,
          max: yMax,
          splitNumber: 2,
          axisLine: { show: false },
          axisTick: { show: false },
          splitLine: { lineStyle: { color: theme.gridline } },
          axisLabel: { fontSize: 9, color: theme.muted },
        },
        tooltip: {
          trigger: 'axis',
          axisPointer: { type: 'line', lineStyle: { color: theme.gridline, width: 1 } },
          backgroundColor: theme.surface,
          borderColor: theme.gridline,
          textStyle: { fontSize: 12, color: theme.text },
          formatter(params) {
            const p = params.find(pp => pp.seriesIndex === focalIndex);
            if (!p) return '';
            const lbl = p.axisValue;
            const text = lbl === 'Race' ? 'Race week' : `${lbl} week${lbl === '1' ? '' : 's'} out`;
            return `<div style="display:flex;justify-content:space-between;gap:1rem">
                      <span>${text}</span><b>${p.value.toFixed(1)} mi</b>
                    </div>`;
          },
        },
        series,
      };
    }

    return { makeOption };
  }

  LAB.register({
    id: 'e',
    name: 'Small multiples',
    blurb: 'One panel per build against the field — kills the spaghetti and the legend at the same time.',
    render(root, data, theme) {
      const weeksData = data.weeks ?? [];
      const real = weeksData.filter(w => !fmt.isVirtual(w))
        .sort((a, b) => (a.finish_time_s ?? Infinity) - (b.finish_time_s ?? Infinity));
      const virt = weeksData.filter(w => fmt.isVirtual(w));
      const sortedWeeks = [...real, ...virt];

      // Panels render only for the union of the 5 fastest real builds and
      // the 5 most recent real builds (virtual never qualifies); `real` is
      // already fastest-first so the filter preserves that order.
      const fastestSet = new Set(real.slice(0, 5).map(w => w.date));
      const recentFive = [...real].sort((a, b) => (b.date ?? '').localeCompare(a.date ?? '')).slice(0, 5);
      const recentSet = new Set(recentFive.map(w => w.date));
      const panelWeeks = real.filter(w => fastestSet.has(w.date) || recentSet.has(w.date));

      const marathonsByDate = new Map((data.marathons ?? []).map(m => [m.date, m]));

      root.innerHTML = `
        <main>
          <section>
            <h2>The builds that matter, one panel each</h2>
            <p class="desc">Fastest five and latest five · blue line is that build · gray field is the whole history · shared scale</p>
            <div class="panel-grid">
              ${panelWeeks.map((w, i) => {
                const isPR = i === 0;
                const badge = isPR ? ' <span class="badge">PR</span>' : '';
                return `<div class="panel">
                  <div class="panel-header">
                    <span class="panel-name">${fmt.shortName(w.name ?? '', w.date)}</span>
                    <span class="panel-time">${fmt.time(w.finish_time_s)}${badge}</span>
                  </div>
                  <div class="panel-chart" id="v-e-chart-${i}"></div>
                </div>`;
              }).join('')}
            </div>
          </section>

          <section>
            <h2>Summary</h2>
            <div class="scroll">
              <table>
                <thead>
                  <tr>
                    <th>Race</th>
                    <th>Date</th>
                    <th class="r">Time</th>
                    <th class="r">Avg mpw</th>
                    <th class="r">Peak wk</th>
                  </tr>
                </thead>
                <tbody id="v-e-summary"></tbody>
              </table>
            </div>
          </section>
        </main>
      `;

      const tbody = root.querySelector('#v-e-summary');
      tbody.innerHTML = sortedWeeks.map((w, i) => {
        const isVirtual = fmt.isVirtual(w);
        const isPR = !isVirtual && i === 0;
        const rc = isPR ? 'pr' : isVirtual ? 'virt' : (w.finish_time_s != null && w.finish_time_s < 10800 ? 'sub3' : '');
        const m = marathonsByDate.get(w.date);
        return `<tr class="${rc}">
          ${td(w.name, '')}
          ${td(w.date, 'dim')}
          ${td(w.finish_time ?? fmt.time(w.finish_time_s), 'r bold')}
          ${td(m?.build?.avg_mpw?.toFixed(1))}
          ${td(m?.build?.peak_week?.toFixed(1))}
        </tr>`;
      }).join('');

      const { makeOption } = buildShared(sortedWeeks, theme);
      const charts = panelWeeks.map((w, i) => {
        const chart = echarts.init(root.querySelector(`#v-e-chart-${i}`));
        chart.setOption(makeOption(sortedWeeks.indexOf(w)));
        return chart;
      });

      function onResize() { charts.forEach(c => c.resize()); }
      window.addEventListener('resize', onResize);

      return () => {
        window.removeEventListener('resize', onResize);
        charts.forEach(c => c.dispose());
      };
    },
  });
})();
