/**
 * PrintGuard 3-Level Query Builder Component (Phase J.8)
 * Client-side interface supporting Prebuilt, Visual, and SQL query creation with real-time preview.
 */
(function (global) {
  "use strict";

  class QueryBuilder {
    constructor(containerId, options = {}) {
      this.container = document.getElementById(containerId);
      this.currentMode = options.mode || "prebuilt";
      this.onChange = options.onChange || null;
      this.prebuiltCatalog = [];

      this.init();
    }

    async init() {
      if (!this.container) return;
      this.renderSkeleton();

      // Fetch prebuilt catalog
      try {
        const res = await fetch("/api/query/prebuilt-catalog");
        if (res.ok) {
          const data = await res.json();
          this.prebuiltCatalog = data.catalog || [];
          this.renderPrebuiltTab();
        }
      } catch (e) {
        console.warn("Could not load prebuilt query catalog:", e);
      }
    }

    renderSkeleton() {
      this.container.innerHTML = `
        <div class="qb-wrapper" style="border:1px solid var(--border); border-radius:8px; background:var(--bg); overflow:hidden;">
          <!-- Tabs -->
          <div style="display:flex; border-bottom:1px solid var(--border); background:var(--bg2);">
            <button type="button" class="qb-tab-btn" data-mode="prebuilt" style="flex:1; padding:9px; background:none; border:none; border-bottom:2px solid var(--cyan); color:var(--text); font-size:12px; font-weight:600; cursor:pointer;">⚙️ آماده (Prebuilt)</button>
            <button type="button" class="qb-tab-btn" data-mode="visual" style="flex:1; padding:9px; background:none; border:none; border-bottom:2px solid transparent; color:var(--text3); font-size:12px; font-weight:600; cursor:pointer;">👁 بصری (Visual)</button>
            <button type="button" class="qb-tab-btn" data-mode="sql" style="flex:1; padding:9px; background:none; border:none; border-bottom:2px solid transparent; color:var(--text3); font-size:12px; font-weight:600; cursor:pointer;">💻 کوئری پیشرفته (SQL)</button>
          </div>

          <!-- Tab Panes -->
          <div class="qb-pane" id="qb-pane-prebuilt" style="padding:14px;">در حال بارگذاری...</div>
          <div class="qb-pane" id="qb-pane-visual" style="display:none; padding:14px;">
            <div style="margin-bottom:10px;">
              <label style="display:block; font-size:11px; color:var(--text2); margin-bottom:4px;">انتخاب جدول داده</label>
              <select id="qb-vis-table" class="form-input" style="width:100%;">
                <option value="daily_stats">آمار روزانه پرینترها (daily_stats)</option>
                <option value="print_events">رویدادهای تفصیلی چاپ (print_events)</option>
                <option value="sensor_readings">سنجه‌های سنسورها (sensor_readings)</option>
                <option value="cartridge_lifecycle">چرخه عمر کارتریج‌ها (cartridge_lifecycle)</option>
                <option value="printer_counters">شمارنده‌ها و وضعیت (printer_counters)</option>
              </select>
            </div>
            <div style="display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-bottom:10px;">
              <div>
                <label style="display:block; font-size:11px; color:var(--text2); margin-bottom:4px;">فیلد تجمیع</label>
                <input type="text" id="qb-vis-agg-field" class="form-input" placeholder="مثال: total_pages" style="width:100%; box-sizing:border-box;" />
              </div>
              <div>
                <label style="display:block; font-size:11px; color:var(--text2); margin-bottom:4px;">عملگر تجمیع</label>
                <select id="qb-vis-agg-op" class="form-input" style="width:100%; box-sizing:border-box;">
                  <option value="SUM">SUM (مجموع)</option>
                  <option value="AVG">AVG (میانگین)</option>
                  <option value="COUNT">COUNT (تعداد)</option>
                  <option value="MAX">MAX (حداکثر)</option>
                  <option value="MIN">MIN (حداقل)</option>
                </select>
              </div>
            </div>
          </div>
          <div class="qb-pane" id="qb-pane-sql" style="display:none; padding:14px;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:6px;">
              <label style="font-size:11px; color:var(--text2);">دستور SQL (فقط SELECT خواندنی با محدودیت خودکار)</label>
            </div>
            <textarea id="qb-sql-editor" class="form-input" style="width:100%; height:110px; font-family:var(--mono); font-size:12px; direction:ltr; text-align:left; box-sizing:border-box;" placeholder="SELECT date, SUM(total_pages) AS pages FROM daily_stats GROUP BY date;"></textarea>
          </div>

          <!-- Preview & Action Bar -->
          <div style="padding:10px 14px; background:var(--bg2); border-top:1px solid var(--border); display:flex; justify-content:space-between; align-items:center;">
            <button type="button" class="btn btn-cyan" id="qb-btn-preview" style="padding:5px 12px; font-size:12px;">▶ تست و پیش‌نمایش داده</button>
            <span id="qb-preview-stats" style="font-size:11px; color:var(--text3); font-family:var(--mono);"></span>
          </div>

          <!-- Mini Result Preview Table -->
          <div id="qb-preview-result" style="max-height:160px; overflow:auto; border-top:1px solid var(--border); display:none; padding:8px; font-size:11px;"></div>
        </div>
      `;

      // Setup tab switcher
      this.container.querySelectorAll(".qb-tab-btn").forEach((btn) => {
        btn.addEventListener("click", () => {
          this.switchMode(btn.dataset.mode);
        });
      });

      // Preview button click
      document.getElementById("qb-btn-preview").addEventListener("click", () => {
        this.executePreview();
      });
    }

    switchMode(mode) {
      this.currentMode = mode;
      this.container.querySelectorAll(".qb-tab-btn").forEach((b) => {
        if (b.dataset.mode === mode) {
          b.style.borderBottomColor = "var(--cyan)";
          b.style.color = "var(--text)";
        } else {
          b.style.borderBottomColor = "transparent";
          b.style.color = "var(--text3)";
        }
      });

      document.getElementById("qb-pane-prebuilt").style.display = mode === "prebuilt" ? "block" : "none";
      document.getElementById("qb-pane-visual").style.display = mode === "visual" ? "block" : "none";
      document.getElementById("qb-pane-sql").style.display = mode === "sql" ? "block" : "none";
    }

    renderPrebuiltTab() {
      const pane = document.getElementById("qb-pane-prebuilt");
      if (!pane) return;

      let optionsHtml = this.prebuiltCatalog
        .map((p) => `<option value="${p.id}">${p.title}</option>`)
        .join("");

      pane.innerHTML = `
        <div style="margin-bottom:10px;">
          <label style="display:block; font-size:11px; color:var(--text2); margin-bottom:4px;">شاخص آماده</label>
          <select id="qb-prebuilt-select" class="form-input" style="width:100%;">
            ${optionsHtml}
          </select>
        </div>
        <div id="qb-prebuilt-desc" style="font-size:11px; color:var(--text3); margin-bottom:10px;"></div>
        <div id="qb-prebuilt-params"></div>
      `;

      const select = document.getElementById("qb-prebuilt-select");
      select.addEventListener("change", () => this.updatePrebuiltDetails());
      this.updatePrebuiltDetails();
    }

    updatePrebuiltDetails() {
      const select = document.getElementById("qb-prebuilt-select");
      if (!select) return;
      const item = this.prebuiltCatalog.find((x) => x.id === select.value);
      if (!item) return;

      document.getElementById("qb-prebuilt-desc").textContent = item.description || "";
      const pContainer = document.getElementById("qb-prebuilt-params");
      pContainer.innerHTML = "";

      Object.entries(item.param_schema || {}).forEach(([pKey, pSchema]) => {
        const wrap = document.createElement("div");
        wrap.style.cssText = "margin-bottom:8px;";
        wrap.innerHTML = `
          <label style="display:block; font-size:11px; color:var(--text2); margin-bottom:2px;">${pSchema.title || pKey}</label>
          <input type="${pSchema.type === "integer" ? "number" : "text"}" id="qb-param-${pKey}" class="form-input" value="${item.default_params[pKey] ?? ""}" style="width:100%; box-sizing:border-box;" />
        `;
        pContainer.appendChild(wrap);
      });
    }

    getQuerySpec() {
      if (this.currentMode === "prebuilt") {
        const select = document.getElementById("qb-prebuilt-select");
        const queryId = select ? select.value : "printer_pages_summary";
        const item = this.prebuiltCatalog.find((x) => x.id === queryId);
        const params = {};
        if (item && item.param_schema) {
          Object.keys(item.param_schema).forEach((k) => {
            const inp = document.getElementById(`qb-param-${k}`);
            if (inp) params[k] = inp.value;
          });
        }
        return { mode: "prebuilt", query_id: queryId, params };
      } else if (this.currentMode === "visual") {
        const table = document.getElementById("qb-vis-table").value;
        const aggField = document.getElementById("qb-vis-agg-field").value.trim();
        const aggOp = document.getElementById("qb-vis-agg-op").value;
        const aggregations = aggField ? [{ op: aggOp, field: aggField, alias: "val" }] : [];
        return {
          mode: "visual",
          spec: { table, aggregations, limit: 100 },
        };
      } else if (this.currentMode === "sql") {
        const rawSql = document.getElementById("qb-sql-editor").value.trim();
        return { mode: "sql", raw_sql: rawSql };
      }
      return { mode: "prebuilt", query_id: "printer_pages_summary" };
    }

    async executePreview() {
      const spec = this.getQuerySpec();
      const statsEl = document.getElementById("qb-preview-stats");
      const resultEl = document.getElementById("qb-preview-result");

      statsEl.textContent = "در حال اجرا...";
      resultEl.style.display = "block";
      resultEl.innerHTML = '<div style="color:var(--text3); text-align:center;">در حال دریافت پیش‌نمایش...</div>';

      try {
        const res = await fetch("/api/query/preview", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": window.CSRF_TOKEN || "",
          },
          body: JSON.stringify(spec),
        });

        const data = await res.json();
        if (!res.ok) throw new Error(data.error || "خطا در اجرای کوئری");

        statsEl.textContent = `⚡ ${data.total} سطر (${data.execution_ms}ms)`;

        if (!data.rows || data.rows.length === 0) {
          resultEl.innerHTML = '<div style="color:var(--text3); text-align:center;">نتیجه‌ای یافت نشد (۰ سطر).</div>';
          return;
        }

        // Render mini table
        let tableHtml = '<table style="width:100%; border-collapse:collapse; direction:rtl; text-align:right;"><thead><tr style="border-bottom:1px solid var(--border); color:var(--text2);">';
        data.columns.forEach((c) => {
          tableHtml += `<th style="padding:4px 8px;">${c.title}</th>`;
        });
        tableHtml += '</tr></thead><tbody>';

        data.rows.slice(0, 10).forEach((r) => {
          tableHtml += '<tr style="border-bottom:1px solid rgba(255,255,255,0.03);">';
          data.columns.forEach((c) => {
            tableHtml += `<td style="padding:4px 8px; color:var(--text); font-family:var(--mono);">${r[c.field] ?? ""}</td>`;
          });
          tableHtml += '</tr>';
        });
        tableHtml += '</tbody></table>';

        resultEl.innerHTML = tableHtml;

        if (typeof this.onChange === "function") {
          this.onChange(data);
        }
      } catch (err) {
        statsEl.textContent = "✘ خطا";
        resultEl.innerHTML = `<div style="color:var(--red); padding:4px;">${err.message}</div>`;
      }
    }
  }

  global.PrintGuardQueryBuilder = QueryBuilder;
})(typeof window !== "undefined" ? window : this);
