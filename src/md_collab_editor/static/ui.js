// Shared front-end helpers for the IDE modules (activity.js, tabs.js, explorer.js, scm.js,
// search.js) and app.js. Plain script, loaded before them all; exposes `UI`.
//
//   UI.api(method, url, body?, {signal}?) → Promise<json>; throws Error with .status and .data
//   UI.store.get(k, d) / .set(k, v)   localStorage that never throws; .json(k, d) / .setJson(k, v)
//   UI.esc(s)                         HTML-escape
//   UI.root()                         the open folder (absolute path), '' before /api/config
//   UI.on(name, fn) / UI.emit(name, …) tiny event bus. Events:
//                                       'root'  (root)   the open folder changed (also fired at start)
//                                       'ready' ()       app.js finished starting (tabs restored)
//                                       'theme' (dark)   light/dark switched
//   UI.ready                          Promise resolved with 'ready'
//   UI.menu(items, x, y)              context menu at (x, y). items: [{label, action, disabled,
//                                       kbd, checked, danger}, '-' for a separator]; returns close()
//   UI.dialog({title, message, buttons: [{label, value, primary, danger}], cancel})
//                                     → Promise<value> (Esc / backdrop → `cancel`, default null)
//   UI.confirm(message, {title, ok, danger}) → Promise<bool>
//   UI.toast(message, {kind: 'err'|'ok', ms})  a short message at the bottom of the window
//   UI.statusItem(id, {side: 'left'|'right', order, title, onClick, tag}) → element in the
//                                       status bar (created once; later calls return it)
//   UI.stat.watch({paths(), dirs(), onResult(res)}) → unwatch(). One shared POST /api/stat
//                                       poll (~1 s while the page is visible) for all watchers;
//                                       results from before a root change are dropped.
//     UI.stat.now()                   poll right away; UI.stat.bump() drops in-flight results
//   UI.encPath(path)                  encodeURIComponent for a path the server listed (also one
//                                       that is not UTF-8); use it for every ?path= and #hash
//   UI.decPath(s)                     its inverse (reads the #hash)
//   UI.rawUrl(path, version?)         /raw/ URL of a ROOT-relative file
//   UI.isMarkdown(path)               .md / .markdown
//   UI.basename(path) / UI.dirname(path)
//   UI.sendToTerminal(path, range?)   types "@path " or "@path#L1-5 " into the Claude terminal
//                                       (path '': the open folder)
//                                       (bracketed paste, no Enter), shows and focuses it.
//                                       range: {from, to} 1-based line numbers. → Promise<bool>
//                                       (waits up to 5 s for a terminal that is connecting)

const UI = (() => {
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch {} },
    del(k) { try { localStorage.removeItem(k); } catch {} },
    json(k, d) { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch { return d; } },
    setJson(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch {} },
  };

  async function api(method, url, body, { signal } = {}) {
    const r = await fetch(url, {
      method, headers: body ? { 'Content-Type': 'application/json' } : {},
      body: body ? JSON.stringify(body) : undefined, signal,
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) { const err = new Error(data.error || r.statusText); err.status = r.status; err.data = data; throw err; }
    return data;
  }

  // ---------------------------------------------------------------- events

  const handlers = new Map();
  function on(name, fn) {
    if (!handlers.has(name)) handlers.set(name, new Set());
    handlers.get(name).add(fn);
    return () => handlers.get(name).delete(fn);
  }
  function emit(name, ...args) {
    for (const fn of [...(handlers.get(name) || [])]) {
      try { fn(...args); } catch (e) { console.error(`UI '${name}' handler failed`, e); }
    }
  }
  let readyResolve;
  const ready = new Promise(r => { readyResolve = r; });
  on('ready', () => readyResolve());
  let root = '';
  on('root', r => { root = r; });

  // ---------------------------------------------------------------- context menu

  let menuEl = null, menuPrev = null;
  function closeMenu() {
    if (!menuEl) return;
    // focus goes back where it was (a menu opened from the keyboard), unless it has moved on
    const back = menuEl.contains(document.activeElement) ? menuPrev : null;
    menuEl.remove();
    menuEl = menuPrev = null;
    document.removeEventListener('mousedown', outside, true);
    window.removeEventListener('blur', closeMenu);
    window.removeEventListener('resize', closeMenu);
    document.removeEventListener('scroll', closeMenu, true);
    if (back && back.isConnected && typeof back.focus === 'function') back.focus({ preventScroll: true });
  }
  function outside(e) { if (menuEl && !menuEl.contains(e.target)) closeMenu(); }

  function menu(items, x, y) {
    closeMenu();
    const prev = document.activeElement;
    const el = document.createElement('div');
    el.className = 'ctx-menu';
    el.setAttribute('role', 'menu');
    const buttons = [];
    for (const it of items) {
      if (it === '-' || it == null) {
        if (el.lastChild && el.lastChild.tagName !== 'HR') el.append(document.createElement('hr'));
        continue;
      }
      const b = document.createElement('button');
      b.type = 'button';
      b.setAttribute('role', it.checked === undefined ? 'menuitem' : 'menuitemcheckbox');
      if (it.checked !== undefined) b.setAttribute('aria-checked', it.checked ? 'true' : 'false');
      b.className = 'ctx-item' + (it.danger ? ' danger' : '');
      b.disabled = !!it.disabled;
      b.innerHTML = `<span class="ctx-check">${it.checked ? '✓' : ''}</span><span class="ctx-label">${esc(it.label)}</span>${it.kbd ? `<span class="ctx-kbd">${esc(it.kbd)}</span>` : ''}`;
      b.onclick = () => { closeMenu(); it.action?.(); };
      el.append(b);
      buttons.push(b);
    }
    if (el.lastChild?.tagName === 'HR') el.lastChild.remove();
    el.addEventListener('keydown', e => {
      const live = buttons.filter(b => !b.disabled);
      const i = live.indexOf(document.activeElement);
      if (e.key === 'ArrowDown') { e.preventDefault(); live[(i + 1) % live.length]?.focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); live[(i - 1 + live.length) % live.length]?.focus(); }
      else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeMenu(); }
      else if (e.key === 'Tab') { e.preventDefault(); }
    });
    el.addEventListener('contextmenu', e => e.preventDefault());
    document.body.append(el);
    const w = el.offsetWidth, h = el.offsetHeight;
    el.style.left = Math.max(4, Math.min(x, innerWidth - w - 4)) + 'px';
    el.style.top = Math.max(4, y + h > innerHeight - 4 ? y - h : y) + 'px';
    menuEl = el;
    menuPrev = prev;
    setTimeout(() => {
      document.addEventListener('mousedown', outside, true);
      window.addEventListener('blur', closeMenu);
      window.addEventListener('resize', closeMenu);
      document.addEventListener('scroll', closeMenu, true);
    }, 0);
    buttons.find(b => !b.disabled)?.focus({ preventScroll: true });
    return closeMenu;
  }

  // ---------------------------------------------------------------- dialogs

  function dialog({ title = '', message = '', html = '', buttons = [{ label: 'OK', value: true, primary: true }], cancel = null }) {
    return new Promise(resolve => {
      const back = document.createElement('div');
      back.className = 'modal-back';
      const box = document.createElement('div');
      box.className = 'modal';
      box.setAttribute('role', 'alertdialog');
      box.setAttribute('aria-modal', 'true');
      box.innerHTML = `${title ? `<h3 class="modal-title">${esc(title)}</h3>` : ''}<div class="modal-msg">${html || esc(message)}</div><div class="modal-actions"></div>`;
      if (title) box.setAttribute('aria-label', title);
      const actions = box.querySelector('.modal-actions');
      const prev = document.activeElement;
      const done = v => {
        back.remove();
        document.removeEventListener('keydown', key, true);
        if (prev && prev.isConnected && typeof prev.focus === 'function') prev.focus({ preventScroll: true });
        resolve(v);
      };
      let primary = null;
      for (const b of buttons) {
        const el = Object.assign(document.createElement('button'), { type: 'button', textContent: b.label });
        if (b.primary) { el.classList.add('primary'); primary = primary || el; }
        if (b.danger) el.classList.add('danger');
        el.onclick = () => done(b.value);
        actions.append(el);
      }
      const key = e => {
        if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); done(cancel); }
        else if (e.key === 'Tab') {   // keep focus inside the dialog
          const els = [...actions.querySelectorAll('button')];
          const i = els.indexOf(document.activeElement);
          e.preventDefault();
          els[(i + (e.shiftKey ? -1 : 1) + els.length) % els.length].focus();
        }
      };
      document.addEventListener('keydown', key, true);
      back.addEventListener('mousedown', e => { if (e.target === back) done(cancel); });
      back.append(box);
      document.body.append(back);
      (primary || actions.querySelector('button'))?.focus();
    });
  }

  function confirm(message, { title = '', ok = 'OK', danger = false } = {}) {
    return dialog({
      title, message, cancel: false,
      buttons: [{ label: ok, value: true, primary: true, danger }, { label: 'Cancel', value: false }],
    });
  }

  // ---------------------------------------------------------------- toast

  let toastEl = null, toastT;
  function toast(message, { kind = '', ms = 3500 } = {}) {
    if (!toastEl) {
      toastEl = document.createElement('div');
      toastEl.id = 'toast';
      toastEl.setAttribute('role', 'status');
      document.body.append(toastEl);
    }
    toastEl.textContent = message;
    toastEl.className = kind;
    toastEl.hidden = false;
    clearTimeout(toastT);
    toastT = setTimeout(() => { toastEl.hidden = true; }, ms);
  }

  // ---------------------------------------------------------------- status bar slots

  function statusItem(id, { side = 'left', order = 50, title = '', onClick = null, tag } = {}) {
    let el = document.getElementById(id);
    if (!el) {
      el = document.createElement(tag || (onClick ? 'button' : 'span'));
      el.id = id;
      el.className = 'stat-item';
      if (el.tagName === 'BUTTON') el.type = 'button';
      const box = document.getElementById(side === 'right' ? 'status-right' : 'status-left');
      el.dataset.order = order;
      el.style.order = order;
      box.append(el);
    }
    if (title) el.title = title;
    if (onClick) el.onclick = onClick;
    return el;
  }

  // ---------------------------------------------------------------- shared /api/stat poll

  const stat = (() => {
    const watchers = new Set();
    let busy = false, epoch = 0, again = false;
    async function tick() {
      if (document.hidden) return;
      if (busy) { again = true; return; }
      const paths = new Set(), dirs = new Set();
      for (const w of watchers) {
        try {
          for (const p of (w.paths?.() || [])) paths.add(p);
          for (const d of (w.dirs?.() || [])) dirs.add(d);
        } catch (e) { console.error(e); }
      }
      if (!paths.size && !dirs.size) return;
      busy = true;
      const ep = epoch;
      try {
        const res = await api('POST', '/api/stat', { paths: [...paths], dirs: [...dirs] });
        if (ep !== epoch) return;
        res.versions = res.versions || {};
        res.dirs = res.dirs || {};
        for (const w of [...watchers]) {
          try { w.onResult?.(res); } catch (e) { console.error(e); }
        }
      } catch { /* server gone or restarting: try again next tick */ }
      finally {
        busy = false;
        if (again) { again = false; setTimeout(tick, 0); }
      }
    }
    setInterval(tick, 1000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
    return {
      watch(w) { watchers.add(w); return () => watchers.delete(w); },
      now: tick,
      bump() { epoch++; },
    };
  })();

  // ---------------------------------------------------------------- paths

  const isMarkdown = p => /\.(md|markdown)$/i.test(p || '');
  const basename = p => (p || '').split('/').pop();
  const dirname = p => { const i = (p || '').lastIndexOf('/'); return i < 0 ? '' : p.slice(0, i); };
  // A path for a URL. The server lists a file name that is not UTF-8 with lone surrogates
  // U+DC80-U+DCFF (Python's surrogateescape), on which encodeURIComponent throws: each goes
  // back as the byte it stands for (%80-%FF), which the server turns into the same name.
  function encPath(p) {
    p = String(p);
    let out = '', from = 0;
    for (let i = 0; i < p.length; i++) {
      const c = p.charCodeAt(i);
      if (c >= 0xdc80 && c <= 0xdcff && !(i > 0 && p.charCodeAt(i - 1) >= 0xd800 && p.charCodeAt(i - 1) <= 0xdbff)) {
        out += encodeURIComponent(p.slice(from, i)) + '%' + (c - 0xdc00).toString(16).toUpperCase();
        from = i + 1;
      }
    }
    return out + encodeURIComponent(p.slice(from));
  }
  // encPath's inverse (decodeURIComponent throws on %80-%FF that are not UTF-8): the bytes as
  // UTF-8, any byte that is not part of a valid sequence as U+DC80-U+DCFF, as the server does
  function decPath(s) {
    const bytes = [], enc = new TextEncoder(), dec = new TextDecoder('utf-8', { fatal: true });
    for (let i = 0; i < s.length;) {
      if (s[i] === '%' && /^[0-9a-fA-F]{2}$/.test(s.slice(i + 1, i + 3))) { bytes.push(parseInt(s.slice(i + 1, i + 3), 16)); i += 3; continue; }
      const ch = String.fromCodePoint(s.codePointAt(i));
      bytes.push(...enc.encode(ch));
      i += ch.length;
    }
    let out = '';
    for (let i = 0; i < bytes.length;) {
      const b = bytes[i];
      const n = b < 0x80 ? 1 : b >= 0xc2 && b < 0xe0 ? 2 : b >= 0xe0 && b < 0xf0 ? 3 : b >= 0xf0 && b < 0xf5 ? 4 : 0;
      if (n && i + n <= bytes.length) {
        try { out += dec.decode(new Uint8Array(bytes.slice(i, i + n))); i += n; continue; } catch {}
      }
      out += String.fromCharCode(0xdc00 + b);
      i++;
    }
    return out;
  }
  const rawUrl = (p, v) => '/raw/' + p.split('/').map(encPath).join('/') + (v ? `?v=${encodeURIComponent(v)}` : '');

  // ---------------------------------------------------------------- send to the Claude terminal

  async function sendToTerminal(path, range) {
    const T = window.Term;
    // before showing the panel, which in a narrow window hides the side bar the user is in
    if (!T || !T.sendText || T.available?.() === false) { toast('The Claude terminal is not available.', { kind: 'err' }); return false; }
    window.App?.showTerminal?.();
    // a terminal that was never on screen connects now: give it a moment
    const ended = () => ['exited', 'failed'].includes(T.state?.());
    for (let i = 0; i < 50 && !T.running() && !ended(); i++) await new Promise(r => setTimeout(r, 100));
    if (!T.running()) {
      toast(ended() ? 'The Claude session has ended: restart it in the terminal first.' : 'The Claude terminal is not running yet; try again when it has started.', { kind: 'err' });
      return false;
    }
    // path '' is the open folder itself
    const abs = (root.replace(/\/+$/, '') + (path ? '/' + path : '')).replace(/\/+$/, '');
    const cwd = (T.cwd() || '').replace(/\/+$/, '');
    let p = abs;   // the terminal's own folder too: Claude Code takes no "@." as a mention
    if (cwd && abs.startsWith(cwd + '/')) p = abs.slice(cwd.length + 1);
    let ref = '@' + (/\s/.test(p) ? `"${p}"` : p);
    if (range && range.from) ref += range.to && range.to !== range.from ? `#L${range.from}-${range.to}` : `#L${range.from}`;
    if (!T.sendText(ref + ' ', { bracketed: true })) {
      toast('The Claude terminal could not take the text.', { kind: 'err' });
      return false;
    }
    T.focus();
    return true;
  }

  return {
    api, store, esc, on, emit, ready, root: () => root,
    menu, closeMenu, dialog, confirm, toast, statusItem, stat,
    isMarkdown, basename, dirname, encPath, decPath, rawUrl, sendToTerminal,
  };
})();
window.UI = UI;
