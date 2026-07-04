// Variant G — Distance builds: the C treatment (stat strip + chart + dense
// table) applied beyond the marathon to 5K/10K/half builds behind a bucket
// switcher. Unlike other variants, the lab only prefetches marathon data, so
// this variant fetches its own from /api/distance-builds(-weeks) per bucket.
(function () {
  const CSS = `
    .v-g main { max-width: 1600px; padding: 1rem 2rem; }
    .v-g section { margin-bottom: 1rem; }
    .v-g .loading { color: var(--muted); font-size: 0.8125rem; padding: 0.5rem 0; }

    .v-g .stat-strip {
      display: flex;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    .v-g .stat { padding: 0.625rem 1.25rem; }
    .v-g .stat + .stat { border-left: 1px solid var(--border); }
    .v-g .stat-label {
      font-size: 0.625rem;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      color: var(--muted);
      margin-bottom: 0.1875rem;
    }
    .v-g .stat-value {
      display: flex;
      align-items: baseline;
      gap: 0.375rem;
      font-size: 1.0625rem;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
      color: var(--text);
    }
    .v-g .stat-sub { font-size: 0.6875rem; font-weight: 400; color: var(--muted); }

    .v-g .chart-wrap { height: 360px; }

    .v-g tbody td { padding: 0.25rem 0.75rem; font-size: 0.75rem; }
    .v-g thead th { padding: 0.375rem 0.75rem; font-size: 0.625rem; }
  `;
  const style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);

  const BUCKETS = ['5K', '10K', 'Half'];

  // Same gradient used by index.html / variant-c's fastest-5 highlight.
  function gradientColor(rank, total, dark) {
    const t = total > 1 ? rank / (total - 1) : 0;
    const l = dark ? 58 + t * 10 : 42 + t * 6;
    return `hsl(${Math.round(215 + t * (15 - 215))}, 82%, ${l}%)`;
  }

  // seconds -> "+M:SS" (or "+H:MM:SS" past an hour); negative deltas get "-".
  function fmtDelta(s) {
    if (s == null) return null;
    const sign = s < 0 ? '-' : '+';
    const abs = Math.abs(s);
    const h = Math.floor(abs / 3600);
    const m = String(Math.floor((abs % 3600) / 60)).padStart(h ? 2 : 1, '0');
    const sec = String(Math.round(abs % 60)).padStart(2, '0');
    return h ? `${sign}${h}:${m}:${sec}` : `${sign}${m}:${sec}`;
  }

  function td(val, cls = 'r') {
    if (val == null) return `<td class="dash">—</td>`;
    return `<td class="${cls}">${val}</td>`;
  }

  LAB.register({
    id: 'g',
    name: 'Distance builds',
    blurb: 'The C treatment applied beyond the marathon: 5K, 10K, and half builds behind one switcher.',
    render(root, _data, theme) {
      let chart = null;
      let onResize = null;
      let activeToken = 0; // bumped per fetch so a stale response can't clobber a newer tab

      root.innerHTML = `
        <main>
          <section>
            <div class="mode-tabs" id="v-g-tabs">
              ${BUCKETS.map((b, i) => `<button class="mode-tab${i === 0 ? ' active' : ''}" data-bucket="${b}">${b}</button>`).join('')}
            </div>
          </section>
          <section id="v-g-body"><div class="loading">Loading…</div></section>
        </main>
      `;

      function disposeChart() {
        if (onResize) { window.removeEventListener('resize', onResize); onResize = null; }
        if (chart) { chart.dispose(); chart = null; }
      }

      function renderBody(body, builds, weeks) {
        const real = builds.filter(m => !fmt.isVirtual(m));
        const virt = builds.filter(m => fmt.isVirtual(m));
        const sortedReal = [...real].sort((a, b) => (a.finish_time_s ?? Infinity) - (b.finish_time_s ?? Infinity));
        const sorted = [...sortedReal, ...virt];
        const pr = sortedReal[0];
        const latest = [...real].sort((a, b) => (b.date ?? '').localeCompare(a.date ?? ''))[0];
        const buildWeeks = sorted[0]?.build_weeks ?? 8;

        const avgVals = real.map(m => m.build?.avg_mpw).filter(v => v != null);
        const peakVals = real.map(m => m.build?.peak_week).filter(v => v != null);
        const bestAvg = avgVals.length ? Math.max(...avgVals) : null;
        const bestPeak = peakVals.length ? Math.max(...peakVals) : null;

        const weeksByDate = new Map(weeks.map(w => [w.date, w.weeks]));

        body.innerHTML = `
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
                <div class="stat-label">Races</div>
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
            </div>
          </section>

          <section>
            <div class="chart-wrap"><div id="v-g-chart" style="width:100%;height:100%"></div></div>
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
                <tbody id="v-g-tbody"></tbody>
              </table>
            </div>
          </section>
        `;

        const tbody = body.querySelector('#v-g-tbody');
        tbody.innerHTML = sorted.map((r, i) => {
          const isVirtual = fmt.isVirtual(r);
          const isPR = !isVirtual && i === 0;
          const rc = isPR ? 'pr' : isVirtual ? 'virt' : '';
          const bt = r.build?.by_type ?? {};
          const wko = bt.workout, lr = bt.long_run, easy = bt.easy;
          const delta = !isPR && r.finish_time_s != null && pr?.finish_time_s != null
            ? r.finish_time_s - pr.finish_time_s : null;

          return `<tr class="${rc}">
            ${td(r.name, '')}
            ${td(r.date, 'dim')}
            ${td(r.finish_time, 'r bold')}
            ${isPR ? `<td class="r dim">—</td>` : td(fmtDelta(delta), 'r dim')}
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

        // --- Chart: fixed fastest-5 highlight, adapted from variant-c ---
        const offsets = Array.from({ length: buildWeeks + 1 }, (_, i) => i - buildWeeks);
        const labels = offsets.map(o => o === 0 ? 'Race wk' : String(Math.abs(o)));

        const chartSeries = sorted.map((m, i) => {
          const isVirtual = fmt.isVirtual(m);
          const isPR = !isVirtual && i === 0;
          const highlighted = !isVirtual && i < 5;
          const weekMap = Object.fromEntries((weeksByDate.get(m.date) ?? []).map(w => [w.offset, w.miles]));
          const shortName = fmt.shortName(m.name ?? '', m.date);

          const color = highlighted ? gradientColor(i, 5, theme.isDark) : theme.baseline;
          const lineWidth = isPR ? 3 : highlighted ? 2 : 1;
          const lineOpacity = highlighted || isVirtual ? 1 : 0.35;
          // Natural color across the full spectrum — emphasis color so hovering
          // a gray line reveals its identity.
          const naturalColor = isVirtual ? theme.muted : gradientColor(i, real.length, theme.isDark);
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

          return {
            name: shortName,
            type: 'line',
            color,
            z: isPR ? 10 : highlighted ? 2 : 1,
            data: offsets.map(o => weekMap[o] ?? 0),
            connectNulls: false,
            symbol: 'none',
            lineStyle: { color, width: lineWidth, opacity: lineOpacity, type: isVirtual ? 'dashed' : 'solid' },
            emphasis: { focus: 'series', lineStyle: { color: emphasisColor, width: lineWidth + 1.5, opacity: 1 } },
            blur: { lineStyle: { opacity: 0.12 } },
            ...(highlighted ? { endLabel: { show: true, formatter: () => shortName, fontSize: 11, color: theme.textSecondary } } : {}),
            ...(isPR ? prMark : {}),
          };
        });

        chart = echarts.init(body.querySelector('#v-g-chart'));
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

        onResize = () => { chart.resize(); staggerEndLabels(chart); };
        window.addEventListener('resize', onResize);
      }

      async function load(bucket) {
        const token = ++activeToken;
        disposeChart();
        const body = root.querySelector('#v-g-body');
        body.innerHTML = `<div class="loading">Loading…</div>`;

        let builds, weeks;
        try {
          const [bRes, wRes] = await Promise.all([
            fetch(`/api/distance-builds?bucket=${encodeURIComponent(bucket)}`),
            fetch(`/api/distance-build-weeks?bucket=${encodeURIComponent(bucket)}`),
          ]);
          if (!root.isConnected || token !== activeToken) return;
          if (!bRes.ok || !wRes.ok) {
            body.innerHTML = `<div class="loading">Endpoints not wired yet</div>`;
            return;
          }
          builds = await bRes.json();
          weeks = await wRes.json();
        } catch {
          if (!root.isConnected || token !== activeToken) return;
          body.innerHTML = `<div class="loading">Endpoints not wired yet</div>`;
          return;
        }
        if (!root.isConnected || token !== activeToken) return;

        renderBody(body, builds, weeks);
      }

      const tabs = root.querySelectorAll('.mode-tab');
      tabs.forEach(btn => {
        btn.addEventListener('click', () => {
          tabs.forEach(b => b.classList.remove('active'));
          btn.classList.add('active');
          load(btn.dataset.bucket);
        });
      });

      load(BUCKETS[0]);

      return () => {
        disposeChart();
      };
    },
  });
})();
