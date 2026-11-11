/* Sections that open and close with a click on their title (cards inside an element with class="collapsibles").
   The choice is remembered per page in this browser; "collapse all / expand all" buttons go on top. */
(function () {
  'use strict';
  document.querySelectorAll('.collapsibles').forEach((root) => {
    const key = 'collapse:' + location.pathname + ':' + (root.dataset.ck || '');
    let state = {};
    try { state = JSON.parse(localStorage.getItem(key) || '{}') || {}; } catch (e) { state = {}; }
    const save = () => { try { localStorage.setItem(key, JSON.stringify(state)); } catch (e) {} };
    const items = [];
    [...root.querySelectorAll('.card')].forEach((card, i) => {
      const h = card.querySelector('h2');
      if (!h || card.dataset.cl || card.classList.contains('cl-skip')) return;
      card.dataset.cl = '1';
      const id = card.id || ('c' + i + ':' + h.textContent.replace(/\s+/g, ' ').trim().slice(0, 40));
      let head = h;
      while (head.parentElement && head.parentElement !== card) head = head.parentElement;
      head.classList.add('cl-head');
      h.classList.add('cl-toggle');
      h.setAttribute('role', 'button');
      h.tabIndex = 0;
      const set = (closed) => {
        card.classList.toggle('collapsed', closed);
        h.setAttribute('aria-expanded', String(!closed));
        if (closed) state[id] = 1; else delete state[id];
      };
      set(!!state[id]);
      const flip = (e) => {
        if (e.target.closest('a,button,input,select,label,form,summary')) return;
        set(!card.classList.contains('collapsed'));
        save();
      };
      h.addEventListener('click', flip);
      h.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(e); } });
      items.push(set);
    });
    root.querySelectorAll('details[data-remember]').forEach((d) => {  // parts of a card (e.g. a server's bots)
      const id = 'd:' + d.dataset.remember;
      if (id in state) d.open = state[id] === 1;
      d.addEventListener('toggle', () => { state[id] = d.open ? 1 : 0; save(); });
      items.push((closed) => { d.open = !closed; state[id] = closed ? 0 : 1; });
    });
    if (!items.length) return;
    const bar = document.createElement('div');
    bar.className = 'cl-bar';
    const btn = (label, closed) => {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'ghost sm'; b.textContent = label;
      b.addEventListener('click', () => { items.forEach((set) => set(closed)); save(); });
      return b;
    };
    bar.append(btn(root.dataset.collapseAll || 'Collapse all', true), btn(root.dataset.expandAll || 'Expand all', false));
    root.prepend(bar);
  });
})();
