// Quick open (Ctrl/Cmd+P), find in files (Ctrl/Cmd+Shift+F, the Search panel) and Send to
// Claude terminal from any editor that shows a file (Ctrl/Cmd+Alt+L and the editor's context
// menu; Shift+right-click still gives the browser's own menu). Plain script after ui.js,
// activity.js, tabs.js and explorer.js; exposes `Search`.
//
//   Search.quickOpen(text?)        the Go to file picker: fuzzy file names over GET /api/allfiles,
//                                  recently opened files first; "name:12" (or "name:12:5") opens
//                                  at that line. Enter opens a preview tab, Ctrl/Cmd+Enter or a
//                                  double-click keeps it open, Esc closes.
//   Search.closeQuickOpen()
//   Search.find({query, glob, focus = true})   show the Search panel (and search for `query`)
//   Search.run()                   search again with the panel's query, options and globs
//   Search.results() → [{path, line, col, len, text, offset}]   the matches on screen
//   Search.recent() → [path]       recently opened files of the open folder, most recent first
//   Search.fuzzy(query, path) → {score, hits: [index…]} | null   the quick-open matcher
//   Search.fileOf(cm) → model|null the open file a CodeMirror editor shows: its Doc is the
//                                  model's, or linked to it (Tabs.linkedDoc, e.g. a diff's
//                                  working side)
//   Search.sendSelection(cm?) → Promise<bool>   "@path#La-b " for that editor's selection
//                                  (default: the main editor) into the Claude terminal
//   Search.editorMenu(cm, x, y)    the editor context menu (Send to Claude terminal, Ask Claude,
//                                  Cut, Copy, Paste, Select all)
//
// Send to Claude terminal works in every CodeMirror editor whose document is an open file,
// with no wiring needed: the keys and the context menu are handled on `document`, after the
// editor (so an editor's own binding or menu, e.g. spelling suggestions, wins). Other modules
// send a whole file with UI.sendToTerminal(path) (or a line range with {from, to}).
// Ctrl/Cmd+P and Ctrl/Cmd+Shift+F are caught on window in the capture phase, so no editor sees
// them; they are left to the terminal (#term) and do nothing while a dialog is open.
// CodeMirror's replace, which was Shift-Ctrl-F, becomes Ctrl-H (pcDefault keymap; the Mac
// keeps Cmd-Alt-F).

const Search = (() => {
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform);
  const kbd = s => isMac ? s.replace(/Ctrl\+/g, '⌘').replace(/Alt\+/g, '⌥').replace(/Shift\+/g, '⇧') : s;
  const ICON = {
    file: '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.2" d="M3.5 1.75h5.5l3.5 3.5v9a.5.5 0 0 1-.5.5h-8.5a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5Z"/><path fill="none" stroke="currentColor" stroke-width="1.2" d="M9 1.75v3.5h3.5"/></svg>',
    refresh: '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" d="M13.2 8A5.2 5.2 0 1 1 11.6 4.3"/><path fill="currentColor" d="M13.6 1.6v4.2H9.4z"/></svg>',
    clear: '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" d="M4 4l8 8M12 4l-8 8"/></svg>',
    collapse: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="11" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.2"/><path stroke="currentColor" stroke-width="1.3" d="M5 8h6"/></svg>',
  };

  // a letter shortcut: by the character typed, or by the key's position on layouts without
  // Latin letters (and with the Mac's Option key, which changes the character)
  function keyIs(e, letter) {
    const k = (e.key || '').toLowerCase();
    return k === letter || ((!/^[a-z]$/.test(k) || (isMac && e.altKey)) && e.code === 'Key' + letter.toUpperCase());
  }

  function copy(text, note = true) {
    const fallback = () => {
      const ta = Object.assign(document.createElement('textarea'), { value: text });
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.append(ta);
      ta.select();
      let ok = false;
      try { ok = document.execCommand('copy'); } catch {}
      ta.remove();
      return ok;
    };
    const p = navigator.clipboard && navigator.clipboard.writeText
      ? navigator.clipboard.writeText(text).then(() => true, fallback) : Promise.resolve(fallback());
    if (note) p.then(ok => UI.toast(ok ? `Copied ${text.length > 80 ? text.slice(0, 80) + '…' : text}` : 'Could not copy to the clipboard.', ok ? {} : { kind: 'err' }));
    return p;
  }

  // a dialog or the folder browser is open: the global shortcuts do nothing
  const dialogOpen = () => !!document.querySelector('.modal-back') || document.getElementById('browser')?.hidden === false;

  // ---------------------------------------------------------------- recently opened files

  const RECENT_MAX = 60;
  const recentKey = () => 'mdedit.recent:' + UI.root();
  let recentList = [];
  function loadRecent() {
    const v = UI.store.json(recentKey(), []);
    recentList = Array.isArray(v) ? v.filter(p => typeof p === 'string') : [];
  }
  function touch(path) {
    if (!UI.root() || !path) return;
    recentList = [path, ...recentList.filter(p => p !== path)].slice(0, RECENT_MAX);
    UI.store.setJson(recentKey(), recentList);
  }
  Tabs.on('activate', t => { if (t && t.type !== 'custom' && t.path) touch(t.path); });
  Tabs.on('rename', (from, to) => {
    const moved = p => (p === from || p.startsWith(from + '/')) ? to + p.slice(from.length) : p;
    recentList = [...new Set(recentList.map(moved))];
    UI.store.setJson(recentKey(), recentList);
  });

  // ---------------------------------------------------------------- fuzzy matching

  // The best way to match `q` (lower case) to text[start..] as a subsequence, by dynamic
  // programming: matches at the start, after a separator or at a camelCase hump score more, and
  // runs of consecutive characters more still. → {score, hits: [index in text]} | null
  const NEG = -1e9;
  const DP_MAX = 40000;
  const dpS = new Float64Array(DP_MAX), dpB = new Int32Array(DP_MAX);
  function bonusAt(text, a, start) {
    if (a === start) return 8;
    const p = text[a - 1], c = text[a];
    if (p === '/') return 7;
    if (p === '_' || p === '-' || p === '.' || p === ' ' || p === '\\') return 6;
    if (p >= 'a' && p <= 'z' && c >= 'A' && c <= 'Z') return 5;
    if (!(p >= '0' && p <= '9') && c >= '0' && c <= '9') return 2;
    return 0;
  }
  function align(q, text, lower, start) {
    const n = text.length - start, m = q.length;
    if (n < m) return null;
    if (n * m > DP_MAX) {   // very long: the leftmost match will do
      const hits = [];
      for (let j = start, i = 0; j < text.length && i < m; j++) if (lower.charCodeAt(j) === q.charCodeAt(i)) { hits.push(j); i++; }
      return hits.length === m ? { score: m, hits } : null;
    }
    dpS.fill(NEG, 0, n * m);
    for (let i = 0; i < m; i++) {
      const qc = q.charCodeAt(i);
      let best = NEG, bestJ = -1;   // max of row i-1 over the columns before j
      for (let j = i; j < n; j++) {
        if (i > 0) { const v = dpS[(i - 1) * n + j - 1]; if (v > best) { best = v; bestJ = j - 1; } }
        if (lower.charCodeAt(start + j) !== qc) continue;
        const bonus = 2 + bonusAt(text, start + j, start);
        if (i === 0) { dpS[j] = bonus - Math.min(j, 3); continue; }
        const run = dpS[(i - 1) * n + j - 1];
        let v = NEG, from = -1;
        if (run > NEG) { v = run + bonus + 5; from = j - 1; }
        if (best > NEG && best - 1 + bonus > v) { v = best - 1 + bonus; from = bestJ; }
        if (from >= 0) { dpS[i * n + j] = v; dpB[i * n + j] = from; }
      }
    }
    let end = -1, top = NEG;
    for (let j = m - 1; j < n; j++) { const v = dpS[(m - 1) * n + j]; if (v > top) { top = v; end = j; } }
    if (end < 0) return null;
    const hits = new Array(m);
    for (let i = m - 1, j = end; i >= 0; i--) { hits[i] = start + j; j = dpB[i * n + j]; }
    return { score: top, hits };
  }
  function subseq(q, lower, from = 0) {
    let i = 0;
    for (let j = from; j < lower.length && i < q.length; j++) if (lower.charCodeAt(j) === q.charCodeAt(i)) i++;
    return i === q.length;
  }
  // lower case with the same indices as the path (a few characters change length)
  function lowerOf(path) {
    const l = path.toLowerCase();
    return l.length === path.length ? l : Array.from(path, ch => { const x = ch.toLowerCase(); return x.length === ch.length ? x : ch; }).join('');
  }
  const prep = path => ({ path, lower: lowerOf(path), base: path.lastIndexOf('/') + 1 });
  // Matches in the file name rank above matches that need the folders.
  function match(q, f) {
    if (!subseq(q, f.lower)) return null;
    let r = null, inBase = false;
    if (q.indexOf('/') < 0 && subseq(q, f.lower, f.base)) { r = align(q, f.path, f.lower, f.base); inBase = !!r; }
    if (!r) r = align(q, f.path, f.lower, 0);
    if (!r) return null;
    let score = r.score + (inBase ? 100 : 0);
    if (inBase) {
      const name = f.lower.slice(f.base);
      if (name === q || name.startsWith(q + '.')) score += 50;
    }
    return { score, hits: r.hits };
  }
  function fuzzy(query, path) {
    const q = String(query).replace(/\s+/g, '').toLowerCase();
    return q ? match(q, prep(path)) : { score: 0, hits: [] };
  }

  // ---------------------------------------------------------------- quick open

  const QO_MAX = 200;
  let qo = null;          // the open picker
  let files = null;       // {root, list: [prep()], set, truncated, at, error}
  let filesLoad = null;   // {root, p}

  function loadFiles() {
    const root = UI.root();
    if (filesLoad && filesLoad.root === root) return filesLoad.p;
    const p = UI.api('GET', '/api/allfiles').then(r => {
      if (root !== UI.root()) return;
      const list = Array.isArray(r.files) ? r.files : [];
      files = { root, list: list.map(prep), set: new Set(list), truncated: !!r.truncated, at: Date.now(), error: null };
    }, e => {
      if (root !== UI.root()) return;
      if (files && files.root === root) files.error = e.message;
      else files = { root, list: [], set: new Set(), truncated: false, at: 0, error: e.message };
    }).finally(() => {
      if (filesLoad && filesLoad.p === p) filesLoad = null;
      if (qo) updateQo(true);
    });
    filesLoad = { root, p };
    return p;
  }

  function quickOpen(text) {
    if (qo) {
      if (typeof text === 'string') { qo.input.value = text; updateQo(); }
      qo.input.focus();
      qo.input.select();
      return;
    }
    UI.closeMenu();
    const back = document.createElement('div');
    back.className = 'qo-back';
    back.innerHTML = `<div class="qo" role="dialog" aria-label="Go to file">
      <input class="qo-input" type="text" role="combobox" aria-expanded="true" aria-controls="qo-list"
        aria-autocomplete="list" aria-label="Go to file" spellcheck="false" autocomplete="off"
        placeholder="Go to file by name (add :line to jump to a line)">
      <ul class="qo-list" id="qo-list" role="listbox" aria-label="Files"></ul>
      <div class="qo-foot muted"></div></div>`;
    qo = {
      back, input: back.querySelector('.qo-input'), list: back.querySelector('.qo-list'), foot: back.querySelector('.qo-foot'),
      items: [], sel: 0, line: null, ch: 0, prev: document.activeElement, clickT: null,
    };
    const { input, list } = qo;
    input.value = typeof text === 'string' ? text : '';
    input.addEventListener('input', () => updateQo());
    input.addEventListener('keydown', qoKey);
    back.addEventListener('mousedown', e => { if (e.target === back) closeQuickOpen(); });
    list.addEventListener('mousedown', e => e.preventDefault());   // keep the focus in the box
    list.addEventListener('click', e => {
      const li = e.target.closest('.qo-item');
      if (!li) return;
      const i = +li.dataset.i;
      select(i);
      clearTimeout(qo.clickT);
      // wait for a possible second click: a double-click keeps the tab open
      if (e.detail >= 2) openItem(i, true);
      else qo.clickT = setTimeout(() => openItem(i, false), 220);
    });
    document.body.append(back);
    input.focus();
    if (!files || files.root !== UI.root() || Date.now() - files.at > 2000) loadFiles();
    updateQo();
  }

  function closeQuickOpen(restore = true) {
    if (!qo) return;
    const { back, prev, clickT } = qo;
    clearTimeout(clickT);
    qo = null;
    back.remove();
    if (restore && prev && prev.isConnected && typeof prev.focus === 'function') prev.focus({ preventScroll: true });
  }

  function qoKey(e) {
    const n = qo.items.length;
    const mod = e.ctrlKey || e.metaKey;
    let handled = true;
    if (e.key === 'ArrowDown' || (e.ctrlKey && !e.shiftKey && !e.altKey && keyIs(e, 'n'))) select(qo.sel + 1, true);
    else if (e.key === 'ArrowUp' || (e.ctrlKey && e.shiftKey && !e.altKey && keyIs(e, 'n'))) select(qo.sel - 1, true);
    else if (e.key === 'PageDown') select(Math.min(n - 1, qo.sel + 10));
    else if (e.key === 'PageUp') select(Math.max(0, qo.sel - 10));
    else if (e.key === 'Enter') { if (!e.isComposing) openItem(qo.sel, mod); }
    else if (e.key === 'Escape') closeQuickOpen();
    else if (e.key === 'Tab') { /* stay in the box */ }
    else handled = false;
    if (handled) { e.preventDefault(); e.stopPropagation(); }
  }

  function select(i, wrap = false) {
    const n = qo.items.length;
    if (!n) return;
    qo.sel = wrap ? (i + n) % n : Math.max(0, Math.min(n - 1, i));
    for (const li of qo.list.querySelectorAll('.qo-item')) {
      const on = +li.dataset.i === qo.sel;
      li.classList.toggle('sel', on);
      li.setAttribute('aria-selected', on ? 'true' : 'false');
      if (on) { qo.input.setAttribute('aria-activedescendant', li.id); li.scrollIntoView({ block: 'nearest' }); }
    }
  }

  function hl(text, hits, from) {
    let out = '', k = 0;
    while (k < hits.length && hits[k] < from) k++;
    for (let i = 0; i < text.length; i++) {
      const c = UI.esc(text[i]);
      if (k < hits.length && hits[k] === from + i) { out += `<b class="qo-hit">${c}</b>`; k++; }
      else out += c;
    }
    return out.replace(/<\/b><b class="qo-hit">/g, '');
  }

  function updateQo(keep = false) {
    if (!qo) return;
    const raw = qo.input.value;
    const m = /^(.*?)(?::(\d+)(?::(\d+))?)?\s*$/.exec(raw);
    qo.line = m && m[2] ? Math.max(0, +m[2] - 1) : null;
    qo.ch = m && m[3] ? Math.max(0, +m[3] - 1) : 0;
    const q = (m ? m[1] : raw).replace(/\s+/g, '').toLowerCase();
    const ready = files && files.root === UI.root() && files.at > 0;
    const prevPath = keep && qo.items[qo.sel] ? qo.items[qo.sel].path : null;
    const recent = recentList.filter(p => !ready || files.set.has(p) || Tabs.list().some(t => t.path === p));
    const isRecent = new Set(recent);
    let items = [], total = 0;
    if (!q) {
      items = recent.slice(0, QO_MAX).map(path => ({ path, hits: [], recent: true }));
      if (ready) {
        for (const f of files.list) {
          if (items.length >= QO_MAX) break;
          if (!isRecent.has(f.path)) items.push({ path: f.path, hits: [] });
        }
      }
      total = ready ? files.list.length : items.length;
    } else {
      const pool = ready ? files.list : recent.map(prep);
      const scored = [];
      for (const f of pool) {
        const r = match(q, f);
        if (r) scored.push({ path: f.path, hits: r.hits, score: r.score + (isRecent.has(f.path) ? 20 : 0), recent: isRecent.has(f.path) });
      }
      scored.sort((a, b) => (b.score - a.score) || (a.path.length - b.path.length) || (a.path < b.path ? -1 : a.path > b.path ? 1 : 0));
      total = scored.length;
      items = scored.slice(0, QO_MAX);
    }
    qo.items = items;
    // with nothing typed, Enter goes back to the file before the one on screen
    const act = Tabs.active();
    let sel = 0;
    if (!q && items.length > 1 && act && items[0].path === act.path) sel = 1;
    if (prevPath) { const i = items.findIndex(it => it.path === prevPath); if (i >= 0) sel = i; }
    qo.sel = sel;
    const html = items.map((it, i) => {
      const base = it.path.lastIndexOf('/') + 1;
      const name = it.path.slice(base), dir = base ? it.path.slice(0, base - 1) : '';
      const tag = !q && it.recent && i === 0 ? 'recently opened' : !q && !it.recent && (i === 0 || items[i - 1].recent) && recent.length ? 'other files' : '';
      return `<li class="qo-item${i === sel ? ' sel' : ''}" id="qo-opt-${i}" role="option" aria-selected="${i === sel}" data-i="${i}" title="${UI.esc(it.path)}">`
        + `<span class="ico">${ICON.file}</span><span class="qo-name">${hl(name, it.hits, base)}</span>`
        + `<span class="qo-dir">${dir ? hl(dir, it.hits, 0) : ''}</span>${tag ? `<span class="qo-tag">${tag}</span>` : ''}</li>`;
    }).join('');
    const failed = !ready && files && files.root === UI.root() && files.error;
    const empty = failed ? 'The file list could not be loaded.' : !ready ? 'Loading the file list…'
      : q ? 'No matching files.' : 'This folder has no files.';
    qo.list.innerHTML = html || `<li class="qo-empty" role="presentation">${UI.esc(empty)}</li>`;
    if (items.length) qo.input.setAttribute('aria-activedescendant', `qo-opt-${sel}`);
    else qo.input.removeAttribute('aria-activedescendant');
    qo.list.querySelector('.qo-item.sel')?.scrollIntoView({ block: 'nearest' });
    const notes = [];
    if (files && files.error) notes.push(`Could not list the files: ${files.error}`);
    if (q && total > items.length) notes.push(`Showing the best ${items.length} of ${total.toLocaleString()} matches.`);
    if (ready && files.truncated) notes.push(`Only the first ${files.list.length.toLocaleString()} files are listed.`);
    if (qo.line !== null && items.length) notes.push(`Opens at line ${qo.line + 1}.`);
    qo.foot.textContent = notes.join(' ');
    qo.foot.classList.toggle('err', !!(files && files.error));
  }

  function openItem(i, pin) {
    if (!qo) return;
    const it = qo.items[i];
    if (!it) return;
    const { line, ch } = qo;
    closeQuickOpen(false);
    const opts = { preview: !pin, focus: true };
    if (line !== null) Object.assign(opts, { line, ch });
    Tabs.open(it.path, opts).then(t => { if (!t) restoreFocus(); });
  }

  function restoreFocus() { if (Tabs.active()?.type === 'file') Tabs.cm().focus(); }

  // ---------------------------------------------------------------- find in files (Search panel)

  const OPTS = [['case', 'Aa', 'Match case', 'C'], ['word', 'ab', 'Match whole word', 'W'], ['regex', '.*', 'Use regular expression', 'R']];
  const view = document.createElement('div');
  view.className = 'search-view';
  view.innerHTML = `
    <div class="side-head"><span class="side-title">Search</span><span class="side-actions">
      <button type="button" class="tool-btn" data-act="refresh" title="Search again">${ICON.refresh}</button>
      <button type="button" class="tool-btn" data-act="clear" title="Clear the search">${ICON.clear}</button>
      <button type="button" class="tool-btn" data-act="collapse" title="Collapse or expand every file">${ICON.collapse}</button>
    </span></div>
    <div class="search-form">
      <div class="search-box">
        <input id="search-q" type="text" spellcheck="false" autocomplete="off" placeholder="Search" aria-label="Search">
        ${OPTS.map(([k, label, title, key]) => `<button type="button" class="search-opt" data-opt="${k}" title="${title} (${kbd('Alt+' + key)})" aria-label="${title}" aria-pressed="false">${label}</button>`).join('')}
      </div>
      <input id="search-glob" type="text" spellcheck="false" autocomplete="off" placeholder="Files to include, e.g. *.py, src/, !*.min.js" aria-label="Files to include"
        title="Globs separated by commas; ! excludes. A glob without a / matches at any depth, and a folder covers its contents.">
    </div>
    <div class="search-msg muted" role="status" aria-live="polite"></div>
    <div id="search-results" role="tree" tabindex="0" aria-label="Search results"></div>`;
  const qEl = view.querySelector('#search-q');
  const globEl = view.querySelector('#search-glob');
  const box = view.querySelector('.search-box');
  const msgEl = view.querySelector('.search-msg');
  const resultsEl = view.querySelector('#search-results');

  const saved = UI.store.json('mdedit.search', {}) || {};
  const opts = { case: !!saved.case, word: !!saved.word, regex: !!saved.regex };
  qEl.value = typeof saved.q === 'string' ? saved.q : '';
  globEl.value = typeof saved.glob === 'string' ? saved.glob : '';

  let groups = [];        // [{path, matches: [result], collapsed}] in the order git / the walk gave
  let flat = [];          // the rows on screen: {g} for a file, {g, r} for a match
  let focusI = -1;
  let last = null;        // {q, truncated, engine} of the results on screen; null: none
  let collapsedPaths = new Set();
  let searchT = null, busy = false, again = false, epoch = 0, error = null;

  const handle = Activity.register('search', {
    panelEl: view,
    onShow() { if (UI.root() && qEl.value && !last && !busy && !error) run(); },
  });
  handle.button.title = `Search (${kbd('Ctrl+Shift+F')})`;
  handle.button.addEventListener('click', () => { if (handle.visible()) setTimeout(() => focusQuery(), 0); });

  function saveSettings() {
    UI.store.setJson('mdedit.search', { q: qEl.value, glob: globEl.value, ...opts });
  }
  function syncOpts() {
    for (const b of view.querySelectorAll('.search-opt')) {
      const on = !!opts[b.dataset.opt];
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
    }
  }
  syncOpts();
  function toggleOpt(k) {
    opts[k] = !opts[k];
    syncOpts();
    run();
  }

  function focusQuery() {
    qEl.focus();
    qEl.select();
  }

  function schedule(ms = 300) {
    clearTimeout(searchT);
    searchT = setTimeout(run, ms);
  }

  async function run() {
    clearTimeout(searchT);
    saveSettings();
    const q = qEl.value;
    if (!q) { epoch++; error = null; box.classList.remove('bad'); setResults(null); return; }
    if (busy) { again = true; return; }
    busy = true;
    const ep = ++epoch;
    const params = new URLSearchParams({
      q, regex: opts.regex ? '1' : '0', case: opts.case ? '1' : '0', word: opts.word ? '1' : '0', glob: globEl.value.trim(),
    });
    view.classList.add('busy');
    renderMsg('Searching…');
    let res = null, err = null;
    try { res = await UI.api('GET', '/api/search?' + params); } catch (e) { err = e; }
    busy = false;
    view.classList.remove('busy');
    if (again) { again = false; run(); return; }
    if (ep !== epoch) { renderMsg(); return; }
    if (err) {
      error = err.message || 'The search failed.';
      box.classList.toggle('bad', err.status === 400 && /regular expression/i.test(error));
      setResults(null, true);
      return;
    }
    error = null;
    box.classList.remove('bad');
    if (!last || last.q !== q) collapsedPaths = new Set();
    const by = new Map();
    for (const r of res.results || []) {
      let g = by.get(r.path);
      if (!g) { g = { path: r.path, matches: [], collapsed: collapsedPaths.has(r.path) }; by.set(r.path, g); }
      g.matches.push(r);
    }
    setResults({ q, truncated: !!res.truncated, engine: res.engine, groups: [...by.values()] });
  }

  function setResults(res, keepError = false) {
    if (!keepError) error = null;
    if (!res) { groups = []; last = null; }
    else { groups = res.groups; last = { q: res.q, truncated: res.truncated, engine: res.engine }; }
    focusI = -1;
    render();
  }

  function count() { return groups.reduce((s, g) => s + g.matches.length, 0); }

  function renderMsg(text) {
    msgEl.classList.remove('err');
    if (text) { msgEl.textContent = text; msgEl.title = ''; return; }
    if (error) { msgEl.textContent = error; msgEl.classList.add('err'); msgEl.title = ''; return; }
    if (!last) { msgEl.textContent = ''; return; }
    const n = count(), files = groups.length;
    const s = (k, w) => `${k.toLocaleString()} ${w}${k === 1 ? '' : 's'}`;
    msgEl.textContent = n ? `${s(n, 'result')} in ${s(files, 'file')}` : `No results found${globEl.value.trim() ? ' in the files to include' : ''}.`;
    msgEl.title = last.engine === 'git'
      ? 'Searched with git grep: tracked and untracked files, skipping git-ignored and binary files.'
      : 'Searched every text file under 5 MB, skipping hidden folders.';
    if (last.truncated) {
      msgEl.append(Object.assign(document.createElement('div'), {
        className: 'search-warn',
        textContent: 'Results truncated: only the first 2,000 matches are shown. Narrow the search to see the rest.',
      }));
    }
  }

  // the line with the match marked; long lines start a little before the match
  function previewHtml(r) {
    const text = r.text || '';
    const s = Math.max(0, Math.min(text.length, r.col - (r.offset || 0)));
    const e = Math.max(s, Math.min(text.length, s + (r.len || 0)));
    let from = /^\s*/.exec(text)[0].length;
    if (from > s) from = s;
    let lead = (r.offset || 0) > 0;
    if (s - from > 28) { from = s - 20; lead = true; }
    if (from > 0 && /[\uDC00-\uDFFF]/.test(text[from])) from--;   // not half a surrogate pair
    return `${lead ? '…' : ''}${UI.esc(text.slice(from, s))}<mark>${UI.esc(text.slice(s, e))}</mark>${UI.esc(text.slice(e))}`;
  }

  function render() {
    renderMsg();
    flat = [];
    const out = [];
    for (const g of groups) {
      const i = flat.length;
      flat.push({ g });
      const base = g.path.lastIndexOf('/') + 1;
      const name = g.path.slice(base), dir = base ? g.path.slice(0, base - 1) : '';
      out.push(`<div class="sr-row sr-file" role="treeitem" aria-level="1" aria-expanded="${!g.collapsed}" data-i="${i}" title="${UI.esc(g.path)}">`
        + `<span class="tw">›</span><span class="ico">${ICON.file}</span><span class="sr-name">${UI.esc(name)}</span>`
        + `<span class="sr-dir">${UI.esc(dir)}</span><span class="sr-count">${g.matches.length}</span></div>`);
      if (g.collapsed) continue;
      for (const r of g.matches) {
        const k = flat.length;
        flat.push({ g, r });
        out.push(`<div class="sr-row sr-match" role="treeitem" aria-level="2" data-i="${k}" title="${UI.esc(`${r.path}:${r.line}:${r.col + 1}`)}">`
          + `<span class="sr-text">${previewHtml(r)}</span></div>`);
      }
    }
    resultsEl.innerHTML = out.join('');
    if (focusI >= flat.length) focusI = flat.length - 1;
    marks();
  }

  function marks() {
    for (const el of resultsEl.querySelectorAll('.sr-row.focus')) { el.classList.remove('focus'); el.removeAttribute('aria-selected'); }
    if (focusI < 0) return;
    const el = resultsEl.querySelector(`.sr-row[data-i="${focusI}"]`);
    if (el) { el.classList.add('focus'); el.setAttribute('aria-selected', 'true'); el.scrollIntoView({ block: 'nearest' }); }
  }

  function setFocus(i) {
    if (!flat.length) { focusI = -1; return; }
    focusI = Math.max(0, Math.min(flat.length - 1, i));
    marks();
  }

  function toggleGroup(g, collapsed = !g.collapsed) {
    if (g.collapsed === collapsed) return;
    g.collapsed = collapsed;
    if (collapsed) collapsedPaths.add(g.path); else collapsedPaths.delete(g.path);
    render();
    focusI = flat.findIndex(x => x.g === g && !x.r);
    marks();
  }

  function openResult(r, { pin = false, focus = true } = {}) {
    const line = r.line - 1;
    return Tabs.open(r.path, { preview: !pin, focus, sel: { from: { line, ch: r.col }, to: { line, ch: r.col + r.len } } });
  }

  function dismiss(i) {
    const it = flat[i];
    if (!it) return;
    if (it.r) {
      it.g.matches = it.g.matches.filter(x => x !== it.r);
      if (!it.g.matches.length) groups = groups.filter(g => g !== it.g);
    } else groups = groups.filter(g => g !== it.g);
    render();
    setFocus(Math.min(i, flat.length - 1));
  }

  const rowIndex = t => { const el = t instanceof Element && t.closest('.sr-row'); return el ? +el.dataset.i : -1; };
  resultsEl.addEventListener('mousedown', e => { if (e.detail > 1) e.preventDefault(); });   // no text selection
  resultsEl.addEventListener('click', e => {
    const i = rowIndex(e.target);
    if (i < 0 || !flat[i]) return;
    setFocus(i);
    const { g, r } = flat[i];
    if (!r) { if (e.detail === 1) toggleGroup(g); }
    else if (e.detail === 1) openResult(r, { focus: false });
  });
  resultsEl.addEventListener('dblclick', e => {
    const i = rowIndex(e.target);
    const it = flat[i];
    if (it && it.r) openResult(it.r, { pin: true, focus: true });
  });
  resultsEl.addEventListener('contextmenu', e => {
    const i = rowIndex(e.target);
    if (i < 0 || !flat[i]) return;
    e.preventDefault();
    setFocus(i);
    menuFor(i, e.clientX, e.clientY);
  });
  function menuFor(i, x, y) {
    const { g, r } = flat[i];
    UI.menu([
      { label: 'Open', action: () => r ? openResult(r) : Tabs.open(g.path, { preview: true, focus: true }) },
      { label: 'Open and keep open', action: () => r ? openResult(r, { pin: true }) : Tabs.open(g.path, { preview: false, focus: true }) },
      '-',
      { label: 'Copy relative path', action: () => copy(g.path) },
      { label: 'Reveal in Explorer', action: () => window.Explorer?.reveal(g.path, { show: true }) },
      { label: r ? 'Send line to Claude terminal' : 'Send to Claude terminal', action: () => UI.sendToTerminal(g.path, r ? { from: r.line, to: r.line } : null) },
      '-',
      { label: 'Dismiss', kbd: 'Delete', action: () => dismiss(i) },
    ], x, y);
  }

  resultsEl.addEventListener('focus', () => { if (focusI < 0 && flat.length) setFocus(0); });
  resultsEl.addEventListener('keydown', e => {
    if (e.target !== resultsEl) return;
    const it = flat[focusI];
    let handled = true;
    switch (e.key) {
      case 'ArrowDown': setFocus(focusI + 1); break;
      case 'ArrowUp':
        if (focusI <= 0) qEl.focus();
        else setFocus(focusI - 1);
        break;
      case 'Home': setFocus(0); break;
      case 'End': setFocus(flat.length - 1); break;
      case 'PageDown': setFocus(focusI + 15); break;
      case 'PageUp': setFocus(focusI - 15); break;
      case 'ArrowRight': if (it && !it.r) { if (it.g.collapsed) toggleGroup(it.g, false); else setFocus(focusI + 1); } break;
      case 'ArrowLeft':
        if (it && !it.r) toggleGroup(it.g, true);
        else if (it) setFocus(flat.findIndex(x => x.g === it.g && !x.r));
        break;
      case 'Enter':
        if (it && it.r) openResult(it.r, { pin: e.ctrlKey || e.metaKey, focus: true });
        else if (it) toggleGroup(it.g);
        break;
      case ' ':
        if (it && it.r) openResult(it.r, { focus: false });
        else if (it) toggleGroup(it.g);
        break;
      case 'Delete': if (it) dismiss(focusI); break;
      case 'ContextMenu': {
        const el = resultsEl.querySelector('.sr-row.focus');
        if (el) { const b = el.getBoundingClientRect(); menuFor(focusI, b.left + 24, b.bottom); }
        break;
      }
      default:
        if (e.key === 'F10' && e.shiftKey && it) {
          const el = resultsEl.querySelector('.sr-row.focus');
          if (el) { const b = el.getBoundingClientRect(); menuFor(focusI, b.left + 24, b.bottom); }
        } else handled = false;
    }
    if (handled) e.preventDefault();
  });

  for (const input of [qEl, globEl]) {
    input.addEventListener('input', () => schedule());
    input.addEventListener('keydown', e => {
      // Alt+C / W / R toggle the options (and Alt+W must not close the tab, as it does elsewhere)
      if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey) {
        const o = OPTS.find(([, , , key]) => keyIs(e, key.toLowerCase()));
        if (o) { e.preventDefault(); e.stopPropagation(); toggleOpt(o[0]); return; }
      }
      if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); run(); }
      else if (e.key === 'ArrowDown' && flat.length) { e.preventDefault(); resultsEl.focus(); setFocus(Math.max(0, focusI)); }
      else if (e.key === 'ArrowUp' && input === globEl) { e.preventDefault(); qEl.focus(); }
      else if (e.key === 'Escape' && Tabs.active()?.type === 'file') { e.preventDefault(); Tabs.cm().focus(); }
    });
  }
  for (const b of view.querySelectorAll('.search-opt')) {
    b.addEventListener('mousedown', e => e.preventDefault());   // keep the focus in the box
    b.addEventListener('click', () => toggleOpt(b.dataset.opt));
  }
  view.querySelector('.side-actions').addEventListener('click', e => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    if (b.dataset.act === 'refresh') run();
    else if (b.dataset.act === 'clear') { qEl.value = ''; run(); focusQuery(); }
    else if (b.dataset.act === 'collapse') {
      const collapse = groups.some(g => !g.collapsed);
      for (const g of groups) {
        g.collapsed = collapse;
        if (collapse) collapsedPaths.add(g.path); else collapsedPaths.delete(g.path);
      }
      render();
    }
  });

  function find({ query, glob, focus = true } = {}) {
    closeQuickOpen(false);
    Activity.show('search');
    if (typeof glob === 'string') globEl.value = glob;
    if (typeof query === 'string' && query) {
      qEl.value = opts.regex ? query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') : query;
      run();
    }
    if (focus) focusQuery();
  }

  // text to search for: the selection in the editor with the focus (or the main editor),
  // when it is on one line
  function seedText() {
    const a = document.activeElement;
    const w = a && a.closest && a.closest('.CodeMirror');
    const cm = (w && w.CodeMirror) || (Tabs.active()?.type === 'file' ? Tabs.cm() : null);
    if (!cm || !cm.somethingSelected()) return null;
    const s = cm.getSelection();
    return s && !/[\r\n]/.test(s) && s.length <= 300 ? s : null;
  }

  UI.on('root', () => {
    epoch++;
    files = null;
    loadRecent();
    closeQuickOpen(false);
    error = null;
    box.classList.remove('bad');
    collapsedPaths = new Set();
    setResults(null);
    if (Activity.visible('search') && qEl.value) run();
  });

  // ---------------------------------------------------------------- send to the Claude terminal

  function fileOf(cm) {
    if (!cm || typeof cm.getDoc !== 'function') return null;
    const doc = cm.getDoc();
    for (const m of Tabs.models()) {
      if (m.doc === doc) return m;
      let hit = false;
      m.doc.iterLinkedDocs(d => { if (d === doc) hit = true; });
      if (hit) return m;
    }
    return null;
  }

  function sendSelection(cm) {
    cm = cm || Tabs.cm();
    const m = fileOf(cm);
    if (!m) { UI.toast('This editor is not showing a file that can be sent.', { kind: 'err' }); return Promise.resolve(false); }
    let range = null;
    if (cm.somethingSelected()) {
      const from = cm.getCursor('from'), to = cm.getCursor('to');
      let lastLine = to.line;
      if (to.ch === 0 && to.line > from.line) lastLine--;   // whole lines end at the next line's start
      range = { from: from.line + 1, to: lastLine + 1 };
    }
    return Promise.resolve(UI.sendToTerminal(m.path, range));
  }

  async function clip(cm, cut) {
    const ok = await copy(cm.getSelection(), false);
    if (!ok) UI.toast(`The browser did not allow copying from the menu: press ${kbd(cut ? 'Ctrl+X' : 'Ctrl+C')} instead.`, { kind: 'err' });
    else if (cut && !cm.getOption('readOnly')) cm.replaceSelection('', null, 'cut');
    cm.focus();
  }

  async function paste(cm) {
    try {
      const text = await navigator.clipboard.readText();
      cm.replaceSelection(text, null, 'paste');
    } catch {
      UI.toast(`The browser did not allow pasting from the menu: press ${kbd('Ctrl+V')} instead.`, { kind: 'err' });
    }
    cm.focus();
  }

  function editorMenu(cm, x, y) {
    const sel = cm.somethingSelected();
    const ro = !!cm.getOption('readOnly');
    const ask = cm === Tabs.cm() ? document.querySelector('#toolbar [data-cmd="ask"]') : null;
    return UI.menu([
      { label: sel ? 'Send selection to Claude terminal' : 'Send file to Claude terminal', kbd: kbd('Ctrl+Alt+L'), action: () => sendSelection(cm) },
      ask ? { label: 'Ask Claude…', kbd: kbd('Ctrl+J'), disabled: ask.disabled, action: () => ask.click() } : null,
      '-',
      { label: 'Cut', kbd: kbd('Ctrl+X'), disabled: ro || !sel, action: () => clip(cm, true) },
      { label: 'Copy', kbd: kbd('Ctrl+C'), disabled: !sel, action: () => clip(cm, false) },
      { label: 'Paste', kbd: kbd('Ctrl+V'), disabled: ro, action: () => paste(cm) },
      '-',
      { label: 'Select all', kbd: kbd('Ctrl+A'), action: () => { cm.execCommand('selectAll'); cm.focus(); } },
    ], x, y);
  }

  const editorAt = t => {
    const w = t instanceof Element && !t.closest('#term, .CodeMirror-dialog') ? t.closest('.CodeMirror') : null;
    return w && w.CodeMirror && typeof w.CodeMirror.getDoc === 'function' ? w.CodeMirror : null;
  };

  document.addEventListener('contextmenu', e => {
    if (e.defaultPrevented || e.shiftKey) return;   // Shift+right-click: the browser's own menu
    const cm = editorAt(e.target);
    if (!cm || !fileOf(cm)) return;
    e.preventDefault();
    editorMenu(cm, e.clientX, e.clientY);
  });

  // Ctrl/Cmd+Alt+L in any editor showing a file (the main editor binds it itself)
  document.addEventListener('keydown', e => {
    if (e.defaultPrevented || !(e.ctrlKey || e.metaKey) || !e.altKey || e.shiftKey || !keyIs(e, 'l')) return;
    const cm = editorAt(e.target);
    if (!cm || !fileOf(cm)) return;
    e.preventDefault();
    sendSelection(cm);
  });

  // ---------------------------------------------------------------- global shortcuts

  // Shift-Ctrl-F was CodeMirror's replace; it is find in files now, so replace moves to Ctrl-H
  // as in VS Code (the Mac keeps Cmd-Alt-F)
  const pc = CodeMirror.keyMap.pcDefault;
  if (pc) {
    if (pc['Shift-Ctrl-F'] === 'replace') delete pc['Shift-Ctrl-F'];
    if (!pc['Ctrl-H']) pc['Ctrl-H'] = 'replace';
  }

  window.addEventListener('keydown', e => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.defaultPrevented) return;
    const quick = !e.shiftKey && keyIs(e, 'p'), inFiles = e.shiftKey && keyIs(e, 'f');
    if (!quick && !inFiles) return;
    if (e.target instanceof Element && e.target.closest('#term')) return;   // Claude Code uses Ctrl+P
    e.preventDefault();   // not the browser's Print (that stays in its menu), nor CodeMirror's replace
    e.stopPropagation();
    if (dialogOpen()) return;
    if (inFiles) {
      const seed = seedText();
      find(seed ? { query: seed } : {});
    } else if (qo && e.target === qo.input) select(qo.sel + 1, true);
    else quickOpen();
  }, true);

  loadRecent();

  return {
    quickOpen, closeQuickOpen, find, run,
    results: () => groups.flatMap(g => g.matches),
    recent: () => recentList.slice(),
    fuzzy, fileOf, sendSelection, editorMenu,
  };
})();
window.Search = Search;
