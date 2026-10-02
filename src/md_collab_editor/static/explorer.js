// The Explorer panel: a lazily loaded file tree of the open folder (GET /api/tree, one folder
// at a time) with a context menu, inline new / rename, delete, keyboard navigation and live
// updates (folder stamps from the shared POST /api/stat poll). "Markdown only" switches to
// the old view: the .md files from /api/files, live through /api/events. Plain script after
// ui.js, activity.js and tabs.js; exposes `Explorer`.
//
//   Explorer.reveal(path, {show, focus})  expand to and select a file (show: open the panel)
//   Explorer.refresh()                    reload every open folder
//   Explorer.render()                     redraw (e.g. after decorations changed)
//   Explorer.decorate(fn) → undo()        fn(path, isDir) → {cls, badge, title} | null, called
//                                         per row on each render (scm.js: status letters)
//   Explorer.newFile(dir?) / Explorer.newFolder(dir?)   inline name box in `dir` (default:
//                                         the selected folder, else the root)
//   Explorer.selected() → path | null     the row with keyboard focus
//   Explorer.menuFor(path, x, y)          the context menu of a row

const Explorer = (() => {
  const ICON = {
    folder: '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M1.75 2.5h4l1.5 1.5h7a.75.75 0 0 1 .75.75v8.5a.75.75 0 0 1-.75.75H1.75a.75.75 0 0 1-.75-.75V3.25a.75.75 0 0 1 .75-.75Z" opacity=".85"/></svg>',
    file: '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.2" d="M3.5 1.75h5.5l3.5 3.5v9a.5.5 0 0 1-.5.5h-8.5a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5Z"/><path fill="none" stroke="currentColor" stroke-width="1.2" d="M9 1.75v3.5h3.5"/></svg>',
    newFile: '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.2" d="M8.5 14.25h-5a.5.5 0 0 1-.5-.5v-12a.5.5 0 0 1 .5-.5h5.5l3.5 3.5v3"/><path fill="none" stroke="currentColor" stroke-width="1.3" d="M12.5 10v5M10 12.5h5"/></svg>',
    newFolder: '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.2" d="M8 13.25H1.75a.5.5 0 0 1-.5-.5v-9.5a.5.5 0 0 1 .5-.5h4l1.5 1.5h7a.5.5 0 0 1 .5.5V8"/><path fill="none" stroke="currentColor" stroke-width="1.3" d="M12.5 9.5v5M10 12h5"/></svg>',
    refresh: '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" d="M13.2 8A5.2 5.2 0 1 1 11.6 4.3"/><path fill="currentColor" d="M13.6 1.6v4.2H9.4z"/></svg>',
    collapse: '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="2.5" y="2.5" width="11" height="11" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.2"/><path stroke="currentColor" stroke-width="1.3" d="M5 8h6"/></svg>',
    reveal: '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="4.6" fill="none" stroke="currentColor" stroke-width="1.2"/><circle cx="8" cy="8" r="1.6" fill="currentColor"/><path stroke="currentColor" stroke-width="1.2" d="M8 .8v2.4M8 12.8v2.4M.8 8h2.4M12.8 8h2.4"/></svg>',
    eye: '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="none" stroke="currentColor" stroke-width="1.2" d="M1 8s2.6-4.8 7-4.8S15 8 15 8s-2.6 4.8-7 4.8S1 8 1 8Z"/><circle cx="8" cy="8" r="2" fill="currentColor"/></svg>',
  };

  // ---------------------------------------------------------------- DOM

  const el = document.createElement('div');
  el.className = 'explorer';
  el.innerHTML = `
    <div class="side-head"><span class="side-title">Explorer</span><span class="side-actions">
      <button type="button" id="browse-btn" class="small-btn" title="Open another folder or file (Ctrl+O)">Open…</button>
      <button type="button" id="new-file" class="small-btn" title="New file (no extension: .md is added)">+ New</button>
    </span></div>
    <div class="tree-tools" role="toolbar" aria-label="Explorer">
      <button type="button" class="tool-btn" data-act="new-file" title="New file">${ICON.newFile}</button>
      <button type="button" class="tool-btn" data-act="new-folder" title="New folder">${ICON.newFolder}</button>
      <button type="button" class="tool-btn" data-act="refresh" title="Refresh">${ICON.refresh}</button>
      <button type="button" class="tool-btn" data-act="collapse" title="Collapse all folders">${ICON.collapse}</button>
      <button type="button" class="tool-btn" data-act="reveal" title="Reveal the active file">${ICON.reveal}</button>
      <span class="spacer"></span>
      <button type="button" class="tool-toggle" id="tree-md-only" data-act="md-only" title="Markdown only: show just .md files and the folders holding them" aria-pressed="false">MD</button>
      <button type="button" class="tool-toggle" id="tree-show-all" data-act="show-all" title="Show hidden and git-ignored files" aria-pressed="false">${ICON.eye}</button>
    </div>
    <div id="file-root" class="muted"></div>
    <div id="tree" role="tree" tabindex="0" aria-label="Files"></div>`;
  const tree = el.querySelector('#tree');
  const handle = Activity.register('explorer', {
    panelEl: el,
    onShow() {
      UI.stat.now();   // catch up on changes made while the panel was hidden
      if (pendingReveal !== null) { const p = pendingReveal; pendingReveal = null; reveal(p); }
    },
  });

  // ---------------------------------------------------------------- state

  let showAll = UI.store.get('mdedit.tree.all', '0') === '1';
  let mdOnly = UI.store.get('mdedit.tree.md', '0') === '1';
  let fs = newSource();          // the real tree (lazy)
  let md = newSource();          // the "Markdown only" tree (from /api/files)
  let expanded = new Set();      // open folders in the real tree (remembered per root)
  let mdClosed = new Set();      // closed folders in the markdown tree (all open by default)
  const stamps = new Map();      // folder → stamp from /api/stat
  let focusPath = null;          // keyboard / selection row
  let activePath = null;         // the active tab's file
  let pendingReveal = null;
  let input = null;              // {parent, kind: 'file'|'folder'} or {rename: path}
  let es = null;                 // /api/events stream in markdown-only mode
  let decorators = [];
  let rootEpoch = 0;

  function newSource() {
    return { nodes: new Map([['', { path: '', name: '', dir: true, children: null }]]) };
  }
  const src = () => mdOnly ? md : fs;
  const parentOf = p => UI.dirname(p);
  const isOpen = p => p === '' || (mdOnly ? !mdClosed.has(p) : expanded.has(p));
  const openKey = () => 'mdedit.tree.open:' + UI.root();
  const saveOpen = () => UI.store.setJson(openKey(), [...expanded]);
  const cmpEntries = (a, b) => (a.dir !== b.dir ? (a.dir ? -1 : 1)
    : a.name.toLowerCase() < b.name.toLowerCase() ? -1 : a.name.toLowerCase() > b.name.toLowerCase() ? 1
    : a.name < b.name ? -1 : a.name > b.name ? 1 : 0);

  // ---------------------------------------------------------------- loading (real tree)

  // → whether the rows change (a file saved only changes its folder's stamp, not its rows)
  function setChildren(source, dir, entries) {
    const node = source.nodes.get(dir);
    if (!node) return false;
    const before = node.children || [];
    entries = entries.slice().sort(cmpEntries);
    let changed = !node.children || !!node.error || before.length !== entries.length;
    node.error = null;
    node.children = entries.map(e => e.path);
    const keep = new Set(node.children);
    entries.forEach((e, i) => {
      const n = source.nodes.get(e.path);
      if (before[i] !== e.path || !n || n.dir !== e.dir || n.hidden !== e.hidden || n.ignored !== e.ignored) changed = true;
      if (n && n.dir === e.dir) Object.assign(n, e, { children: n.children });
      else source.nodes.set(e.path, Object.assign({ children: null }, e));
    });
    for (const p of before) if (!keep.has(p)) dropSubtree(source, p);
    return changed;
  }

  function dropSubtree(source, p) {
    for (const k of [...source.nodes.keys()]) {
      if (k === p || k.startsWith(p + '/')) { source.nodes.delete(k); stamps.delete(k); }
    }
  }

  async function loadDir(dir) {
    const node = fs.nodes.get(dir);
    if (!node || !node.dir) return;
    if (node.loading) return node.loading;
    const epoch = rootEpoch;
    let changed = true;
    node.loading = (async () => {
      // the stamp first, then the listing: a change in between only makes the next poll reload
      try {
        const s = await UI.api('POST', '/api/stat', { paths: [], dirs: [dir] });
        if (epoch === rootEpoch && s.dirs && dir in s.dirs) stamps.set(dir, s.dirs[dir]);
      } catch {}
      try {
        const r = await UI.api('GET', `/api/tree?dir=${encodeURIComponent(dir)}&all=${showAll ? 1 : 0}`);
        if (epoch !== rootEpoch || fs.nodes.get(dir) !== node) return;
        changed = setChildren(fs, dir, r.entries || []);
      } catch (e) {
        if (epoch !== rootEpoch) return;
        if (dir === '' || !(e.status === 404 || e.status === 400)) { node.children = []; node.error = e.message; }
        else {
          const par = fs.nodes.get(parentOf(dir));
          if (par && par.children) par.children = par.children.filter(p => p !== dir);
          dropSubtree(fs, dir);
          expanded.delete(dir);
        }
      }
    })();
    try { await node.loading; } finally { node.loading = null; }
    if (changed) render();   // rows replaced under a click would lose it
  }

  async function loadOpen(dir) {
    await loadDir(dir);
    const n = fs.nodes.get(dir);
    await Promise.all((n && n.children || []).filter(p => expanded.has(p) && fs.nodes.get(p)?.dir).map(loadOpen));
  }

  // ---------------------------------------------------------------- markdown-only tree

  function buildMd(files) {
    const s = newSource();
    s.nodes.get('').children = [];
    for (const f of files) {
      const parts = f.path.split('/');
      let dir = '';
      for (let i = 0; i < parts.length; i++) {
        const p = parts.slice(0, i + 1).join('/');
        const isDir = i < parts.length - 1;
        if (!s.nodes.has(p)) {
          s.nodes.set(p, { path: p, name: parts[i], dir: isDir, children: isDir ? [] : null, size: null, hidden: false, ignored: false });
          s.nodes.get(dir).children.push(p);
        }
        dir = p;
      }
    }
    for (const n of s.nodes.values()) {
      if (n.children) n.children.sort((a, b) => cmpEntries(s.nodes.get(a), s.nodes.get(b)));
    }
    md = s;
    render();
  }

  async function loadMd() {
    try { buildMd(await UI.api('GET', '/api/files')); } catch (e) { UI.toast(`Could not list files: ${e.message}`, { kind: 'err' }); }
  }

  function setLive() {
    if (mdOnly && !es) {
      es = new EventSource('/api/events');
      es.onmessage = ev => { try { buildMd(JSON.parse(ev.data)); } catch {} };
    } else if (!mdOnly && es) { es.close(); es = null; }
  }

  // ---------------------------------------------------------------- polling (real tree)

  UI.stat.watch({
    dirs() {
      if (mdOnly || !handle.visible()) return [];
      return [...fs.nodes.values()].filter(n => n.dir && n.children && isOpen(n.path)).map(n => n.path);
    },
    onResult(res) {
      let gone = false;
      for (const [d, s] of Object.entries(res.dirs)) {
        const n = fs.nodes.get(d);
        if (!n || !n.children) continue;
        if (s === '0' && d !== '') {   // the folder itself has gone: its parent's reload drops it
          const par = fs.nodes.get(parentOf(d));
          if (par && par.children) par.children = par.children.filter(p => p !== d);
          dropSubtree(fs, d);
          gone = true;
        } else if (!stamps.has(d)) stamps.set(d, s);
        else if (stamps.get(d) !== s) loadDir(d);
      }
      if (gone) render();
    },
  });

  // ---------------------------------------------------------------- rendering

  function rows() {
    const out = [];
    const s = src();
    const walk = (dir, depth) => {
      const n = s.nodes.get(dir);
      if (input && !input.rename && input.parent === dir) out.push({ input: true, depth });
      if (!n || !n.children) { if (n && dir !== '') out.push({ loading: true, depth }); return; }
      for (const p of n.children) {
        const c = s.nodes.get(p);
        if (!c) continue;
        out.push({ node: c, depth });
        if (c.dir && isOpen(p)) {
          if (!mdOnly && !c.children && !c.loading) setTimeout(() => loadDir(p), 0);   // e.g. a new folder
          walk(p, depth + 1);
        }
      }
    };
    walk('', 0);
    return out;
  }

  let visible = [];   // paths of the rendered rows, in order
  function render() {
    const list = rows();
    visible = list.filter(r => r.node).map(r => r.node.path);
    if (focusPath !== null && !visible.includes(focusPath)) focusPath = null;
    const frag = document.createDocumentFragment();
    const pad = d => `padding-left:${6 + d * 14}px`;
    for (const r of list) {
      if (r.loading) {
        const d = document.createElement('div');
        d.className = 'tree-row loading muted';
        d.style.cssText = pad(r.depth) + ';padding-left:' + (22 + r.depth * 14) + 'px';
        d.textContent = 'Loading…';
        frag.append(d);
        continue;
      }
      if (r.input) { frag.append(inputRow(r.depth)); continue; }
      const n = r.node;
      const d = document.createElement('div');
      d.className = 'tree-row ' + (n.dir ? 'dir' : 'file');
      d.setAttribute('role', 'treeitem');
      d.setAttribute('aria-level', r.depth + 1);
      d.dataset.path = n.path;
      d.style.cssText = pad(r.depth);
      if (n.dir) d.setAttribute('aria-expanded', isOpen(n.path) ? 'true' : 'false');
      if (n.hidden) d.classList.add('is-hidden');
      if (n.ignored) d.classList.add('is-ignored');
      if (n.path === activePath) d.classList.add('active');
      if (n.path === focusPath) { d.classList.add('focus'); d.setAttribute('aria-selected', 'true'); }
      d.title = n.path + (n.ignored ? ' (git-ignored)' : n.hidden ? ' (hidden)' : '');
      if (input && input.rename === n.path) {
        d.innerHTML = `<span class="tw">${n.dir ? '›' : ''}</span><span class="ico">${n.dir ? ICON.folder : ICON.file}</span>`;
        d.append(inputBox(n.name));
      } else {
        d.innerHTML = `<span class="tw">${n.dir ? '›' : ''}</span><span class="ico">${n.dir ? ICON.folder : ICON.file}</span><span class="nm">${UI.esc(n.name)}</span>`;
        for (const fn of decorators) {
          let dec = null;
          try { dec = fn(n.path, n.dir); } catch (e) { console.error(e); }
          if (!dec) continue;
          if (dec.cls) d.classList.add(...String(dec.cls).split(/\s+/).filter(Boolean));
          if (dec.badge) d.insertAdjacentHTML('beforeend', `<span class="deco">${UI.esc(dec.badge)}</span>`);
          if (dec.title) d.title += ` — ${dec.title}`;
        }
      }
      frag.append(d);
    }
    if (!list.length) {
      const n = src().nodes.get('');
      const msg = n.error ? `Could not list this folder: ${n.error}` : !n.children && !mdOnly ? 'Loading…'
        : mdOnly ? 'No markdown files in this folder.' : 'This folder is empty.';
      frag.append(Object.assign(document.createElement('div'), { className: 'tree-empty muted', textContent: msg }));
    }
    const typing = input && tree.contains(document.activeElement) && document.activeElement.tagName === 'INPUT';
    if (typing) return;   // don't rebuild under the name box being typed in
    tree.replaceChildren(frag);
    const box = tree.querySelector('input.tree-input');
    if (box) { box.focus(); if (box.dataset.sel) { const [a, b] = box.dataset.sel.split(',').map(Number); box.setSelectionRange(a, b); } }
  }

  // focus / active highlight only: cheaper than render(), and keeps the rows under the pointer
  function updateMarks() {
    for (const r of tree.querySelectorAll('.tree-row[data-path]')) {
      const p = r.dataset.path;
      r.classList.toggle('active', p === activePath);
      r.classList.toggle('focus', p === focusPath);
      if (p === focusPath) r.setAttribute('aria-selected', 'true'); else r.removeAttribute('aria-selected');
    }
  }

  function inputRow(depth) {
    const d = document.createElement('div');
    d.className = 'tree-row input-row';
    d.style.cssText = `padding-left:${6 + depth * 14}px`;
    d.innerHTML = `<span class="tw"></span><span class="ico">${input.kind === 'folder' ? ICON.folder : ICON.file}</span>`;
    d.append(inputBox(''));
    return d;
  }

  function inputBox(value) {
    const box = Object.assign(document.createElement('input'), {
      className: 'tree-input', value, spellcheck: false, autocomplete: 'off',
      placeholder: input.rename ? '' : input.kind === 'folder' ? 'folder name' : input.md ? 'name (.md is added if there is no extension)' : 'file name',
    });
    if (input.rename && value) {
      const dot = value.lastIndexOf('.');
      box.dataset.sel = `0,${dot > 0 && !fs.nodes.get(input.rename)?.dir ? dot : value.length}`;
    }
    box.addEventListener('keydown', e => {
      e.stopPropagation();
      if (e.key === 'Escape') { e.preventDefault(); cancelInput(); }
      else if (e.key === 'Enter') { e.preventDefault(); commitInput(box); }
    });
    box.addEventListener('blur', () => setTimeout(() => { if (input && !box.dataset.busy && document.activeElement !== box) cancelInput(); }, 120));
    box.addEventListener('click', e => e.stopPropagation());
    box.addEventListener('dblclick', e => e.stopPropagation());
    return box;
  }

  function cancelInput() {
    if (!input) return;
    input = null;
    const a = document.activeElement;
    if (a && a.classList.contains('tree-input')) a.blur();
    render();
  }

  async function commitInput(box) {
    const name = box.value.trim().replace(/^\/+|\/+$/g, '');
    if (!name) { cancelInput(); return; }
    if (/(^|\/)\.\.?(\/|$)/.test(name)) { bad(box, 'Names cannot contain . or .. as a folder'); return; }
    const cur = input;
    box.dataset.busy = '1';
    try {
      if (cur.rename) {
        const from = cur.rename;
        const to = (parentOf(from) ? parentOf(from) + '/' : '') + name;
        if (to !== from) {
          const r = await UI.api('POST', '/api/rename', { from, to });
          const dest = r.path || to;
          Tabs.renamePath(from, dest);
          expanded = new Set([...expanded].map(p => p === from || p.startsWith(from + '/') ? dest + p.slice(from.length) : p));
          saveOpen();
          focusPath = dest;
          input = null;
          await afterChange([parentOf(from), parentOf(dest)]);
          reveal(dest, { focus: true });
          return;
        }
        input = null;
        render();
        tree.focus();
        return;
      }
      const rel = (cur.parent ? cur.parent + '/' : '') + name;
      // "+ New" adds .md to a name without an extension (as it always has); New file… is exact
      const body = cur.kind === 'folder' ? { path: rel, dir: true } : cur.md ? { path: rel } : { path: rel, exact: true };
      const r = await UI.api('POST', '/api/new', body);
      const made = r.path || rel;
      input = null;
      if (cur.kind === 'folder') { expanded.add(made); saveOpen(); }
      await afterChange([parentOf(made)]);
      if (cur.kind === 'folder') reveal(made, { focus: true });
      else { await Tabs.open(made, { preview: false, focus: true }); reveal(made); }
    } catch (e) {
      delete box.dataset.busy;
      bad(box, e.message);
      box.focus();
    }
  }

  function bad(box, msg) {
    box.classList.add('bad');
    box.title = msg;
    UI.toast(msg, { kind: 'err' });
  }

  // after creating / renaming / deleting: reload the touched folders now (polling would too)
  async function afterChange(dirs) {
    if (mdOnly) { await loadMd(); return; }
    const want = [...new Set(dirs)];
    // make sure every folder on the way is loaded and open
    for (const d of want) {
      let acc = '';
      for (const part of d ? d.split('/') : []) {
        acc = acc ? acc + '/' + part : part;
        if (!fs.nodes.get(acc)) await loadDir(parentOf(acc));
        expanded.add(acc);
      }
    }
    await Promise.all(want.map(d => fs.nodes.get(d) ? loadDir(d) : null));
    render();
  }

  // ---------------------------------------------------------------- actions

  function targetDir(dir) {
    if (dir !== undefined && dir !== null) return dir;
    if (focusPath !== null) {
      const n = src().nodes.get(focusPath);
      if (n) return n.dir ? n.path : parentOf(n.path);
    }
    return '';
  }

  function startNew(kind, dir, { md = false } = {}) {
    dir = targetDir(dir);
    Activity.show('explorer');
    input = { parent: dir, kind, md };
    if (dir) {
      if (mdOnly) mdClosed.delete(dir);
      else { expanded.add(dir); saveOpen(); }
    }
    render();   // the name box first; the folder's listing can follow
    if (dir && !mdOnly && !fs.nodes.get(dir)?.children) loadDir(dir);
  }

  function startRename(path) {
    if (!path) return;
    input = { rename: path };
    render();
  }

  async function remove(path) {
    const n = src().nodes.get(path);
    if (!n) return;
    let msg = `Delete “${n.name}”? This cannot be undone.`;
    if (n.dir) {   // count what is inside first (a dry run)
      let c = null;
      try { c = await UI.api('POST', '/api/delete', { path, dry_run: true }); } catch {}
      const files = c && typeof c.files === 'number' ? c.files : null, dirs = c && typeof c.dirs === 'number' ? c.dirs : 0;
      const plural = (k, w) => `${k.toLocaleString()} ${w}${k === 1 ? '' : 's'}`;
      const parts = [files ? plural(files, 'file') : '', dirs ? plural(dirs, 'folder') : ''].filter(Boolean);
      msg = files === null ? `Delete the folder “${n.name}” and everything in it? This cannot be undone.`
        : parts.length ? `Delete the folder “${n.name}” and the ${parts.join(' and ')} in it? This cannot be undone.`
        : `Delete the empty folder “${n.name}”? This cannot be undone.`;
    }
    const dirty = Tabs.models().filter(m => m.dirty && (m.path === path || m.path.startsWith(path + '/')));
    if (dirty.length) msg += ` ${dirty.length === 1 ? 'An open file has' : `${dirty.length} open files have`} unsaved changes, which will stay in the editor.`;
    if (!(await UI.confirm(msg, { title: 'Delete', ok: 'Delete', danger: true }))) return;
    try {
      await UI.api('POST', '/api/delete', { path });
    } catch (e) { UI.toast(`Could not delete ${path}: ${e.message}`, { kind: 'err' }); return; }
    Tabs.pathDeleted(path);
    for (const p of [...expanded]) if (p === path || p.startsWith(path + '/')) expanded.delete(p);
    saveOpen();
    if (focusPath === path) focusPath = null;
    dropSubtree(fs, path);
    const par = fs.nodes.get(parentOf(path));
    if (par && par.children) par.children = par.children.filter(p => p !== path);
    render();
    await afterChange([parentOf(path)]);
  }

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
    const n = path === null ? null : src().nodes.get(path);
    const abs = p => UI.root().replace(/\/+$/, '') + (p ? '/' + p : '');
    if (!n) {
      UI.menu([
        { label: 'New file…', action: () => startNew('file', '') },
        { label: 'New folder…', action: () => startNew('folder', '') },
        '-',
        { label: 'Refresh', action: refresh },
        { label: 'Collapse all', action: collapseAll },
        '-',
        { label: 'Copy path', action: () => copy(abs('')) },
        { label: 'Send to Claude terminal', action: () => UI.sendToTerminal('.') },
      ], x, y);
      return;
    }
    const dir = n.dir ? n.path : parentOf(n.path);
    UI.menu([
      n.dir ? null : { label: 'Open', action: () => Tabs.open(n.path, { preview: false, focus: true }) },
      n.dir ? null : '-',
      { label: 'New file…', action: () => startNew('file', dir) },
      { label: 'New folder…', action: () => startNew('folder', dir) },
      '-',
      { label: 'Rename…', kbd: 'F2', action: () => startRename(n.path) },
      { label: 'Delete', kbd: 'Delete', danger: true, action: () => remove(n.path) },
      '-',
      { label: 'Copy relative path', action: () => copy(n.path) },
      { label: 'Copy path', action: () => copy(abs(n.path)) },
      { label: 'Send to Claude terminal', kbd: '@', action: () => UI.sendToTerminal(n.path) },
    ], x, y);
  }

  function toggleDir(path, open) {
    if (path === '') return;
    const now = isOpen(path);
    open = open === undefined ? !now : open;
    if (open === now) return;
    if (mdOnly) { if (open) mdClosed.delete(path); else mdClosed.add(path); render(); return; }
    if (open) { expanded.add(path); if (!fs.nodes.get(path)?.children) loadDir(path); }
    else expanded.delete(path);
    saveOpen();
    render();
  }

  function collapseAll() {
    if (mdOnly) { for (const n of md.nodes.values()) if (n.dir && n.path) mdClosed.add(n.path); }
    else { expanded.clear(); saveOpen(); }
    render();
  }

  async function refresh() {
    if (mdOnly) { await loadMd(); return; }
    for (const n of fs.nodes.values()) if (n.dir) n.children = n.children && n.path !== '' && !isOpen(n.path) ? null : n.children;
    await loadOpen('');
  }

  async function reveal(path, { show = false, focus = false } = {}) {
    if (show) Activity.show('explorer');
    if (!path) return;
    if (!handle.visible() && !show) { pendingReveal = path; return; }
    const parts = path.split('/');
    let changed = false;
    if (mdOnly) {
      for (let i = 1; i < parts.length; i++) changed = mdClosed.delete(parts.slice(0, i).join('/')) || changed;
    } else {
      if (!fs.nodes.get('').children) { await loadDir(''); changed = true; }
      let acc = '';
      for (const part of parts.slice(0, -1)) {
        acc = acc ? acc + '/' + part : part;
        const n = fs.nodes.get(acc);
        if (!n || !n.dir) break;   // hidden / ignored, or gone
        if (!expanded.has(acc)) { expanded.add(acc); saveOpen(); changed = true; }
        if (!n.children) { await loadDir(acc); changed = true; }
      }
    }
    if (src().nodes.get(path)) focusPath = path;
    if (changed || !tree.querySelector(`.tree-row[data-path="${CSS.escape(path)}"]`)) render();
    else updateMarks();
    const row = tree.querySelector(`.tree-row[data-path="${CSS.escape(path)}"]`);
    if (row) row.scrollIntoView({ block: 'nearest' });
    if (focus) tree.focus({ preventScroll: true });
  }

  // ---------------------------------------------------------------- events

  const rowOf = t => t.closest && t.closest('.tree-row[data-path]');
  tree.addEventListener('click', e => {
    const r = rowOf(e.target);
    if (!r) return;
    const p = r.dataset.path;
    const n = src().nodes.get(p);
    focusPath = p;
    if (!n) return;
    if (n.dir) toggleDir(p);
    else { updateMarks(); Tabs.open(p, { preview: true, focus: false }); }
  });
  tree.addEventListener('dblclick', e => {
    const r = rowOf(e.target);
    if (!r) return;
    const n = src().nodes.get(r.dataset.path);
    if (n && !n.dir) Tabs.open(n.path, { preview: false, focus: true });
  });
  tree.addEventListener('mousedown', e => { if (e.detail > 1) e.preventDefault(); });   // no text selection on double-click
  tree.addEventListener('contextmenu', e => {
    e.preventDefault();
    const r = rowOf(e.target);
    if (r) { focusPath = r.dataset.path; updateMarks(); }
    menuFor(r ? r.dataset.path : null, e.clientX, e.clientY);
  });

  tree.addEventListener('keydown', e => {
    if (e.target !== tree) return;
    const i = focusPath === null ? -1 : visible.indexOf(focusPath);
    const n = focusPath === null ? null : src().nodes.get(focusPath);
    const go = j => {
      if (!visible.length) return;
      focusPath = visible[Math.max(0, Math.min(visible.length - 1, j))];
      updateMarks();
      tree.querySelector('.tree-row.focus')?.scrollIntoView({ block: 'nearest' });
    };
    let handled = true;
    switch (e.key) {
      case 'ArrowDown': go(i + 1); break;
      case 'ArrowUp': go(i < 0 ? visible.length - 1 : i - 1); break;
      case 'Home': go(0); break;
      case 'End': go(visible.length - 1); break;
      case 'PageDown': go(i + 15); break;
      case 'PageUp': go(i - 15); break;
      case 'ArrowRight':
        if (n && n.dir) { if (!isOpen(n.path)) toggleDir(n.path, true); else go(i + 1); }
        break;
      case 'ArrowLeft':
        if (n && n.dir && isOpen(n.path)) toggleDir(n.path, false);
        else if (n && parentOf(n.path) !== '') { focusPath = parentOf(n.path); updateMarks(); tree.querySelector('.tree-row.focus')?.scrollIntoView({ block: 'nearest' }); }
        break;
      case 'Enter':
        if (n && n.dir) toggleDir(n.path);
        else if (n) Tabs.open(n.path, { preview: !e.ctrlKey && !e.metaKey ? true : false, focus: true });
        break;
      case ' ':
        if (n && !n.dir) Tabs.open(n.path, { preview: true, focus: false });
        else if (n) toggleDir(n.path);
        break;
      case 'F2': if (n) startRename(n.path); break;
      case 'Delete': if (n) remove(n.path); break;
      case 'ContextMenu': {
        const row = tree.querySelector('.tree-row.focus') || tree;
        const b = row.getBoundingClientRect();
        menuFor(n ? n.path : null, b.left + 24, b.bottom);
        break;
      }
      default:
        if (e.key === 'F10' && e.shiftKey) {
          const row = tree.querySelector('.tree-row.focus') || tree;
          const b = row.getBoundingClientRect();
          menuFor(n ? n.path : null, b.left + 24, b.bottom);
        } else handled = false;
    }
    if (handled) e.preventDefault();
  });
  tree.addEventListener('focus', () => {
    if (focusPath === null && visible.length) { focusPath = activePath && visible.includes(activePath) ? activePath : visible[0]; updateMarks(); }
  });

  el.querySelector('.tree-tools').addEventListener('click', e => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    switch (b.dataset.act) {
      case 'new-file': startNew('file'); break;
      case 'new-folder': startNew('folder'); break;
      case 'refresh': refresh(); break;
      case 'collapse': collapseAll(); break;
      case 'reveal': {
        const t = Tabs.active();
        if (t && t.path) reveal(t.path, { show: true, focus: true });
        break;
      }
      case 'md-only': setMdOnly(!mdOnly); break;
      case 'show-all': setShowAll(!showAll); break;
    }
  });
  el.querySelector('#new-file').onclick = () => startNew('file', undefined, { md: true });

  function syncToggles() {
    const a = el.querySelector('#tree-md-only'), b = el.querySelector('#tree-show-all');
    a.setAttribute('aria-pressed', mdOnly ? 'true' : 'false');
    a.classList.toggle('on', mdOnly);
    b.setAttribute('aria-pressed', showAll ? 'true' : 'false');
    b.classList.toggle('on', showAll);
    b.disabled = mdOnly;
    b.title = mdOnly ? 'Hidden and ignored files: not used in the Markdown only view' : showAll ? 'Hide hidden and git-ignored files' : 'Show hidden and git-ignored files';
  }

  async function setMdOnly(on) {
    mdOnly = on;
    UI.store.set('mdedit.tree.md', on ? '1' : '0');
    syncToggles();
    setLive();
    if (on) await loadMd();
    else { render(); if (!fs.nodes.get('').children) await loadOpen(''); }
    if (activePath) reveal(activePath);
  }

  async function setShowAll(on) {
    showAll = on;
    UI.store.set('mdedit.tree.all', on ? '1' : '0');
    syncToggles();
    if (mdOnly) return;
    for (const n of fs.nodes.values()) if (n.dir && n.path !== '') n.children = null;
    await loadOpen('');
  }

  // ---------------------------------------------------------------- wiring

  Tabs.on('activate', t => {
    activePath = t && t.path ? t.path : null;
    if (activePath && handle.visible()) reveal(activePath);
    else { if (activePath) pendingReveal = activePath; updateMarks(); }
  });
  Tabs.on('rename', () => { const t = Tabs.active(); activePath = t && t.path ? t.path : null; updateMarks(); });

  UI.on('root', () => {
    rootEpoch++;
    fs = newSource();
    md = newSource();
    stamps.clear();
    input = null;
    focusPath = null;
    expanded = new Set(UI.store.json(openKey(), []));
    mdClosed = new Set();
    syncToggles();
    setLive();
    render();
    if (mdOnly) loadMd();
    else loadOpen('');
  });
  syncToggles();

  return {
    reveal, refresh, render,
    decorate(fn) { decorators.push(fn); render(); return () => { decorators = decorators.filter(f => f !== fn); render(); }; },
    newFile: dir => startNew('file', dir),
    newFolder: dir => startNew('folder', dir),
    selected: () => focusPath,
    menuFor,
  };
})();
window.Explorer = Explorer;
