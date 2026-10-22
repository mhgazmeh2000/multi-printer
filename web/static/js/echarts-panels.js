/**
 * PrintGuard Base Panels (Phase J.1 / J.2)
 * Client-side renderers and lifecycle handlers for the 8 base dashboard panels:
 * 1. Stat (Large metric with thresholds & sparkline)
 * 2. Table (Interactive, sortable, searchable data table)
 * 3. TimeSeries (Multi-series smooth line/area chart with dataZoom)
 * 4. BarGauge (Horizontal / Vertical threshold bar gauge)
 * 5. PieChart (Donut / Pie distribution chart)
 * 6. Heatmap (Activity / Load matrix heatmap)
 * 7. Text (Markdown / HTML card)
 * 8. AlertList (Real-time severity-badged alert list)
 */
(function (global) {
  "use strict";

  const ECH = global.PrintGuardECharts;

  const PanelRenderers = {
    // ─── 1. Stat Panel ────────────────────────────────────────────────────────
    stat: {
      render(container, data, config = {}) {
        container.innerHTML = "";
        container.className = "pg-panel-stat-container";
        container.style.cssText =
          "display:flex; flex-direction:column; justify-content:space-between; height:100%; padding:14px; box-sizing:border-box; position:relative; overflow:hidden;";

        const title = config.title || "شاخص";
        const unit = config.unit || "";
        const prefix = config.prefix || "";
        const decimals = config.decimals ?? 0;
        const value = typeof data === "object" && data !== null ? data.value ?? 0 : data ?? 0;

        // Color based on thresholds
        let color = "var(--text)";
        if (config.thresholds && Array.isArray(config.thresholds)) {
          const sorted = [...config.thresholds].sort((a, b) => a.value - b.value);
          for (const t of sorted) {
            if (value >= t.value) {
              color = t.color;
            }
          }
        }

        const titleEl = document.createElement("div");
        titleEl.style.cssText = "font-size:12px; color:var(--text2); font-weight:600; margin-bottom:4px;";
        titleEl.textContent = title;

        const valWrap = document.createElement("div");
        valWrap.style.cssText = "display:flex; align-items:baseline; gap:6px; margin:auto 0;";

        if (prefix) {
          const pEl = document.createElement("span");
          pEl.style.cssText = "font-size:16px; color:var(--text2);";
          pEl.textContent = prefix;
          valWrap.appendChild(pEl);
        }

        const valEl = document.createElement("span");
        valEl.style.cssText = `font-size:32px; font-weight:800; font-family:var(--mono); color:${color}; line-height:1.1;`;
        valEl.textContent = ECH.formatNumber(value, { decimals });
        valWrap.appendChild(valEl);

        if (unit) {
          const uEl = document.createElement("span");
          uEl.style.cssText = "font-size:13px; color:var(--text3); font-weight:500;";
          uEl.textContent = unit;
          valWrap.appendChild(uEl);
        }

        container.appendChild(titleEl);
        container.appendChild(valWrap);

        // Optional Sparkline chart at the bottom
        if (config.show_sparkline && data && Array.isArray(data.history) && data.history.length > 1) {
          const sparkDom = document.createElement("div");
          sparkDom.style.cssText = "height:38px; width:100%; margin-top:4px;";
          container.appendChild(sparkDom);

          const chart = ECH.init(sparkDom);
          if (chart) {
            chart.setOption({
              grid: { top: 2, bottom: 2, left: 0, right: 0 },
              xAxis: { type: "category", show: false, data: data.history.map((_, i) => i) },
              yAxis: { type: "value", show: false },
              series: [
                {
                  type: "line",
                  data: data.history,
                  smooth: true,
                  symbol: "none",
                  lineStyle: { width: 2, color: color },
                  areaStyle: {
                    color: new global.echarts.graphic.LinearGradient(0, 0, 0, 1, [
                      { offset: 0, color: color + "66" },
                      { offset: 1, color: color + "05" },
                    ]),
                  },
                },
              ],
            });
          }
        }
      },
    },

    // ─── 2. Table Panel ───────────────────────────────────────────────────────
    table: {
      render(container, data, config = {}) {
        container.innerHTML = "";
        container.style.cssText =
          "height:100%; width:100%; overflow:auto; padding:8px; box-sizing:border-box; font-size:12px;";

        const rows = Array.isArray(data) ? data : data?.rows || [];
        const columns = config.columns && config.columns.length > 0
          ? config.columns
          : Object.keys(rows[0] || {}).map((k) => ({ field: k, title: k, type: "text" }));

        if (rows.length === 0) {
          container.innerHTML =
            '<div style="display:flex; height:100%; align-items:center; justify-content:center; color:var(--text3);">داده‌ای برای نمایش جدول وجود ندارد</div>';
          return;
        }

        const table = document.createElement("table");
        table.style.cssText = "width:100%; border-collapse:collapse; direction:rtl; text-align:right;";

        const thead = document.createElement("thead");
        const headTr = document.createElement("tr");
        columns.forEach((col) => {
          const th = document.createElement("th");
          th.style.cssText =
            "padding:8px 10px; border-bottom:1px solid var(--border); color:var(--text2); font-weight:600; white-space:nowrap; background:var(--bg2); position:sticky; top:0;";
          th.textContent = col.title;
          headTr.appendChild(th);
        });
        thead.appendChild(headTr);
        table.appendChild(thead);

        const tbody = document.createElement("tbody");
        rows.slice(0, config.page_size || 25).forEach((row) => {
          const tr = document.createElement("tr");
          tr.style.cssText = "border-bottom:1px solid rgba(255,255,255,0.04);";
          columns.forEach((col) => {
            const td = document.createElement("td");
            td.style.cssText = "padding:7px 10px; color:var(--text); white-space:nowrap;";
            const val = row[col.field];

            if (col.type === "badge") {
              const badge = document.createElement("span");
              badge.className = `pill ${val === "online" || val === "active" ? "p2" : "p0"}`;
              badge.textContent = val;
              td.appendChild(badge);
            } else if (col.type === "number") {
              td.style.fontFamily = "var(--mono)";
              td.textContent = ECH.formatNumber(val);
            } else {
              td.textContent = val ?? "—";
            }
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
        table.appendChild(tbody);
        container.appendChild(table);
      },
    },

    // ─── 3. Time Series Panel ─────────────────────────────────────────────────
    timeseries: {
      render(container, data, config = {}) {
        container.innerHTML = "";
        const chart = ECH.init(container);
        if (!chart) return;

        const dates = data?.dates || [];
        const seriesData = data?.series || [];

        const eSeries = seriesData.map((s) => ({
          name: s.name,
          type: config.chart_type || "line",
          data: s.data || [],
          smooth: config.smooth ?? true,
          stack: config.stack ? "Total" : undefined,
          areaStyle: config.chart_type === "area" || config.stack ? { opacity: 0.25 } : undefined,
        }));

        const option = {
          title: config.title ? { text: config.title } : undefined,
          tooltip: { trigger: "axis" },
          legend: { show: seriesData.length > 1, top: 4 },
          grid: { top: config.title ? 42 : 24, bottom: config.show_data_zoom ? 55 : 25, left: 45, right: 25 },
          xAxis: {
            type: "category",
            data: dates.map((d) => {
              try {
                return new Date(d).toLocaleDateString("fa-IR", { month: "short", day: "numeric" });
              } catch {
                return d;
              }
            }),
          },
          yAxis: {
            type: "value",
            name: config.y_axis_unit || "",
          },
          series: eSeries,
        };

        if (config.show_data_zoom && dates.length > 15) {
          option.dataZoom = [
            { type: "inside", start: 30, end: 100 },
            {
              type: "slider",
              start: 30,
              end: 100,
              height: 18,
              bottom: 8,
              borderColor: "var(--border)",
              fillerColor: "rgba(0, 212, 255, 0.15)",
              textStyle: { color: "var(--text3)", fontSize: 10 },
            },
          ];
        }

        chart.setOption(option);
      },
    },

    // ─── 4. Bar Gauge Panel ───────────────────────────────────────────────────
    barGauge: {
      render(container, data, config = {}) {
        container.innerHTML = "";
        const chart = ECH.init(container);
        if (!chart) return;

        const items = Array.isArray(data) ? data : data?.items || [];
        const names = items.map((x) => x.name || x.label || "");

        const option = {
          title: config.title ? { text: config.title } : undefined,
          tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
          grid: { top: 30, bottom: 20, left: 80, right: 40 },
          xAxis: {
            type: "value",
            min: config.min ?? 0,
            max: config.max ?? 100,
            axisLabel: { formatter: `{value}${config.unit || "%"}` },
          },
          yAxis: {
            type: "category",
            data: names,
            axisLabel: { color: "var(--text)" },
          },
          series: [
            {
              type: "bar",
              data: items.map((item) => {
                let barColor = "#00d4ff";
                if (item.color) {
                  barColor = item.color;
                } else if (item.value <= 15) {
                  barColor = "#ff3d3d";
                } else if (item.value <= 30) {
                  barColor = "#ffd740";
                } else {
                  barColor = "#00e676";
                }
                return {
                  value: item.value,
                  itemStyle: { color: barColor, borderRadius: [0, 4, 4, 0] },
                };
              }),
              label: {
                show: true,
                position: "right",
                formatter: `{c}${config.unit || "%"}`,
                color: "var(--text2)",
                fontFamily: "var(--mono)",
              },
            },
          ],
        };

        chart.setOption(option);
      },
    },

    // ─── 5. Pie Chart Panel ───────────────────────────────────────────────────
    piechart: {
      render(container, data, config = {}) {
        container.innerHTML = "";
        const chart = ECH.init(container);
        if (!chart) return;

        const items = Array.isArray(data) ? data : data?.items || [];

        const option = {
          title: config.title ? { text: config.title } : undefined,
          tooltip: {
            trigger: "item",
            formatter: "{b}: {c} ({d}٪)",
          },
          legend: config.show_legend !== false ? { orient: "vertical", right: "right", top: "center" } : undefined,
          series: [
            {
              name: config.title || "توزیع",
              type: "pie",
              radius: config.donut ? ["45%", "72%"] : "70%",
              roseType: config.rose_type ? "radius" : false,
              center: config.show_legend !== false ? ["40%", "50%"] : ["50%", "50%"],
              avoidLabelOverlap: true,
              itemStyle: { borderRadius: 4 },
              label: {
                show: true,
                formatter: "{b}\n{d}٪",
                fontSize: 11,
              },
              emphasis: {
                label: { show: true, fontSize: 13, fontWeight: "bold" },
              },
              data: items,
            },
          ],
        };

        chart.setOption(option);
      },
    },

    // ─── 6. Heatmap Panel ─────────────────────────────────────────────────────
    heatmap: {
      render(container, data, config = {}) {
        container.innerHTML = "";
        const chart = ECH.init(container);
        if (!chart) return;

        const hours = Array.from({ length: 24 }, (_, i) => `${i}:00`);
        const days = ["شنبه", "یک‌شنبه", "دوشنبه", "سه‌شنبه", "چهارشنبه", "پنج‌شنبه", "جمعه"];
        const matrix = data?.matrix || [];

        const option = {
          title: config.title ? { text: config.title } : undefined,
          tooltip: {
            position: "top",
            formatter: (p) => `${days[p.value[1]]}، ساعت ${hours[p.value[0]]}: <b>${p.value[2]}</b> رخداد`,
          },
          grid: { top: 35, bottom: 40, left: 60, right: 25 },
          xAxis: { type: "category", data: hours, splitArea: { show: true } },
          yAxis: { type: "category", data: days, splitArea: { show: true } },
          visualMap: {
            min: 0,
            max: data?.max_val || 50,
            calculable: true,
            orient: "horizontal",
            left: "center",
            bottom: 0,
            inRange: {
              color: ["#141720", "#0086a7", "#00d4ff", "#ea80fc"],
            },
            textStyle: { color: "var(--text3)", fontSize: 10 },
          },
          series: [
            {
              name: "فعالیت",
              type: "heatmap",
              data: matrix,
              label: { show: false },
              emphasis: {
                itemStyle: { shadowBlur: 10, shadowColor: "rgba(0, 0, 0, 0.5)" },
              },
            },
          ],
        };

        chart.setOption(option);
      },
    },

    // ─── 7. Text Panel ────────────────────────────────────────────────────────
    text: {
      render(container, data, config = {}) {
        container.innerHTML = "";
        container.style.cssText =
          "height:100%; width:100%; overflow:auto; padding:16px; box-sizing:border-box; color:var(--text); line-height:1.7; font-size:13px;";

        const title = config.title || "";
        if (title) {
          const h = document.createElement("h4");
          h.style.cssText = "margin:0 0 8px 0; color:var(--cyan); font-size:15px;";
          h.textContent = title;
          container.appendChild(h);
        }

        const bodyEl = document.createElement("div");
        const rawText = config.content || (typeof data === "string" ? data : "");
        bodyEl.innerHTML = rawText;
        container.appendChild(bodyEl);
      },
    },

    // ─── 8. Alert List Panel (Phase J.11 Alert Engine Integration) ───────────
    alertlist: {
      render(container, data, config = {}) {
        // Clear previous poll timer if any
        if (container._alertPollTimer) {
          clearInterval(container._alertPollTimer);
          container._alertPollTimer = null;
        }

        let rawAlerts = Array.isArray(data) ? data : data?.alerts || [];
        let activeFilter = config.default_filter || "all";

        const renderDOM = () => {
          container.innerHTML = "";
          container.style.cssText =
            "height:100%; width:100%; display:flex; flex-direction:column; padding:10px; box-sizing:border-box; font-size:12px; overflow:hidden;";

          // Filter Toolbar
          if (config.show_filters !== false) {
            const filterBar = document.createElement("div");
            filterBar.style.cssText =
              "display:flex; gap:6px; margin-bottom:8px; border-bottom:1px solid var(--border); padding-bottom:6px;";

            const filters = [
              { id: "all", label: "همه" },
              { id: "critical", label: "بحرانی" },
              { id: "warning", label: "هشدار" },
              { id: "info", label: "اطلاع" },
            ];

            filters.forEach((f) => {
              const fBtn = document.createElement("button");
              const isSelected = activeFilter === f.id;
              fBtn.style.cssText =
                `background:${isSelected ? 'var(--cyan)' : 'transparent'}; color:${isSelected ? '#0f172a' : 'var(--text3)'}; border:1px solid ${isSelected ? 'var(--cyan)' : 'var(--border)'}; border-radius:4px; padding:2px 8px; font-size:11px; cursor:pointer; font-weight:${isSelected ? '600' : 'normal'};`;
              fBtn.textContent = f.label;
              fBtn.onclick = () => {
                activeFilter = f.id;
                renderDOM();
              };
              filterBar.appendChild(fBtn);
            });
            container.appendChild(filterBar);
          }

          const filteredAlerts = activeFilter === "all"
            ? rawAlerts
            : rawAlerts.filter((a) => (a.severity || "").toLowerCase() === activeFilter);

          const listWrap = document.createElement("div");
          listWrap.style.cssText = "flex:1; overflow-y:auto; display:flex; flex-direction:column; gap:8px;";

          if (filteredAlerts.length === 0) {
            listWrap.innerHTML =
              '<div style="display:flex; height:100%; min-height:80px; align-items:center; justify-content:center; color:#00e676; font-weight:600;">✔ هیچ هشدار فعالی در این بخش وجود ندارد</div>';
            container.appendChild(listWrap);
            return;
          }

          filteredAlerts.slice(0, config.max_items || 20).forEach((al) => {
            const item = document.createElement("div");
            const sev = (al.severity || "warning").toLowerCase();
            const borderCl = sev === "critical" ? "var(--red)" : sev === "warning" ? "var(--yellow)" : "var(--cyan)";

            item.style.cssText =
              `background:var(--bg2); border-right:3px solid ${borderCl}; border:1px solid var(--border); border-right-width:3px; border-right-color:${borderCl}; border-radius:6px; padding:8px 12px; display:flex; justify-content:space-between; align-items:center; cursor:pointer; transition:background 0.2s;`;
            item.title = "برای مشاهده جزئیات کلیک کنید";
            item.onmouseenter = () => { item.style.background = "rgba(255,255,255,0.04)"; };
            item.onmouseleave = () => { item.style.background = "var(--bg2)"; };

            item.onclick = () => {
              showAlertDetailModal(al);
            };

            const left = document.createElement("div");
            const msg = document.createElement("div");
            msg.style.cssText = "color:var(--text); font-weight:600; font-size:12px;";
            msg.textContent = al.title || al.message || al.text || "هشدار دستگاه";

            const sub = document.createElement("div");
            sub.style.cssText = "color:var(--text3); font-size:11px; margin-top:2px;";
            const timeStr = al.timestamp || al.time || "";
            const displayTime = timeStr.includes("T") ? timeStr.slice(0, 19).replace("T", " ") : timeStr;
            sub.textContent = `${al.printer_name || al.printer_ip || "سیستم"} • ${displayTime}`;

            left.appendChild(msg);
            left.appendChild(sub);

            const badge = document.createElement("span");
            badge.className = `pill ${sev === "critical" ? "p0" : sev === "warning" ? "p1" : "p2"}`;
            badge.textContent = sev === "critical" ? "بحرانی" : sev === "warning" ? "هشدار" : "اطلاع";

            item.appendChild(left);
            item.appendChild(badge);
            listWrap.appendChild(item);
          });

          container.appendChild(listWrap);
        };

        const showAlertDetailModal = (al) => {
          let modal = document.getElementById("pg-alert-detail-modal");
          if (!modal) {
            modal = document.createElement("div");
            modal.id = "pg-alert-detail-modal";
            modal.style.cssText =
              "position:fixed; inset:0; background:rgba(0,0,0,0.7); z-index:10000; display:flex; align-items:center; justify-content:center;";
            document.body.appendChild(modal);
          }

          const sev = (al.severity || "warning").toLowerCase();
          const sevColor = sev === "critical" ? "var(--red)" : sev === "warning" ? "var(--yellow)" : "var(--cyan)";
          const sevLabel = sev === "critical" ? "بحرانی" : sev === "warning" ? "هشدار" : "اطلاع";

          modal.innerHTML = `
            <div class="card" style="width:520px; max-width:92%; background:var(--bg2); border:1px solid var(--border); border-radius:12px; padding:20px; box-shadow:0 12px 36px rgba(0,0,0,0.6); direction:rtl; text-align:right;">
              <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid var(--border); padding-bottom:12px; margin-bottom:14px;">
                <div style="display:flex; align-items:center; gap:8px;">
                  <span style="display:inline-block; width:10px; height:10px; border-radius:50%; background:${sevColor};"></span>
                  <h3 style="margin:0; font-size:16px; color:var(--text);">${al.title || 'جزئیات هشدار'}</h3>
                </div>
                <button class="btn" style="padding:2px 8px; font-size:14px;" onclick="document.getElementById('pg-alert-detail-modal').style.display='none'">×</button>
              </div>
              <div style="font-size:13px; color:var(--text); display:flex; flex-direction:column; gap:10px;">
                <div><b>شدت هشدار:</b> <span style="color:${sevColor}; font-weight:bold;">${sevLabel}</span></div>
                <div><b>دستگاه / منبع:</b> ${al.printer_name || al.printer_ip || 'سیستم'} ${al.printer_ip ? `(${al.printer_ip})` : ''}</div>
                <div><b>کد فنی:</b> <code>${al.code || 'SYS_ALERT'}</code></div>
                <div><b>زمان ثبت:</b> ${al.timestamp || al.time || 'نامشخص'}</div>
                <div style="background:rgba(255,255,255,0.03); border:1px solid var(--border); border-radius:6px; padding:10px; margin-top:4px;">
                  <b style="color:var(--text2); display:block; margin-bottom:4px;">شرح پیام:</b>
                  <div>${al.message || al.title || ''}</div>
                </div>
                ${al.suggested_action ? `
                <div style="background:rgba(6,182,212,0.08); border:1px solid var(--cyan); border-radius:6px; padding:10px; color:var(--cyan);">
                  <b>اقدام پیشنهادی PrintGuard:</b>
                  <div style="margin-top:4px; font-size:12.5px; color:var(--text);">${al.suggested_action}</div>
                </div>` : ''}
              </div>
              <div style="display:flex; justify-content:flex-end; margin-top:18px;">
                <button class="btn btn-cyan" onclick="document.getElementById('pg-alert-detail-modal').style.display='none'">بستن</button>
              </div>
            </div>
          `;
          modal.style.display = "flex";
        };

        renderDOM();

        // Auto-refresh polling (every 30 seconds if live)
        if (config.poll_interval !== false) {
          const pollMs = (config.poll_interval_s || 30) * 1000;
          container._alertPollTimer = setInterval(async () => {
            try {
              const res = await fetch("/api/alerts/active");
              if (res.ok) {
                const j = await res.json();
                if (j.alerts) {
                  rawAlerts = j.alerts;
                  renderDOM();
                }
              }
            } catch (e) {
              // silent poll fail
            }
          }, pollMs);
        }
      },
    },
  };

  global.PrintGuardPanels = {
    render(type, container, data, config) {
      const renderer = PanelRenderers[type];
      if (!renderer) {
        container.innerHTML = `<div style="color:var(--red); padding:10px;">نوع پنل ناشناخته: ${type}</div>`;
        return;
      }
      renderer.render(container, data, config);
    },
    getRenderer(type) {
      return PanelRenderers[type];
    },
  };
})(typeof window !== "undefined" ? window : this);
