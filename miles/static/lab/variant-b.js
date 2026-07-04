// Variant B — "Refined utility": same data, sharpened hierarchy — header +
// stat tiles, direct-labeled chart (no legend), one merged builds table.
(function () {
  const css = document.createElement('style');
  css.textContent = `
    .v-b h1 {
      font-size: 1.25rem;
      font-weight: 650;
      letter-spacing: -0.02em;
      margin-bottom: 0.25rem;
    }
    .v-b .subtitle {
      font-size: 0.8125rem;
      color: var(--muted);
      margin-bottom: 1.5rem;
    }
  `;
  document.head.appendChild(css);

  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
                   'July', 'August', 'September', 'October', 'November', 'December'];

  // "2018-06-16" -> "June 2018"
  function monthYear(dateStr) {
    const [y, m] = dateStr.split('-');
    return `${MONTHS[parseInt(m, 10) - 1]} ${y}`;
  }

  // seconds -> "+5:19" (< 1h) or "+1:02:07" (>= 1h)
  function deltaFmt(s) {
    if (s == null) return null;
    if (s < 3600) {
      const m = Math.floor(s / 60);
      const sec = String(Math.round(s % 60)).padStart(2, '0');
      return `+${m}:${sec}`;
    }
    const h = Math.floor(s / 3600);
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
    const sec = String(Math.round(s % 60)).padStart(2, '0');
    return `+${h}:${m}:${sec}`;
  }

  // copied from index.html — chart gradient + colors
  function gradientColor(rank, total, dark) {
    const t = total > 1 ? rank / (total - 1) : 0;
    const l = dark ? 58 + t * 10 : 42 + t * 6;
    return `hsl(${Math.round(215 + t * (15 - 215))}, 82%, ${l}%)`;
  }

  function recentColor(rank, total, dark) {
    const t = total > 1 ? rank / (total - 1) : 0;
    const l = dark ? 60 : 45;
    return `hsl(${Math.round(48 + t * (20 - 48))}, 90%, ${l}%)`;
  }

  // full "name · h:mm" label, used for series name / tooltip rows (from index.html)
  function legendLabel(name, finish_time_s, date) {
    const nameYear = (name.match(/\d{4}/) ?? [])[0];
    const fallback = date ? date.slice(0, 4) : null;
    const year = nameYear ?? fallback;
    const yr = year ? `'${year.slice(2)}` : '';
    let short = name
      .replace("Grandma's Marathon", "Grandma's")
      .replace('Marathon', '')
      .replace('Virtual', '(v)')
      .replace(/\s+/g, ' ').trim();
    if (nameYear) {
      short = short.replace(nameYear, yr).replace(/\s+/g, ' ').trim();
    } else if (year) {
      short = `${short} ${yr}`;
    }
    const h = Math.floor(finish_time_s / 3600);
    const m = String(Math.floor((finish_time_s % 3600) / 60)).padStart(2, '0');
    return `${short} · ${h}:${m}`;
  }

  function td(val, cls = 'r') {
    if (val == null) return `<td class="dash">—</td>`;
    return `<td class="${cls}">${val}</td>`;
  }

  function rowClass(r, isVirtual) {
    if (isVirtual) return 'virt';
    const t = r.finish_time_s;
    if (t != null && t < 10800) return 'sub3';
    return '';
  }

  function render(root, data, theme) {
    const marathons = data.marathons;
    const real = marathons.filter(r => !r.name?.includes('Virtual'));
    const virt = marathons.filter(r => r.name?.includes('Virtual'));
    const sortedReal = [...real].sort((a, b) => (a.finish_time_s ?? 99999) - (b.finish_time_s ?? 99999));
    const sorted = [...sortedReal, ...virt];

    const pr = sortedReal[0];
    const latest = [...real].sort((a, b) => b.date.localeCompare(a.date))[0];
    const earliestYear = real.reduce((min, r) => (r.date < min ? r.date : min), real[0].date).slice(0, 4);

    const withPeak = real.filter(r => r.build?.peak_week != null);
    const peakHolder = withPeak.length
      ? withPeak.reduce((max, r) => (r.build.peak_week > max.build.peak_week ? r : max), withPeak[0])
      : null;

    const subtitle = `${real.length} marathons since ${earliestYear} · `
      + `PR ${fmt.time(pr.finish_time_s)} · ${fmt.shortName(pr.name, pr.date)}, ${monthYear(pr.date)}`;

    root.innerHTML = `
      <main>
        <h1>Marathons</h1>
        <p class="subtitle"></p>

        <div class="tiles">
          <div class="tile">
            <div class="tile-label">PR</div>
            <div class="tile-value">${fmt.time(pr.finish_time_s)}</div>
            <div class="tile-sub">${fmt.shortName(pr.name, pr.date)}</div>
          </div>
          <div class="tile">
            <div class="tile-label">Latest</div>
            <div class="tile-value">${fmt.time(latest.finish_time_s)}</div>
            <div class="tile-sub">${fmt.shortName(latest.name, latest.date)}</div>
          </div>
          <div class="tile">
            <div class="tile-label">Best build</div>
            <div class="tile-value">${pr.build?.avg_mpw != null ? `${pr.build.avg_mpw.toFixed(1)} mpw` : '—'}</div>
            <div class="tile-sub">average, ${fmt.shortName(pr.name, pr.date)}</div>
          </div>
          <div class="tile">
            <div class="tile-label">Peak week</div>
            <div class="tile-value">${peakHolder ? `${peakHolder.build.peak_week.toFixed(1)} mi` : '—'}</div>
            <div class="tile-sub">${peakHolder ? fmt.shortName(peakHolder.name, peakHolder.date) : '—'}</div>
          </div>
          <div class="tile">
            <div class="tile-label">Marathons</div>
            <div class="tile-value">${real.length}</div>
            ${virt.length ? `<div class="tile-sub">+${virt.length} virtual</div>` : ''}
          </div>
        </div>

        <section>
          <h2>Weekly Mileage by Build</h2>
          <p class="desc">Each line is one build · direct labels replace the legend · hover any gray line to identify it</p>
          <div class="mode-tabs">
            <button class="mode-tab active" data-mode="fastest">Fastest 5</button>
            <button class="mode-tab" data-mode="recent">Recent 3</button>
            <button class="mode-tab" data-mode="focus">PR vs Recent</button>
          </div>
          <div class="chart-wrap">
            <div class="chart-builds" style="width:100%;height:100%"></div>
          </div>
        </section>

        <section>
          <h2>Builds</h2>
          <p class="desc">Sorted fastest to slowest · Δ against the PR</p>
          <div class="scroll">
            <table>
              <thead>
                <tr>
                  <th>Race</th>
                  <th>Date</th>
                  <th class="r">Time</th>
                  <th class="r">Δ PR</th>
                  <th class="r">Avg mpw</th>
                  <th class="r">Peak wk</th>
                  <th class="r">Peak 3-wk</th>
                  <th class="r">Workouts</th>
                  <th class="r">WO pace</th>
                  <th class="r">Long runs</th>
                  <th class="r">LR pace</th>
                  <th class="r">Easy HR</th>
                </tr>
              </thead>
              <tbody class="body-builds"></tbody>
            </table>
          </div>
        </section>
      </main>
    `;

    root.querySelector('.subtitle').textContent = subtitle;

    // --- merged table ---

    const rows = sorted.map((r, i) => {
      const isVirtual = r.name?.includes('Virtual') ?? false;
      const isPR      = !isVirtual && i === 0;
      const rc        = isPR ? 'pr' : rowClass(r, isVirtual);
      const bt        = r.build?.by_type ?? {};
      const wko       = bt.workout;
      const lr        = bt.long_run;
      const easy      = bt.easy;
      const deltaS    = r.finish_time_s != null && pr.finish_time_s != null ? r.finish_time_s - pr.finish_time_s : null;

      return `<tr class="${rc}">
        ${td(r.name, '')}
        ${td(r.date, 'dim')}
        ${td(r.finish_time, 'r bold')}
        ${isPR ? `<td class="dash">—</td>` : td(deltaFmt(deltaS), 'r dim')}
        ${td(r.build?.avg_mpw?.toFixed(1))}
        ${td(r.build?.peak_week?.toFixed(1))}
        ${td(r.build?.peak_3wk_avg?.toFixed(1))}
        ${td(wko?.runs)}
        ${td(fmt.pace(wko?.avg_pace_min_per_mile))}
        ${td(lr?.runs)}
        ${td(fmt.pace(lr?.avg_pace_min_per_mile))}
        ${td(easy?.avg_hr != null ? Math.round(easy.avg_hr) : null)}
      </tr>`;
    });

    root.querySelector('.body-builds').innerHTML = rows.join('');

    // --- chart ---

    const weeksData  = data.weeks;
    const BUILD_WEEKS = 12;
    const offsets = Array.from({ length: BUILD_WEEKS + 1 }, (_, i) => i - BUILD_WEEKS);
    const labels  = offsets.map(o => o === 0 ? 'Race wk' : String(Math.abs(o)));

    const wReal   = weeksData.filter(r => !r.name?.includes('Virtual'))
                              .sort((a, b) => (a.finish_time_s ?? 99999) - (b.finish_time_s ?? 99999));
    const wVirt   = weeksData.filter(r => r.name?.includes('Virtual'));
    const wSorted = [...wReal, ...wVirt];

    const byDate      = [...wReal].sort((a, b) => b.date.localeCompare(a.date));
    const recent3Set  = new Set(byDate.slice(0, 3).map(m => m.date));
    const recentRank  = new Map(byDate.slice(0, 3).map((m, i) => [m.date, i]));
    const recentIdx   = wSorted.findIndex(m => m.date === byDate[0].date);

    function makeSeries(mode) {
      return wSorted.map((marathon, i) => {
        const isVirtual = marathon.name?.includes('Virtual') ?? false;
        const isPR      = !isVirtual && i === 0;
        const inRecent3 = recent3Set.has(marathon.date ?? '');
        const isRecent1 = i === recentIdx;
        const weekMap   = Object.fromEntries(marathon.weeks.map(w => [w.offset, w.miles]));
        const label     = legendLabel(marathon.name ?? '', marathon.finish_time_s ?? 0, marathon.date);
        const shortLabel = fmt.shortName(marathon.name ?? '', marathon.date);

        let color, lineWidth, lineOpacity, highlighted;

        // Natural color each series would have across the full spectrum —
        // used as the emphasis color so hovering a gray line reveals it.
        const naturalColor = isVirtual ? theme.muted : gradientColor(i, wReal.length, theme.isDark);

        if (mode === 'fastest') {
          highlighted  = !isVirtual && i < 5;
        } else if (mode === 'recent') {
          highlighted  = inRecent3 && !isVirtual;
        } else { // focus: PR vs most recent
          highlighted  = isPR || isRecent1;
        }

        if (highlighted) {
          if (mode === 'fastest') {
            color       = gradientColor(i, 5, theme.isDark);
            lineWidth   = isPR ? 3 : 2;
            lineOpacity = 1;
          } else if (mode === 'recent') {
            const rRank = recentRank.get(marathon.date ?? '') ?? 0;
            color       = recentColor(rRank, 3, theme.isDark);
            lineWidth   = 2.5;
            lineOpacity = 1;
          } else {
            color       = isPR ? theme.accent : theme.series[2];
            lineWidth   = 3;
            lineOpacity = 1;
          }
        } else {
          // Context lines: uniform low-emphasis treatment regardless of mode.
          color       = isVirtual ? theme.muted : theme.baseline;
          lineWidth   = 1;
          lineOpacity = 0.35;
        }

        const emphasisColor = highlighted ? color : naturalColor;
        const endLabelFormatter = () => `${shortLabel} · ${fmt.timeShort(marathon.finish_time_s ?? 0)}`;

        const prMark = {
          markPoint: {
            symbol: 'roundRect',
            symbolSize: [24, 14],
            symbolOffset: [0, -20],
            itemStyle: { color: theme.accent, borderRadius: 2 },
            data: [{ type: 'max', label: { show: true, formatter: 'PR', color: theme.surface, fontSize: 9, fontWeight: 700 } }],
          },
        };
        const dotMark = {
          markPoint: {
            symbol: 'circle', symbolSize: 6,
            itemStyle: { color, borderColor: theme.surface, borderWidth: 1.5 },
            data: [{ type: 'max', label: { show: false } }],
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
          name: label,
          type: 'line',
          color,
          z: isPR ? 10 : highlighted ? 2 : 1,
          data: offsets.map(o => weekMap[o] ?? 0),
          connectNulls: false,
          symbol: 'none',
          lineStyle: { color, width: lineWidth, opacity: lineOpacity, type: isVirtual ? 'dashed' : 'solid' },
          endLabel: highlighted
            ? { show: true, formatter: endLabelFormatter, fontSize: 11, color: theme.textSecondary, fontWeight: 600 }
            : { show: false },
          labelLayout: { moveOverlap: 'shiftY' },
          emphasis: {
            focus: 'series',
            lineStyle: { color: emphasisColor, width: lineWidth + 1.5, opacity: 1 },
            // Hovering a gray context line reveals its identity via end label too.
            endLabel: { show: true, formatter: endLabelFormatter, fontSize: 11, color: theme.textSecondary, fontWeight: 600 },
          },
          blur:     { lineStyle: { opacity: 0.12 } },
          ...(isPR ? prMark : highlighted ? dotMark : {}),
          ...(i === 0 ? taper : {}),
        };
      });
    }

    const chart = echarts.init(root.querySelector('.chart-builds'));
    chart.setOption({
      animation: false,
      grid: { top: 16, right: 150, bottom: 24, left: 52 },
      tooltip: {
        trigger: 'axis',
        axisPointer: { type: 'line', lineStyle: { color: theme.gridline, width: 1 } },
        backgroundColor: theme.surface,
        borderColor: theme.gridline,
        textStyle: { fontSize: 12, color: theme.text },
        formatter(params) {
          const lbl   = params[0].axisValue;
          const title = lbl === 'Race wk' ? 'Race week'
                      : `${lbl} week${lbl === '1' ? '' : 's'} before race`;
          const rows  = params
            .filter(p => p.value > 0)
            .sort((a, b) => b.value - a.value)
            .map(p => `<div style="display:flex;justify-content:space-between;gap:1rem">
                         <span>${p.marker}${p.seriesName}</span>
                         <b>${p.value.toFixed(1)} mi</b>
                       </div>`);
          const visible = rows.slice(0, 8);
          const extra   = rows.length - visible.length;
          const more    = extra > 0
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
        axisLabel: { fontSize: 11, color: theme.muted },
        name: 'Weeks before race',
        nameLocation: 'middle',
        nameGap: 28,
        nameTextStyle: { fontSize: 11, color: theme.muted },
      },
      yAxis: {
        type: 'value',
        name: 'Miles',
        nameLocation: 'middle',
        nameGap: 40,
        nameTextStyle: { fontSize: 11, color: theme.muted },
        axisLine: { show: false },
        axisTick: { show: false },
        splitLine: { lineStyle: { color: theme.gridline } },
        axisLabel: { fontSize: 11, color: theme.muted },
        min: val => Math.max(0, Math.floor((val.min - 5) / 5) * 5),
      },
      series: makeSeries('fastest'),
    });
    staggerEndLabels(chart);

    root.querySelectorAll('.mode-tab').forEach(btn => {
      btn.addEventListener('click', () => {
        root.querySelectorAll('.mode-tab').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        chart.setOption({ series: makeSeries(/** @type {string} */ (btn.dataset.mode)) }, { replaceMerge: ['series'] });
        staggerEndLabels(chart);
      });
    });

    function onResize() { chart.resize(); staggerEndLabels(chart); }
    window.addEventListener('resize', onResize);

    return () => {
      window.removeEventListener('resize', onResize);
      chart.dispose();
    };
  }

  LAB.register({
    id: 'b',
    name: 'Refined utility',
    blurb: 'Same quiet tool, sharpened: real hierarchy, stat tiles, direct-labeled chart, one merged table.',
    render,
  });
})();
