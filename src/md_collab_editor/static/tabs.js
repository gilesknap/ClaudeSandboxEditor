// Editor tabs (VS Code style) and the open-file models behind them. Plain script after ui.js;
// exposes `Tabs`. app.js owns the single CodeMirror editor and hands it over with Tabs.init.
//
// A *model* is one open text file: {path, doc (a CodeMirror.Doc), version, dirty, deleted,
// saving (a Promise while a write is under way), category ('md' | 'other'), modeInfo
// ({name, mode, mime} or null)}; treat it as read-only outside tabs.js. Every tab or
// other user of a file shares its model, so dirty state, saving and on-disk changes are
// handled once per file. The main editor shows the active file tab's doc via cm.swapDoc, so
// undo history, selection, scroll and marks stay with each file. A second
// editor on the same file (the diff view) must use Tabs.linkedDoc(model), never model.doc.
//
// Tab kinds: 'file' (text in the main editor), 'image' (viewer on /raw/), 'binary' (binary
// or > 5 MB: a "not viewable" placeholder) and 'custom' (anything else, e.g. a diff, which
// draws itself into its own element in the editor area). One tab at a time can be the
// *preview* tab (italic): the next preview open replaces it; editing, double-clicking it or
// opening with {preview: false} pins it.
//
//   Tabs.init(cm)                      app.js: hand over the editor (before restore)
//   Tabs.open(path, {preview = true, focus = true, line, ch, sel: {from, to}})
//                                      → Promise<tab|null>. Opens or activates a file tab;
//                                      line/ch are 0-based; sel takes CodeMirror positions.
//   Tabs.openCustom({id, title, tooltip, path, kind, state, preview = true, focus = true,
//                    activate = true, model, render(el, tab), onShow(tab), onHide(tab),
//                    dispose(tab), save(tab), onRename(tab, newPath), waiting(tab)}) → tab
//       A tab with the same id is activated instead (and the passed model reference released).
//       render runs once, the first time the tab is shown, with an element filling the editor
//       area. model: a reference from Tabs.acquire(path) the tab takes over (released when the
//       tab closes); the tab then shows that file's ● and Ctrl+S / close prompts use it.
//       kind + state: with Tabs.registerType(kind, restore) the tab is remembered across
//       reloads; restore(state) → spec (or a Promise of one, or null to drop it).
//       waiting(tab) → true: the tab waits for the user's answer (Claude's proposed change),
//       so Close saved leaves it, and closeAll (opening another folder) keeps it.
//   Tabs.update(tab, {title, tooltip}) relabel a tab
//   Tabs.close(tab, {force}) → Promise<bool>   prompts Save / Don't save / Cancel when needed
//   Tabs.pin(tab)                      make a preview tab permanent
//   Tabs.activate(tab, {focus})
//   Tabs.active() → tab | null         {id, type, path, model, preview, title, spec}
//   Tabs.activeModel() → model | null  the active tab's model (file tabs and custom tabs with one)
//   Tabs.list() → [tab]                open tabs in strip order
//   Tabs.model(path) → model | undefined;  Tabs.models() → [model]  (every open file)
//   Tabs.acquire(path) → Promise<model|null>   load (or share) a file's model without a tab
//   Tabs.release(model)                drop a reference from acquire
//   Tabs.linkedDoc(model) → Doc        a doc linked to the model's (shared history), for a
//                                      second editor; call model.doc.unlinkDoc(it) when done
//   Tabs.loadMode(path) → Promise<modeSpec>  make sure the file's CodeMirror mode is loaded
//   Tabs.save(model?, force?, auto?) → Promise<bool>   (default: the active model / custom tab);
//                                      auto: an autosave, which skips a file deleted on disk
//   Tabs.saveAll(); Tabs.anyDirty() → bool
//   Tabs.reveal(model, {line, ch, sel, focus})   select / scroll, if the model's tab is active
//   Tabs.renamePath(from, to)          follow a rename on disk (files and folders)
//   Tabs.renameBlocked(from, to) → path | null  an open file with unsaved changes that the
//                                      rename would put another file in place of
//   Tabs.pathDeleted(path)             a file / folder was deleted by us: close clean tabs
//   Tabs.reload(model) → Promise<bool> load the file from disk again, dropping unsaved changes
//                                      (scm.js, after a discard)
//   Tabs.confirmLeave(message) → Promise<bool>  Save all / Don't save / Cancel for every dirty file
//   Tabs.closeAll()                    close everything without asking (after confirmLeave),
//                                      except the tabs that are `waiting`
//   Tabs.restore() → Promise           reopen the tabs remembered for the current root
//   Tabs.setStatus(text, cls)          message in the top bar's save-state slot
//   Tabs.autosave(category) → bool     the autosave setting for 'md' / 'other'
//   Tabs.hold(path, {msg, actions}) → release({save}?)   while Claude's proposed change to a
//                                      file waits for an answer (proposal.js): its tab is
//                                      read-only under a banner (msg, [[label, fn]…]; msg may be a
//                                      function of the model) and it is not saved, so nothing
//                                      races Claude's own write. Also for a file opened later;
//                                      holds stack. release() autosaves the unsaved changes held
//                                      back; release({save: false}) leaves them unsaved (after an
//                                      Accept: Claude writes the file). Tabs.held(model or path) → bool
//   Tabs.on(event, fn) → off()         events:
//       'activate' (tab, prevTab)   'deactivate' (tab)   'open' (tab)   'close' (tab)
//       'change' (model, change)    any edit of a model's doc (also from linked docs)
//       'dirty' (model)             its dirty flag flipped
//       'saved' (model)             written to disk by us
//       'disk-change' (model, {deleted, conflict}) reloaded / found changed (conflict: over
//                                   unsaved changes, so not loaded) or deleted on disk
//       'dispose' (model)           closed: nothing uses it any more
//       'hold' (path)               a hold on the file began or ended (Tabs.held says which)
//       'rename' (from, to)         after renamePath

const Tabs = (() => {
  const CM_BASE = 'https://cdnjs.cloudflare.com/ajax/libs/codemirror/5.65.16/';
  CodeMirror.modeURL = CM_BASE + 'mode/%N/%N.min.js';
  const MD_MODE = { name: 'gfm', highlightFormatting: true, fencedCodeBlockHighlighting: true };
  const SAVE_DELAY = 800;
  const RAW_LINK_MAX = 100 * 1024 * 1024;   // above this, do not offer a raw-file link (a GB download)

  const $ = s => document.querySelector(s);
  const main = $('#main');
  const strip = $('#tabstrip');
  const views = $('#custom-view');

  let cm = null;
  const blank = new CodeMirror.Doc('');
  const tabs = [];
  let active = null;
  let mru = [];                    // tabs, most recently active last
  const models = new Map();        // path → model
  const loading = new Map();       // path → Promise<{model}|{info}|{error}>
  const types = new Map();         // custom tab kind → restore(state)
  const holds = new Map();         // path → [{root, banner}] (Tabs.hold), latest last
  const handlers = new Map();
  let seq = 0;
  let restoring = false;

  function on(name, fn) {
    if (!handlers.has(name)) handlers.set(name, new Set());
    handlers.get(name).add(fn);
    return () => handlers.get(name).delete(fn);
  }
  function emit(name, ...args) {
    for (const fn of [...(handlers.get(name) || [])]) {
      try { fn(...args); } catch (e) { console.error(`Tabs '${name}' handler failed`, e); }
    }
  }

  const categoryOf = p => UI.isMarkdown(p) ? 'md' : 'other';
  const autosave = cat => UI.store.get(`mdedit.autosave.${cat}`, cat === 'md' ? '1' : '0') === '1';
  const fmtSize = n => n == null ? '' : n < 1024 ? `${n} bytes` : n < 1 << 20 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;

  // ---------------------------------------------------------------- modes

  function modeFor(path) {
    if (categoryOf(path) === 'md') return { spec: MD_MODE, info: { name: 'Markdown', mode: 'gfm', mime: 'text/x-gfm' } };
    const info = CodeMirror.findModeByFileName ? CodeMirror.findModeByFileName(UI.basename(path)) : null;
    if (!info || info.mode === 'null') return { spec: 'text/plain', info: info ? { name: info.name, mode: 'null' } : null };
    return { spec: info.mime || (info.mimes && info.mimes[0]) || info.mode, info: { name: info.name, mode: info.mode, mime: info.mime } };
  }

  const modeWaits = new Map();     // mode name → Promise
  function requireMode(mode) {
    if (!mode || mode === 'null' || CodeMirror.modes[mode] || !CodeMirror.requireMode) return Promise.resolve();
    if (!modeWaits.has(mode)) {
      modeWaits.set(mode, new Promise(res => {
        CodeMirror.requireMode(mode, res);
        setTimeout(res, 15000);   // a script that fails to load never calls back
      }));
    }
    return modeWaits.get(mode);
  }

  function loadMode(path) {
    const { spec, info } = modeFor(path);
    return requireMode(info && info.mode).then(() => spec);
  }

  // re-apply a model's mode once its script has arrived
  function ensureMode(m) {
    const mode = m.modeInfo && m.modeInfo.mode;
    if (!mode || mode === 'null' || mode === 'gfm' || CodeMirror.modes[mode]) return;
    requireMode(mode).then(() => {
      if (cm && cm.getDoc() === m.doc) cm.setOption('mode', m.doc.modeOption);
    });
  }

  function detectIndent(text) {
    let tabsN = 0, spaces = 0, prev = 0;
    const steps = new Map();
    for (const line of text.split('\n', 4000)) {
      if (!line.trim()) continue;
      if (line[0] === '\t') { tabsN++; prev = -1; continue; }
      const w = /^ */.exec(line)[0].length;
      if (w) spaces++;
      if (prev >= 0 && w > prev && [2, 3, 4, 8].includes(w - prev)) steps.set(w - prev, (steps.get(w - prev) || 0) + 1);
      prev = w;
    }
    if (tabsN > spaces) return { tabs: true, unit: 4 };
    let unit = 4, best = 0;
    for (const [d, c] of steps) if (c > best) { best = c; unit = d; }
    return { tabs: false, unit };
  }

  // ---------------------------------------------------------------- models

  function getFile(path) {
    if (models.has(path)) return Promise.resolve({ model: models.get(path) });
    if (loading.has(path)) return loading.get(path);
    const p = UI.api('GET', `/api/file?path=${UI.encPath(path)}`)
      .then(f => {
        if (models.has(path)) return { model: models.get(path) };
        if (!f.kind || f.kind === 'text') return { model: makeModel(path, f) };
        return { info: f };
      }, error => ({ error }))
      .finally(() => loading.delete(path));
    loading.set(path, p);
    return p;
  }

  function makeModel(path, f) {
    let { spec, info } = modeFor(path);
    if ((!info || info.mode === 'null') && f.mode && CodeMirror.findModeByName) {
      // no mode by name: the server's hint (e.g. from a #! line)
      const byName = CodeMirror.findModeByName(f.mode);
      if (byName && byName.mode !== 'null') {
        spec = byName.mime || (byName.mimes && byName.mimes[0]) || byName.mode;
        info = { name: byName.name, mode: byName.mode, mime: byName.mime };
      }
    }
    const doc = new CodeMirror.Doc(f.text, spec);
    const m = {
      path, doc, version: f.version, category: categoryOf(path), modeInfo: info,
      dirty: false, saving: null, deleted: false, refs: 0,
      cleanGen: doc.changeGeneration(true),
      indent: categoryOf(path) === 'md' ? null : detectIndent(f.text),
      state: { text: 'Saved', cls: '' }, banner: null,
      applying: false, saveT: null, seenVersion: null,
    };
    CodeMirror.on(doc, 'change', (_, ch) => onDocChange(m, ch));
    models.set(path, m);
    return m;
  }

  function release(m) {
    if (!m) return;
    m.refs--;
    if (m.refs > 0 || models.get(m.path) !== m) return;
    clearTimeout(m.saveT);
    models.delete(m.path);
    if (cm && cm.getDoc() === m.doc) cm.swapDoc(blank);
    emit('dispose', m);
  }

  async function acquire(path) {
    const r = await getFile(path);
    if (!r.model) return null;
    r.model.refs++;
    return r.model;
  }

  function setDirty(m, dirty) {
    if (m.dirty === dirty) return;
    m.dirty = dirty;
    renderStrip();
    renderHead();
    emit('dirty', m);
  }

  function setState(m, text, cls = '') {
    m.state = { text, cls };
    if (activeModel() === m) setStatus(text, cls);
  }

  function onDocChange(m, change) {
    if (m.applying) return;
    setDirty(m, !m.doc.isClean(m.cleanGen));
    if (m.dirty) {
      if (active && active.model === m && active.preview) pin(active);   // edited here: keep it
      // a file deleted (or moved) on disk is only written again when asked: Ctrl+S, the banner
      const auto = autosave(m.category) && !m.deleted;
      setState(m, auto ? 'Editing…' : 'Unsaved changes');
      if (auto) scheduleSave(m);
    } else if (!m.saving) {
      if (m.seenVersion && m.seenVersion !== '0') {
        // undone back to the text the disk's change was a conflict with: a clean file simply
        // takes the disk's version, as it would have then
        const v = m.seenVersion;
        m.seenVersion = null;
        checkModel(m, v);
      } else setState(m, 'Saved');
    }
    emit('change', m, change);
  }

  function scheduleSave(m) {
    clearTimeout(m.saveT);
    m.saveT = setTimeout(() => save(m, false, true), SAVE_DELAY);
  }

  // Stop a pending autosave and hand back the write under way (if any) to await: a caller about to
  // change the file on disk (e.g. a git discard) must not have an autosave write the editor's text
  // back over it afterwards.
  function cancelAutosave(m) {
    if (!m) return null;
    clearTimeout(m.saveT);
    m.saveT = null;
    return m.saving;
  }

  // auto: an autosave, which never writes a file that was deleted on disk back
  async function save(m, force = false, auto = false) {
    if (m === undefined) {
      const t = active;
      if (t && t.type === 'custom' && t.spec.save) return t.spec.save(t);
      m = activeModel();
    }
    if (!m || models.get(m.path) !== m) return false;
    clearTimeout(m.saveT);
    if (heldBy(m)) {   // Claude's proposed change is waiting: its write must not be raced
      if (!auto && m.dirty) setState(m, 'Not saved: answer Claude\'s proposed change first', 'err');
      return false;
    }
    if (m.saving) {   // one write at a time: wait for the one under way, then go again
      await m.saving;
      return save(m, force, auto);
    }
    if (auto && m.deleted) return false;
    if (!m.dirty && !force && !m.deleted) return true;
    const gen = m.doc.changeGeneration(true);
    const text = m.doc.getValue();
    let done;
    m.saving = new Promise(r => { done = r; });
    setState(m, 'Saving…');
    let ok = false, retry = false;
    const path = m.path;
    try {
      const r = await UI.api('PUT', '/api/file', { path, text, base_version: m.deleted ? '0' : m.version, force });
      m.version = r.version;
      m.seenVersion = null;
      m.cleanGen = gen;
      m.deleted = false;
      setBanner(m, null);
      setDirty(m, !m.doc.isClean(gen));
      setState(m, m.dirty ? 'Editing…' : 'Saved');
      ok = true;
      renderStrip();
      emit('saved', m);
    } catch (e) {
      if (m.path !== path && models.get(m.path) === m) retry = true;   // renamed meanwhile: not a conflict
      else if (e.status === 409 && String(e.data && e.data.version) === '0') markDeleted(m);   // before the poll saw it
      else if (e.status === 409) {
        setState(m, 'Conflict', 'err');
        conflict(m, e.data.text, e.data.version);
      } else setState(m, `Save failed: ${e.message}`, 'err');
    } finally {
      m.saving = null;
      done();
    }
    return retry ? save(m, force, auto) : ok;
  }

  function saveAll() { return Promise.all([...models.values()].filter(m => m.dirty).map(m => save(m))); }
  const anyDirty = () => [...models.values()].some(m => m.dirty);

  // Replace the doc's text with `text` by editing only the changed middle, so the cursor,
  // undo history and marks elsewhere survive; flash what changed.
  function applyRemote(m, text, version) {
    const doc = m.doc;
    const old = doc.getValue();
    if (old !== text) {
      let a = 0;
      while (a < old.length && a < text.length && old[a] === text[a]) a++;
      let b = 0;
      while (b < old.length - a && b < text.length - a && old[old.length - 1 - b] === text[text.length - 1 - b]) b++;
      const from = doc.posFromIndex(a), to = doc.posFromIndex(old.length - b);
      const ins = text.slice(a, text.length - b);
      m.applying = true;
      try { doc.replaceRange(ins, from, to, 'remote'); } finally { m.applying = false; }
      if (ins) {
        const mk = doc.markText(from, doc.posFromIndex(a + ins.length), { className: 'remote-flash' });
        setTimeout(() => mk.clear(), 2500);
      }
    }
    m.version = version;
    m.seenVersion = null;
    m.deleted = false;
    m.cleanGen = doc.changeGeneration(true);
    setDirty(m, false);
    setBanner(m, null);
    setState(m, 'Updated from disk', 'ok');
    renderStrip();
    emit('disk-change', m, { deleted: false });
  }

  async function reload(m) {
    if (!m || models.get(m.path) !== m) return false;
    clearTimeout(m.saveT);
    let f;
    try { f = await UI.api('GET', `/api/file?path=${UI.encPath(m.path)}`); }
    catch (e) { if (e.status === 404 && models.get(m.path) === m) checkModel(m, '0'); return false; }
    if (models.get(m.path) !== m || (f.kind && f.kind !== 'text')) return false;
    applyRemote(m, f.text, f.version);
    return true;
  }

  // text null: the disk's version is not text any more (a 409 for a binary file), so it can
  // only be overwritten
  function conflict(m, text, version) {
    if (typeof text !== 'string') { notText(m, version, 'binary'); return; }
    m.seenVersion = version;
    setBanner(m, {
      msg: `${UI.basename(m.path)} was changed on disk while you were editing.`,
      actions: [['Load disk version', () => applyRemote(m, text, version)],
                ['Keep mine (overwrite)', () => save(m, true)]],
    });
    emit('disk-change', m, { deleted: false, conflict: true });
  }

  function notText(m, version, what) {
    m.seenVersion = version;
    setBanner(m, {
      msg: `${UI.basename(m.path)} is no longer a text file on disk (${what}).`,
      actions: [['Keep mine (overwrite)', () => save(m, true)]],
    });
    emit('disk-change', m, { deleted: false, conflict: true });
  }

  // ---------------------------------------------------------------- on-disk changes (polled)

  // the file is gone from disk (deleted, or moved by something other than this page): the tab
  // stays, struck through, and nothing writes the file again unless asked to
  function markDeleted(m) {
    clearTimeout(m.saveT);
    m.deleted = true;
    m.seenVersion = '0';
    setBanner(m, {
      msg: `${UI.basename(m.path)} has been deleted on disk.`,
      actions: [['Save to re-create it', () => save(m, true)],
                ['Close', () => closeModel(m)]],
    });
    if (m.dirty) setState(m, 'Deleted on disk', 'err');
    renderStrip();
    emit('disk-change', m, { deleted: true });
  }

  // close every tab of a model; the last one asks about unsaved changes as its × does
  async function closeModel(m) {
    for (const t of tabs.filter(t => t.model === m)) if (!(await close(t))) return false;
    return true;
  }

  async function checkModel(m, v) {
    if (m.saving || m.checking || v === m.version || v === m.seenVersion) return;
    if (v === '0') {
      if (!m.deleted) markDeleted(m);
      return;
    }
    m.checking = true;
    let f;
    try { f = await UI.api('GET', `/api/file?path=${UI.encPath(m.path)}`); }
    catch { f = null; }
    finally { m.checking = false; }
    if (!f || models.get(m.path) !== m || m.saving || f.version === m.version) return;
    if (f.kind && f.kind !== 'text') { notText(m, f.version, f.kind === 'too_large' ? 'over 5 MB' : 'binary'); return; }
    if (m.deleted) { m.deleted = false; renderStrip(); }
    if (f.text === m.doc.getValue()) {
      m.version = f.version;
      m.seenVersion = null;
      if (m.dirty) { m.cleanGen = m.doc.changeGeneration(true); setDirty(m, false); setState(m, 'Saved'); }
      setBanner(m, null);
      emit('disk-change', m, { deleted: false });
      return;
    }
    if (!m.dirty) applyRemote(m, f.text, f.version);
    else { setState(m, 'Changed on disk', 'err'); conflict(m, f.text, f.version); }
  }

  function checkViewer(t, v) {
    if (v === t.version) return;
    t.version = v;
    t.info = Object.assign({}, t.info, { version: v, deleted: v === '0' });
    renderFileView(t);
    renderStrip();
  }

  UI.stat.watch({
    paths: () => [...new Set([...models.keys(), ...tabs.filter(t => t.type === 'image' || t.type === 'binary').map(t => t.path)])],
    onResult(res) {
      for (const [p, v] of Object.entries(res.versions)) {
        const m = models.get(p);
        if (m) checkModel(m, String(v));
        for (const t of tabs) if ((t.type === 'image' || t.type === 'binary') && t.path === p) checkViewer(t, String(v));
      }
    },
  });

  // ---------------------------------------------------------------- banner & top bar

  function setBanner(m, banner) {
    m.banner = banner;
    if (activeModel() === m) renderBanner();
  }

  // ---------------------------------------------------------------- holds (Claude's proposals)

  function heldBy(m) {
    const list = m && holds.get(m.path);
    const h = list && list.filter(x => x.root === UI.root());
    return h && h.length ? h[h.length - 1] : null;
  }

  // the main editor is read-only while the file on screen is held
  function syncReadOnly() {
    if (!cm) return;
    const ro = !!(active && active.type === 'file' && heldBy(active.model));
    if (cm.getOption('readOnly') !== ro) cm.setOption('readOnly', ro);
  }

  // A doc linked to the model's, for a second editor on the file (a diff view). Its edits are
  // the model's, so while the file is held they are refused, as in the main editor.
  let heldToastAt = 0;
  function linkedDoc(m) {
    const d = m.doc.linkedDoc({ sharedHist: true, mode: m.doc.modeOption });
    d.on('beforeChange', (doc, change) => {
      if (!heldBy(m)) return;
      change.cancel();
      if (Date.now() - heldToastAt > 2000) {
        heldToastAt = Date.now();
        UI.toast(`${UI.basename(m.path)} is read-only until you accept or reject Claude's proposed change.`, { kind: 'err' });
      }
    });
    return d;
  }

  function hold(path, banner) {
    const rec = { root: UI.root(), banner };
    if (!holds.has(path)) holds.set(path, []);
    holds.get(path).push(rec);
    const changed = (save = true) => {
      const m = models.get(path);
      if (!m) return;
      if (heldBy(m)) clearTimeout(m.saveT);
      else if (save && m.dirty && autosave(m.category) && !m.deleted) scheduleSave(m);   // what waited
      if (activeModel() === m) { renderBanner(); syncReadOnly(); }
      emit('hold', path);
    };
    changed();
    let done = false;
    return ({ save = true } = {}) => {
      if (done) return;
      done = true;
      const list = (holds.get(path) || []).filter(x => x !== rec);
      if (list.length) holds.set(path, list); else holds.delete(path);
      changed(save);
    };
  }

  function renderBanner() {
    const el = $('#banner');
    const m = activeModel();
    const banner = m && (m.banner || (heldBy(m) || {}).banner);
    if (!banner) { el.hidden = true; return; }
    const msg = typeof banner.msg === 'function' ? banner.msg(m) : banner.msg;
    el.replaceChildren(Object.assign(document.createElement('span'), { textContent: msg }));
    for (const [label, fn] of banner.actions || []) {
      const b = Object.assign(document.createElement('button'), { type: 'button', textContent: label });
      b.onclick = fn;
      el.append(b);
    }
    el.hidden = false;
  }

  function setStatus(text, cls = '') {
    const el = $('#save-state');
    el.textContent = text;
    el.className = cls || 'muted';
  }

  function renderHead() {
    const t = active;
    const m = activeModel();
    const name = $('#doc-name');
    name.textContent = t ? (t.type === 'custom' ? t.title : t.path) : 'No file open';
    name.title = t ? (t.spec?.tooltip || t.path || t.title) : '';
    name.classList.toggle('dirty', !!(m && m.dirty));
    document.title = t ? `${m && m.dirty ? '● ' : ''}${t.type === 'custom' ? t.title : UI.basename(t.path)} — MD Editor` : 'MD Editor';
    const as = UI.statusItem('stat-autosave', { side: 'right', order: 80, onClick: toggleAutosave });
    const mode = UI.statusItem('stat-mode', { side: 'right', order: 70 });
    if (m) {
      const on = autosave(m.category);
      const kind = m.category === 'md' ? 'markdown files' : 'files other than markdown';
      as.hidden = false;
      as.textContent = `Autosave: ${on ? 'on' : 'off'}`;
      as.title = `Autosave for ${kind} is ${on ? 'on' : 'off'}; click to turn it ${on ? 'off' : 'on'} (Ctrl+S always saves)`;
      as.classList.toggle('off', !on);
      mode.hidden = false;
      mode.textContent = (m.modeInfo && m.modeInfo.name) || 'Plain Text';
    } else {
      as.hidden = true;
      mode.hidden = !(t && t.type !== 'custom');
      mode.textContent = t && t.type === 'image' ? 'Image' : t && t.type === 'binary' ? 'Binary' : '';
    }
  }

  function toggleAutosave() {
    const m = activeModel();
    if (!m) return;
    const on = !autosave(m.category);
    UI.store.set(`mdedit.autosave.${m.category}`, on ? '1' : '0');
    if (on) for (const x of models.values()) if (x.category === m.category && x.dirty) scheduleSave(x);
    renderHead();
    UI.toast(`Autosave ${on ? 'on' : 'off'} for ${m.category === 'md' ? 'markdown files' : 'files other than markdown'}`);
  }

  // ---------------------------------------------------------------- the tab strip

  function tabTitle(t) { return t.type === 'custom' ? t.title : UI.basename(t.path); }
  function tabDirty(t) {
    if (t.model) return t.model.dirty;
    return !!(t.type === 'custom' && t.spec.isDirty && t.spec.isDirty(t));
  }
  // it waits for the user's answer (spec.waiting): neither Close saved nor closeAll closes it
  const waits = t => t.type === 'custom' && !!t.spec.waiting?.(t);
  // closing it would drop unsaved changes: it is the last holder of a dirty file
  const losesWork = t => (t.model ? t.model.dirty && t.model.refs <= 1 : tabDirty(t));

  // Tab elements are kept (and their contents only rewritten when they change): a click
  // re-renders the strip, and Chrome only sees a double-click on the same node twice.
  function renderStrip() {
    const names = new Map();
    for (const t of tabs) names.set(tabTitle(t), (names.get(tabTitle(t)) || 0) + 1);
    const old = new Map([...strip.children].map(e => [e.dataset.id, e]));
    const els = tabs.map(t => {
      const el = old.get(t.id) || document.createElement('div');
      el.className = 'etab';
      el.dataset.id = t.id;
      el.setAttribute('role', 'tab');
      el.setAttribute('aria-selected', t === active ? 'true' : 'false');
      el.classList.toggle('active', t === active);
      el.classList.toggle('preview', t.preview);
      el.classList.toggle('dirty', tabDirty(t));
      const deleted = (t.model && t.model.deleted) || (t.info && t.info.deleted);
      el.classList.toggle('deleted', !!deleted);
      el.title = (t.spec?.tooltip || t.path || t.title) + (deleted ? ' (deleted)' : '') + (t.preview ? ' — preview: double-click to keep it open' : '');
      const title = tabTitle(t);
      const dir = t.type !== 'custom' && names.get(title) > 1 ? UI.dirname(t.path) : '';
      const html = `<span class="etab-name">${UI.esc(title)}</span>${dir ? `<span class="etab-desc">${UI.esc(dir)}</span>` : ''}`
        + `<span class="etab-dirty" title="Unsaved changes">●</span><button type="button" class="etab-close" title="Close (Alt+W)" aria-label="Close ${UI.esc(title)}">×</button>`;
      if (el._html !== html) { el.innerHTML = html; el._html = html; }
      return el;
    });
    if (els.length !== strip.children.length || els.some((e, i) => strip.children[i] !== e)) strip.replaceChildren(...els);
    strip.hidden = !tabs.length;
    strip.querySelector('.etab.active')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }

  const tabOf = el => el && tabs.find(t => t.id === el.closest('.etab')?.dataset.id);
  strip.addEventListener('mousedown', e => { if (e.button === 1) e.preventDefault(); });   // no autoscroll
  strip.addEventListener('click', e => {
    const t = tabOf(e.target);
    if (!t) return;
    if (e.target.closest('.etab-close')) { close(t); return; }
    activate(t, { focus: true });
  });
  strip.addEventListener('auxclick', e => {
    if (e.button !== 1) return;
    const t = tabOf(e.target);
    if (t) { e.preventDefault(); close(t); }
  });
  strip.addEventListener('dblclick', e => {
    const t = tabOf(e.target);
    if (t && !e.target.closest('.etab-close')) pin(t);
  });
  strip.addEventListener('wheel', e => {
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { strip.scrollLeft += e.deltaY; e.preventDefault(); }
  }, { passive: false });
  strip.addEventListener('contextmenu', e => {
    const t = tabOf(e.target);
    if (!t) return;
    e.preventDefault();
    const others = tabs.filter(x => x !== t);
    const right = tabs.slice(tabs.indexOf(t) + 1);
    UI.menu([
      { label: 'Close', kbd: 'Alt+W', action: () => close(t) },
      { label: 'Close others', disabled: !others.some(x => !waits(x)), action: () => closeBulk(others) },
      { label: 'Close to the right', disabled: !right.some(x => !waits(x)), action: () => closeBulk(right) },
      { label: 'Close saved', action: () => closeBulk(tabs.filter(x => !tabDirty(x))) },
      { label: 'Close all', action: () => closeBulk(tabs.slice()) },
      '-',
      { label: 'Keep open', disabled: !t.preview, action: () => pin(t) },
      t.path ? { label: 'Copy relative path', action: () => navigator.clipboard?.writeText(t.path) } : null,
      t.path ? { label: 'Reveal in Explorer', action: () => window.Explorer?.reveal(t.path, { show: true }) } : null,
      t.path ? { label: 'Send to Claude terminal', action: () => UI.sendToTerminal(t.path) } : null,
    ], e.clientX, e.clientY);
  });

  async function closeMany(list) {
    for (const t of list) if (!(await close(t))) return false;
    return true;
  }

  // the tab menu's Close others / to the right / saved / all: a tab waiting for the user's
  // answer is left open (closing Claude's proposed change rejects it: only its own Close does)
  async function closeBulk(list) {
    const kept = list.filter(waits);
    if (!(await closeMany(list.filter(x => !waits(x)))) || !kept.length) return;
    UI.toast(kept.length === 1
      ? `Left “${kept[0].title}” open: Claude waits for you to accept or reject it.`
      : `Left ${kept.length} proposed changes open: Claude waits for you to accept or reject them.`);
  }

  // ---------------------------------------------------------------- tab lifecycle

  function insert(tab, { preview, append }) {
    let at = active ? tabs.indexOf(active) + 1 : tabs.length;
    if (append) at = tabs.length;
    if (preview && !append) {
      const old = tabs.find(t => t.preview && t !== tab);
      if (old && !losesWork(old)) {
        at = tabs.indexOf(old);
        tabs.splice(at, 0, tab);
        remove(old, { replaced: true });
        return;
      }
      if (old) old.preview = false;
    }
    tabs.splice(at, 0, tab);
  }

  function remove(tab, { replaced = false } = {}) {
    const i = tabs.indexOf(tab);
    if (i < 0) return;
    tabs.splice(i, 1);
    mru = mru.filter(t => t !== tab);
    const wasActive = active === tab;
    if (wasActive) {
      emit('deactivate', tab);
      if (tab.type === 'custom') { try { tab.spec.onHide?.(tab); } catch (e) { console.error(e); } }
      active = null;
    }
    if (tab.type === 'custom') { try { tab.spec.dispose?.(tab); } catch (e) { console.error(e); } }
    tab.view?.remove();
    tab.closed = true;
    if (tab.model) release(tab.model);
    emit('close', tab);
    if (wasActive && !replaced) {
      const next = mru[mru.length - 1] || tabs[i] || tabs[i - 1];
      if (next) activate(next);
      else showEmpty();
    }
    renderStrip();
    persist();
  }

  async function close(tab, { force = false } = {}) {
    if (!tab || tab.closed) return true;
    const m = tab.model;
    const last = m && tabs.filter(t => t.model === m).length === 1 && m.refs <= 1;
    if (!force && m && m.dirty && last) {
      if (autosave(m.category) && !m.deleted && await save(m, false, true)) { /* saved: close quietly */ }
      else {
        if (active !== tab) activate(tab);
        const name = UI.basename(m.path);
        const choice = await UI.dialog({
          title: m.deleted ? `${name} has been deleted on disk. Do you want to save your changes to it?`
            : `Do you want to save the changes you made to ${name}?`,
          message: m.deleted ? 'Saving creates the file again. Your changes will be lost if you don\'t save them.'
            : 'Your changes will be lost if you don\'t save them.',
          buttons: [{ label: 'Save', value: 'save', primary: true }, { label: 'Don\'t save', value: 'discard' }, { label: 'Cancel', value: 'cancel' }],
          cancel: 'cancel',
        });
        if (choice === 'cancel') return false;
        if (choice === 'save' && !(await save(m))) return false;
      }
    }
    if (tab.closed) return true;
    remove(tab);
    return true;
  }

  function pin(tab) {
    if (!tab || !tab.preview) return;
    tab.preview = false;
    renderStrip();
    persist();
  }

  function showEmpty() {
    active = null;
    // no file on screen: a reload must not open the one the URL named (in another folder, say)
    if (location.hash) history.replaceState(null, '', location.pathname + location.search);
    if (cm && cm.getDoc() !== blank) cm.swapDoc(blank);
    main.dataset.kind = 'empty';
    renderHead();
    renderBanner();
    setStatus('');
    emit('activate', null, null);
  }

  function applyEditorOptions(m) {
    const md = m.category === 'md';
    const set = (k, v) => { if (cm.getOption(k) !== v) cm.setOption(k, v); };
    cm.getWrapperElement().classList.toggle('cm-code', !md);
    set('lineWrapping', md);
    set('lineNumbers', !md);
    set('indentUnit', md ? 2 : m.indent.unit);
    set('indentWithTabs', !md && m.indent.tabs);
    set('tabSize', md ? 4 : m.indent.tabs ? 4 : m.indent.unit);
  }

  function activate(tab, { focus = false } = {}) {
    if (!tab || tab.closed) return;
    const prev = active;
    if (prev && prev !== tab) {
      emit('deactivate', prev);
      if (prev.type === 'custom') { try { prev.spec.onHide?.(prev); } catch (e) { console.error(e); } }
      if (prev.view) prev.view.hidden = true;
    }
    active = tab;
    mru = mru.filter(t => t !== tab).concat(tab);
    // the URL names the file on screen, which app.js opens after a reload: none for a custom tab
    // (a diff), which the remembered tabs bring back by themselves
    const hash = tab.type === 'custom' || !tab.path ? '' : `#${UI.encPath(tab.path)}`;
    if (location.hash !== hash) history.replaceState(null, '', hash || location.pathname + location.search);
    if (tab.type === 'file') {
      const m = tab.model;
      const wasHidden = !['md', 'text'].includes(main.dataset.kind);
      main.dataset.kind = m.category === 'md' ? 'md' : 'text';
      const swapped = cm.getDoc() !== m.doc;
      if (swapped) cm.swapDoc(m.doc);
      applyEditorOptions(m);
      syncReadOnly();
      ensureMode(m);
      if (wasHidden && !swapped) cm.refresh();   // back from a viewer: remeasure, restore the scroll
      UI.store.set('mdedit.last', tab.path);
      setStatus(m.state.text, m.state.cls);
      if (focus) cm.focus();
    } else {
      main.dataset.kind = 'custom';
      tab.view.hidden = false;
      if (tab.type === 'custom') {
        if (!tab.rendered) {
          tab.rendered = true;
          try { tab.spec.render?.(tab.view, tab); }
          catch (e) { console.error(e); tab.view.textContent = `Could not show this tab: ${e.message}`; }
        }
        if (prev !== tab) { try { tab.spec.onShow?.(tab); } catch (e) { console.error(e); } }
      }
      const m = tab.model;
      setStatus(m ? m.state.text : '', m ? m.state.cls : '');
    }
    renderStrip();
    renderHead();
    renderBanner();
    persist();
    if (prev !== tab) emit('activate', tab, prev);
  }

  // ---------------------------------------------------------------- opening

  async function makeTab(path, { preview, append }) {
    const r = await getFile(path);
    if (r.error) {
      setStatus(`Could not open ${path}: ${r.error.message}`, 'err');
      return null;
    }
    const existing = tabs.find(t => t.type !== 'custom' && t.path === path);
    if (existing) return existing;
    const tab = { id: 't' + (++seq), path, preview: !!preview, title: UI.basename(path) };
    if (r.model) {
      tab.type = 'file';
      tab.model = r.model;
      r.model.refs++;
    } else {
      tab.type = r.info.kind === 'image' ? 'image' : 'binary';
      tab.info = r.info;
      tab.version = String(r.info.version ?? '');
      tab.view = document.createElement('div');
      tab.view.className = 'file-view';
      tab.view.hidden = true;
      views.append(tab.view);
      renderFileView(tab);
    }
    insert(tab, { preview, append });
    emit('open', tab);
    return tab;
  }

  const opening = new Map();
  async function open(path, { preview = true, pin: pinIt = false, focus = true, line, ch, sel, activate: act = true, append = false } = {}) {
    if (!path) return null;
    path = String(path).replace(/^\.?\/+/, '');
    let tab = tabs.find(t => t.type !== 'custom' && t.path === path);
    if (!tab) {
      if (!opening.has(path)) {
        const p = makeTab(path, { preview: preview && !pinIt, append });
        opening.set(path, p);
        p.finally(() => opening.delete(path));
      }
      tab = await opening.get(path);
      if (!tab || tab.closed) return null;
    }
    if (pinIt || !preview) pin(tab);
    if (act) {
      activate(tab, { focus });
      if (tab.type === 'file' && (line != null || sel)) reveal(tab.model, { line, ch, sel, focus });
    }
    return tab;
  }

  // select / scroll to a place in the active file tab (blurring first: a focused editor's input
  // poll would re-type the old selection)
  function reveal(m, { line, ch = 0, sel, focus = true } = {}) {
    if (!active || active.model !== m || active.type !== 'file') return;
    cm.getInputField().blur();
    if (sel) cm.setSelection(sel.from, sel.to || sel.from);
    else if (line != null) cm.setCursor({ line, ch });
    const from = cm.getCursor('from'), to = cm.getCursor('to');
    setTimeout(() => {
      cm.scrollIntoView({ from, to }, Math.round(cm.getScrollInfo().clientHeight / 3));
      if (focus) cm.focus();
    }, 0);
  }

  function renderFileView(t) {
    const i = t.info || {};
    const name = UI.esc(UI.basename(t.path));
    const raw = UI.rawUrl(t.path, t.version);
    if (i.deleted) {
      t.view.innerHTML = `<div class="file-view-msg"><p><strong>${name}</strong></p><p class="muted">This file has been deleted.</p></div>`;
    } else if (t.type === 'image') {
      t.view.innerHTML = `<div class="image-view"><img alt="${name}" src="${UI.esc(raw)}"></div>
        <div class="file-view-meta muted">${name} · ${UI.esc(fmtSize(i.size))}<span class="dims"></span></div>`;
      const img = t.view.querySelector('img');
      img.onload = () => { t.view.querySelector('.dims').textContent = ` · ${img.naturalWidth} × ${img.naturalHeight}`; };
      img.onerror = () => { t.view.querySelector('.dims').textContent = ' · could not be displayed'; };
    } else {
      const why = i.kind === 'too_large'
        ? `It is not shown because it is too large (${fmtSize(i.size)}; the limit is 5 MB).`
        : `It is not shown because it is binary or uses an unsupported text encoding${i.size != null ? ` (${fmtSize(i.size)})` : ''}.`;
      // offer the raw file only when it is not enormous: a link to a multi-GB artefact is a trap
      const link = i.size != null && i.size > RAW_LINK_MAX
        ? `<p class="muted">Too large to open in the browser.</p>`
        : `<p><a href="${UI.esc(raw)}" target="_blank" rel="noopener">Open the raw file in a new browser tab</a></p>`;
      t.view.innerHTML = `<div class="file-view-msg"><p><strong>${name}</strong></p><p class="muted">${UI.esc(why)}</p>${link}</div>`;
    }
  }

  function openCustom(spec) {
    if (spec.id) {
      const t = tabs.find(x => x.type === 'custom' && x.spec.id === spec.id);
      if (t) {
        if (spec.model) release(spec.model);   // the open tab already holds its own reference
        if (spec.preview === false) pin(t);
        if (spec.title && spec.title !== t.title) update(t, { title: spec.title, tooltip: spec.tooltip });
        if (spec.activate !== false) activate(t, { focus: spec.focus !== false });
        return t;
      }
    }
    return addCustom(spec, { preview: spec.preview !== false, activate: spec.activate !== false });
  }

  function addCustom(spec, { preview, activate: act = true, append = false }) {
    const tab = {
      id: 'c' + (++seq), type: 'custom', spec, path: spec.path || null, model: spec.model || null,
      preview, title: spec.title || 'Untitled', rendered: false,
    };
    tab.view = document.createElement('div');
    tab.view.className = 'custom-tab-view';
    tab.view.hidden = true;
    views.append(tab.view);
    insert(tab, { preview, append });
    emit('open', tab);
    if (act) activate(tab, { focus: spec.focus !== false });
    else { renderStrip(); persist(); }
    return tab;
  }

  // a tab that persist() remembers
  const remembered = t => t.type !== 'custom' || !!(t.spec.kind && types.has(t.spec.kind) && t.spec.state !== undefined);

  function update(tab, { title, tooltip, path } = {}) {
    if (title !== undefined) tab.title = title;
    if (tooltip !== undefined) tab.spec.tooltip = tooltip;
    if (path !== undefined) tab.path = path;
    renderStrip();
    if (tab === active) renderHead();
    // only for a tab that is remembered: a proposal's moves on the root event, before the new
    // folder's remembered tabs are read back, which this would overwrite
    if (remembered(tab)) persist();
  }

  // ---------------------------------------------------------------- renames and deletions

  const under = (p, base) => p === base || p.startsWith(base + '/');
  const moved = (p, from, to) => to + p.slice(from.length);

  // an open file with unsaved changes at a path a rename would move something onto (one deleted
  // on disk, so git or the server allow the move): the rename must wait until it is saved or closed
  function renameBlocked(from, to) {
    for (const m of models.values()) {
      if (!under(m.path, from)) continue;
      const o = models.get(moved(m.path, from, to));
      if (o && o !== m && !under(o.path, from) && o.dirty) return o.path;
    }
    return null;
  }

  function renamePath(from, to) {
    UI.stat.bump();   // polls already under way still name the old paths
    // what is open at the paths things move onto (files deleted on disk, or the move would have
    // failed) closes: it would be left with the name of the file moved there, and no way to save
    // (Explorer asks for unsaved changes there to be dealt with first: renameBlocked)
    const dests = new Set([...models.keys(), ...tabs.map(t => t.path)].filter(p => p && under(p, from)).map(p => moved(p, from, to)));
    for (const t of tabs.slice()) {
      const p = t.model ? t.model.path : t.type !== 'custom' ? t.path : null;
      if (p && !under(p, from) && dests.has(p)) remove(t);
    }
    for (const m of [...models.values()]) {
      if (!under(m.path, from)) continue;
      models.delete(m.path);
      m.path = moved(m.path, from, to);
      models.set(m.path, m);
      if (m.deleted) {   // a poll that raced the move saw the old path gone; the next one checks again
        m.deleted = false;
        m.seenVersion = null;
        setBanner(m, null);
      }
      const cat = categoryOf(m.path);
      const { spec, info } = modeFor(m.path);
      const modeChanged = cat !== m.category || (info && info.name) !== (m.modeInfo && m.modeInfo.name);
      m.category = cat;
      m.modeInfo = info;
      if (cat !== 'md' && !m.indent) m.indent = detectIndent(m.doc.getValue());
      if (modeChanged) {
        if (cm.getDoc() === m.doc) cm.setOption('mode', spec);
        else m.doc.modeOption = spec;
        ensureMode(m);
      }
    }
    for (const t of tabs) {
      if (!t.path || !under(t.path, from)) continue;
      const np = moved(t.path, from, to);
      if (t.type === 'custom') { try { t.spec.onRename?.(t, np); } catch (e) { console.error(e); } }
      t.path = np;
      if (t.type !== 'custom') t.title = UI.basename(np);
    }
    if (active && active.type === 'file') {
      applyEditorOptions(active.model);
      main.dataset.kind = active.model.category === 'md' ? 'md' : 'text';
      history.replaceState(null, '', `#${UI.encPath(active.path)}`);
    }
    renderStrip();
    renderHead();
    persist();
    emit('rename', from, to);
    if (active) emit('activate', active, active);   // category may have changed: relayout
  }

  function pathDeleted(path) {
    for (const t of tabs.slice()) {
      if (!t.path || !under(t.path, path) || t.type === 'custom') continue;
      if (t.model && t.model.dirty) { checkModel(t.model, '0'); continue; }
      remove(t);
    }
  }

  // ---------------------------------------------------------------- leaving / root switching

  async function confirmLeave(message = 'You have unsaved changes.') {
    const dirty = [...models.values()].filter(m => m.dirty);
    if (!dirty.length) return true;
    const list = dirty.slice(0, 8).map(m => `<li>${UI.esc(m.path)}</li>`).join('') + (dirty.length > 8 ? `<li>… and ${dirty.length - 8} more</li>` : '');
    const choice = await UI.dialog({
      title: message,
      html: `<p>${dirty.length === 1 ? 'This file has' : `These ${dirty.length} files have`} unsaved changes:</p><ul>${list}</ul>`,
      buttons: [{ label: 'Save all', value: 'save', primary: true }, { label: 'Don\'t save', value: 'discard' }, { label: 'Cancel', value: 'cancel' }],
      cancel: 'cancel',
    });
    if (choice === 'cancel') return false;
    if (choice === 'save') {
      await saveAll();
      if (anyDirty()) { UI.toast('Some files could not be saved.', { kind: 'err' }); return false; }
    }
    return true;
  }

  function closeAll() {
    restoring = true;   // keep the remembered tabs of the root being left
    try { for (const t of tabs.slice()) if (!waits(t)) remove(t, { replaced: true }); }
    finally { restoring = false; }
    if (active) {   // one that is kept steps back
      const t = active;
      emit('deactivate', t);
      try { t.spec.onHide?.(t); } catch (e) { console.error(e); }
      if (t.view) t.view.hidden = true;
    }
    mru = mru.filter(t => tabs.includes(t));
    showEmpty();
    renderStrip();
  }

  // ---------------------------------------------------------------- persistence (per root)

  const key = () => 'mdedit.tabs:' + UI.root();
  function persist() {
    if (restoring || !UI.root()) return;
    const list = [];
    let act = -1;
    for (const t of tabs) {
      let rec = null;
      if (t.type === 'custom') {
        if (t.spec.kind && types.has(t.spec.kind) && t.spec.state !== undefined) rec = { t: 'custom', kind: t.spec.kind, state: t.spec.state, preview: t.preview };
      } else rec = { t: 'file', path: t.path, preview: t.preview };
      if (!rec) continue;
      if (t === active) act = list.length;
      list.push(rec);
    }
    UI.store.setJson(key(), { tabs: list, active: act });
  }

  async function restore() {
    const saved = UI.store.json(key(), null);
    if (!saved || !Array.isArray(saved.tabs) || !saved.tabs.length) return;
    restoring = true;
    let want = null;
    try {
      // load in parallel, then add in the remembered order
      const specs = await Promise.all(saved.tabs.map(async rec => {
        try {
          if (rec.t === 'file') { const r = await getFile(rec.path); return r.error ? null : rec; }
          const fn = types.get(rec.kind);
          const spec = fn ? await fn(rec.state) : null;
          return spec ? Object.assign({}, rec, { spec }) : null;
        } catch (e) { console.error(e); return null; }
      }));
      for (let i = 0; i < specs.length; i++) {
        const rec = specs[i];
        if (!rec) continue;
        let t;
        if (rec.t === 'file') t = await open(rec.path, { preview: rec.preview, activate: false, append: true });
        else t = addCustom(Object.assign({ kind: rec.kind, state: rec.state }, rec.spec), { preview: rec.preview, activate: false, append: true });
        if (i === saved.active) want = t;
      }
    } finally { restoring = false; }
    want = want || tabs[tabs.length - 1];
    if (want) activate(want);
    renderStrip();
  }

  function registerType(kind, fn) { types.set(kind, fn); }

  // ---------------------------------------------------------------- keys

  document.addEventListener('keydown', e => {
    if (e.defaultPrevented || !(e.target instanceof Element)) return;
    if (e.target.closest('#term, .modal-back, .qo-back')) return;
    // text boxes (the folder browser's path, the ask bar) keep the key
    if (e.target.matches('input, select, textarea:not(.CodeMirror textarea), [contenteditable="true"]')) return;
    if (e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey && e.code === 'KeyW' && active) {
      e.preventDefault();
      close(active);
    }
  });

  // ---------------------------------------------------------------- init

  function init(editor) {
    cm = editor;
    if (cm.getDoc() !== blank) cm.swapDoc(blank);
    main.dataset.kind = 'empty';
    renderStrip();
    renderHead();
  }

  function activeModel() { return active ? active.model || null : null; }

  return {
    init, open, openCustom, update, close, pin, activate, reveal,
    active: () => active, activeModel, list: () => tabs.slice(),
    model: p => models.get(p), models: () => [...models.values()],
    acquire, release,
    linkedDoc,
    loadMode, save, saveAll, anyDirty, renamePath, renameBlocked, pathDeleted, reload, cancelAutosave,
    confirmLeave, closeAll, restore, registerType, setStatus, autosave, hold, held: x => !!heldBy(typeof x === 'string' ? { path: x } : x),
    on, cm: () => cm, categoryOf,
  };
})();
window.Tabs = Tabs;
