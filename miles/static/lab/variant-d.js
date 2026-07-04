// Variant D — "Editorial journal": hero number, computed narrative, one
// naked figure, a records list. Reads like a story, not a dashboard.

(function () {
  const style = document.createElement('style');
  style.textContent = `
    .v-d main { max-width: 60rem; }
    .v-d section { margin-bottom: 3.5rem; }
    .v-d { --lab-serif: Georgia, 'Iowan Old Style', 'Times New Roman', serif; }

    .v-d .vd-hero { margin: 2.5rem 0 3rem; }
    .v-d .vd-overline {
      font-size: 0.6875rem;
      letter-spacing: 0.14em;
      text-transform: uppercase;
      color: var(--muted);
      font-weight: 600;
      margin-bottom: 0.5rem;
    }
    .v-d .vd-hero-number {
      font-family: var(--lab-serif);
      font-size: clamp(3.5rem, 7vw, 5.5rem);
      font-weight: 400;
      letter-spacing: -0.01em;
      line-height: 1;
      color: var(--text);
      font-variant-numeric: tabular-nums;
    }
    .v-d .vd-hero-support {
      margin-top: 0.75rem;
      font-size: 0.9375rem;
      color: var(--text-secondary);
    }

    .v-d .vd-narrative {
      font-size: 1.0625rem;
      line-height: 1.65;
      max-width: 62ch;
      color: var(--text);
      margin-bottom: 3rem;
    }

    .v-d .vd-figure h2,
    .v-d .vd-record h2 {
      font-family: var(--lab-serif);
      font-size: 1.25rem;
      font-weight: 600;
      margin-bottom: 1rem;
    }

    .v-d .vd-chart-wrap { position: relative; height: 440px; }

    .v-d .vd-caption {
      margin-top: 0.75rem;
      font-size: 0.8125rem;
      color: var(--muted);
      font-style: italic;
      font-family: var(--lab-serif);
    }

    .v-d .vd-table { width: 100%; border-collapse: collapse; }
    .v-d .vd-table tr { border-top: 1px solid var(--gridline); }
    .v-d .vd-table td { padding: 0.875rem 0.5rem; }
    .v-d .vd-table td.vd-race { font-family: var(--lab-serif); font-size: 0.9375rem; color: var(--text); }
    .v-d .vd-table td.vd-date { font-size: 0.8125rem; color: var(--muted); }
    .v-d .vd-table td.vd-build { font-size: 0.8125rem; color: var(--text-secondary); }
    .v-d .vd-table td.vd-time {
      text-align: right;
      font-size: 0.9375rem;
      font-weight: 700;
      font-variant-numeric: tabular-nums;
    }
    .v-d .vd-table tr.pr td.vd-time { color: var(--good); }
    .v-d .vd-table tr.virt td { color: var(--muted); font-style: italic; }
    .v-d .vd-dash { color: var(--baseline); }
  `;
  document.head.appendChild(style);

  const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'];
  const WORDS = ['Zero', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven',
    'Eight', 'Nine', 'Ten', 'Eleven', 'Twelve', 'Thirteen', 'Fourteen'];

  function longDate(dateStr) {
    const [y, m, d] = dateStr.split('-').map(Number);
    return `${MONTHS[m - 1]} ${d}, ${y}`;
  }
  function monthYear(dateStr) {
    const [y, m] = dateStr.split('-').map(Number);
    return `${MONTHS[m - 1]} ${y}`;
  }
  function countWord(n) { return n < WORDS.length ? WORDS[n] : String(n); }

  LAB.register({
    id: 'd',
    name: 'Editorial journal',
    blurb: 'The data as a story: hero number, computed narrative, one clean figure, a records list.',
    render(root, data, theme) {
      const real = data.marathons.filter(m => !fmt.isVirtual(m))
        .sort((a, b) => (a.finish_time_s ?? Infinity) - (b.finish_time_s ?? Infinity));
      const virt = data.marathons.filter(fmt.isVirtual);
      const pr = real[0];
      const recent = [...real].sort((a, b) => b.date.localeCompare(a.date))[0];

      const years = real.map(m => Number(m.date.slice(0, 4)));
      const firstYear = Math.min(...years);
      const spanYears = Math.max(...years) - firstYear;

      const withPeak = data.marathons.filter(m => m.build?.peak_week != null);
      const maxPeak = withPeak.reduce((a, b) => (a.build.peak_week >= b.build.peak_week ? a : b));

      const narrative = `${countWord(real.length)} marathons in ${spanYears} years. `
        + `The fastest of them came off a ${pr.build.avg_mpw.toFixed(1)}-mile-a-week build `
        + `with ${pr.build.by_type?.workout?.runs ?? '—'} workouts; the most recent, ${recent.name}, `
        + `went ${fmt.time(recent.finish_time_s)} off ${recent.build.avg_mpw?.toFixed(1) ?? '—'} a week. `
        + `The biggest single training week ever: ${maxPeak.build.peak_week.toFixed(1)} miles, ${monthYear(maxPeak.date)}.`;

      const main = document.createElement('main');
      main.innerHTML = `
        <div class="vd-hero">
          <div class="vd-overline">Personal Best</div>
          <div class="vd-hero-number">${fmt.time(pr.finish_time_s)}</div>
          <div class="vd-hero-support">${pr.name} · ${longDate(pr.date)} · ${fmt.pace(pr.pace_min_per_mile)} / mi</div>
        </div>

        <p class="vd-narrative">${narrative}</p>

        <section class="vd-figure">
          <h2>The build that did it</h2>
          <div class="vd-chart-wrap"><div id="vd-chart" style="width:100%;height:100%"></div></div>
          <p class="vd-caption">Weekly mileage over the final twelve weeks. Gray: every other build since ${firstYear}.</p>
        </section>

        <section class="vd-record">
          <h2>The record</h2>
          <table class="vd-table"><tbody id="vd-record-body"></tbody></table>
        </section>
      `;
      root.appendChild(main);

      // ---- records table ----
      const sortedRows = [...real, ...virt];
      const body = document.getElementById('vd-record-body');
      body.innerHTML = sortedRows.map(r => {
        const isVirtual = fmt.isVirtual(r);
        const isPR = r === pr;
        const rowCls = isVirtual ? 'virt' : (isPR ? 'pr' : '');
        const name = `${r.name}${isPR ? ' <span class="badge">PR</span>' : ''}`;
        const buildTxt = r.build?.avg_mpw != null
          ? `${r.build.avg_mpw.toFixed(1)} mpw`
          : '<span class="vd-dash">—</span>';
        return `<tr class="${rowCls}">
          <td class="vd-race">${name}</td>
          <td class="vd-date">${r.date ?? ''}</td>
          <td class="vd-build">${buildTxt}</td>
          <td class="vd-time">${r.finish_time ?? '—'}</td>
        </tr>`;
      }).join('');

      // ---- figure ----
      const BUILD_WEEKS = 12;
      const offsets = Array.from({ length: BUILD_WEEKS + 1 }, (_, i) => i - BUILD_WEEKS);
      const labels = offsets.map(o => o === 0 ? 'Race wk' : String(Math.abs(o)));

      function series() {
        return data.weeks.map(m => {
          const isVirtual = fmt.isVirtual(m);
          const isPR = m.date === pr.date;
          const isRecent = m.date === recent.date;
          const focal = isPR || isRecent;
          const weekMap = Object.fromEntries((m.weeks || []).map(w => [w.offset, w.miles]));
          const color = isPR ? theme.accent : isRecent ? theme.series[2] : theme.gridline;
          const label = `${fmt.shortName(m.name, m.date)} · ${fmt.timeShort(m.finish_time_s)}`;

          return {
            name: m.name,
            type: 'line',
            color,
            z: focal ? 10 : 1,
            data: offsets.map(o => weekMap[o] ?? 0),
            connectNulls: false,
            symbol: 'none',
            silent: !focal,
            lineStyle: { color, width: focal ? 2.5 : 1, opacity: focal ? 1 : 0.45, type: isVirtual ? 'dashed' : 'solid' },
            ...(focal ? {
              endLabel: { show: true, formatter: label, fontSize: 11, color: theme.textSecondary, fontWeight: 600 },
              labelLayout: { moveOverlap: 'shiftY' },
            } : {}),
          };
        });
      }

      const chart = echarts.init(document.getElementById('vd-chart'));
      chart.setOption({
        animation: false,
        grid: { top: 16, right: 150, bottom: 28, left: 58 },
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
        series: series(),
      });
      staggerEndLabels(chart);

      const onResize = () => { chart.resize(); staggerEndLabels(chart); };
      window.addEventListener('resize', onResize);

      return () => {
        window.removeEventListener('resize', onResize);
        chart.dispose();
      };
    },
  });
})();
