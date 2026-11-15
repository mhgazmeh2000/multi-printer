/* Parallel, read-only supply diagnostics. Never feeds page counting or yield estimation. */
(function () {
  'use strict';
  const colors = {black: 'مشکی', cyan: 'فیروزه‌ای', magenta: 'ارغوانی', yellow: 'زرد'};
  const validCount = value => typeof value === 'number' && Number.isSafeInteger(value) && value >= 0;
  const countText = value => validCount(value) ? value.toLocaleString('en-US') : 'ناموجود / نامعتبر';

  // Raw integer usage is authoritative. Round only at the presentation boundary.
  function megaText(raw, legacyMega) {
    const value = validCount(raw) ? raw / 1000000
      : (raw == null && typeof legacyMega === 'number' && Number.isFinite(legacyMega) && legacyMega >= 0
        ? legacyMega : null);
    return value === null ? null : value.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
  }

  function infoButton(printer, color, toner) {
    if (printer.device_type !== 'color' || !Object.hasOwn(colors, color)) return '';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'supply-info-button';
    button.textContent = '؟';
    button.title = 'اطلاعات شمارش صفحات و مصرف گزارش‌شده';
    button.setAttribute('aria-label', `اطلاعات ردیابی تونر ${colors[color]}`);
    button.setAttribute('aria-haspopup', 'dialog');
    button.dataset.supplyInfo = JSON.stringify({
      color, ip: printer.ip, name: toner.name, pages: toner.supply_pages,
      start: toner.first_install_date, last: toner.last_use_date,
      raw: toner.usage, total: (printer.counters || {}).total,
      fullColor: (printer.counters || {}).full_color, observed: printer.last_poll
    });
    return button.outerHTML;
  }

  let dialog;
  let returnFocus;
  function element(tag, text, className) {
    const node = document.createElement(tag);
    if (text != null) node.textContent = String(text);
    if (className) node.className = className;
    return node;
  }
  function ensureDialog() {
    if (dialog) return dialog;
    dialog = document.createElement('dialog');
    dialog.id = 'supply-info-dialog';
    dialog.className = 'supply-info-dialog';
    dialog.dir = 'rtl';
    dialog.setAttribute('aria-labelledby', 'supply-info-title');
    dialog.setAttribute('aria-describedby', 'supply-info-description');
    dialog.addEventListener('click', event => {
      if (event.target === dialog) dialog.close();
    });
    dialog.addEventListener('cancel', event => {
      event.preventDefault();
      dialog.close();
    });
    dialog.addEventListener('close', () => {
      if (returnFocus && returnFocus.isConnected) returnFocus.focus();
      returnFocus = null;
    });
    document.body.appendChild(dialog);
    return dialog;
  }
  function open(button) {
    let data;
    try { data = JSON.parse(button.dataset.supplyInfo); } catch (_) { return; }
    if (!data || !Object.hasOwn(colors, data.color)) return;
    const popup = ensureDialog();
    if (typeof popup.showModal !== 'function') return;
    const panel = element('section', null, 'modal supply-info-panel');
    const title = element('h3', `اطلاعات تونر ${colors[data.color]}`);
    title.id = 'supply-info-title';
    panel.appendChild(title);
    panel.appendChild(element('p', `${data.ip || '—'} · ${data.name || colors[data.color]}`, 'supply-info-device'));
    const description = element('p', 'این پنجره فقط مسیر کمکی بررسی است؛ فرمول شمارش صفحات، تخمین فعلی و رفتار ریست را تغییر نمی‌دهد.', 'auth-alert');
    description.id = 'supply-info-description';
    panel.appendChild(description);
    const rows = element('dl', null, 'supply-info-values');
    const row = (label, value) => {
      rows.appendChild(element('dt', label));
      rows.appendChild(element('dd', value));
    };
    row('صفحات از شروع ردیابی', countText(data.pages));
    row('مبنای فعلی شمارش', data.color === 'black' ? 'افزایش شمارندهٔ کل صفحات دستگاه' : 'افزایش شمارندهٔ تمام‌رنگی دستگاه');
    row('شمارندهٔ کل دستگاه در این نمونه', countText(data.total));
    row('شمارندهٔ تمام‌رنگی در این نمونه', countText(data.fullColor));
    row('شروع ردیابی کارتریج', data.start || 'ناموجود');
    row('آخرین افزایش شمارندهٔ صفحات', data.last || 'هنوز ثبت نشده / ناموجود');
    row('Dot Count — مقدار خام گزارش‌شده', countText(data.raw));
    const mega = megaText(data.raw);
    row('Mega Dots — نمایش بر حسب میلیون', mega === null ? 'ناموجود / نامعتبر' : `${mega}M`);
    row('افزایش مصرف از شروع ردیابی', 'نامشخص؛ مبنای مستقل مصرف ثبت نشده است');
    row('صفحات دارای مصرف این رنگ', 'محاسبه نمی‌شود؛ از تغییر مصرف بین دو نمونه، تعداد دقیق صفحات هر رنگ مشخص نیست');
    row('زمان نمونهٔ نمایش‌داده‌شده', data.observed || 'ناموجود');
    panel.appendChild(rows);
    panel.appendChild(element('p', 'Mega Dots همان مقدار خام تقسیم بر ۱٬۰۰۰٬۰۰۰ است؛ فقط در نمایش گرد می‌شود. واحد واقعی و رفتار ریست شمارندهٔ مصرف باید برای مدل دستگاه اعتبارسنجی شود. صفر گزارش‌شده به‌تنهایی اثبات نبود مصرف نیست.', 'supply-info-note'));
    panel.appendChild(element('p', 'این مقادیر مربوط به نمونهٔ زمان بازکردن پنجره‌اند و ممکن است با تأخیر دستگاه یا polling تغییر کنند. تاریخ شروع ردیابی لزوماً تاریخ نصب واقعی نیست. شمارش صفحه اندازه‌گیری دقیق مصرف تونر نیست و مسیر کمکی خودکار به مرجع اصلی تبدیل نمی‌شود.', 'supply-info-note'));
    const actions = element('div', null, 'form-actions');
    const close = element('button', 'بستن', 'btn btn-secondary');
    close.type = 'button';
    close.autofocus = true;
    close.addEventListener('click', () => popup.close());
    actions.appendChild(close);
    panel.appendChild(actions);
    popup.replaceChildren(panel);
    returnFocus = button;
    if (!popup.open) popup.showModal();
    close.focus();
  }

  // One delegated listener survives dashboard card refreshes. Native dialog supplies
  // modal focus containment and Escape dismissal without a library or network call.
  document.addEventListener('click', event => {
    const button = event.target.closest && event.target.closest('button[data-supply-info]');
    if (button) open(button);
  });
  window.PrintGuardSupplyUsage = Object.freeze({infoButton, megaText});
})();
