// Source control, VS Code style: the Source Control panel (the files changed against HEAD or
// against the branch's merge-base with the default branch, polled from /api/git/status), diff
// tabs (CodeMirror's merge addon side by side, or an inline view), hunk revert and per-file
// discard, change bars in the editor's gutter, status letters in the Explorer and the branch in
// the status bar. Plain script after ui.js, activity.js, tabs.js and explorer.js; exposes `SCM`.
//
//   SCM.refresh() → Promise          ask git now (it is also polled: ~2 s while the panel is
//                                    open, ~10 s otherwise, and right after saves and discards)
//   SCM.openDiff(path, {preview = true, focus = true, line, mode}) → Promise<tab|null>
//                                    the file's diff tab against the panel's base (or mode:
//                                    'head' | 'branch'); line (0-based) scrolls the working side
//   SCM.files() → [{path, status, old_path?}]   the changed files, in the panel's order
//   SCM.status(path) → entry | undefined
//   SCM.info() → the last /api/git/info answer, or null
//   SCM.nextFile(dir = 1)            open the next (-1: previous) changed file's diff
//   SCM.nextChange(dir = 1)          next / previous change in the active diff or file tab
//                                    (Alt+F5 / Shift+Alt+F5)
//
// A diff tab is a Tabs custom tab (kind 'diff', remembered across reloads) holding a reference
// to the file's model: its working side is Tabs.linkedDoc(model), so edits there are edits of
// the file (one dirty state, Ctrl+S, autosave rules) and show in its normal tab too. Change bars
// are 'gutter' line classes on each model's Doc (diff_match_patch in line mode against the HEAD
// text, fetched once per file and HEAD), drawn in an 'scm-gutter' column of the main editor.

const SCM = (() => {
  const POLL_OPEN = 2000, POLL_BG = 10000;
  const GUTTER = 'scm-gutter';
  const NAMES = { M: 'Modified', A: 'Added', D: 'Deleted', R: 'Renamed', U: 'Untracked', C: 'Conflict' };
  const svg = body => `<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
  const ICON = {
    file: svg('<path stroke-width="1.2" d="M3.5 1.75h5.5l3.5 3.5v9a.5.5 0 0 1-.5.5h-8.5a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5Z"/><path stroke-width="1.2" d="M9 1.75v3.5h3.5"/>'),
    refresh: '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" d="M13.2 8A5.2 5.2 0 1 1 11.6 4.3"/><path fill="currentColor" d="M13.6 1.6v4.2H9.4z"/></svg>',
    up: svg('<path d="M8 13V3M3.5 7.5 8 3l4.5 4.5"/>'),
    down: svg('<path d="M8 3v10M3.5 8.5 8 13l4.5-4.5"/>'),
    prevFile: svg('<path stroke-width="1.2" d="M6 14.25H3.5a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5h5.5l3.5 3.5V7"/><path d="M11.5 15V9.5M9.2 11.7l2.3-2.3 2.3 2.3"/>'),
    nextFile: svg('<path stroke-width="1.2" d="M6 14.25H3.5a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5h5.5l3.5 3.5V7"/><path d="M11.5 9v5.5M9.2 12.3l2.3 2.3 2.3-2.3"/>'),
    open: svg('<path stroke-width="1.2" d="M7 14.25H3.5a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5h5.5l3.5 3.5V8"/><path d="M9.5 12.5h5M12.5 10.5l2 2-2 2"/>'),
  };

  // ---------------------------------------------------------------- state

  let mode = UI.store.get('mdedit.scm.base', 'head') === 'branch' ? 'branch' : 'head';
  let info = null;         // last /api/git/info answer (null: not known yet)
  let st = null;           // last /api/git/status answer
  let files = [];
  let byPath = new Map();
  let error = '';
  let epoch = 0;           // bumped when the open folder changes: older answers are dropped
  let listSig = null;
  let focusPath = null;    // keyboard row in the list
  const diffs = new Set(); // live diff tab states

  const plural = (n, w) => `${n.toLocaleString()} ${w}${n === 1 ? '' : 's'}`;
  const short = sha => sha ? sha.slice(0, 7) : '';
  const isRepo = () => !!(info && info.repo);

  // ---------------------------------------------------------------- the panel

  const el = document.createElement('div');
  el.className = 'scm';
  el.innerHTML = `
    <div class="side-head"><span class="side-title">Source Control</span><span class="side-actions">
      <button type="button" class="tool-btn" data-act="prev-file" title="Previous changed file">${ICON.prevFile}</button>
      <button type="button" class="tool-btn" data-act="next-file" title="Next changed file">${ICON.nextFile}</button>
      <button type="button" class="tool-btn" data-act="refresh" title="Refresh">${ICON.refresh}</button>
    </span></div>
    <div class="scm-base">
      <div class="seg small" role="radiogroup" aria-label="Compare the working tree with">
        <button type="button" role="radio" data-mode="head" title="Uncommitted changes: the working tree against HEAD, untracked files included">Uncommitted</button><button type="button" role="radio" data-mode="branch" title="Everything this branch changed: the working tree against where it left the default branch (git merge-base), like a pull request">Branch</button>
      </div>
    </div>
    <div class="scm-desc muted"></div>
    <div class="scm-note" hidden></div>
    <div class="scm-group" hidden><span>Changes</span><span class="count"></span></div>
    <div class="scm-list" role="listbox" tabindex="0" aria-label="Changed files"></div>
    <div class="scm-empty muted" hidden></div>
    <div class="scm-foot muted" hidden></div>`;
  const $ = s => el.querySelector(s);
  const list = $('.scm-list');

  const handle = Activity.register('scm', {
    panelEl: el,
    onShow() { refresh(); schedule(); },
    onHide() { schedule(); },
  });

  function renderPanel() {
    const repo = isRepo();
    for (const b of el.querySelectorAll('.scm-base button')) {
      const on = b.dataset.mode === mode;
      b.classList.toggle('on', on);
      b.setAttribute('aria-checked', on ? 'true' : 'false');
    }
    $('.scm-base').hidden = !repo;
    for (const b of el.querySelectorAll('.side-actions button')) b.disabled = !repo;
    const desc = $('.scm-desc'), note = $('.scm-note'), empty = $('.scm-empty'), foot = $('.scm-foot'), group = $('.scm-group');
    desc.textContent = '';
    note.hidden = true;
    foot.hidden = true;
    group.hidden = true;
    empty.hidden = true;
    if (error) { empty.hidden = false; empty.textContent = `Could not ask git: ${error}`; }
    else if (!info) { empty.hidden = false; empty.textContent = 'Loading…'; }
    else if (!repo) {
      empty.hidden = false;
      empty.textContent = 'The open folder is not in a git repository, so there are no changes to show.'
        + (info.reason ? ` (${info.reason})` : '');
    } else if (st && st.base) {
      const b = st.base;
      desc.textContent = b.mode === 'branch'
        ? `Working tree against ${b.label} (merge-base ${short(b.sha)}): everything this branch changed.`
        : b.sha ? `Working tree against HEAD (${short(b.sha)}).` : 'No commits yet: every file is new.';
      desc.title = b.mode === 'branch' ? `${b.ref} ${b.sha}` : b.sha || '';
      if (b.note) { note.hidden = false; note.textContent = b.note; }
      group.hidden = false;
      group.querySelector('.count').textContent = files.length.toLocaleString();
      if (!files.length) { empty.hidden = false; empty.textContent = 'No changes.'; }
      const extra = [];
      if (st.outside) extra.push(`${plural(st.outside, 'change')} outside this folder ${st.outside === 1 ? 'is' : 'are'} hidden.`);
      if (st.truncated) extra.push(`Only the first ${files.length.toLocaleString()} changed files are listed.`);
      if (st.nested) extra.push(`${plural(st.nested, 'folder')} with a git repository of ${st.nested === 1 ? 'its' : 'their'} own (or a worktree) ${st.nested === 1 ? 'is' : 'are'} not listed.`);
      if (extra.length) { foot.hidden = false; foot.textContent = extra.join(' '); }
    } else { empty.hidden = false; empty.textContent = 'Loading…'; }
    renderList();
  }

  function renderList() {
    const frag = document.createDocumentFragment();
    for (const f of files) {
      const r = document.createElement('div');
      r.className = `scm-row scm-${f.status}`;
      r.dataset.path = f.path;
      r.setAttribute('role', 'option');
      const dir = UI.dirname(f.path);
      r.title = `${f.old_path ? `${f.old_path} → ` : ''}${f.path} — ${NAMES[f.status] || f.status}`;
      r.innerHTML = `<span class="ico">${ICON.file}</span><span class="nm">${UI.esc(UI.basename(f.path))}</span>`
        + `<span class="dir">${UI.esc(f.old_path && UI.dirname(f.old_path) !== dir ? `${UI.dirname(f.old_path) || '.'} → ${dir || '.'}` : dir)}</span>`
        + `<span class="st" aria-label="${NAMES[f.status] || f.status}">${UI.esc(f.status)}</span>`;
      frag.append(r);
    }
    list.replaceChildren(frag);
    if (focusPath !== null && !byPath.has(focusPath)) focusPath = null;
    updateRows();
  }

  // active (the diff on screen) and keyboard-focus marks, without rebuilding the rows
  function updateRows() {
    const t = Tabs.active();
    const act = t && t.type === 'custom' && t.spec.kind === 'diff' ? t.path : null;
    for (const r of list.querySelectorAll('.scm-row')) {
      r.classList.toggle('active', r.dataset.path === act);
      r.classList.toggle('focus', r.dataset.path === focusPath);
      r.setAttribute('aria-selected', r.dataset.path === focusPath ? 'true' : 'false');
    }
  }

  function setMode(m) {
    if (m === mode) return;
    mode = m;
    UI.store.set('mdedit.scm.base', m);
    st = null;
    files = [];
    byPath = new Map();
    listSig = null;
    renderPanel();
    handle.setBadge(null);
    refresh();
  }

  el.querySelector('.scm-base').addEventListener('click', e => {
    const b = e.target.closest('button[data-mode]');
    if (b) setMode(b.dataset.mode);
  });
  el.querySelector('.side-actions').addEventListener('click', e => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    if (b.dataset.act === 'refresh') refresh();
    else nextFile(b.dataset.act === 'next-file' ? 1 : -1);
  });

  const rowOf = t => t.closest && t.closest('.scm-row[data-path]');
  list.addEventListener('mousedown', e => { if (e.detail > 1) e.preventDefault(); });   // no text selection
  list.addEventListener('click', e => {
    const r = rowOf(e.target);
    if (!r) return;
    focusPath = r.dataset.path;
    updateRows();
    openDiff(r.dataset.path, { preview: true, focus: false });
  });
  list.addEventListener('dblclick', e => {
    const r = rowOf(e.target);
    if (r) openDiff(r.dataset.path, { preview: false, focus: true });
  });
  list.addEventListener('contextmenu', e => {
    e.preventDefault();
    const r = rowOf(e.target);
    if (!r) return;
    focusPath = r.dataset.path;
    updateRows();
    menuFor(r.dataset.path, e.clientX, e.clientY);
  });
  list.addEventListener('focus', () => {
    if (focusPath === null && files.length) { focusPath = files[0].path; updateRows(); }
  });
  list.addEventListener('keydown', e => {
    const i = files.findIndex(f => f.path === focusPath);
    const go = j => {
      if (!files.length) return;
      focusPath = files[Math.max(0, Math.min(files.length - 1, j))].path;
      updateRows();
      list.querySelector('.scm-row.focus')?.scrollIntoView({ block: 'nearest' });
    };
    let handled = true;
    switch (e.key) {
      case 'ArrowDown': go(i + 1); break;
      case 'ArrowUp': go(i < 0 ? files.length - 1 : i - 1); break;
      case 'Home': go(0); break;
      case 'End': go(files.length - 1); break;
      case 'Enter': if (focusPath) openDiff(focusPath, { preview: false, focus: true }); break;
      case ' ': if (focusPath) openDiff(focusPath, { preview: true, focus: false }); break;
      case 'ContextMenu': {
        const row = list.querySelector('.scm-row.focus');
        if (row) { const b = row.getBoundingClientRect(); menuFor(focusPath, b.left + 24, b.bottom); }
        break;
      }
      default:
        if (e.key === 'F10' && e.shiftKey && focusPath) {
          const b = list.querySelector('.scm-row.focus').getBoundingClientRect();
          menuFor(focusPath, b.left + 24, b.bottom);
        } else handled = false;
    }
    if (handled) e.preventDefault();
  });

  function copy(text) {
    const fallback = () => {
      const ta = Object.assign(document.createElement('textarea'), { value: text });
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.append(ta);
      ta.select();
      try { document.execCommand('copy'); } catch {}
      ta.remove();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).catch(fallback);
    else fallback();
    UI.toast(`Copied ${text}`);
  }

  function menuFor(path, x, y) {
    const f = byPath.get(path);
    if (!f) return;
    const uncommitted = !!(st && st.base && st.base.mode === 'head');
    UI.menu([
      { label: 'Open diff', action: () => openDiff(path, { preview: false, focus: true }) },
      { label: 'Open file', disabled: f.status === 'D', action: () => Tabs.open(path, { preview: false, focus: true }) },
      '-',
      uncommitted
        ? { label: f.status === 'U' ? 'Delete untracked file…' : 'Discard changes…', danger: true, action: () => discard(f) }
        : { label: 'Discard changes (Uncommitted only)', disabled: true },
      '-',
      { label: 'Copy relative path', action: () => copy(path) },
      { label: 'Reveal in Explorer', disabled: f.status === 'D', action: () => window.Explorer?.reveal(path, { show: true }) },
      { label: 'Send to Claude terminal', action: () => UI.sendToTerminal(path) },
    ], x, y);
  }

  // ---------------------------------------------------------------- polling git

  let pollT = null;
  function schedule() {
    clearTimeout(pollT);
    pollT = setTimeout(async () => {
      if (!document.hidden && UI.root()) await refresh();
      schedule();
    }, handle.visible() ? POLL_OPEN : POLL_BG);
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { refresh(); schedule(); } });

  let inflight = null, again = false;
  function refresh() {
    if (inflight) { again = true; return inflight; }
    const ep = epoch, m = mode;
    inflight = (async () => {
      try {
        const [ni, ns] = await Promise.all([UI.api('GET', '/api/git/info'), UI.api('GET', `/api/git/status?base=${m}`)]);
        if (ep === epoch && m === mode) apply(ni, ns);
      } catch (e) {
        if (ep === epoch && m === mode) {
          const was = error;
          error = e.message || 'unknown error';
          if (was !== error) renderPanel();
        }
      }
    })().finally(() => {
      inflight = null;
      if (again) { again = false; refresh(); }
    });
    return inflight;
  }

  let soonT = null;
  const refreshSoon = (ms = 150) => { clearTimeout(soonT); soonT = setTimeout(refresh, ms); };

  function apply(ni, ns) {
    const headChanged = !info || info.head !== ni.head || info.top !== ni.top || !!info.repo !== !!ni.repo;
    const branchChanged = !info || info.branch !== ni.branch || info.top !== ni.top;
    info = ni;
    st = ns && ns.repo ? ns : null;
    const hadError = !!error;
    error = '';
    files = st ? st.files || [] : [];
    byPath = new Map(files.map(f => [f.path, f]));
    const sig = JSON.stringify([!!info.repo, info.reason, st && st.base, st && st.outside, st && st.truncated, st && st.nested, files]);
    if (headChanged) headCache = new Map();
    if (sig !== listSig || hadError) {
      listSig = sig;
      renderPanel();
      decorations();
      scheduleGutter(0);
    }
    handle.setBadge(files.length || null, files.length ? `${plural(files.length, 'changed file')}${st && st.base && st.base.mode === 'branch' ? ` since ${st.base.label}` : ''}` : undefined);
    if (headChanged || branchChanged) renderBranch();
    if (headChanged) scheduleGutter(0);
    // diff tabs follow a moved base (a commit, a checkout)
    for (const s of diffs) {
      if (!s.base || s.loading) continue;
      const sha = baseSha(s.d.mode);
      if (sha === undefined) continue;
      if (s.sha === undefined) s.sha = sha;
      else if (sha !== s.sha) loadBase(s);
    }
  }

  // the base commit of a mode as far as we know (undefined: not known)
  function baseSha(m) {
    if (!info) return undefined;
    if (!info.repo) return null;
    if (m === 'head') return info.head || null;
    return st && st.base && st.base.mode === 'branch' ? st.base.sha : undefined;
  }

  // ---------------------------------------------------------------- status bar and Explorer

  function renderBranch() {
    const item = UI.statusItem('stat-branch', { side: 'left', order: 10, onClick: () => Activity.show('scm') });
    if (!isRepo()) { item.hidden = true; return; }
    item.hidden = false;
    const name = UI.basename(String(info.top).replace(/\/+$/, '')) || info.top;
    const br = info.branch || (info.head ? `${short(info.head)} (detached)` : 'no commits');
    item.innerHTML = `<span class="stat-repo">${UI.esc(name)}</span> ⎇ ${UI.esc(br)}`;
    item.title = `Repository ${info.top}, ${info.branch ? `branch ${info.branch}` : info.head ? `detached HEAD at ${short(info.head)}` : 'no commits yet'}`
      + `${info.default_branch ? ` (default branch: ${info.default_branch})` : ''}. Click to open Source Control.`;
  }

  let decoSig = '';
  let decoDirs = new Set();
  function decorations() {
    const sig = files.map(f => f.status + f.path).join('\n');
    if (sig === decoSig) return;
    decoSig = sig;
    decoDirs = new Set();
    for (const f of files) {
      for (let d = UI.dirname(f.path); d; d = UI.dirname(d)) decoDirs.add(d);
    }
    window.Explorer?.render();
  }
  window.Explorer?.decorate((path, isDir) => {
    if (isDir) return decoDirs.has(path) ? { cls: 'scm-dir', badge: '•', title: 'Contains changes' } : null;
    const f = byPath.get(path);
    return f ? { cls: `scm-${f.status}`, badge: f.status, title: NAMES[f.status] || f.status } : null;
  });

  // ---------------------------------------------------------------- line diffs

  const lineChunks = LineDiff.lineChunks;   // linediff.js

  // the same, in scm-worker.js: the gutter must not stop the editor for a second on a big file
  let worker = null, workerSeq = 0;
  const waiting = new Map();
  function lineChunksAsync(a, b) {
    if (worker === null) {
      try {
        worker = new Worker('scm-worker.js');
        const dmp = document.querySelector('script[src*="diff_match_patch"]');
        worker.postMessage({ init: { dmp: dmp.src, linediff: new URL('linediff.js', location.href).href } });
        worker.onmessage = ({ data }) => { const r = waiting.get(data.id); waiting.delete(data.id); if (r) r(data.chunks); };
        worker.onerror = e => {   // its scripts did not load: work here instead
          e.preventDefault();
          worker = false;
          for (const r of waiting.values()) r(null);
          waiting.clear();
        };
      } catch { worker = false; }
    }
    if (!worker) return Promise.resolve(null);
    return new Promise(res => {
      const id = ++workerSeq;
      waiting.set(id, res);
      worker.postMessage({ id, a, b });
    });
  }

  // a deleted file's lines: all of them removed (no line after a final newline)
  const deletedChunks = base => {
    const n = base === '' ? 0 : base.split('\n').length - (base.endsWith('\n') ? 1 : 0);
    return n ? [{ origFrom: 0, origTo: n, editFrom: 0, editTo: 0 }] : [];
  };

  // a diff tab's line diff, worked out once for each version of the file and the base
  // (changeGeneration identifies a version, undo included; called without an argument it has
  // no side effects)
  function diffChunks(s) {
    const gen = s.linked ? s.linked.changeGeneration() : -1;
    const c = s.chunkCache;
    if (c && c.gen === gen && c.base === s.base.text) return c.list;
    const list = s.linked ? lineChunks(s.base.text, s.linked.getValue()) : deletedChunks(s.base.text);
    s.chunkCache = { gen, base: s.base.text, list };
    return list;
  }

  // Put the base's lines of `chunk` back into `doc` (as the merge addon's revert arrows do).
  function revertChunk(doc, baseText, c) {
    const orig = new CodeMirror.Doc(baseText);
    const Pos = CodeMirror.Pos;
    const origStart = c.origTo > orig.lastLine() ? Pos(c.origFrom - 1) : Pos(c.origFrom, 0);
    const editStart = c.editTo > doc.lastLine() ? Pos(c.editFrom - 1) : Pos(c.editFrom, 0);
    doc.replaceRange(orig.getRange(origStart, Pos(c.origTo, 0)), editStart, Pos(c.editTo, 0), '+revert');
  }

  // ---------------------------------------------------------------- gutter change bars

  // path (+ "\0" + old path for a rename) → Promise</api/git/show answer against HEAD>
  let headCache = new Map();
  function headText(path, old = null) {
    const key = old ? `${path}\0${old}` : path;
    if (!headCache.has(key)) {
      const cache = headCache;
      const p = UI.api('GET', `/api/git/show?path=${UI.encPath(path)}&base=head${old ? `&old_path=${UI.encPath(old)}` : ''}`);
      p.catch(() => { if (cache.get(key) === p) cache.delete(key); });
      cache.set(key, p);
    }
    return headCache.get(key);
  }

  const gut = new WeakMap();   // model → {marks: [[line handle, class]], chunks}
  let gutterHooked = false;
  function mainCm() {
    const cm = Tabs.cm();
    if (!cm) return null;
    if (!gutterHooked) {
      gutterHooked = true;
      cm.on('gutterClick', (c, line, gutter) => { if (gutter === GUTTER) gutterClick(line); });
    }
    const want = isRepo();
    const now = cm.getOption('gutters') || [];
    if (want !== now.includes(GUTTER)) {
      const rest = now.filter(g => g !== GUTTER);
      cm.setOption('gutters', want ? [...(rest.includes('CodeMirror-linenumbers') ? rest : ['CodeMirror-linenumbers', ...rest]), GUTTER] : rest);
    }
    return cm;
  }

  let gutterT = null, gutterBusy = false, gutterAgain = false;
  function scheduleGutter(ms = 300) { clearTimeout(gutterT); gutterT = setTimeout(updateGutter, ms); }

  async function updateGutter() {
    if (gutterBusy) { gutterAgain = true; return; }   // one diff at a time; then the latest text
    gutterBusy = true;
    try { await updateGutterNow(); }
    catch (e) { console.error(e); }
    finally {
      gutterBusy = false;
      if (gutterAgain) { gutterAgain = false; scheduleGutter(0); }
    }
  }

  async function updateGutterNow() {
    const cm = mainCm();
    const t = Tabs.active();
    if (!cm || !t || t.type !== 'file') return;
    const m = t.model;
    if (!isRepo()) { await paint(m, null); return; }
    const ep = epoch;
    let base = null;
    const f = byPath.get(m.path);
    try {
      base = await headText(m.path);
      // renamed since HEAD (Uncommitted: git mv): its HEAD text is under the old name
      if (base && !base.exists && f?.status === 'R' && f.old_path) base = await headText(m.path, f.old_path);
    } catch {}
    if (ep !== epoch || Tabs.active() !== t || t.closed) return;
    let text = null;
    if (base && base.exists && !base.binary && !base.too_large) text = base.text;
    else if (base && !base.exists && ['U', 'A'].includes(f?.status)) text = '';   // new: all added
    await paint(m, text);
  }

  async function paint(m, baseText) {
    const doc = m.doc;
    const g = gut.get(m) || { marks: [], chunks: [], gen: null, base: null };
    gut.set(m, g);
    const gen = doc.changeGeneration();
    if (g.gen === gen && g.base === baseText) return;   // the bars (on the Doc) are still right
    let chunks = [];
    if (baseText != null) {
      const text = doc.getValue();
      chunks = await lineChunksAsync(baseText, text);
      if (doc.changeGeneration() !== gen) return;   // edited meanwhile: the 'change' handler goes again
      if (!chunks) chunks = lineChunks(baseText, text);
    }
    g.gen = gen;
    g.base = baseText;
    const cm = Tabs.cm();
    const run = fn => (cm && cm.getDoc() === doc ? cm.operation(fn) : fn());
    run(() => {
      for (const [h, cls] of g.marks) doc.removeLineClass(h, 'gutter', cls);
      g.marks = [];
      g.chunks = chunks;
      const last = doc.lastLine();
      const mark = (line, cls) => g.marks.push([doc.addLineClass(line, 'gutter', cls), cls]);
      for (const c of g.chunks) {
        if (c.editTo > c.editFrom) {
          const cls = c.origTo > c.origFrom ? 'scm-mod' : 'scm-add';
          for (let l = c.editFrom; l < c.editTo && l <= last; l++) mark(l, cls);
        } else if (c.editFrom > last) mark(last, 'scm-del-below');
        else mark(c.editFrom, 'scm-del');
      }
    });
  }

  function gutterClick(line) {
    const m = Tabs.activeModel();
    const g = m && gut.get(m);
    if (!g) return;
    const last = m.doc.lastLine();
    const hit = g.chunks.find(c => (c.editTo > c.editFrom ? line >= c.editFrom && line < c.editTo : Math.min(c.editFrom, last) === line));
    if (hit) openDiff(m.path, { preview: true, focus: true, line, mode: 'head' });
  }

  Tabs.on('activate', t => {
    updateRows();
    if (t && t.type === 'file') scheduleGutter(0);
  });
  Tabs.on('change', m => {
    if (m === Tabs.activeModel() && Tabs.active().type === 'file') scheduleGutter(300);
    modelChanged(m);
  });
  Tabs.on('disk-change', m => {
    if (m === Tabs.activeModel() && Tabs.active().type === 'file') scheduleGutter(0);
    modelChanged(m);
    refreshSoon(300);
  });
  Tabs.on('saved', () => refreshSoon());
  Tabs.on('rename', () => { headCache = new Map(); refreshSoon(); scheduleGutter(0); });

  // ---------------------------------------------------------------- diff tabs

  const diffId = d => `scm:${d.mode}:${d.path}`;
  const diffTitle = d => `${UI.basename(d.path)} (${d.mode === 'branch' ? `vs ${d.label}` : 'Working Tree'})`;
  const diffTooltip = d => `${d.old ? `${d.old} → ` : ''}${d.path}: the working tree against ${d.mode === 'branch' ? `${d.label} (where this branch left it)` : 'HEAD'}`;
  const defaultView = () => UI.store.get('mdedit.diff.view', innerWidth < 800 ? 'inline' : 'split') === 'inline' ? 'inline' : 'split';

  async function prepare(d) {
    let model = null, kind = 'text';
    if (d.status === 'D') kind = 'deleted';
    else {
      model = await Tabs.acquire(d.path);
      if (!model) {
        try {
          const f = await UI.api('GET', `/api/file?path=${UI.encPath(d.path)}`);
          kind = f.kind === 'too_large' ? 'too_large' : f.kind === 'text' ? 'deleted' : 'binary';
        } catch { kind = 'deleted'; }
      }
    }
    const s = { d, kind, view: defaultView(), base: null, sha: undefined };
    return {
      id: diffId(d), kind: 'diff', state: d, path: d.path, model,
      title: diffTitle(d), tooltip: diffTooltip(d), scm: s,
      render: renderDiff, onShow: showDiff, onHide: hideDiff, dispose: disposeDiff, onRename: renameDiff,
    };
  }

  Tabs.registerType('diff', state => (state && state.path && state.mode ? prepare(Object.assign({}, state)) : null));

  const findDiff = id => Tabs.list().find(t => t.type === 'custom' && t.spec.id === id);

  async function openDiff(path, { preview = true, focus = true, line = null, mode: want } = {}) {
    if (!path) return null;
    const f = byPath.get(path);
    const m = want || (st && st.base ? st.base.mode : mode);
    const d = {
      path, mode: m, status: f ? f.status : 'M', old: (f && f.old_path) || null,
      label: m === 'branch' ? (st && st.base && st.base.mode === 'branch' ? st.base.label : (info && info.default_branch) || 'the default branch') : 'HEAD',
    };
    let t = findDiff(diffId(d));
    // made again for a file that is gone, or back again (restored, so no longer in the list)
    if (t && (f ? (t.spec.scm.kind === 'deleted') !== (f.status === 'D') : t.spec.scm.kind === 'deleted')) {
      if (!(await Tabs.close(t))) return t;
      t = null;
    }
    if (t) Tabs.openCustom({ id: t.spec.id, preview, focus });
    else {
      const spec = await prepare(d);
      t = Tabs.openCustom(Object.assign(spec, { preview, focus }));
    }
    if (!t || t.closed) return null;
    const s = t.spec.scm;
    if (line != null) { s.line = line; if (s.built) revealLine(s); }
    if (focus && Tabs.active() === t) { if (s.built) focusDiff(s); else s.wantFocus = true; }
    updateRows();
    return t;
  }

  function renderDiff(view, tab) {
    const s = tab.spec.scm;
    s.tab = tab;
    diffs.add(s);
    view.classList.add('diff-tab');
    view.innerHTML = `
      <div class="diff-head">
        <span class="diff-title"></span>
        <span class="diff-base muted"></span>
        <span class="diff-stats"></span>
        <span class="spacer"></span>
        <span class="diff-tools">
          <button type="button" class="tool-btn" data-act="prev" title="Previous change (Shift+Alt+F5)">${ICON.up}</button>
          <button type="button" class="tool-btn" data-act="next" title="Next change (Alt+F5)">${ICON.down}</button>
          <span class="seg small"><button type="button" data-view="split" title="Show the base and the working file side by side">Side by side</button><button type="button" data-view="inline" title="Show the changes inline, in one column">Inline</button></span>
          <button type="button" class="tool-btn" data-act="open" title="Open the file">${ICON.open}</button>
          <button type="button" class="tool-btn" data-act="prev-file" title="Previous changed file">${ICON.prevFile}</button>
          <button type="button" class="tool-btn" data-act="next-file" title="Next changed file">${ICON.nextFile}</button>
        </span>
      </div>
      <div class="diff-body"><div class="diff-msg muted">Loading…</div></div>`;
    s.el = { head: view.querySelector('.diff-head'), body: view.querySelector('.diff-body'), stats: view.querySelector('.diff-stats') };
    s.el.head.addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      if (b.dataset.view) { setView(s, b.dataset.view); return; }
      switch (b.dataset.act) {
        case 'prev': navDiff(s, -1); break;
        case 'next': navDiff(s, 1); break;
        case 'open': openFileAt(s); break;
        case 'prev-file': nextFile(-1); break;
        case 'next-file': nextFile(1); break;
      }
    });
    if (s.kind === 'text' && tab.model) s.linked = Tabs.linkedDoc(tab.model);
    syncHead(s);
    loadBase(s);
  }

  function syncHead(s) {
    const d = s.d;
    const title = s.el.head.querySelector('.diff-title');
    title.textContent = d.path;
    title.title = diffTooltip(d);
    s.el.head.querySelector('.diff-base').textContent = d.mode === 'branch' ? `against ${d.label}` : 'against HEAD';
    for (const b of s.el.head.querySelectorAll('[data-view]')) b.classList.toggle('on', b.dataset.view === s.view);
    const usable = s.built && !s.msg;
    for (const b of s.el.head.querySelectorAll('[data-view], [data-act="prev"], [data-act="next"]')) b.disabled = !usable;
    s.el.head.querySelector('[data-act="open"]').disabled = s.kind === 'deleted';
  }

  async function loadBase(s) {
    const d = s.d;
    const q = `path=${UI.encPath(d.path)}&base=${d.mode}${d.old ? `&old_path=${UI.encPath(d.old)}` : ''}`;
    const sha = baseSha(d.mode);
    s.loading = true;
    let r, spec;
    try {
      [r, spec] = await Promise.all([UI.api('GET', `/api/git/show?${q}`), Tabs.loadMode(d.path)]);
    } catch (e) {
      s.loading = false;
      if (!s.tab.closed) message(s, UI.esc(`Could not load the base version: ${e.message}`));
      return;
    } finally { s.loading = false; }
    if (s.tab.closed) return;
    s.sha = sha;
    s.modeSpec = spec;
    const base = r.binary ? { binary: true } : r.too_large ? { tooLarge: true } : { text: r.exists ? r.text : '', exists: !!r.exists };
    const old = s.base;
    s.base = base;
    if (!old || !s.built || s.msg || old.text == null || base.text == null) { reset(s); build(s); return; }
    // the base moved (a commit, a checkout): show the new one
    if (s.mv) s.mv.leftOriginal().setValue(base.text);
    s.inlineStale = true;
    if (s.view === 'inline' && Tabs.active() === s.tab) buildInline(s);
    updateStats(s);
  }

  // drop the views (keeping the working side's linked doc for the next ones)
  function reset(s) {
    if (s.mv) {
      try { s.mv.editor().swapDoc(new CodeMirror.Doc('')); } catch {}
      s.mv = null;
    }
    s.parked = false;
    s.inl = null;
    s.built = false;
    s.msg = false;
  }

  function message(s, html) {
    reset(s);
    s.built = true;
    s.msg = true;
    s.el.body.innerHTML = `<div class="diff-msg">${html}</div>`;
    s.el.stats.textContent = '';
    syncHead(s);
  }

  // CodeMirror measures what it shows, so the views are made while the tab is on screen
  function build(s) {
    if (s.built || !s.base || s.tab.closed || Tabs.active() !== s.tab) return;
    const name = `<strong>${UI.esc(UI.basename(s.d.path))}</strong>`;
    if (s.base.binary || s.kind === 'binary') return message(s, `<p>${name}</p><p class="muted">Binary file changed.</p>`);
    if (s.base.tooLarge || s.kind === 'too_large') return message(s, `<p>${name}</p><p class="muted">This file is too large to compare (over 5 MB).</p>`);
    s.built = true;
    s.msg = false;
    s.el.split = Object.assign(document.createElement('div'), { className: 'diff-split' });
    s.el.inline = Object.assign(document.createElement('div'), { className: 'diff-inline' });
    s.el.body.replaceChildren(s.el.split, s.el.inline);
    showView(s);
    if (s.line != null) revealLine(s);
    else firstChange(s);
    if (s.wantFocus) { s.wantFocus = false; focusDiff(s); }
  }

  function showView(s) {
    syncHead(s);
    if (!s.built || s.msg) return;
    const split = s.view === 'split';
    s.el.split.hidden = !split;
    s.el.inline.hidden = split;
    if (split) {
      if (!s.mv) buildMerge(s);
      else {
        if (s.parked) { s.parked = false; s.mv.editor().swapDoc(s.linked); }   // the addon diffs it again
        refreshMerge(s);
      }
    } else if (!s.inl || s.inlineStale) buildInline(s);
    else s.inl.cm.refresh();
    updateStats(s);
  }

  function setView(s, v) {
    if (s.view === v) return;
    // keep the place: the working file's line at the cursor
    const line = cursorLine(s);
    s.view = v;
    UI.store.set('mdedit.diff.view', v);
    showView(s);
    if (line != null) { s.line = line; revealLine(s); }
  }

  const md = s => UI.isMarkdown(s.d.path);
  function editorOptions(s) {
    const ind = s.tab.model && s.tab.model.indent;
    return {
      mode: s.modeSpec, lineNumbers: true, lineWrapping: md(s),
      indentUnit: md(s) ? 2 : ind ? ind.unit : 4,
      tabSize: md(s) ? 4 : ind && !ind.tabs ? ind.unit : 4,
      indentWithTabs: !md(s) && !!(ind && ind.tabs),
    };
  }

  function keys(s) {
    const send = c => sendSel(s, c);
    return {
      'Ctrl-Alt-L': send, 'Cmd-Alt-L': send,
      Tab: c => c.somethingSelected() ? c.indentSelection('add') : c.execCommand(c.getOption('indentWithTabs') ? 'insertTab' : 'insertSoftTab'),
      'Shift-Tab': c => c.indentSelection('subtract'),
    };
  }

  function buildMerge(s) {
    s.mv = CodeMirror.MergeView(s.el.split, Object.assign(editorOptions(s), {
      value: s.linked || '', origLeft: s.base.text, readOnly: !s.linked,
      connect: 'align', revertButtons: !!s.linked, extraKeys: keys(s),
    }));
    const ed = s.mv.editor(), orig = s.mv.leftOriginal();
    for (const c of [ed, orig]) {
      c.getWrapperElement().classList.toggle('cm-code', !md(s));
      c.on('contextmenu', (cc, e) => diffMenu(s, cc, e));
    }
  }

  function refreshMerge(s) {
    s.mv.editor().refresh();
    s.mv.leftOriginal().refresh();
    const dv = s.mv.left;   // realign after being hidden (the addon's own update, forced)
    if (dv && typeof dv.forceUpdate === 'function') { dv.dealigned = true; dv.forceUpdate('full'); }
  }

  // ---- the inline view: one read-only column with the removed lines between the others

  function inlineModel(s) {
    const base = s.base.text;
    const a = base.split('\n');
    if (!s.linked) {   // deleted: every line went
      const n = deletedChunks(base).reduce((k, c) => k + c.origTo, 0);
      return { rows: a.slice(0, n).map((text, i) => ({ t: 'del', text, o: i })), hunks: [{ row: 0, chunk: null }] };
    }
    const work = s.linked.getValue();
    const b = work.split('\n');
    const rows = [], hunks = [];
    let o = 0, e = 0;
    for (const c of diffChunks(s)) {
      while (e < c.editFrom) rows.push({ t: 'ctx', text: b[e], o: o++, e: e++ });
      hunks.push({ row: rows.length, chunk: c });
      for (let i = c.origFrom; i < c.origTo; i++) rows.push({ t: 'del', text: a[i], o: i });
      for (let i = c.editFrom; i < c.editTo; i++) rows.push({ t: 'add', text: b[i], e: i });
      o = c.origTo;
      e = c.editTo;
    }
    while (e < b.length) rows.push({ t: 'ctx', text: b[e], o: o++, e: e++ });
    return { rows, hunks };
  }

  function buildInline(s) {
    s.inlineStale = false;
    const { rows, hunks } = inlineModel(s);
    let width = 1;
    for (const r of rows) if (r.e != null) width = Math.max(width, String(r.e + 1).length);
    s.inlBlank = String.fromCharCode(160).repeat(width);   // removed rows: no number, same width
    s.inlRows = rows;
    let cm = s.inl && s.inl.cm;
    const text = rows.map(r => r.text).join('\n');
    let keep = null;
    if (!cm) {
      cm = CodeMirror(s.el.inline, Object.assign(editorOptions(s), {
        value: text, readOnly: true, extraKeys: keys(s),
        gutters: ['diff-revert', 'CodeMirror-linenumbers', 'diff-sign'],
        lineNumberFormatter: n => { const r = s.inlRows[n - 1]; return r && r.e != null ? String(r.e + 1) : s.inlBlank; },
      }));
      cm.getWrapperElement().classList.add('diff-inline-cm');
      cm.getWrapperElement().classList.toggle('cm-code', !md(s));
      cm.on('gutterClick', (c, line, gutter) => {
        if (gutter !== 'diff-revert' || !s.linked) return;
        // the rows are of the file as it was when they were drawn: after an edit (the first
        // click of a double-click, say) they are drawn again first, and this click is dropped
        if (s.inl.gen !== s.linked.changeGeneration()) { buildInline(s); return; }
        const h = s.inl.hunks.find(x => x.row === line);
        if (h && h.chunk) revertChunk(s.linked, s.base.text, h.chunk);
      });
      cm.on('contextmenu', (c, e) => diffMenu(s, c, e));
    } else {
      keep = { top: cm.getScrollInfo().top, cursor: cm.getCursor() };
      cm.setValue(text);
    }
    s.inl = { cm, hunks, gen: s.linked ? s.linked.changeGeneration() : null };
    cm.operation(() => {
      rows.forEach((r, i) => {
        if (r.t === 'ctx') return;
        cm.addLineClass(i, 'background', r.t === 'add' ? 'diff-add-line' : 'diff-del-line');
        cm.setGutterMarker(i, 'diff-sign', Object.assign(document.createElement('span'), { className: `diff-sign-${r.t}`, textContent: r.t === 'add' ? '+' : '−' }));
      });
      if (s.linked) {
        for (const h of hunks) {
          const b = Object.assign(document.createElement('span'), { className: 'diff-revert-btn', textContent: '↶', title: 'Revert this change' });
          b.setAttribute('role', 'button');
          b.setAttribute('aria-label', 'Revert this change');
          cm.setGutterMarker(h.row, 'diff-revert', b);
        }
      }
    });
    if (keep) { cm.setCursor(keep.cursor); cm.scrollTo(null, keep.top); }
    updateStats(s);
  }

  // edits of a diff's file (here, in its tab or from disk): refresh the inline view and the counts
  function modelChanged(m) {
    for (const s of diffs) {
      if (s.tab.model !== m || !s.built || s.msg) continue;
      s.inlineStale = true;
      if (Tabs.active() !== s.tab) continue;   // a hidden tab catches up when it is shown
      clearTimeout(s.inlineT);
      s.inlineT = setTimeout(() => {
        if (s.tab.closed || Tabs.active() !== s.tab) return;
        if (s.view === 'inline') buildInline(s);
        else updateStats(s);
      }, 300);
    }
  }

  function updateStats(s) {
    if (!s.built || s.msg || !s.base || s.base.text == null) { s.el.stats.textContent = ''; return; }
    const chunks = diffChunks(s);
    let add = 0, del = 0;
    for (const c of chunks) { add += c.editTo - c.editFrom; del += c.origTo - c.origFrom; }
    s.el.stats.innerHTML = chunks.length
      ? `<span class="add">+${add.toLocaleString()}</span> <span class="del">−${del.toLocaleString()}</span>`
      : '<span class="muted">No changes</span>';
    s.el.stats.title = `${plural(chunks.length, 'change')}: ${plural(add, 'line')} added, ${plural(del, 'line')} removed`;
  }

  // ---- navigation within a diff

  // the editor on screen, and where the changes start in it
  function navTarget(s) {
    if (!s.built || s.msg) return null;
    if (s.view === 'split' && s.mv) {
      const useOrig = !s.linked;
      return { cm: useOrig ? s.mv.leftOriginal() : s.mv.editor(), starts: (s.mv.leftChunks() || []).map(c => (useOrig ? c.origFrom : c.editFrom)) };
    }
    if (s.inl) return { cm: s.inl.cm, starts: s.inl.hunks.map(h => h.row) };
    return null;
  }

  function jump(cm, line, focus = true) {
    line = Math.max(0, Math.min(line, cm.lastLine()));
    cm.setCursor({ line, ch: 0 });
    const info = cm.getScrollInfo();
    cm.scrollIntoView({ line, ch: 0 }, Math.round(info.clientHeight / 3));
    if (focus) cm.focus();
  }

  function step(cm, starts, dir) {
    if (!starts.length) { UI.toast('No changes'); return; }
    const line = cm.getCursor().line;
    let target = dir > 0 ? starts.find(l => l > line) : starts.slice().reverse().find(l => l < line);
    if (target == null) target = dir > 0 ? starts[0] : starts[starts.length - 1];   // wrap around
    jump(cm, target);
  }

  function navDiff(s, dir) {
    const n = navTarget(s);
    if (n) step(n.cm, n.starts, dir);
  }

  function firstChange(s) {
    const n = navTarget(s);
    if (n && n.starts.length) jump(n.cm, n.starts[0], false);
  }

  // working-file line ↔ the view's line
  function cursorLine(s) {
    if (!s.built || s.msg) return null;
    if (s.view === 'split' && s.mv) return s.linked ? s.mv.editor().getCursor().line : null;
    if (s.inl) {
      const rows = s.inlRows;
      for (let i = s.inl.cm.getCursor().line; i < rows.length; i++) if (rows[i].e != null) return rows[i].e;
    }
    return null;
  }

  function revealLine(s) {
    const line = s.line;
    s.line = null;
    if (line == null || !s.built || s.msg) return;
    if (s.view === 'split' && s.mv) jump(s.linked ? s.mv.editor() : s.mv.leftOriginal(), line, false);
    else if (s.inl) {
      const i = s.inlRows.findIndex(r => r.e != null && r.e >= line);
      jump(s.inl.cm, i < 0 ? s.inlRows.length - 1 : i, false);
    }
  }

  function focusDiff(s) {
    if (!s.built || s.msg) return;
    const cm = s.view === 'split' ? s.mv && (s.linked ? s.mv.editor() : s.mv.leftOriginal()) : s.inl && s.inl.cm;
    if (cm) cm.focus();
  }

  function openFileAt(s) {
    if (s.kind === 'deleted') return;
    const line = cursorLine(s);
    Tabs.open(s.d.path, line != null ? { preview: false, focus: true, line } : { preview: false, focus: true });
  }

  function sendSel(s, cm) {
    let range = null;
    if (cm.somethingSelected()) {
      const from = cm.getCursor('from'), to = cm.getCursor('to');
      let a = from.line, b = to.line;
      if (to.ch === 0 && b > a) b--;   // whole lines end at the next line's start
      if (s.mv && cm === s.mv.editor() && s.linked) range = { from: a + 1, to: b + 1 };
      else if (s.inl && cm === s.inl.cm) {   // the working file's lines in the selection
        const ls = s.inlRows.slice(a, b + 1).filter(r => r.e != null).map(r => r.e + 1);
        if (ls.length) range = { from: ls[0], to: ls[ls.length - 1] };
      }
    }
    UI.sendToTerminal(s.d.path, range);
  }

  function chunkAt(s, cm) {
    const line = cm.getCursor().line;
    if (s.mv && cm === s.mv.editor() && s.linked) {
      return (s.mv.leftChunks() || []).find(c => (c.editTo > c.editFrom ? line >= c.editFrom && line < c.editTo : line === c.editFrom));
    }
    if (s.inl && cm === s.inl.cm && s.linked) {
      if (s.inl.gen !== s.linked.changeGeneration()) return null;   // rows not redrawn yet
      const h = s.inl.hunks.find((x, i) => line >= x.row && line < (s.inl.hunks[i + 1] ? s.inl.hunks[i + 1].row : Infinity)
        && s.inlRows.slice(x.row, line + 1).every(r => r.t !== 'ctx'));
      return h ? h.chunk : null;
    }
    return null;
  }

  function diffMenu(s, cm, e) {
    if (e.shiftKey) return;   // Shift+right-click: the browser's own menu
    e.preventDefault();
    if (!cm.somethingSelected()) cm.setCursor(cm.coordsChar({ left: e.clientX, top: e.clientY }, 'window'));
    const chunk = chunkAt(s, cm);
    const gen = s.linked && s.linked.changeGeneration();
    const sel = cm.getSelection();
    UI.menu([
      chunk ? {
        label: 'Revert this change',
        action: () => {
          if (s.linked.changeGeneration() !== gen) { UI.toast('The file changed while the menu was open: try again.'); return; }
          revertChunk(s.linked, s.base.text, chunk);
        },
      } : null,
      { label: 'Open file at this line', disabled: s.kind === 'deleted', action: () => openFileAt(s) },
      '-',
      sel ? { label: 'Copy', action: () => copy(sel) } : null,
      { label: sel ? 'Send selection to Claude terminal' : 'Send to Claude terminal', kbd: 'Ctrl+Alt+L', action: () => sendSel(s, cm) },
    ], e.clientX, e.clientY);
  }

  function showDiff(tab) {
    const s = tab.spec.scm;
    if (!s.el) return;
    if (!s.built) build(s);
    else showView(s);
    updateRows();
    if (s.kind === 'deleted') reviveIfBack(s);
  }

  // a deleted file's diff whose file is back (git checkout -- path in the terminal): made again
  async function reviveIfBack(s) {
    if (s.reviving) return;
    s.reviving = true;
    let back = false;
    try { back = (await UI.api('POST', '/api/stat', { paths: [s.d.path] })).versions[s.d.path] !== '0'; } catch {}
    s.reviving = false;
    if (back && !s.tab.closed && Tabs.active() === s.tab && !(byPath.get(s.d.path)?.status === 'D')) {
      openDiff(s.d.path, { preview: s.tab.preview, focus: true, mode: s.d.mode });
    }
  }

  // A hidden merge view would still follow every edit of its file (a character diff 250 ms
  // after each): its editor gets an empty doc until the tab is shown again.
  function hideDiff(tab) {
    const s = tab.spec.scm;
    if (s.mv && s.linked && !s.parked) {
      s.parked = true;
      s.mv.editor().swapDoc(new CodeMirror.Doc(''));
    }
    updateRows();
  }

  function disposeDiff(tab) {
    const s = tab.spec.scm;
    diffs.delete(s);
    clearTimeout(s.inlineT);
    if (s.linked && tab.model) { try { tab.model.doc.unlinkDoc(s.linked); } catch (e) { console.error(e); } }
    s.linked = null;
    s.mv = null;
    s.inl = null;
    setTimeout(updateRows, 0);
  }

  function renameDiff(tab, np) {
    const s = tab.spec.scm;
    const old = Object.assign({}, s.d);
    s.d.path = np;
    tab.spec.id = diffId(s.d);
    Tabs.update(tab, { title: diffTitle(s.d), tooltip: diffTooltip(s.d) });
    if (s.el) syncHead(s);
    if (old.path !== np && !s.d.old) s.d.old = old.path;   // the base still has the old name
  }

  Activity.onLayout(() => {
    const t = Tabs.active();
    if (t && t.type === 'custom' && t.spec.kind === 'diff') showView(t.spec.scm);
  });

  // ---------------------------------------------------------------- next / previous

  function nextFile(dir = 1) {
    if (!files.length) { UI.toast('No changed files'); return; }
    const t = Tabs.active();
    let i = t && t.path ? files.findIndex(f => f.path === t.path) : -1;
    i = i < 0 ? (dir > 0 ? 0 : files.length - 1) : (i + dir + files.length) % files.length;
    focusPath = files[i].path;
    updateRows();
    list.querySelector('.scm-row.focus')?.scrollIntoView({ block: 'nearest' });
    openDiff(files[i].path, { preview: true, focus: true });
  }

  function nextChange(dir = 1) {
    const t = Tabs.active();
    if (!t) return;
    if (t.type === 'custom' && t.spec.kind === 'diff') { navDiff(t.spec.scm, dir); return; }
    if (t.type !== 'file') return;
    const cm = Tabs.cm(), g = gut.get(t.model);
    if (!cm || !g) return;
    const last = cm.lastLine();
    step(cm, g.chunks.map(c => Math.min(c.editFrom, last)), dir);
  }

  document.addEventListener('keydown', e => {
    if (e.key !== 'F5' || !e.altKey || e.ctrlKey || e.metaKey) return;
    if (e.target instanceof Element && e.target.closest('#term, .modal-back')) return;
    e.preventDefault();
    nextChange(e.shiftKey ? -1 : 1);
  });

  // ---------------------------------------------------------------- discard

  async function discard(f) {
    const name = UI.basename(f.path);
    const dirty = [f.path, f.old_path].filter(Boolean).some(p => Tabs.model(p)?.dirty);
    let msg, ok = 'Discard';
    switch (f.status) {
      case 'U': msg = `Delete the untracked file “${name}”? This cannot be undone.`; ok = 'Delete'; break;
      case 'A': msg = `Discard the new file “${name}”? It is taken out of git's index and deleted. This cannot be undone.`; break;
      case 'D': msg = `Restore “${name}” as it is in HEAD?`; ok = 'Restore'; break;
      case 'R': msg = `Undo the rename of “${f.old_path}” to “${f.path}”? “${f.old_path}” comes back as it is in HEAD and “${name}” is deleted. This cannot be undone.`; break;
      default: msg = `Discard the changes to “${name}”? It goes back to how it is in HEAD, and the changes cannot be recovered.`;
    }
    if (dirty) msg += ' Its unsaved changes in the editor will be lost too.';
    if (!(await UI.confirm(msg, { title: f.status === 'U' ? 'Delete file' : 'Discard changes', ok, danger: true }))) return;
    // stop a pending autosave (and let any write under way finish) before git rewrites the file,
    // so the discarded edits are not written back on top of the restored HEAD version
    for (const p of [f.path, f.old_path].filter(Boolean)) {
      const m = Tabs.model(p);
      if (m) await Tabs.cancelAutosave(m);
    }
    try {
      await UI.api('POST', '/api/git/discard', f.old_path ? { path: f.path, old_path: f.old_path } : { path: f.path });
    } catch (e) {
      UI.toast(`Could not discard the changes to ${f.path}: ${e.message}`, { kind: 'err' });
      return;
    }
    const gone = new Set(f.status === 'U' || f.status === 'A' || f.status === 'R' ? [f.path] : []);
    for (const p of [f.path, f.old_path].filter(Boolean)) {
      for (const t of Tabs.list()) {   // diffs of files that are gone or are back
        if (t.type === 'custom' && t.spec.kind === 'diff' && t.path === p && (gone.has(p) || t.spec.scm.kind !== 'text')) await Tabs.close(t, { force: true });
      }
      const m = Tabs.model(p);
      if (gone.has(p)) {
        for (const t of Tabs.list()) if (t.type !== 'custom' && t.path === p) await Tabs.close(t, { force: true });
      } else if (m) await Tabs.reload(m);
    }
    headCache.delete(f.path);
    if (f.old_path) headCache.delete(`${f.path}\0${f.old_path}`);
    UI.toast(f.status === 'U' ? `Deleted ${f.path}` : `Discarded the changes to ${f.path}`);
    window.Explorer?.refresh();
    refresh();
  }

  // ---------------------------------------------------------------- the open folder

  UI.on('root', () => {
    epoch++;
    info = null;
    st = null;
    files = [];
    byPath = new Map();
    error = '';
    listSig = null;
    focusPath = null;
    headCache = new Map();
    renderPanel();
    renderBranch();
    handle.setBadge(null);
    decorations();
    refresh().then(() => scheduleGutter(0));
    schedule();
  });
  renderPanel();

  return {
    refresh, openDiff, nextFile, nextChange,
    files: () => files.slice(),
    status: p => byPath.get(p),
    info: () => info,
  };
})();
window.SCM = SCM;
