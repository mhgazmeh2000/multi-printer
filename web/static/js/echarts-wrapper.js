/**
 * PrintGuard ECharts Wrapper & Theme Integration
 * Supports Dark/Light themes mapped to CSS variables, RTL formatting, auto-resize, and Persian numerals.
 */
(function (global) {
  "use strict";

  if (!global.echarts) {
    console.warn("ECharts library not loaded before echarts-wrapper.");
    return;
  }

  const registeredInstances = new Set();

  function isLightTheme() {
    return document.documentElement.classList.contains("light");
  }

  function getActiveThemeName() {
    return isLightTheme() ? "printguard-light" : "printguard-dark";
  }

  // ─── Theme Registration ──────────────────────────────────────────────────────
  function registerThemes() {
    const darkPalette = [
      "#00d4ff", // cyan
      "#00e676", // green
      "#ffd740", // yellow
      "#ff7043", // orange
      "#ea80fc", // magenta
      "#448aff", // blue
      "#ff3d3d", // red
    ];

    const lightPalette = [
      "#0086a7", // darker cyan
      "#10b981", // green
      "#f59e0b", // amber/yellow
      "#f97316", // orange
      "#a855f7", // purple/magenta
      "#3b82f6", // blue
      "#ef4444", // red
    ];

    global.echarts.registerTheme("printguard-dark", {
      color: darkPalette,
      backgroundColor: "transparent",
      textStyle: {
        fontFamily: "'Noto Sans Arabic', 'Vazirmatn', 'Segoe UI', Tahoma, sans-serif",
        color: "#e4e8f0",
      },
      title: {
        textStyle: { color: "#e4e8f0", fontWeight: 600, fontSize: 15 },
        subtextStyle: { color: "#7b86a0", fontSize: 12 },
      },
      line: {
        itemStyle: { borderWidth: 2 },
        lineStyle: { width: 2.5 },
        symbolSize: 6,
        symbol: "circle",
        smooth: true,
      },
      bar: {
        itemStyle: {
          borderRadius: [4, 4, 0, 0],
        },
      },
      pie: {
        itemStyle: {
          borderWidth: 2,
          borderColor: "#151720",
        },
      },
      gauge: {
        axisLine: {
          lineStyle: {
            color: [
              [0.2, "#ff3d3d"],
              [0.5, "#ffd740"],
              [1, "#00e676"],
            ],
            width: 12,
          },
        },
      },
      grid: {
        top: 36,
        bottom: 30,
        left: 45,
        right: 25,
        containLabel: true,
      },
      categoryAxis: {
        axisLine: { show: true, lineStyle: { color: "#2a3248" } },
        axisTick: { show: false },
        axisLabel: { color: "#7b86a0", fontSize: 11 },
        splitLine: { show: false },
      },
      valueAxis: {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: "#7b86a0", fontSize: 11 },
        splitLine: {
          show: true,
          lineStyle: { color: "rgba(42, 50, 72, 0.45)", type: "dashed" },
        },
      },
      tooltip: {
        backgroundColor: "#151720",
        borderColor: "#2a3248",
        borderWidth: 1,
        textStyle: { color: "#e4e8f0", fontSize: 12 },
        extraCssText:
          "box-shadow: 0 8px 24px rgba(0,0,0,0.5); border-radius: 8px; direction: rtl; text-align: right; font-family: 'Noto Sans Arabic', 'Vazirmatn', sans-serif;",
      },
      legend: {
        textStyle: { color: "#9aa5bd", fontSize: 12 },
        pageTextStyle: { color: "#9aa5bd" },
      },
    });

    global.echarts.registerTheme("printguard-light", {
      color: lightPalette,
      backgroundColor: "transparent",
      textStyle: {
        fontFamily: "'Noto Sans Arabic', 'Vazirmatn', 'Segoe UI', Tahoma, sans-serif",
        color: "#0f172a",
      },
      title: {
        textStyle: { color: "#0f172a", fontWeight: 600, fontSize: 15 },
        subtextStyle: { color: "#64748b", fontSize: 12 },
      },
      line: {
        itemStyle: { borderWidth: 2 },
        lineStyle: { width: 2.5 },
        symbolSize: 6,
        symbol: "circle",
        smooth: true,
      },
      bar: {
        itemStyle: {
          borderRadius: [4, 4, 0, 0],
        },
      },
      pie: {
        itemStyle: {
          borderWidth: 2,
          borderColor: "#ffffff",
        },
      },
      gauge: {
        axisLine: {
          lineStyle: {
            color: [
              [0.2, "#ef4444"],
              [0.5, "#f59e0b"],
              [1, "#10b981"],
            ],
            width: 12,
          },
        },
      },
      grid: {
        top: 36,
        bottom: 30,
        left: 45,
        right: 25,
        containLabel: true,
      },
      categoryAxis: {
        axisLine: { show: true, lineStyle: { color: "#cbd5e1" } },
        axisTick: { show: false },
        axisLabel: { color: "#64748b", fontSize: 11 },
        splitLine: { show: false },
      },
      valueAxis: {
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: "#64748b", fontSize: 11 },
        splitLine: {
          show: true,
          lineStyle: { color: "rgba(203, 213, 225, 0.6)", type: "dashed" },
        },
      },
      tooltip: {
        backgroundColor: "#ffffff",
        borderColor: "#cbd5e1",
        borderWidth: 1,
        textStyle: { color: "#0f172a", fontSize: 12 },
        extraCssText:
          "box-shadow: 0 8px 24px rgba(0,0,0,0.12); border-radius: 8px; direction: rtl; text-align: right; font-family: 'Noto Sans Arabic', 'Vazirmatn', sans-serif;",
      },
      legend: {
        textStyle: { color: "#334155", fontSize: 12 },
        pageTextStyle: { color: "#334155" },
      },
    });
  }

  registerThemes();

  // ─── RTL & Numeric Formatting Helpers ─────────────────────────────────────────
  function toPersianDigits(n) {
    if (n === null || n === undefined || isNaN(n)) return "—";
    const farsiDigits = ["۰", "۱", "۲", "۳", "۴", "۵", "۶", "۷", "۸", "۹"];
    return n
      .toString()
      .replace(/\d/g, (x) => farsiDigits[x])
      .replace(/\./g, "٫");
  }

  function formatNumber(value, options = {}) {
    if (value === null || value === undefined || isNaN(value)) return "—";
    const num = Number(value);
    const formatted = new Intl.NumberFormat("fa-IR", {
      maximumFractionDigits: options.decimals ?? 1,
      useGrouping: options.useGrouping ?? true,
    }).format(num);
    return options.unit ? `${formatted} ${options.unit}` : formatted;
  }

  // ─── Wrapper API ─────────────────────────────────────────────────────────────
  const PrintGuardECharts = {
    echarts: global.echarts,
    isLightTheme,
    getActiveThemeName,
    toPersianDigits,
    formatNumber,

    /**
     * Initializes an ECharts instance on container with PrintGuard theme and auto-resize.
     */
    init(container, options = {}) {
      if (!container) return null;

      // Dispose existing instance if present
      const existing = global.echarts.getInstanceByDom(container);
      if (existing) {
        existing.dispose();
        registeredInstances.delete(existing);
      }

      const theme = options.theme || getActiveThemeName();
      const chart = global.echarts.init(container, theme, {
        renderer: options.renderer || "canvas",
        locale: "FA",
      });

      registeredInstances.add(chart);

      // Auto-resize observer
      if (typeof ResizeObserver !== "undefined") {
        const ro = new ResizeObserver(() => {
          chart.resize();
        });
        ro.observe(container);
        chart.__ro = ro;
      }

      return chart;
    },

    /**
     * Updates theme of all active charts when user toggles Dark/Light mode.
     */
    refreshAllThemes() {
      const newTheme = getActiveThemeName();
      registeredInstances.forEach((chart) => {
        if (!chart.isDisposed()) {
          const dom = chart.getDom();
          const option = chart.getOption();
          const ro = chart.__ro;
          chart.dispose();
          const next = global.echarts.init(dom, newTheme, { locale: "FA" });
          next.__ro = ro;
          next.setOption(option);
          registeredInstances.delete(chart);
          registeredInstances.add(next);
        }
      });
    },

    dispose(chart) {
      if (chart) {
        if (chart.__ro) chart.__ro.disconnect();
        chart.dispose();
        registeredInstances.delete(chart);
      }
    },
  };

  // Listen to document theme toggle to update all active charts automatically
  const observer = new MutationObserver((mutations) => {
    for (const m of mutations) {
      if (m.attributeName === "class") {
        PrintGuardECharts.refreshAllThemes();
        break;
      }
    }
  });
  observer.observe(document.documentElement, { attributes: true });

  global.PrintGuardECharts = PrintGuardECharts;
})(typeof window !== "undefined" ? window : this);
