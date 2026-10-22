/**
 * PrintGuard Dashboard Builder (Phase J.4)
 * Interactive Drag & Drop Gridstack-based Builder with 12-column grid,
 * auto-save, live preview toggle, undo/redo, and responsive mobile adaptation.
 */
(function (global) {
  "use strict";

  class DashboardBuilder {
    constructor(containerId, options = {}) {
      this.container = document.getElementById(containerId);
      this.uid = options.uid || "";
      this.isEditable = options.isEditable !== false;
      this.dashboard = options.dashboard || { panels: [] };
      this.grid = null;

      // History stacks for Undo / Redo
      this.historyStack = [];
      this.redoStack = [];
      this.isApplyingHistory = false;
      this.autoSaveTimer = null;

      this.init();
    }

    init() {
      if (!this.container) return;

      const isMobile = window.innerWidth < 768;
      const initialCols = isMobile ? 1 : 12;

      // GridStack 12-column initialization (1-column on mobile)
      this.grid = GridStack.init(
        {
          column: initialCols,
          cellHeight: isMobile ? 85 : 75,
          margin: isMobile ? 6 : 10,
          animate: true,
          staticGrid: !this.isEditable,
          rtl: true,
          resizable: {
            handles: "e, se, s, sw, w",
          },
        },
        this.container
      );

      // Window resize listener to adapt layout dynamically
      window.addEventListener("resize", () => {
        clearTimeout(this._resizeTimer);
        this._resizeTimer = setTimeout(() => {
          const mobileNow = window.innerWidth < 768;
          const targetCols = mobileNow ? 1 : 12;
          if (this.grid && this.grid.getColumn() !== targetCols) {
            this.grid.column(targetCols);
            if (global.PrintGuardECharts && typeof global.PrintGuardECharts.resizeAll === "function") {
              global.PrintGuardECharts.resizeAll();
            }
          }
        }, 150);
      });

      // Render initial panels
      if (this.dashboard.panels && this.dashboard.panels.length > 0) {
        this.dashboard.panels.forEach((p) => this.renderPanelWidget(p));
      }

      // Save initial state to history
      this.pushHistory();

      // Setup GridStack change listener for drag & resize
      if (this.isEditable) {
        this.grid.on("change", (event, items) => {
          if (this.isApplyingHistory) return;
          this.onGridChange(items);
        });
      }
    }

    renderPanelWidget(panel) {
      const pos = panel.grid_pos || { x: 0, y: 0, w: 6, h: 4 };
      const itemEl = document.createElement("div");
      itemEl.className = "grid-stack-item";
      itemEl.setAttribute("gs-id", panel.uid);
      itemEl.setAttribute("gs-x", pos.x ?? 0);
      itemEl.setAttribute("gs-y", pos.y ?? 0);
      itemEl.setAttribute("gs-w", pos.w ?? 6);
      itemEl.setAttribute("gs-h", pos.h ?? 4);

      const contentEl = document.createElement("div");
      contentEl.className = "grid-stack-item-content card";
      contentEl.style.cssText =
        "background:var(--bg2); border:1px solid var(--border); border-radius:10px; overflow:hidden; display:flex; flex-direction:column; position:relative; box-shadow:0 4px 14px rgba(0,0,0,0.25);";

      // Widget Header (Enabled in both edit and viewer modes for title, collapse & fullscreen)
      const headerEl = document.createElement("div");
      headerEl.className = "widget-header";
      headerEl.style.cssText =
        `display:flex; justify-content:space-between; align-items:center; padding:6px 12px; background:rgba(255,255,255,0.02); border-bottom:1px solid var(--border); ${this.isEditable ? 'cursor:grab;' : ''}`;

      const titleSpan = document.createElement("span");
      titleSpan.style.cssText = "font-size:12px; font-weight:600; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;";
      titleSpan.textContent = panel.title || "پنل بدون عنوان";

      const actions = document.createElement("div");
      actions.style.cssText = "display:flex; gap:6px; align-items:center;";

      // Collapse / Expand Toggle Button
      const collapseBtn = document.createElement("button");
      collapseBtn.innerHTML = "−";
      collapseBtn.style.cssText =
        "background:none; border:none; color:var(--text3); font-size:14px; font-weight:bold; cursor:pointer; padding:2px 6px; line-height:1; min-width:28px; min-height:28px; border-radius:4px;";
      collapseBtn.title = "جمع / باز کردن پنل";
      collapseBtn.onclick = (e) => {
        e.stopPropagation();
        this.togglePanelCollapse(itemEl, collapseBtn, bodyEl, panel);
      };
      actions.appendChild(collapseBtn);

      // Fullscreen Toggle Button
      const fsBtn = document.createElement("button");
      fsBtn.innerHTML = "⛶";
      fsBtn.style.cssText =
        "background:none; border:none; color:var(--text3); font-size:13px; cursor:pointer; padding:2px 6px; line-height:1; min-width:28px; min-height:28px; border-radius:4px;";
      fsBtn.title = "تمام صفحه";
      fsBtn.onclick = (e) => {
        e.stopPropagation();
        this.togglePanelFullscreen(contentEl, fsBtn);
      };
      actions.appendChild(fsBtn);

      if (this.isEditable) {
        const delBtn = document.createElement("button");
        delBtn.innerHTML = "×";
        delBtn.style.cssText =
          "background:none; border:none; color:var(--red); font-size:16px; cursor:pointer; padding:0 4px; line-height:1; min-width:24px; min-height:24px;";
        delBtn.title = "حذف پنل";
        delBtn.onclick = (e) => {
          e.stopPropagation();
          this.deletePanel(panel.uid);
        };
        actions.appendChild(delBtn);
      }

      headerEl.appendChild(titleSpan);
      headerEl.appendChild(actions);
      contentEl.appendChild(headerEl);

      // Panel Body (Where ECharts / Panel Renderer mounts)
      const bodyEl = document.createElement("div");
      bodyEl.className = "widget-body";
      bodyEl.id = `panel-body-${panel.uid}`;
      bodyEl.style.cssText = "flex:1; width:100%; height:100%; position:relative; overflow:hidden;";
      contentEl.appendChild(bodyEl);

      itemEl.appendChild(contentEl);
      this.grid.makeWidget(itemEl);

      // Render actual panel visualization
      this.renderPanelContent(panel);
    }

    togglePanelCollapse(itemEl, btn, bodyEl, panel) {
      const isCollapsed = itemEl.getAttribute("data-collapsed") === "true";
      if (isCollapsed) {
        // Expand
        const prevH = parseInt(itemEl.getAttribute("data-prev-h") || "4", 10);
        bodyEl.style.display = "block";
        itemEl.setAttribute("data-collapsed", "false");
        btn.innerHTML = "−";
        this.grid.update(itemEl, { h: prevH });
        setTimeout(() => {
          if (global.PrintGuardECharts && typeof global.PrintGuardECharts.resizeAll === "function") {
            global.PrintGuardECharts.resizeAll();
          }
        }, 100);
      } else {
        // Collapse
        const currentH = parseInt(itemEl.getAttribute("gs-h") || "4", 10);
        itemEl.setAttribute("data-prev-h", currentH);
        bodyEl.style.display = "none";
        itemEl.setAttribute("data-collapsed", "true");
        btn.innerHTML = "＋";
        this.grid.update(itemEl, { h: 1 });
      }
    }

    togglePanelFullscreen(contentEl, btn) {
      if (!document.fullscreenElement) {
        if (contentEl.requestFullscreen) {
          contentEl.requestFullscreen();
          btn.innerHTML = "🗗";
        }
      } else {
        if (document.exitFullscreen) {
          document.exitFullscreen();
          btn.innerHTML = "⛶";
        }
      }
    }

    async renderPanelContent(panel) {
      const container = document.getElementById(`panel-body-${panel.uid}`);
      if (!container) return;

      const pType = panel.type || "stat";
      const config = panel.config || {};
      const sampleData = this.getMockOrSampleData(pType);

      // Render initial sample/cached data immediately
      if (global.PrintGuardPanels) {
        global.PrintGuardPanels.render(pType, container, sampleData, {
          title: panel.title,
          ...config,
        });
      }

      // If panel is AlertList, fetch real live alerts from API
      if (pType === "alertlist") {
        try {
          const res = await fetch("/api/alerts/active");
          if (res.ok) {
            const json = await res.json();
            if (json.alerts && global.PrintGuardPanels) {
              global.PrintGuardPanels.render(pType, container, json.alerts, {
                title: panel.title,
                ...config,
              });
            }
          }
        } catch (e) {
          // fallback data remains
        }
      } else if (panel.query && (panel.query.mode || panel.query.raw_sql)) {
        // Query Engine integration
        try {
          const res = await fetch("/api/query/preview", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRFToken": window.CSRF_TOKEN || "",
            },
            body: JSON.stringify({
              query: panel.query,
              datasource_uid: panel.datasource_uid || "default",
            }),
          });
          if (res.ok) {
            const qRes = await res.json();
            if (qRes.success && qRes.result && qRes.result.rows && qRes.result.rows.length > 0) {
              const liveData = this.formatQueryResultForPanel(pType, qRes.result);
              if (liveData && global.PrintGuardPanels) {
                global.PrintGuardPanels.render(pType, container, liveData, {
                  title: panel.title,
                  ...config,
                });
              }
            }
          }
        } catch (e) {
          // fallback data remains
        }
      }
    }

    formatQueryResultForPanel(type, result) {
      const rows = result.rows || [];
      if (!rows.length) return null;

      switch (type) {
        case "stat": {
          const firstVal = Object.values(rows[0])[0];
          return { value: Number(firstVal) || firstVal, history: [] };
        }
        case "table":
          return rows;
        case "bargauge":
        case "piechart":
          return rows.map((r) => {
            const vals = Object.values(r);
            return {
              name: String(vals[0] ?? ""),
              value: Number(vals[1] ?? 0),
            };
          });
        case "alertlist":
          return rows;
        default:
          return null;
      }
    }

    getMockOrSampleData(type) {
      switch (type) {
        case "stat":
          return { value: 3840, history: [120, 180, 290, 410, 520, 680, 890] };
        case "bargauge":
          return [
            { name: "HP LaserJet 425dn", value: 78, color: "#00e676" },
            { name: "Toshiba 257", value: 12, color: "#ff3d3d" },
            { name: "Canon MF240", value: 34, color: "#ffd740" },
          ];
        case "piechart":
          return [
            { name: "تک‌رنگ (A4)", value: 720 },
            { name: "رنگی (A4)", value: 240 },
            { name: "نقشه (A3)", value: 85 },
          ];
        case "timeseries": {
          const dates = [];
          const s1 = [];
          const now = new Date();
          for (let i = 14; i >= 0; i--) {
            dates.push(new Date(now.getTime() - i * 86400000).toISOString().slice(0, 10));
            s1.push(Math.floor(100 + Math.random() * 80));
          }
          return { dates, series: [{ name: "کارکرد چاپ", data: s1 }] };
        }
        case "table":
          return [
            { ip: "172.16.25.37", model: "HP 425dn", pages: 44839, status: "online" },
            { ip: "172.16.25.53", model: "Toshiba 257", pages: 536361, status: "online" },
            { ip: "172.16.25.43", model: "Brother 8510", pages: 36433, status: "online" },
          ];
        case "alertlist":
          return [
            { severity: "critical", message: "اتمام تونر مشکی", printer_ip: "172.16.25.53", time: "۵ دقیقه پیش" },
            { severity: "warning", message: "کاهش سطح مخزن به کمتر از ۱۵٪", printer_ip: "172.16.25.39", time: "۲۰ دقیقه پیش" },
          ];
        case "heatmap": {
          const m = [];
          for (let d = 0; d < 7; d++) {
            for (let h = 0; h < 24; h++) {
              m.push([h, d, d < 5 && h >= 8 && h <= 17 ? Math.floor(10 + Math.random() * 30) : 0]);
            }
          }
          return { matrix: m, max_val: 45 };
        }
        case "text":
          return "داشبورد عملیاتی پایش پرینترهای سازمان PrintGuard.";
        default:
          return {};
      }
    }

    onGridChange(items) {
      if (!items || items.length === 0) return;

      items.forEach((item) => {
        const uid = item.id;
        const panel = this.dashboard.panels.find((p) => p.uid === uid);
        if (panel) {
          panel.grid_pos = { x: item.x, y: item.y, w: item.w, h: item.h };
        }
      });

      this.pushHistory();
      this.debouncedSave();
    }

    pushHistory() {
      const snapshot = JSON.stringify(this.getLayoutSnapshot());
      if (this.historyStack.length > 0 && this.historyStack[this.historyStack.length - 1] === snapshot) {
        return;
      }
      this.historyStack.push(snapshot);
      if (this.historyStack.length > 30) this.historyStack.shift();
      this.redoStack = []; // Clear redo stack on new action
      this.updateUndoRedoButtons();
    }

    undo() {
      if (this.historyStack.length <= 1) return;
      this.redoStack.push(this.historyStack.pop());
      const previous = JSON.parse(this.historyStack[this.historyStack.length - 1]);
      this.applySnapshot(previous);
      this.debouncedSave();
    }

    redo() {
      if (this.redoStack.length === 0) return;
      const next = JSON.parse(this.redoStack.pop());
      this.historyStack.push(JSON.stringify(next));
      this.applySnapshot(next);
      this.debouncedSave();
    }

    applySnapshot(snapshot) {
      this.isApplyingHistory = true;
      if (Array.isArray(snapshot)) {
        snapshot.forEach((item) => {
          const el = this.container.querySelector(`[gs-id="${item.id}"]`);
          if (el) {
            this.grid.update(el, { x: item.x, y: item.y, w: item.w, h: item.h });
          }
        });
      }
      this.isApplyingHistory = false;
      this.updateUndoRedoButtons();
    }

    updateUndoRedoButtons() {
      const uBtn = document.getElementById("btn-undo");
      const rBtn = document.getElementById("btn-redo");
      if (uBtn) uBtn.disabled = this.historyStack.length <= 1;
      if (rBtn) rBtn.disabled = this.redoStack.length === 0;
    }

    getLayoutSnapshot() {
      return this.grid.save(false);
    }

    debouncedSave() {
      this.setSaveStatus("saving");
      clearTimeout(this.autoSaveTimer);
      this.autoSaveTimer = setTimeout(() => this.saveToServer(), 600);
    }

    async saveToServer() {
      const layoutData = this.getLayoutSnapshot();
      try {
        const res = await fetch(`/api/dashboards/${this.uid}`, {
          method: "PUT",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": window.CSRF_TOKEN || "",
          },
          body: JSON.stringify({ layout: { items: layoutData } }),
        });
        if (!res.ok) throw new Error("خطا در ذخیره خودکار");
        this.setSaveStatus("saved");
      } catch (err) {
        console.error("Auto-save error:", err);
        this.setSaveStatus("error");
      }
    }

    setSaveStatus(status) {
      const el = document.getElementById("save-indicator");
      if (!el) return;
      if (status === "saving") {
        el.innerHTML = '<span style="color:var(--yellow);">● در حال ذخیره...</span>';
      } else if (status === "saved") {
        el.innerHTML = '<span style="color:var(--green);">✔ همه تغییرات ذخیره شد</span>';
      } else if (status === "error") {
        el.innerHTML = '<span style="color:var(--red);">✘ خطا در ذخیره خودکار</span>';
      }
    }

    async addPanel(type, title, config = {}, query = {}) {
      let defaultW = 6;
      let defaultH = 4;
      if (type === "timeseries" || type === "heatmap") {
        defaultW = 12;
      } else if (type === "stat") {
        defaultW = 3;
        defaultH = 3;
      }

      const gridPos = { x: 0, y: 0, w: defaultW, h: defaultH };
      try {
        const res = await fetch(`/api/dashboards/${this.uid}/panels`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": window.CSRF_TOKEN || "",
          },
          body: JSON.stringify({ type, title, config, query, grid_pos: gridPos }),
        });
        if (!res.ok) throw new Error("افزودن پنل با خطا مواجه شد.");
        const newPanel = await res.json();
        this.dashboard.panels.push(newPanel);
        this.renderPanelWidget(newPanel);
        this.pushHistory();
        this.debouncedSave();
      } catch (e) {
        alert(e.message);
      }
    }

    async deletePanel(panelUid) {
      if (!confirm("آیا از حذف این پنل اطمینان دارید؟")) return;
      try {
        const res = await fetch(`/api/dashboards/${this.uid}/panels/${panelUid}`, {
          method: "DELETE",
          headers: { "X-CSRFToken": window.CSRF_TOKEN || "" },
        });
        if (!res.ok) throw new Error("حذف پنل ناموفق بود.");

        const el = this.container.querySelector(`[gs-id="${panelUid}"]`);
        if (el) this.grid.removeWidget(el);

        this.dashboard.panels = this.dashboard.panels.filter((p) => p.uid !== panelUid);
        this.pushHistory();
        this.debouncedSave();
      } catch (e) {
        alert(e.message);
      }
    }

    togglePreviewMode() {
      const isStatic = this.grid.opts.staticGrid;
      this.grid.setStatic(!isStatic);
      const headers = this.container.querySelectorAll(".widget-header");
      headers.forEach((h) => {
        h.style.display = isStatic ? "flex" : "none";
      });

      const btn = document.getElementById("btn-toggle-preview");
      if (btn) {
        btn.textContent = isStatic ? "👁 پیش‌نمایش زنده" : "✏️ حالت ویرایش";
      }
    }
  }

  global.PrintGuardBuilder = DashboardBuilder;
})(typeof window !== "undefined" ? window : this);
