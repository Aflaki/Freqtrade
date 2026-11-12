/* The site's own confirmation dialog instead of the browser's (which looks foreign: other font, top of the window).
   window.siteConfirm(text, {danger}) -> Promise<bool>. Every existing `return confirm(...)` in onclick / onsubmit
   keeps working: confirm() opens this dialog and blocks the action; "OK" replays the same click or submit. */
(function () {
  const body = document.body, OK = body.dataset.ok || 'OK', CANCEL = body.dataset.cancel || 'Cancel';
  let pending = null, pass = false;
  document.addEventListener('click', e => {
    const el = e.target.closest('button, input[type=submit], a');
    if (el) { pending = { type: 'click', el }; setTimeout(() => { if (pending && pending.el === el) pending = null; }, 0); }
  }, true);
  document.addEventListener('submit', e => {
    pending = { type: 'submit', form: e.target, submitter: e.submitter };
    setTimeout(() => { if (pending && pending.form === e.target) pending = null; }, 0);
  }, true);

  function open(text, opt) {
    opt = opt || {};
    return new Promise(resolve => {
      const back = document.createElement('div');
      back.className = 'dlg-back';
      back.innerHTML = '<div class="dlg" role="alertdialog" aria-modal="true"><div class="dlg-icon"></div>' +
        '<div class="dlg-text"></div><div class="dlg-actions"><button type="button" class="dlg-ok"></button>' +
        '<button type="button" class="ghost dlg-cancel"></button></div></div>';
      back.querySelector('.dlg-icon').textContent = opt.danger ? '⚠️' : '❔';
      back.querySelector('.dlg-text').textContent = text;
      const ok = back.querySelector('.dlg-ok'), no = back.querySelector('.dlg-cancel');
      ok.textContent = opt.ok || OK; no.textContent = opt.cancel || CANCEL;
      if (opt.danger) ok.classList.add('danger');
      if (opt.alert) { no.remove(); back.querySelector('.dlg-icon').textContent = 'ℹ️'; }
      const done = v => { document.removeEventListener('keydown', key, true); back.classList.remove('in');
        setTimeout(() => back.remove(), 150); resolve(v); };
      const key = e => { if (e.key === 'Escape') { e.preventDefault(); done(false); } };
      ok.onclick = () => done(true); no.onclick = () => done(false);
      back.addEventListener('mousedown', e => { if (e.target === back) done(false); });
      document.addEventListener('keydown', key, true);
      body.appendChild(back); requestAnimationFrame(() => back.classList.add('in')); ok.focus();
    });
  }
  window.siteConfirm = open;

  window.confirm = function (text) {
    if (pass) { pass = false; return true; }  // the replay of an action the user already confirmed
    const act = pending, danger = !!(act && ((act.el && act.el.classList.contains('danger')) ||
      (act.submitter && act.submitter.classList.contains('danger')) ||
      (act.form && act.form.querySelector('button.danger'))));
    open(String(text), { danger }).then(yes => {
      if (!yes || !act) return;
      pass = true;
      if (act.type === 'submit') act.form.requestSubmit(act.submitter || undefined);
      else act.el.click();
      setTimeout(() => { pass = false; }, 0);
    });
    return false;
  };
})();
