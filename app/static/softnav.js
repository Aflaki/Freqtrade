/* Filters without reloading the page: inside #softzone, a filter link or the filter form fetches the same page with
   the new filters and swaps only #softzone (performance, tabs, tables). The scroll position and the open tab stay;
   back / forward work. Anything unexpected falls back to a normal page load. */
(function () {
  'use strict';
  if (!window.fetch || !window.DOMParser) return;
  const timers = [];
  window.softClear = () => timers.splice(0).forEach(clearInterval);
  // run the scripts of HTML put into the page (parsed HTML doesn't run them by itself)
  const runScripts = root => root.querySelectorAll('script').forEach(old => {
    const s = document.createElement('script');
    [...old.attributes].forEach(a => s.setAttribute(a.name, a.value));
    s.textContent = old.textContent; old.replaceWith(s);
  });
  // load the #softzone of `url` into `container` (a dashboard card); true when it worked
  window.softLoad = async (container, url) => {
    try {
      const r = await fetch(url, { headers: { 'X-Soft': '1' }, credentials: 'same-origin' });
      if (!r.ok || r.redirected) return false;
      const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
      const fresh = doc.getElementById('softzone'); if (!fresh) return false;
      timers.splice(0).forEach(clearInterval);
      container.innerHTML = '';
      const node = document.importNode(fresh, true);
      container.appendChild(node); runScripts(node);
      return true;
    } catch (e) { return false; }
  };
  // scripts inside the zone register their repeating jobs here, so a swap can stop the old ones
  window.zoneTimer = (fn, ms) => { const id = setInterval(fn, ms); timers.push(id); return id; };
  let busy = false;

  function samePage(url) {
    try { const u = new URL(url, location.href); return u.origin === location.origin && u.pathname === location.pathname; }
    catch (e) { return false; }
  }

  async function go(url, push) {
    if (busy) return;
    const zone = document.getElementById('softzone');
    if (!zone) { location.href = url; return; }
    busy = true;
    zone.classList.add('soft-loading');
    const y = window.scrollY, hash = location.hash;
    try {
      const target = new URL(url, location.href); target.hash = '';
      const r = await fetch(target.href, { headers: { 'X-Soft': '1' }, credentials: 'same-origin' });
      if (!r.ok || r.redirected) throw new Error('status ' + r.status);
      const doc = new DOMParser().parseFromString(await r.text(), 'text/html');
      const fresh = doc.getElementById('softzone');
      if (!fresh) throw new Error('no zone');
      timers.splice(0).forEach(clearInterval);
      const next = document.importNode(fresh, true);
      zone.replaceWith(next);
      runScripts(next);
      if (push) history.pushState({ soft: 1 }, '', target.pathname + target.search + hash);
      if (window.initPerfUI) window.initPerfUI();
      window.scrollTo(0, y);
    } catch (e) {
      location.href = url;
    } finally {
      busy = false;
      const z = document.getElementById('softzone'); if (z) z.classList.remove('soft-loading');
    }
  }

  document.addEventListener('click', e => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest('#softzone .filterbar a[href], #softzone a.soft[href]');
    if (!a || a.target || !samePage(a.href) || a.getAttribute('href').startsWith('#')) return;
    e.preventDefault();
    go(a.href, true);
  });
  document.addEventListener('submit', e => {
    const f = e.target.closest('#softzone .filterbar form');
    if (!f || (f.method || 'get').toLowerCase() !== 'get') return;
    e.preventDefault();
    const u = new URL(f.action, location.href);
    u.search = new URLSearchParams(new FormData(f)).toString();
    go(u.href, true);
  });
  window.addEventListener('popstate', () => { if (document.getElementById('softzone') || document.getElementById('dcards')) location.reload(); });
})();
