// Claude Code's proposed edits, as accept/reject diff tabs. When Claude Code would ask
// permission for an Edit or Write (not in auto mode) and md-editor's IDE link is up, it asks
// md-editor to show the change (MCP openDiff); the server sends that to the page as a `diff`
// message over /api/term, and term.js hands it here. Plain script after tabs.js and
// linediff.js; exposes `Proposals`.
//
//   Proposals.open(msg, decide)   msg: {id, file (the file's absolute path), path (ROOT-relative,
//                                 or null outside the open folder), old (the file on disk; ''
//                                 for a new file), exists (false for a new file), new (Claude's
//                                 whole proposed file), title (Claude's tab name)}.
//                                 decide({accept: true, contents} | {accept: false}) answers it.
//                                 A copy sent again (after a reconnect) is ignored, as is an
//                                 answered one.
//   Proposals.close(id)           the server withdrew it (answered in the terminal, or by
//                                 another page): close the tab without answering
//   Proposals.sync(ids)           the ids the server still waits on (each status): close any
//                                 other without answering (an earlier md-editor's, say)
//   Proposals.list() → [{id, path, file}]
//
// The tab is a CodeMirror MergeView like scm.js's diff tabs: the file on disk on the left
// (read-only) and Claude's version on the right, in a Doc of its own (never linked to the
// file's model: md-editor writes nothing; Claude writes the file itself after an Accept), so
// it can be edited and each change put back with the arrows between the sides. Accept sends
// the right-hand text, Reject (or closing the tab) tells Claude no. While it waits, the
// file's own tab is read-only under a banner and is not saved (Tabs.hold). A proposal that
// arrives while the user is typing in an editor opens beside it, not over it. The tabs belong
// to the session, not the folder: opening another folder keeps them (Tabs `waiting`), as
// does Close saved. They are not remembered across reloads: the server sends the waiting
// proposals again.

const Proposals = (() => {
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform);
  const KEYS = isMac ? { accept: '⌘Enter', reject: '⌘⇧Enter' } : { accept: 'Ctrl+Enter', reject: 'Ctrl+Shift+Enter' };
  const svg = body => `<svg viewBox="0 0 16 16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
  const ICON = { up: svg('<path d="M8 13V3M3.5 7.5 8 3l4.5 4.5"/>'), down: svg('<path d="M8 3v10M3.5 8.5 8 13l4.5-4.5"/>') };
  const HOLD_AFTER_ACCEPT = 4000;   // ms the file stays read-only after Accept, until Claude writes it

  const shown = new Map();   // id → proposal
  const done = new Set();    // ids answered or withdrawn here

  // the file's path in the open folder, or null
  function relPath(abs) {
    const root = UI.root().replace(/\/+$/, '');
    return root && abs.startsWith(root + '/') ? abs.slice(root.length + 1) : null;
  }

  // While it waits, the file's tab in the open folder is held (read-only, not saved).
  function holdFile(p) {
    if (p.release) { p.release(); p.release = null; }
    if (!p.path) return;
    p.release = Tabs.hold(p.path, {
      msg: `Claude has proposed a change to ${p.name}: it stays read-only until you accept or reject it.`,
      actions: [['Show the proposed change', () => { if (!p.tab.closed) Tabs.activate(p.tab); }]],
    });
  }

  // someone is typing in an editor or a text box (not the terminal): a new tab must not take
  // the keys away (the editor it hides would leave them to nothing)
  const TYPING_MS = 2000;
  let keyAt = 0;
  document.addEventListener('keydown', e => {
    if (e.target instanceof Element && !e.target.closest('#term')) keyAt = Date.now();
  }, true);
  function typing() {
    const a = document.activeElement;
    return Date.now() - keyAt < TYPING_MS && !!(a && a !== document.body && !a.closest('#term')
      && (a.closest('.CodeMirror') || a.matches('input, textarea, [contenteditable="true"]')));
  }

  function open(msg, decide) {
    const id = String(msg.id);
    if (done.has(id) || shown.has(id)) return;   // answered, or a copy sent again (a reconnect)
    const abs = String(msg.file || msg.path || '');
    const p = {
      id, decide, abs, path: typeof msg.path === 'string' && msg.path ? msg.path : relPath(abs),
      name: UI.basename(abs) || 'file',
      old: typeof msg.old === 'string' ? msg.old : '', text: typeof msg.new === 'string' ? msg.new : '',
      isNew: msg.exists === false, claudeTitle: String(msg.title || msg.tab_name || ''),
      answered: false, mv: null, built: false, spec: null, release: null,
    };
    shown.set(id, p);
    holdFile(p);
    const behind = typing();
    p.tab = Tabs.openCustom({
      id: 'claude-diff:' + id, title: `${p.name} (proposed)`, path: p.path,
      tooltip: `Claude's proposed change to ${abs}${p.claudeTitle ? ` (${p.claudeTitle})` : ''}`,
      preview: false, focus: false, activate: !behind,
      waiting: () => !p.answered,
      render: (view, tab) => render(p, view, tab), onShow: () => show(p), dispose: () => dispose(p),
    });
    if (behind) UI.toast(`Claude has proposed a change to ${p.name}: see the "${p.name} (proposed)" tab.`, { ms: 6000 });
    Tabs.loadMode(abs || p.name).then(spec => { p.spec = spec; build(p); }, () => { p.spec = 'text/plain'; build(p); });
  }

  // another folder was opened: the proposals stay (they are the session's), held in that folder
  UI.on('root', () => {
    for (const p of shown.values()) {
      if (p.answered || p.tab.closed) continue;
      p.path = relPath(p.abs);
      Tabs.update(p.tab, { path: p.path });
      holdFile(p);
    }
  });

  function render(p, view, tab) {
    p.tab = tab;   // render runs inside openCustom, before it returns the tab
    view.classList.add('diff-tab', 'proposal-tab');
    view.innerHTML = `
      <div class="diff-head">
        <span class="diff-title"></span>
        <span class="diff-base muted"></span>
        <span class="diff-stats"></span>
        <span class="spacer"></span>
        <span class="diff-tools">
          <button type="button" class="tool-btn" data-act="prev" title="Previous change">${ICON.up}</button>
          <button type="button" class="tool-btn" data-act="next" title="Next change">${ICON.down}</button>
          <button type="button" class="primary proposal-accept" data-act="accept" title="Let Claude write this version of the file (${KEYS.accept})">Accept</button>
          <button type="button" class="proposal-reject" data-act="reject" title="Tell Claude no (${KEYS.reject}); closing the tab does the same">Reject</button>
        </span>
      </div>
      <div class="proposal-note muted"></div>
      <div class="diff-body"><div class="diff-msg muted">Loading…</div></div>`;
    p.el = {
      head: view.querySelector('.diff-head'), body: view.querySelector('.diff-body'),
      stats: view.querySelector('.diff-stats'), accept: view.querySelector('[data-act="accept"]'),
    };
    const title = view.querySelector('.diff-title');
    title.textContent = `✻ ${p.path || p.abs}`;
    title.title = p.abs;
    view.querySelector('.diff-base').textContent = p.isNew ? 'a new file, proposed by Claude' : 'proposed by Claude';
    view.querySelector('.proposal-note').textContent = `Left: the file now. Right: Claude's version, which you can edit (the arrows put a change back). Accept lets Claude write the right-hand side; Reject tells Claude no.`;
    p.el.head.addEventListener('click', e => {
      const b = e.target.closest('button');
      if (!b) return;
      switch (b.dataset.act) {
        case 'prev': step(p, -1); break;
        case 'next': step(p, 1); break;
        case 'accept': accept(p); break;
        case 'reject': reject(p); break;
      }
    });
    // the shortcuts work anywhere in the tab (the editors also bind them: extraKeys)
    view.addEventListener('keydown', e => {
      if (e.defaultPrevented || !(isMac ? e.metaKey : e.ctrlKey) || e.altKey || e.key !== 'Enter') return;
      e.preventDefault();
      if (e.shiftKey) reject(p); else accept(p);
    });
    build(p);
  }

  // CodeMirror measures what it shows, so the view is made while its tab is on screen
  function build(p) {
    if (p.built || !p.spec || !p.el || p.tab.closed || Tabs.active() !== p.tab) return;
    p.built = true;
    const md = UI.isMarkdown(p.abs || p.name);
    p.doc = new CodeMirror.Doc(p.text, p.spec);
    const keys = {
      'Ctrl-Enter': () => accept(p), 'Cmd-Enter': () => accept(p),
      'Shift-Ctrl-Enter': () => reject(p), 'Shift-Cmd-Enter': () => reject(p),
    };
    const split = Object.assign(document.createElement('div'), { className: 'diff-split' });
    p.el.body.replaceChildren(split);
    p.mv = CodeMirror.MergeView(split, {
      value: p.doc, origLeft: p.old, mode: p.spec, lineNumbers: true, lineWrapping: md,
      connect: 'align', revertButtons: true, extraKeys: keys,
    });
    for (const c of [p.mv.editor(), p.mv.leftOriginal()]) c.getWrapperElement().classList.toggle('cm-code', !md);
    let t;
    p.doc.on('change', () => { clearTimeout(t); t = setTimeout(() => update(p), 150); });
    update(p);
    const chunks = p.mv.leftChunks() || [];
    if (chunks.length) jump(p.mv.editor(), chunks[0].editFrom, false);
  }

  function show(p) {
    if (!p.built) { build(p); return; }
    const ed = p.mv.editor(), orig = p.mv.leftOriginal();
    ed.refresh();
    orig.refresh();
    const dv = p.mv.left;   // realign after being hidden (the addon's own update, forced)
    if (dv && typeof dv.forceUpdate === 'function') { dv.dealigned = true; dv.forceUpdate('full'); }
  }

  // the counts, and Accept only when the right side differs from the file (Claude takes an
  // unchanged file as a rejection)
  function update(p) {
    if (!p.doc || p.tab.closed) return;
    const now = p.doc.getValue();
    const chunks = LineDiff.lineChunks(p.old, now);
    let add = 0, del = 0;
    for (const c of chunks) { add += c.editTo - c.editFrom; del += c.origTo - c.origFrom; }
    p.el.stats.innerHTML = chunks.length
      ? `<span class="add">+${add.toLocaleString()}</span> <span class="del">−${del.toLocaleString()}</span>`
      : '<span class="muted">No changes</span>';
    const same = now === p.old;
    p.el.accept.disabled = same;
    p.el.accept.title = same ? 'The right-hand side is the same as the file: nothing to accept' : `Let Claude write this version of the file (${KEYS.accept})`;
  }

  function jump(cm, line, focus = true) {
    line = Math.max(0, Math.min(line, cm.lastLine()));
    cm.setCursor({ line, ch: 0 });
    cm.scrollIntoView({ line, ch: 0 }, Math.round(cm.getScrollInfo().clientHeight / 3));
    if (focus) cm.focus();
  }

  function step(p, dir) {
    if (!p.mv) return;
    const ed = p.mv.editor();
    const starts = (p.mv.leftChunks() || []).map(c => c.editFrom);
    if (!starts.length) { UI.toast('No changes'); return; }
    const line = ed.getCursor().line;
    let target = dir > 0 ? starts.find(l => l > line) : starts.slice().reverse().find(l => l < line);
    if (target == null) target = dir > 0 ? starts[0] : starts[starts.length - 1];
    jump(ed, target);
  }

  function answer(p, d) {
    if (p.answered) return;
    p.answered = true;
    done.add(p.id);
    p.decide(d);
  }

  function accept(p) {
    if (p.answered || !p.doc) return;
    const contents = p.doc.getValue();
    if (contents === p.old) { UI.toast('The right-hand side is the same as the file: nothing to accept.'); return; }
    answer(p, { accept: true, contents });
    // Claude writes the file next: keep it read-only until the change arrives (or a moment)
    const release = p.release;
    p.release = null;
    if (release) {
      const off = Tabs.on('disk-change', m => { if (m.path === p.path) { off(); release(); } });
      setTimeout(() => { off(); release(); }, HOLD_AFTER_ACCEPT);
    }
    closeTab(p);
  }

  function reject(p) {
    if (p.answered) return;
    answer(p, { accept: false });
    closeTab(p);
  }

  function closeTab(p) {
    if (!p.tab.closed) Tabs.close(p.tab, { force: true });
    else dispose(p);
  }

  // the tab closed: by an answer, by the server, or by the user (which is a rejection)
  function dispose(p) {
    if (!p.answered) answer(p, { accept: false });
    if (p.release) { p.release(); p.release = null; }
    if (p.mv) { try { p.mv.editor().swapDoc(new CodeMirror.Doc('')); } catch {} }
    p.mv = null;
    shown.delete(p.id);
  }

  // withdrawn by the server: close without answering
  function close(id) {
    id = String(id);
    done.add(id);
    const p = shown.get(id);
    if (!p) return;
    p.answered = true;
    closeTab(p);
  }

  // the server waits on these: the others shown here it knows nothing of (an md-editor that
  // was restarted numbers its proposals afresh), so they close without an answer
  function sync(ids) {
    const live = new Set(ids.map(String));
    for (const id of [...shown.keys()]) if (!live.has(id)) close(id);
  }

  return { open, close, sync, list: () => [...shown.values()].map(p => ({ id: p.id, path: p.path, file: p.abs })) };
})();
window.Proposals = Proposals;
