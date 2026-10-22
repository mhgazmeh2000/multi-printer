/**
 * PrintGuard Onboarding Tours with Shepherd.js (Phase J.9)
 * 4 interactive Persian RTL tours:
 * 1. dashboard_intro: Overview of dashboards list and management
 * 2. dashboard_builder: 12-column GridStack layout, undo/redo, auto-save
 * 3. panel_creation: Panel types selection, 3-level query builder, preview
 * 4. datasource_connect: Driver types, secure credentials, connection test
 */
(function (global) {
  "use strict";

  class OnboardingManager {
    constructor() {
      this.status = null;
      this.activeTour = null;
      this.initialized = false;
    }

    async init() {
      if (this.initialized) return;
      this.initialized = true;

      try {
        const res = await fetch("/api/onboarding/status");
        if (res.ok) {
          this.status = await res.json();
        }
      } catch (e) {
        console.warn("Could not fetch onboarding status:", e);
      }

      this.checkAndStartAutoTours();
    }

    isTourCompletedOrDismissed(tourName) {
      if (!this.status) return false;
      if (this.status.disabled_all) return true;
      const completed = this.status.completed && this.status.completed[tourName];
      const dismissed = this.status.dismissed && this.status.dismissed[tourName];
      return Boolean(completed || dismissed);
    }

    async markTour(tourName, action = "complete") {
      try {
        await fetch("/api/onboarding/complete", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": window.CSRF_TOKEN || "",
          },
          body: JSON.stringify({ tour: tourName, action }),
        });
        if (this.status && this.status.completed) {
          if (action === "dismiss") {
            this.status.dismissed[tourName] = true;
          } else if (action === "complete") {
            this.status.completed[tourName] = true;
          } else if (action === "disable_all") {
            this.status.disabled_all = true;
          }
        }
      } catch (e) {
        console.warn("Error marking onboarding tour:", e);
      }
    }

    async resetTours(tourName = null) {
      try {
        await fetch("/api/onboarding/reset", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": window.CSRF_TOKEN || "",
          },
          body: JSON.stringify({ tour: tourName }),
        });
        if (this.status) {
          this.status.disabled_all = false;
          if (tourName) {
            this.status.completed[tourName] = false;
            this.status.dismissed[tourName] = false;
          } else {
            Object.keys(this.status.completed || {}).forEach((k) => {
              this.status.completed[k] = false;
              this.status.dismissed[k] = false;
            });
          }
        }
      } catch (e) {
        console.warn("Error resetting onboarding tours:", e);
      }
    }

    createShepherdInstance(tourName) {
      if (typeof Shepherd === "undefined") {
        console.warn("Shepherd.js is not loaded.");
        return null;
      }

      const tour = new Shepherd.Tour({
        useModalOverlay: true,
        defaultStepOptions: {
          cancelIcon: { enabled: true },
          scrollTo: { behavior: "smooth", block: "center" },
          classes: "shepherd-theme-printguard",
        },
      });

      tour.on("complete", () => this.markTour(tourName, "complete"));
      tour.on("cancel", () => this.markTour(tourName, "dismiss"));
      return tour;
    }

    // ─── Tour 1: Dashboard Intro ───────────────────────────────────────────────
    startDashboardIntroTour(force = false) {
      const tourName = "dashboard_intro";
      if (!force && this.isTourCompletedOrDismissed(tourName)) return;

      const tour = this.createShepherdInstance(tourName);
      if (!tour) return;

      const headerTarget = document.getElementById("dashboards-header") || document.querySelector("h2");
      const btnNewTarget = document.getElementById("btn-new-dashboard") || document.querySelector("button[onclick*='openCreateModal']");
      const gridTarget = document.getElementById("dashboards-grid") || document.querySelector(".dashboards-grid");

      if (headerTarget) {
        tour.addStep({
          id: "intro-welcome",
          text: "به مرکز داشبوردهای PrintGuard خوش آمدید. در اینجا می‌توانید داشبوردهای سازمانی، تیمی و شخصی خود را بسازید و تحلیل‌های پیشرفته پرینترها را مشاهده کنید.",
          attachTo: { element: headerTarget, on: "bottom" },
          buttons: [
            { text: "رد کردن", secondary: true, action: () => tour.cancel() },
            { text: "بعدی", action: () => tour.next() },
          ],
        });
      }

      if (btnNewTarget) {
        tour.addStep({
          id: "intro-create-btn",
          text: "برای ایجاد داشبورد سفارشی جدید با چیدمان دلخواه، روی این دکمه کلیک کنید.",
          attachTo: { element: btnNewTarget, on: "bottom" },
          buttons: [
            { text: "قبلی", secondary: true, action: () => tour.back() },
            { text: "بعدی", action: () => tour.next() },
          ],
        });
      }

      if (gridTarget) {
        tour.addStep({
          id: "intro-grid",
          text: "تمامی داشبوردهای شما به تفکیک عمومی، تیمی یا ستاره‌دار در این بخش نمایش داده می‌شوند و با یک کلیک قابل باز شدن یا ویرایش هستند.",
          attachTo: { element: gridTarget, on: "top" },
          buttons: [
            { text: "قبلی", secondary: true, action: () => tour.back() },
            { text: "پایان آموزش", action: () => tour.complete() },
          ],
        });
      }

      this.activeTour = tour;
      tour.start();
    }

    // ─── Tour 2: Dashboard Builder ─────────────────────────────────────────────
    startDashboardBuilderTour(force = false) {
      const tourName = "dashboard_builder";
      if (!force && this.isTourCompletedOrDismissed(tourName)) return;

      const tour = this.createShepherdInstance(tourName);
      if (!tour) return;

      const toolbar = document.querySelector(".btn-green")?.parentElement;
      const btnAdd = document.querySelector("button[onclick*='openAddPanelModal']");
      const btnPreview = document.getElementById("btn-toggle-preview");
      const undoRedo = document.getElementById("btn-undo")?.parentElement;
      const gridCanvas = document.getElementById("builder-grid");

      if (toolbar) {
        tour.addStep({
          id: "builder-toolbar",
          text: "این نوار ابزار اصلی سازنده داشبورد است. تمامی تغییرات چیدمان و ابعاد پنل‌ها به صورت خودکار ذخیره می‌شوند.",
          attachTo: { element: toolbar, on: "bottom" },
          buttons: [
            { text: "رد کردن", secondary: true, action: () => tour.cancel() },
            { text: "بعدی", action: () => tour.next() },
          ],
        });
      }

      if (undoRedo) {
        tour.addStep({
          id: "builder-undoredo",
          text: "با دکمه‌های بازگردانی (Undo) و مجدد (Redo)، می‌توانید هر تغییری در چینش یا تغییر اندازه پنل‌ها را مدیریت کنید.",
          attachTo: { element: undoRedo, on: "bottom" },
          buttons: [
            { text: "قبلی", secondary: true, action: () => tour.back() },
            { text: "بعدی", action: () => tour.next() },
          ],
        });
      }

      if (btnAdd) {
        tour.addStep({
          id: "builder-add-btn",
          text: "با کلیک روی «افزودن پنل» می‌توانید از بین ۸ نوع پنل استاندارد ECharts یا ویجت‌های پلاگین‌ها، نمودار دلخواه را اضافه نمایید.",
          attachTo: { element: btnAdd, on: "bottom" },
          buttons: [
            { text: "قبلی", secondary: true, action: () => tour.back() },
            { text: "بعدی", action: () => tour.next() },
          ],
        });
      }

      if (btnPreview) {
        tour.addStep({
          id: "builder-preview-btn",
          text: "با دکمه «پیش‌نمایش زنده»، نوارها و ابزارهای ویرایش موقتاً مخفی شده و شکل نهایی داشبورد برای شما شبیه‌سازی می‌شود.",
          attachTo: { element: btnPreview, on: "bottom" },
          buttons: [
            { text: "قبلی", secondary: true, action: () => tour.back() },
            { text: "بعدی", action: () => tour.next() },
          ],
        });
      }

      if (gridCanvas) {
        tour.addStep({
          id: "builder-canvas",
          text: "صفحه شبکه ۱۲ ستونی: پنل‌ها را آزادانه با ماوس یا لمس جابجا کنید، گوشه‌ها را برای تغییر اندازه بکشید یا دکمه‌های جمع‌شدن را بزنید.",
          attachTo: { element: gridCanvas, on: "top" },
          buttons: [
            { text: "قبلی", secondary: true, action: () => tour.back() },
            { text: "متوجه شدم", action: () => tour.complete() },
          ],
        });
      }

      this.activeTour = tour;
      tour.start();
    }

    // ─── Tour 3: Panel Creation & Query Builder ────────────────────────────────
    startPanelCreationTour(force = false) {
      const tourName = "panel_creation";
      if (!force && this.isTourCompletedOrDismissed(tourName)) return;

      const tour = this.createShepherdInstance(tourName);
      if (!tour) return;

      const typesGrid = document.getElementById("panel-type-grid");
      const qbContainer = document.getElementById("panel-query-builder-container");

      if (typesGrid) {
        tour.addStep({
          id: "panel-types",
          text: "ابتدا نوع نمودار را انتخاب کنید: شاخص عددی (Stat)، جدول (Table)، سری زمانی (TimeSeries)، گیج ستونی (BarGauge)، نمودار دایره‌ای (Pie)، ماتریس حرارتی (Heatmap) یا لیست هشدارها.",
          attachTo: { element: typesGrid, on: "bottom" },
          buttons: [
            { text: "رد کردن", secondary: true, action: () => tour.cancel() },
            { text: "بعدی", action: () => tour.next() },
          ],
        });
      }

      if (qbContainer) {
        tour.addStep({
          id: "panel-query-engine",
          text: "Query Builder دارای سه سطح است: کوئری‌های از پیش‌آماده پرینترها، سازنده بصری (Visual) بدون نیاز به کد، و ویرایشگر امن SQL با فیلترهای ضد تزریق.",
          attachTo: { element: qbContainer, on: "top" },
          buttons: [
            { text: "قبلی", secondary: true, action: () => tour.back() },
            { text: "پایان راهنما", action: () => tour.complete() },
          ],
        });
      }

      this.activeTour = tour;
      tour.start();
    }

    // ─── Tour 4: Data Source Connection ────────────────────────────────────────
    startDatasourceTour(force = false) {
      const tourName = "datasource_connect";
      if (!force && this.isTourCompletedOrDismissed(tourName)) return;

      const tour = this.createShepherdInstance(tourName);
      if (!tour) return;

      const header = document.querySelector("h2");
      const btnAddDs = document.querySelector("button[onclick*='openAddModal']");
      const dsTable = document.querySelector("table");

      if (header) {
        tour.addStep({
          id: "ds-intro",
          text: "در بخش Data Sources می‌توانید سیستم را به منابع داده خارجی شامل پایگاه داده‌های PostgreSQL، سرویس‌های REST API و مخازن GitHub متصل فرمایید.",
          attachTo: { element: header, on: "bottom" },
          buttons: [
            { text: "رد کردن", secondary: true, action: () => tour.cancel() },
            { text: "بعدی", action: () => tour.next() },
          ],
        });
      }

      if (btnAddDs) {
        tour.addStep({
          id: "ds-add-btn",
          text: "با کلیک بر روی این دکمه، منبع داده جدید تعریف کنید. تمامی رمزهای عبور و توکن‌ها با امنیت بالا ذخیره و ماسک می‌شوند.",
          attachTo: { element: btnAddDs, on: "bottom" },
          buttons: [
            { text: "قبلی", secondary: true, action: () => tour.back() },
            { text: "بعدی", action: () => tour.next() },
          ],
        });
      }

      if (dsTable) {
        tour.addStep({
          id: "ds-table",
          text: "برای هر منبع داده می‌توانید تست اتصال آنی (Test Connection) بگیرید و وضعیت سلامت ارتباط را بررسی کنید.",
          attachTo: { element: dsTable, on: "top" },
          buttons: [
            { text: "قبلی", secondary: true, action: () => tour.back() },
            { text: "متوجه شدم", action: () => tour.complete() },
          ],
        });
      }

      this.activeTour = tour;
      tour.start();
    }

    checkAndStartAutoTours() {
      const path = window.location.pathname;
      if (path === "/dashboards") {
        this.startDashboardIntroTour();
      } else if (path.includes("/dashboards/") && path.endsWith("/edit")) {
        this.startDashboardBuilderTour();
      } else if (path === "/datasources") {
        this.startDatasourceTour();
      }
    }
  }

  global.PrintGuardTours = new OnboardingManager();

  document.addEventListener("DOMContentLoaded", () => {
    global.PrintGuardTours.init();
  });
})(typeof window !== "undefined" ? window : this);
