/* Server-clock-based display and lightweight cycle watcher. No printer polling here. */
(function (root) {
  'use strict';
  function create(options) {
    const now = options.now || Date.now;
    const later = options.setTimeout || setTimeout;
    const cancel = options.clearTimeout || clearTimeout;
    let status = {}, receivedAt = 0, serverAt = 0, rendered = null, completionPending = false;
    let timer = null, paintTimer = null, inFlight = false, refreshInFlight = false, stopped = false, errors = 0;
    const token = (count, last) => JSON.stringify([count ?? null, last ?? null]);
    function view() {
      const interval = Number(status.poll_interval);
      const due = Date.parse(status.next_poll_at);
      const remaining = Number.isFinite(due) && serverAt ? Math.max(0, due - (serverAt + now() - receivedAt)) : null;
      if (errors) return {state: 'error', percent: 0, text: 'ارتباط با سرور قطع است؛ تلاش مجدد…'};
      if (status.is_polling) return {state: 'polling', percent: 100, text: 'در حال پایش دستگاه‌ها…'};
      if (remaining === null || !(interval > 0)) return {state: 'waiting', percent: 0, text: 'در انتظار زمان‌بندی سرور…'};
      if (!remaining) return {state: 'waiting', percent: 0, text: 'در انتظار شروع پایش…'};
      return {state: 'idle', percent: Math.min(100, remaining / (interval * 1000) * 100),
        text: `پایش بعدی حدود ${Math.ceil(remaining / 1000)} ثانیه دیگر`, remaining};
    }
    function paint() { options.render(view()); }
    function schedule(delay) {
      if (timer !== null) cancel(timer);
      if (!stopped) timer = later(check, delay);
    }
    function accept(s, meta) {
      if (stopped) return;
      status = s || {};
      receivedAt = now();
      serverAt = Date.parse(status.server_time) || 0;
      if (meta) { rendered = token(meta.poll_count, meta.last_poll); completionPending = false; }
      errors = 0;
      paint();
      if (paintTimer === null) {
        const tick = () => { paintTimer = null; if (!stopped) { paint(); paintTimer = later(tick, 1000); } };
        paintTimer = later(tick, 1000);
      }
      // Catch manual cycles too, even when an automatic cycle is far away.
      schedule(status.is_polling ? 3000 : Math.min(10000, Math.max(1000, view().remaining || 3000)));
    }
    function markRendered(meta) {
      if (stopped || !meta) return;
      rendered = token(meta.poll_count, meta.last_poll);
      completionPending = false;
    }
    function refreshSnapshot() {
      if (refreshInFlight || stopped) return;
      refreshInFlight = true;
      // A slow/failed printer fetch must not block status reads or report the
      // lightweight status connection as offline. Metadata is accepted only
      // after a real printer response has rendered.
      Promise.resolve().then(() => { if (!stopped) return options.refresh(); }).catch(err => {
        if (!stopped) options.onRefreshError?.(err);
      }).finally(() => { refreshInFlight = false; });
    }
    async function check() {
      timer = null;
      if (stopped || inFlight) return;
      inFlight = true;
      try {
        const s = await options.readStatus();
        if (stopped) return;
        const finished = status.is_polling && !s.is_polling;
        accept(s);
        if (finished) completionPending = true;
        // Compare with the PRINTER snapshot, not another status response: the two
        // requests can straddle completion. A skipped/failed fetch retries next check.
        if (completionPending || rendered !== token(s.count, s.last)) refreshSnapshot();
      } catch (err) {
        if (stopped) return;
        errors += 1;
        paint();
        options.onError?.(err);
        schedule(Math.min(60000, 3000 * 2 ** Math.min(errors - 1, 5)));
      } finally {
        inFlight = false;
        if (!stopped && timer === null) schedule(3000);
      }
    }
    return {
      accept,
      markRendered,
      wake() { if (!stopped && !inFlight) schedule(0); },
      stop() { stopped = true; cancel(timer); cancel(paintTimer); timer = paintTimer = null; },
      view,
    };
  }
  root.PrintGuardPollCycle = {create};
})(typeof window !== 'undefined' ? window : globalThis);
