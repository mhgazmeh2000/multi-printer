/*
 * @file:         web/static/js/legacy-mode.js
 * @brief:        Keyboard shortcut (Ctrl+Alt+5) that toggles the compact three-column row layout.
 * @details:      Responsibilities: Toggle the legacy row layout and show a toast with the new state.
 * @dependencies: browser APIs only; loaded by base.html
 * @notes:        None.
 */

// legacy-mode.js – Switch to row mode (three columns) with Ctrl+Alt+5
(function() {
  let legacyActive = false;
  let styleTag = null;

  const rowLayoutCSS = `
    body.legacy-mode .oc-counters {
      display: grid !important;
      grid-template-columns: repeat(3, 1fr) !important;
      gap: 8px !important;
      margin-bottom: 0 !important;
    }
    body.legacy-mode .oc-cnt {
      text-align: center !important;
      background: transparent !important;
      padding: 0 !important;
      border: none !important;
    }
    body.legacy-mode .oc-cnt-val {
      font-size: 17px !important;
    }
    body.legacy-mode .oc-cnt-lbl {
      font-size: 9px !important;
    }
    body.legacy-mode .oc-toners {
      display: flex !important;
      flex-direction: row !important;
      gap: 6px !important;
      margin-top: 10px !important;
    }
    body.legacy-mode .oc-toner {
      flex: 1 !important;
    }
    body.legacy-mode .oc-toner-bar {
      height: 4px !important;
      border-radius: 2px !important;
    }
    /* hide extra column elements when they exist */
    body.legacy-mode .oc-stat-row,
    body.legacy-mode .oc-toner-area,
    body.legacy-mode .oc-toner-group,
    body.legacy-mode .oc-toner-item,
    body.legacy-mode .oc-toner-placeholder {
      display: none !important;
    }
  `;

  /** Add the legacy-mode stylesheet to the page. */
  function addStyle() {
    if (styleTag) return;
    styleTag = document.createElement('style');
    styleTag.id = 'legacy-mode-styles';
    styleTag.textContent = rowLayoutCSS;
    document.head.appendChild(styleTag);
  }

  /** Remove the legacy-mode stylesheet from the page. */
  function removeStyle() {
    if (styleTag) {
      styleTag.remove();
      styleTag = null;
    }
  }

  /** Turn legacy mode on and remember the choice. */
  function enableLegacy() {
    if (!legacyActive) {
      addStyle();
      document.body.classList.add('legacy-mode');
      legacyActive = true;
      showToast('حالت ردیفی (قدیمی) فعال شد', 's');
    }
  }

  /** Turn legacy mode off and remember the choice. */
  function disableLegacy() {
    if (legacyActive) {
      document.body.classList.remove('legacy-mode');
      removeStyle();
      legacyActive = false;
      showToast('حالت عادی برگشت', 's');
    }
  }

  /** Switch between legacy mode and the normal mode. */
  function toggleLegacy() {
    legacyActive ? disableLegacy() : enableLegacy();
  }

  /** Show a short notification message. */
  function showToast(msg, type) {
    if (typeof toast === 'function') {
      toast(msg, type);
    } else {
      const el = document.getElementById('toast');
      if (el) {
        el.textContent = msg;
        el.className = `show ${type}`;
        setTimeout(() => el.className = '', 3000);
      } else {
        console.log(msg);
      }
    }
  }

  document.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.altKey && e.key === '5') {
      e.preventDefault();
      toggleLegacy();
    }
  });
})();