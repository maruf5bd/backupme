/* ═══════════════════════════════════════════════════════════
   BackupMe Dashboard — Chart.js Helpers
   ═══════════════════════════════════════════════════════════ */

const Charts = (() => {
  const instances = {};

  const COLORS = {
    accent: '#6366f1',
    green: '#22c55e',
    red: '#ef4444',
    yellow: '#f59e0b',
    blue: '#3b82f6',
    purple: '#a855f7',
    cyan: '#06b6d4',
    orange: '#f97316',
    pink: '#ec4899',
    indigo: '#6366f1',
  };

  const PALETTE = [
    COLORS.accent, COLORS.green, COLORS.blue, COLORS.purple,
    COLORS.cyan, COLORS.orange, COLORS.pink, COLORS.yellow,
    COLORS.red, COLORS.indigo,
  ];

  function getThemeColors() {
    const style = getComputedStyle(document.documentElement);
    return {
      text: style.getPropertyValue('--text-secondary').trim() || '#94a3b8',
      textMuted: style.getPropertyValue('--text-muted').trim() || '#64748b',
      grid: style.getPropertyValue('--border-subtle').trim() || '#161a25',
      surface: style.getPropertyValue('--surface').trim() || '#12151e',
    };
  }

  function baseOptions(title) {
    const tc = getThemeColors();
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 600, easing: 'easeOutQuart' },
      plugins: {
        legend: {
          display: false,
          labels: { color: tc.text, font: { size: 12 }, boxWidth: 12, padding: 16 },
        },
        tooltip: {
          backgroundColor: tc.surface,
          titleColor: tc.text,
          bodyColor: tc.text,
          borderColor: tc.grid,
          borderWidth: 1,
          cornerRadius: 8,
          padding: 10,
          titleFont: { weight: '600' },
          bodyFont: { size: 12 },
          displayColors: true,
          boxWidth: 8,
          boxHeight: 8,
          boxPadding: 4,
        },
      },
    };
  }

  /* ── Doughnut Chart ──────────────────────────────────── */
  function doughnut(canvasId, labels, data, options = {}) {
    destroy(canvasId);
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;

    const colors = options.colors || PALETTE.slice(0, data.length);
    const tc = getThemeColors();

    instances[canvasId] = new Chart(ctx, {
      type: 'doughnut',
      data: {
        labels,
        datasets: [{
          data,
          backgroundColor: colors,
          borderColor: tc.surface,
          borderWidth: 2,
          hoverOffset: 6,
        }],
      },
      options: {
        ...baseOptions(),
        cutout: options.cutout || '68%',
        plugins: {
          ...baseOptions().plugins,
          legend: {
            display: options.legend !== false,
            position: 'bottom',
            labels: {
              color: tc.text,
              font: { size: 11 },
              boxWidth: 10,
              padding: 12,
              usePointStyle: true,
              pointStyleWidth: 10,
            },
          },
          tooltip: {
            ...baseOptions().plugins.tooltip,
            callbacks: {
              label: (ctx) => {
                const val = ctx.raw;
                const formatted = options.formatValue ? options.formatValue(val) : App.formatBytes(val);
                return ` ${ctx.label}: ${formatted}`;
              },
            },
          },
        },
      },
    });

    return instances[canvasId];
  }

  /* ── Line Chart ──────────────────────────────────────── */
  function line(canvasId, labels, datasets, options = {}) {
    destroy(canvasId);
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;

    const tc = getThemeColors();

    const processedDatasets = datasets.map((ds, i) => ({
      label: ds.label || `Series ${i + 1}`,
      data: ds.data,
      borderColor: ds.color || PALETTE[i],
      backgroundColor: (ds.color || PALETTE[i]) + '1a',
      borderWidth: 2,
      fill: ds.fill !== false,
      tension: 0.4,
      pointRadius: 3,
      pointHoverRadius: 6,
      pointBackgroundColor: ds.color || PALETTE[i],
      pointBorderColor: tc.surface,
      pointBorderWidth: 2,
      ...ds,
    }));

    instances[canvasId] = new Chart(ctx, {
      type: 'line',
      data: { labels, datasets: processedDatasets },
      options: {
        ...baseOptions(),
        interaction: { mode: 'index', intersect: false },
        scales: {
          x: {
            grid: { color: tc.grid, drawBorder: false },
            ticks: { color: tc.textMuted, font: { size: 11 }, maxRotation: 0 },
            border: { display: false },
          },
          y: {
            beginAtZero: true,
            grid: { color: tc.grid, drawBorder: false },
            ticks: {
              color: tc.textMuted,
              font: { size: 11 },
              callback: options.formatY || (v => v),
            },
            border: { display: false },
          },
        },
        plugins: {
          ...baseOptions().plugins,
          legend: {
            display: datasets.length > 1,
            position: 'bottom',
            labels: {
              color: tc.text,
              font: { size: 11 },
              boxWidth: 10,
              padding: 12,
              usePointStyle: true,
            },
          },
          tooltip: {
            ...baseOptions().plugins.tooltip,
            callbacks: options.tooltipCallbacks || {},
          },
        },
      },
    });

    return instances[canvasId];
  }

  /* ── Bar Chart ───────────────────────────────────────── */
  function bar(canvasId, labels, datasets, options = {}) {
    destroy(canvasId);
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;

    const tc = getThemeColors();

    const processedDatasets = datasets.map((ds, i) => ({
      label: ds.label || `Series ${i + 1}`,
      data: ds.data,
      backgroundColor: ds.color || PALETTE[i],
      borderRadius: 6,
      borderSkipped: false,
      maxBarThickness: options.maxBarThickness || 48,
      ...ds,
    }));

    instances[canvasId] = new Chart(ctx, {
      type: 'bar',
      data: { labels, datasets: processedDatasets },
      options: {
        ...baseOptions(),
        scales: {
          x: {
            grid: { display: false },
            ticks: { color: tc.textMuted, font: { size: 11 } },
            border: { display: false },
          },
          y: {
            beginAtZero: true,
            grid: { color: tc.grid, drawBorder: false },
            ticks: {
              color: tc.textMuted,
              font: { size: 11 },
              callback: options.formatY || (v => v),
            },
            border: { display: false },
          },
        },
        plugins: {
          ...baseOptions().plugins,
          legend: {
            display: datasets.length > 1,
            position: 'bottom',
            labels: { color: tc.text, font: { size: 11 }, boxWidth: 10, padding: 12 },
          },
          tooltip: {
            ...baseOptions().plugins.tooltip,
            callbacks: options.tooltipCallbacks || {},
          },
        },
      },
    });

    return instances[canvasId];
  }

  /* ── Horizontal Bar ──────────────────────────────────── */
  function horizontalBar(canvasId, labels, data, options = {}) {
    destroy(canvasId);
    const ctx = document.getElementById(canvasId);
    if (!ctx) return null;

    const tc = getThemeColors();
    const colors = options.colors || PALETTE.slice(0, data.length);

    instances[canvasId] = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [{
          data,
          backgroundColor: colors,
          borderRadius: 6,
          borderSkipped: false,
          maxBarThickness: 28,
        }],
      },
      options: {
        ...baseOptions(),
        indexAxis: 'y',
        scales: {
          x: {
            beginAtZero: true,
            grid: { color: tc.grid, drawBorder: false },
            ticks: {
              color: tc.textMuted,
              font: { size: 11 },
              callback: options.formatX || (v => v),
            },
            border: { display: false },
          },
          y: {
            grid: { display: false },
            ticks: { color: tc.textMuted, font: { size: 11 } },
            border: { display: false },
          },
        },
        plugins: {
          ...baseOptions().plugins,
          legend: { display: false },
          tooltip: {
            ...baseOptions().plugins.tooltip,
            callbacks: {
              label: (ctx) => {
                const val = ctx.raw;
                return options.formatValue ? options.formatValue(val) : val;
              },
            },
          },
        },
      },
    });

    return instances[canvasId];
  }

  /* ── SVG Gauge ───────────────────────────────────────── */
  function createGauge(containerId, value, max = 100, options = {}) {
    const container = document.getElementById(containerId);
    if (!container) return;

    const size = options.size || 120;
    const strokeWidth = options.strokeWidth || 8;
    const radius = (size - strokeWidth) / 2;
    const circumference = 2 * Math.PI * radius;
    const percent = Math.min(Math.max(value / max, 0), 1);
    const offset = circumference * (1 - percent);

    let colorClass = options.color || 'accent';
    if (options.colorThresholds) {
      const pct = percent * 100;
      if (pct >= (options.colorThresholds.green || 80)) colorClass = 'green';
      else if (pct >= (options.colorThresholds.yellow || 50)) colorClass = 'yellow';
      else colorClass = 'red';
    }

    const displayValue = options.formatValue
      ? options.formatValue(value)
      : Math.round(percent * 100) + '%';

    const label = options.label || '';

    container.innerHTML = `
      <div class="gauge" style="width:${size}px;height:${size}px;">
        <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
          <circle class="gauge-track" cx="${size/2}" cy="${size/2}" r="${radius}" />
          <circle class="gauge-fill ${colorClass}" cx="${size/2}" cy="${size/2}" r="${radius}"
            stroke-dasharray="${circumference}"
            stroke-dashoffset="${offset}" />
        </svg>
        <div class="gauge-label">
          <span class="gauge-value">${displayValue}</span>
          ${label ? `<span class="gauge-text">${label}</span>` : ''}
        </div>
      </div>
    `;
  }

  /* ── Update Chart Data ───────────────────────────────── */
  function update(canvasId, newData) {
    const chart = instances[canvasId];
    if (!chart) return;

    if (newData.labels) chart.data.labels = newData.labels;
    if (newData.datasets) {
      newData.datasets.forEach((ds, i) => {
        if (chart.data.datasets[i]) {
          Object.assign(chart.data.datasets[i], ds);
        }
      });
    }
    chart.update('none');
  }

  /* ── Destroy ─────────────────────────────────────────── */
  function destroy(canvasId) {
    if (instances[canvasId]) {
      instances[canvasId].destroy();
      delete instances[canvasId];
    }
  }

  /* ── Theme Change Handler ────────────────────────────── */
  window.addEventListener('themechange', () => {
    Object.keys(instances).forEach(id => {
      const chart = instances[id];
      if (!chart) return;
      const tc = getThemeColors();

      if (chart.options.scales) {
        Object.values(chart.options.scales).forEach(scale => {
          if (scale.grid) scale.grid.color = tc.grid;
          if (scale.ticks) scale.ticks.color = tc.textMuted;
        });
      }

      if (chart.options.plugins?.legend?.labels) {
        chart.options.plugins.legend.labels.color = tc.text;
      }

      if (chart.data.datasets) {
        chart.data.datasets.forEach(ds => {
          if (ds.borderColor === 'var(--surface)' || ds.pointBorderColor === 'var(--surface)') {
            // skip CSS variable references
          }
        });
      }

      chart.update('none');
    });
  });

  return {
    doughnut,
    line,
    bar,
    horizontalBar,
    createGauge,
    update,
    destroy,
    instances,
    COLORS,
    PALETTE,
  };
})();
