// Variant F — "Comparison workbench": pick up to four builds; chart and
// stat panel update together. Interaction as the interface.

(function () {
  const style = document.createElement('style');
  style.textContent = `
    .v-f .chart-wrap { height: 420px; }
    .v-f th .vf-name {
      display: flex;
      align-items: center;
      gap: 0.35rem;
      font-size: 0.8125rem;
      font-weight: 600;
      color: var(--text);
      text-transform: none;
      letter-spacing: normal;
    }
    .v-f th .vf-time {
      font-size: 0.6875rem;
      color: var(--muted);
      font-weight: 400;
      margin-top: 0.15rem;
      text-transform: none;
      letter-spacing: normal;
    }
    .v-f td.vf-best { font-weight: 700; color: var(--good); }
    .v-f .chip[data-capped="true"] { opacity: 0.5; cursor: not-allowed; }
  `;
  document.head.appendChild(style);

  LAB.register({
    id: 'f',
    name: 'Comparison workbench',
    blurb: 'Pick up to four builds; the chart and a stat-by-stat panel update — interaction as the interface.',
    render(root, data, theme) {
      const PALETTE = [theme.series[0], theme.series[7], theme.series[3], theme.series[4]];
      const BUILD_WEEKS = 12;
      const offsets = Array.from({ length: BUILD_WEEKS + 1 }, (_, i) => i - BUILD_WEEKS);
      const labels = offsets.map(o => o === 0 ? 'Race wk' : String(Math.abs(o)));

      const real = data.marathons.filter(m => !fmt.isVirtual(m))
        .sort((a, b) => (a.finish_time_s ?? Infinity) - (b.finish_time_s ?? Infinity));
      const virt = data.marathons.filter(fmt.isVirtual);
      const combined = [...real, ...virt];
      const pr = real[0];
      const recent = [...real].sort((a, b) => b.date.localeCompare(a.date))[0];

      const weeksByDate = new Map(data.weeks.map(w => [w.date, w.weeks]));
      const getEntity = date => combined.find(m => m.date === date);

      // Map<date, slot> — slot assignment persists for an entity while selected.
      const selection = new Map();
      selection.set(pr.date, 0);
      selection.set(recent.date, 1);

      const main = document.createElement('main');
      main.innerHTML = `
        <section>
          <h2>Compare builds</h2>
          <p class="desc">Click chips to add or remove a build (max 4) · colors stick to a build while selected</p>
          <div class="chip-row" id="vf-chips"></div>
          <div class="chart-wrap"><div id="vf-chart" style="width:100%;height:100%"></div></div>
        </section>

        <section>
          <h2>Side by side</h2>
          <div class="scroll">
            <table>
              <thead id="vf-thead"></thead>
              <tbody id="vf-tbody"></tbody>
            </table>
          </div>
        </section>
      `;
      root.appendChild(main);

      const chart = echarts.init(document.getElementById('vf-chart'));

      function selectedEntries() {
        return [...selection.entries()].sort((a, b) => a[1] - b[1]);
      }

      function toggle(date) {
        if (selection.has(date)) {
          selection.delete(date);
        } else {
          if (selection.size >= 4) return;
          const used = new Set(selection.values());
          let slot = 0;
          while (used.has(slot)) slot++;
          selection.set(date, slot);
        }
        rerender();
      }

      function renderChips() {
        const atCap = selection.size >= 4;
        document.getElementById('vf-chips').innerHTML = combined.map(m => {
          const sel = selection.has(m.date);
          const color = sel ? PALETTE[selection.get(m.date)] : 'var(--baseline)';
          const capped = atCap && !sel;
          const label = `${fmt.shortName(m.name, m.date)} · ${fmt.timeShort(m.finish_time_s)}`;
          return `<button class="chip${sel ? ' selected' : ''}" data-date="${m.date}"
            ${capped ? 'data-capped="true" title="deselect one first"' : ''}>
            <span class="dot" style="background:${color}"></span>${label}
          </button>`;
        }).join('');
      }

      function chartSeries() {
        return selectedEntries().map(([date, slot]) => {
          const m = getEntity(date);
          const isVirtual = fmt.isVirtual(m);
          const color = PALETTE[slot];
          const weekMap = Object.fromEntries((weeksByDate.get(date) || []).map(w => [w.offset, w.miles]));
          return {
            name: m.name,
            type: 'line',
            color,
            data: offsets.map(o => weekMap[o] ?? 0),
            connectNulls: false,
            symbol: 'none',
            lineStyle: { color, width: 2.5, type: isVirtual ? 'dashed' : 'solid' },
            endLabel: { show: true, formatter: fmt.shortName(m.name, m.date), fontSize: 11, color: theme.textSecondary, fontWeight: 600 },
            labelLayout: { moveOverlap: 'shiftY' },
          };
        });
      }

      function renderChart() {
        chart.setOption({
          animation: false,
          grid: { top: 16, right: 140, bottom: 24, left: 58 },
          tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'line', lineStyle: { color: theme.gridline, width: 1 } },
            backgroundColor: theme.surface,
            borderColor: theme.gridline,
            textStyle: { fontSize: 12, color: theme.text },
            formatter(params) {
              const lbl = params[0].axisValue;
              const title = lbl === 'Race wk' ? 'Race week' : `${lbl} week${lbl === '1' ? '' : 's'} before race`;
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
            axisLabel: { fontSize: 11, color: theme.muted },
          },
          yAxis: {
            type: 'value',
            name: 'Miles / week',
            nameLocation: 'middle',
            nameGap: 40,
            nameTextStyle: { fontSize: 11, color: theme.muted },
            axisLine: { show: false },
            axisTick: { show: false },
            splitLine: { lineStyle: { color: theme.gridline } },
            axisLabel: { fontSize: 11, color: theme.muted },
          },
          series: chartSeries(),
        }, { replaceMerge: ['series'] });
        staggerEndLabels(chart);
      }

      function metricRow(label, values, higherBetter, formatter, boldAlways, titles) {
        const nonNull = values.filter(v => v != null);
        const best = nonNull.length >= 2 ? (higherBetter ? Math.max(...nonNull) : Math.min(...nonNull)) : null;
        const cells = values.map((v, i) => {
          if (v == null) return `<td class="r dash">—</td>`;
          const isBest = best != null && v === best;
          const cls = ['r', ...(boldAlways ? ['bold'] : []), ...(isBest ? ['vf-best'] : [])];
          const title = titles?.[i] ? ` title="${titles[i]}"` : '';
          return `<td class="${cls.join(' ')}"${title}>${formatter(v)}</td>`;
        });
        return `<tr><td>${label}</td>${cells.join('')}</tr>`;
      }

      function paceClaimRow(label, key, cols) {
        const claims = cols.map(c => c.m.build?.pace_claims?.[key] ?? null);
        return metricRow(
          label,
          claims.map(c => c?.pace_min_per_mile ?? null),
          false,
          v => fmt.pace(v),
          false,
          claims.map(c => c ? `${c.workouts} workout${c.workouts === 1 ? '' : 's'}` : null),
        );
      }

      function renderTable() {
        const cols = selectedEntries().map(([date, slot]) => ({ m: getEntity(date), slot }));

        document.getElementById('vf-thead').innerHTML = `<tr>
          <th>Metric</th>
          ${cols.map(c => `<th class="r">
            <div class="vf-name"><span class="dot" style="background:${PALETTE[c.slot]}"></span>${fmt.shortName(c.m.name, c.m.date)}</div>
            <div class="vf-time">${fmt.time(c.m.finish_time_s)}</div>
          </th>`).join('')}
        </tr>`;

        const rows = [
          metricRow('Result', cols.map(c => c.m.finish_time_s ?? null), false, s => fmt.time(s), true),
          metricRow('Avg mpw', cols.map(c => c.m.build?.avg_mpw ?? null), true, v => v.toFixed(1)),
          metricRow('Peak week', cols.map(c => c.m.build?.peak_week ?? null), true, v => v.toFixed(1)),
          metricRow('Peak 3-wk', cols.map(c => c.m.build?.peak_3wk_avg ?? null), true, v => v.toFixed(1)),
          metricRow('Total miles', cols.map(c => c.m.build?.total_miles ?? null), true, v => v.toFixed(0)),
          metricRow('Workouts', cols.map(c => c.m.build?.by_type?.workout?.runs ?? null), true, v => String(v)),
          paceClaimRow('5K work', '5k', cols),
          paceClaimRow('LT / tempo', 'lt', cols),
          paceClaimRow('MP work', 'mp', cols),
          metricRow('Long runs', cols.map(c => c.m.build?.by_type?.long_run?.runs ?? null), true, v => String(v)),
          metricRow('Long-run pace', cols.map(c => c.m.build?.by_type?.long_run?.avg_pace_min_per_mile ?? null), false, v => fmt.pace(v)),
          metricRow('Easy HR', cols.map(c => c.m.build?.by_type?.easy?.avg_hr ?? null), false, v => String(Math.round(v))),
        ];
        document.getElementById('vf-tbody').innerHTML = rows.join('');
      }

      function rerender() {
        renderChips();
        renderChart();
        renderTable();
      }

      document.getElementById('vf-chips').addEventListener('click', e => {
        const btn = e.target.closest('.chip');
        if (btn) toggle(btn.dataset.date);
      });

      rerender();

      const onResize = () => { chart.resize(); staggerEndLabels(chart); };
      window.addEventListener('resize', onResize);

      return () => {
        window.removeEventListener('resize', onResize);
        chart.dispose();
      };
    },
  });
})();
