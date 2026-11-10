/* Every data table on the site: sort by any column (click its header), filter (quick search + per-column filters
   like ">5", "<0", "=BTC", "!ETH") and hide/show columns. Choices are remembered per page in this browser.
   Opt out with class="no-grid". Rows with a cell spanning several columns (totals, "nothing here") stay at the end.
   Tables added later (live refresh) are enhanced too. */
(function () {
  'use strict';
  const T = (() => { try { return JSON.parse(document.getElementById('grid-i18n').textContent); } catch (e) { return {}; } })();
  const tr = (k, d) => T[k] || d;
  const EMPTY = /^(|—|-|–|n\/a)$/i;
  const DATE = /^\d{2,4}-\d{2}(-\d{2})?([ T]\d{2}:\d{2})?/;

  function store(key, val) {
    try { if (val === undefined) return JSON.parse(localStorage.getItem(key) || 'null'); localStorage.setItem(key, JSON.stringify(val)); }
    catch (e) { return null; }
  }
  function cellText(c) {
    if (!c) return '';
    if (c.dataset.v !== undefined) return c.dataset.v;
    const sel = c.querySelector('select'); if (sel && sel.selectedOptions[0]) return sel.selectedOptions[0].textContent.trim();
    const inp = c.querySelector('input[type=number],input[type=text]'); if (inp && !c.textContent.trim()) return inp.value;
    return c.textContent.replace(/\s+/g, ' ').trim();
  }
  function num(s) {
    s = String(s).trim();
    if (EMPTY.test(s) || DATE.test(s)) return null;
    const t = s.replace(/[۰-۹]/g, d => d.charCodeAt(0) - 0x06F0).replace(/[٬,\s]/g, '').replace('−', '-').replace('٫', '.');
    const m = t.match(/^[^\d+\-.]{0,6}([+\-]?\d*\.?\d+(?:e[+\-]?\d+)?)/i);
    return m ? parseFloat(m[1]) : null;
  }

  function columnsOf(row) {  // [{cell, col}] with colspans
    const out = []; let col = 0;
    for (const c of row.cells) { out.push({ cell: c, col, span: c.colSpan || 1 }); col += c.colSpan || 1; }
    return out;
  }
  function cellAt(row, col) {
    for (const x of columnsOf(row)) if (x.col === col && x.span === 1) return x.cell;
    return null;
  }

  function enhance(table, idx) {
    if (table.dataset.grid || table.classList.contains('no-grid') || table.closest('.no-grid')) return;
    const rows = [...table.rows];
    const head = rows.find(r => r.cells.length && [...r.cells].every(c => c.tagName === 'TH'));
    if (!head) return;
    const heads = columnsOf(head);
    const ncols = heads.reduce((a, h) => a + h.span, 0);
    if (ncols < 3) return;
    table.dataset.grid = '1';
    const isData = r => r !== head && !r.classList.contains('grid-frow') && r.parentElement.tagName !== 'TFOOT'
      && [...r.cells].some(c => c.tagName === 'TD') && ![...r.cells].some(c => (c.colSpan || 1) > 1) && !r.classList.contains('grid-fixed');
    const data = () => [...table.rows].filter(isData);
    const sig = heads.map(h => h.cell.textContent.trim()).join('|').slice(0, 120);
    const key = 'grid:' + location.pathname + ':' + idx + ':' + sig;
    const saved = store(key) || {};
    let hidden = new Set(saved.hidden || []), sort = saved.sort || null;
    const filters = {};
    let quick = '';

    // ---- sorting
    function kind(col) {
      const vals = data().map(r => cellText(cellAt(r, col))).filter(v => !EMPTY.test(v));
      if (!vals.length) return 'text';
      return vals.filter(v => num(v) !== null).length >= vals.length * 0.7 ? 'num' : 'text';
    }
    function applySort() {
      heads.forEach(h => delete h.cell.dataset.dir);
      if (!sort) return;
      const [col, dir] = sort, h = heads.find(x => x.col === col); if (!h) return;
      h.cell.dataset.dir = dir;
      const k = h.cell.dataset.k === 'n' ? 'num' : kind(col), lang = document.documentElement.lang || 'en';
      const list = data(); if (!list.length) return;
      const parent = list[0].parentElement, anchor = [...parent.rows].find(r => r !== head && !isData(r) && !r.classList.contains('grid-frow')
        && list[0].sectionRowIndex < r.sectionRowIndex) || null;
      list.sort((a, b) => {
        const x = cellText(cellAt(a, col)), y = cellText(cellAt(b, col));
        const ex = EMPTY.test(x), ey = EMPTY.test(y);
        if (ex || ey) return ex === ey ? 0 : ex ? 1 : -1;  // empty always last
        let r;
        if (k === 'num') { const nx = num(x), ny = num(y); r = (nx === null) - (ny === null) || (nx ?? 0) - (ny ?? 0); }
        else r = x.localeCompare(y, lang, { numeric: true, sensitivity: 'base' });
        return dir === 'asc' ? r : -r;
      }).forEach(r => parent.insertBefore(r, anchor));
    }
    heads.forEach(h => {
      if (h.span !== 1 || !h.cell.textContent.trim()) return;  // action / checkbox columns
      h.cell.classList.add('grid-sortable');
      h.cell.addEventListener('click', e => {
        if (e.target.closest('input,a,button,select,label,summary')) return;
        sort = sort && sort[0] === h.col ? (sort[1] === 'desc' ? [h.col, 'asc'] : null) : [h.col, 'desc'];
        applySort(); save();
      });
    });

    // ---- filters
    function match(text, f) {
      f = f.trim(); if (!f) return true;
      const m = f.match(/^(>=|<=|>|<|=|!)\s*(.*)$/);
      if (m) {
        const [, op, rest] = m;
        if (op === '!') return !text.toLowerCase().includes(rest.toLowerCase());
        const a = num(text), b = num(rest);
        if (op === '=') return a !== null && b !== null ? a === b : text.toLowerCase() === rest.toLowerCase();
        if (a === null || b === null) return false;
        return op === '>' ? a > b : op === '<' ? a < b : op === '>=' ? a >= b : a <= b;
      }
      return text.toLowerCase().includes(f.toLowerCase());
    }
    function applyFilter() {
      let shown = 0;
      const all = data();
      all.forEach(r => {
        const ok = (!quick || r.textContent.toLowerCase().includes(quick)) &&
          Object.entries(filters).every(([c, f]) => match(cellText(cellAt(r, +c)), f));
        r.classList.toggle('grid-out', !ok);
        if (ok && r.style.display !== 'none') shown++;
      });
      if (count) count.textContent = (quick || Object.values(filters).some(Boolean)) ? tr('rows', '{n} / {m}').replace('{n}', shown).replace('{m}', all.length) : '';
    }

    // ---- columns
    function applyHidden() {
      [...table.rows].forEach(r => columnsOf(r).forEach(x => {
        let off = x.span === 1 ? hidden.has(x.col) : false;
        if (x.span > 1) { off = true; for (let c = x.col; c < x.col + x.span; c++) if (!hidden.has(c)) off = false; }
        x.cell.classList.toggle('grid-hidden', off);
      }));
    }
    function save() { store(key, { hidden: [...hidden], sort }); }

    // ---- toolbar (tables with a few rows)
    let count = null;
    if (data().length >= 4 || table.classList.contains('grid-always')) {
      const bar = document.createElement('div');
      bar.className = 'grid-bar';
      bar.innerHTML = `<input type="search" class="grid-q" placeholder="🔎 ${tr('filter', 'Filter…')}">
        <button type="button" class="ghost sm grid-fbtn" title="${tr('filter_help', '')}">⚲ ${tr('cfilters', 'Column filters')}</button>
        <details class="grid-cols"><summary class="btn ghost sm">☰ ${tr('columns', 'Columns')}</summary><div class="grid-menu"></div></details>
        <span class="small muted grid-count"></span>`;
      const wrap = table.closest('.table-wrap') || table;
      wrap.parentElement.insertBefore(bar, wrap);
      count = bar.querySelector('.grid-count');
      bar.querySelector('.grid-q').addEventListener('input', e => { quick = e.target.value.trim().toLowerCase(); applyFilter(); });
      const menu = bar.querySelector('.grid-menu');
      heads.forEach(h => {
        const name = h.cell.textContent.trim(); if (!name || h.span !== 1) return;
        const lab = document.createElement('label'); lab.className = 'check';
        lab.innerHTML = `<input type="checkbox" ${hidden.has(h.col) ? '' : 'checked'}> <span></span>`;
        lab.querySelector('span').textContent = name;
        lab.querySelector('input').addEventListener('change', e => { e.target.checked ? hidden.delete(h.col) : hidden.add(h.col); applyHidden(); save(); });
        menu.appendChild(lab);
      });
      const all = document.createElement('button'); all.type = 'button'; all.className = 'ghost sm'; all.textContent = tr('show_all', 'Show all');
      all.addEventListener('click', () => { hidden.clear(); menu.querySelectorAll('input').forEach(i => i.checked = true); applyHidden(); save(); });
      menu.appendChild(all);
      bar.querySelector('.grid-fbtn').addEventListener('click', () => {
        let fr = table.querySelector('tr.grid-frow');
        if (fr) { fr.remove(); Object.keys(filters).forEach(k => delete filters[k]); applyFilter(); return; }
        fr = document.createElement('tr'); fr.className = 'grid-frow';
        heads.forEach(h => {
          const td = document.createElement('td'); td.colSpan = h.span;
          if (h.span === 1 && h.cell.textContent.trim()) {
            const i = document.createElement('input'); i.type = 'text'; i.placeholder = '…'; i.title = tr('filter_help', '');
            i.addEventListener('input', () => { filters[h.col] = i.value; applyFilter(); });
            td.appendChild(i);
          }
          fr.appendChild(td);
        });
        head.after(fr); applyHidden(); fr.querySelector('input')?.focus();
      });
    }
    applyHidden(); applySort(); applyFilter();
  }

  function init(root) {
    (root || document).querySelectorAll('main table').forEach((t) => enhance(t, [...document.querySelectorAll('main table')].indexOf(t)));
  }
  window.gridInit = init;
  init();
  const main = document.querySelector('main');
  if (main && window.MutationObserver) {
    let pending = false;
    new MutationObserver(() => { if (pending) return; pending = true; requestAnimationFrame(() => { pending = false; init(); }); })
      .observe(main, { childList: true, subtree: true });
  }
})();
