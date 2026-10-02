// MD Collaborative Editor — editor, preview, file sync and the Claude panel.

const $ = sel => document.querySelector(sel);
const esc = MD.esc;
const store = {
  get(k, d) { try { return localStorage.getItem(k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch {} },
};

// ------------------------------------------------------------------ state

// The file in the editor below (the active tab, when it is a file tab). tabs.js owns the open
// files: one model (and CodeMirror Doc) per file, swapped in when its tab is activated.
const cur = {
  get model() { const t = Tabs.active(); return t && t.type === 'file' ? t.model : null; },
  get path() { return cur.model?.path || null; },
  get dirty() { return !!cur.model?.dirty; },
  get md() { return cur.model?.category === 'md'; },
};
MD.setDocPath(() => cur.path || '');
let blocks = [];          // [{el, s, e}] from the last render
let blockLines = [];      // [{el, line}] for scroll sync
let skills = [];
// Ask Claude sends its questions to the Claude terminal, so it runs when that does
let askConf = { available: false, reason: '' };

// ------------------------------------------------------------------ editor

const cm = CodeMirror($('#editor'), {
  mode: { name: 'gfm', highlightFormatting: true, fencedCodeBlockHighlighting: true },
  lineWrapping: true,
  indentUnit: 2,
  tabSize: 4,
  extraKeys: {
    Enter: c => c.execCommand(cur.md ? 'newlineAndIndentContinueMarkdownList' : 'newlineAndIndent'),
    'Ctrl-Alt-L': () => sendSelectionToTerminal(), 'Cmd-Alt-L': () => sendSelectionToTerminal(),
    'Ctrl-B': () => cmd('bold'), 'Cmd-B': () => cmd('bold'),
    'Ctrl-I': () => cmd('italic'), 'Cmd-I': () => cmd('italic'),
    'Ctrl-K': () => cmd('link'), 'Cmd-K': () => cmd('link'),
    'Ctrl-S': () => save(), 'Cmd-S': () => save(),
    'Ctrl-O': () => openBrowser(), 'Cmd-O': () => openBrowser(),
    'Ctrl-J': () => openAsk('editor', true), 'Cmd-J': () => openAsk('editor', true),
    Tab: c => c.somethingSelected() ? c.indentSelection('add') : c.execCommand(c.getOption('indentWithTabs') ? 'insertTab' : 'insertSoftTab'),
    'Shift-Tab': c => c.indentSelection('subtract'),
  },
});
Tabs.init(cm);

// "@path#L3-7 " into the Claude terminal for the editor's selection (Ctrl/Cmd+Alt+L)
function sendSelectionToTerminal() {
  if (!cur.model) return;
  const from = cm.getCursor('from'), to = cm.getCursor('to');
  let last = to.line;
  if (to.ch === 0 && to.line > from.line) last--;   // a selection of whole lines ends at the next line's start
  UI.sendToTerminal(cur.path, cm.somethingSelected() ? { from: from.line + 1, to: last + 1 } : null);
}

function cmd(name) {
  if (!cur.md && !['undo', 'redo', 'ask'].includes(name)) return;   // markdown formatting
  if (cm.isReadOnly() && name !== 'ask') return;   // a file Claude has proposed a change to
  const doc = cm.getDoc();
  const sel = doc.getSelection();
  const wrap = (a, b, ph) => {
    const text = sel || ph;
    doc.replaceSelection(a + text + b);
    if (!sel) {
      const c = doc.getCursor();
      doc.setSelection({ line: c.line, ch: c.ch - b.length - text.length }, { line: c.line, ch: c.ch - b.length });
    }
  };
  const eachLine = fn => {
    const { from, to } = { from: doc.getCursor('from'), to: doc.getCursor('to') };
    const last = to.ch === 0 && to.line > from.line ? to.line - 1 : to.line;
    cm.operation(() => {
      for (let l = from.line, k = 0; l <= last; l++, k++) {
        const t = doc.getLine(l);
        doc.replaceRange(fn(t, k), { line: l, ch: 0 }, { line: l, ch: t.length });
      }
    });
  };
  const toggle = (re, prefix) => eachLine((t, k) => re.test(t) ? t.replace(re, '') : (typeof prefix === 'function' ? prefix(k) : prefix) + t);
  switch (name) {
    case 'bold': wrap('**', '**', 'bold text'); break;
    case 'italic': wrap('_', '_', 'italic text'); break;
    case 'strike': wrap('~~', '~~', 'struck text'); break;
    case 'code': wrap('`', '`', 'code'); break;
    case 'codeblock': wrap('```\n', '\n```', 'code'); break;
    case 'link': {
      doc.replaceSelection(`[${sel || 'link text'}](url)`);
      const c = doc.getCursor();
      doc.setSelection({ line: c.line, ch: c.ch - 4 }, { line: c.line, ch: c.ch - 1 });
      break;
    }
    case 'image': doc.replaceSelection(`![${sel || 'alt text'}](image.png)`); break;
    case 'h1': eachLine(t => {
      const m = /^(#{1,6}) /.exec(t);
      if (!m) return '# ' + t;
      return m[1].length >= 3 ? t.slice(m[0].length) : '#' + t;
    }); break;
    case 'quote': toggle(/^> ?/, '> '); break;
    case 'ul': toggle(/^\s*[-*+] (?!\[)/, '- '); break;
    case 'ol': toggle(/^\s*\d+[.)] /, k => `${k + 1}. `); break;
    case 'task': toggle(/^\s*[-*+] \[[ xX]\] /, '- [ ] '); break;
    case 'hr': doc.replaceSelection('\n\n---\n\n'); break;
    case 'table': doc.replaceSelection('\n| Column 1 | Column 2 | Column 3 |\n| --- | --- | --- |\n| a | b | c |\n| d | e | f |\n'); break;
    case 'undo': cm.undo(); break;
    case 'redo': cm.redo(); break;
    case 'ask': openAsk(lastSelSource, true); return;
  }
  cm.focus();
}

// Spell checking (spell.js): misspelt words get a wavy underline; code, URLs and HTML are skipped.
// On by default for markdown and off for other files, remembered separately for each.
const spellKey = () => cur.model && !cur.md ? 'mdedit.spell.other' : 'mdedit.spell';
const spellPref = () => store.get(spellKey(), spellKey() === 'mdedit.spell' ? '1' : '0') === '1';
let spellOn = spellPref();
const NO_SPELL = '.cm-comment, .cm-url, .cm-string, .cm-tag, .cm-attribute, .cm-formatting-code-block';
cm.on('renderLine', (c, line, el) => {
  if (!spellOn || !el.querySelector('.cm-spell-error')) return;
  if (!cur.md) {   // code: only comments and strings are prose
    for (const s of el.querySelectorAll('.cm-spell-error')) if (!/\bcm-(comment|string)\b/.test(s.className)) s.classList.add('nospell');
    return;
  }
  const st = c.getStateAfter(c.getLineNumber(line) - 1, true);
  const md = st && (st.base || st);   // gfm wraps the markdown state in an overlay
  if (md && (md.code || md.localMode || md.fencedEndRE) || el.querySelector('.cm-formatting-code-block')) {
    el.classList.add('nospell');      // inside a fenced or indented code block
  } else {
    for (const s of el.querySelectorAll(NO_SPELL)) s.classList.add('nospell');
  }
});
function refreshSpell() {
  cm.removeOverlay('spell');
  if (spellOn) cm.addOverlay(SPELL.overlay);
}
function setSpell(on) {
  spellOn = on;
  $('#spell').classList.toggle('on', on);
  store.set(spellKey(), on ? '1' : '0');
  refreshSpell();
}
$('#spell').classList.toggle('on', spellOn);
$('#spell').onclick = () => setSpell(!spellOn);
SPELL.ready.then(refreshSpell, err => {
  console.warn('Spell check unavailable:', err);
  $('#spell').title = 'Spell check unavailable: the dictionary could not be loaded';
  $('#spell').disabled = true;
});

// Right-click a misspelt word for suggestions, "Add to dictionary" or "Ignore".
const spellMenu = document.createElement('div');
spellMenu.id = 'spell-menu';
spellMenu.hidden = true;
document.body.append(spellMenu);
const hideSpellMenu = () => { spellMenu.hidden = true; };

cm.getWrapperElement().addEventListener('contextmenu', e => {
  const hit = e.target.closest('.cm-spell-error');
  if (!spellOn || !hit || hit.closest('.nospell')) return;
  const pos = cm.coordsChar({ left: e.clientX, top: e.clientY }, 'window');
  const w = SPELL.wordAt(cm.getLine(pos.line), pos.ch);
  if (!w || SPELL.ok(w.text)) return;
  e.preventDefault();
  const from = { line: pos.line, ch: w.from }, to = { line: pos.line, ch: w.to };
  const act = fn => () => { hideSpellMenu(); fn(); refreshSpell(); cm.focus(); };
  const item = (label, fn, cls) => {
    const b = document.createElement('button');
    b.textContent = label;
    if (cls) b.className = cls;
    b.onclick = act(fn);
    return b;
  };
  const sugg = document.createElement('div');
  sugg.innerHTML = '<span class="muted">Finding suggestions…</span>';
  spellMenu.replaceChildren(sugg, document.createElement('hr'),
    item(`Add “${w.text}” to dictionary`, () => SPELL.add(w.text)),
    item('Ignore', () => SPELL.ignore(w.text)));
  spellMenu.style.left = Math.min(e.clientX, innerWidth - 260) + 'px';
  spellMenu.style.top = Math.min(e.clientY, innerHeight - 220) + 'px';
  spellMenu.hidden = false;
  SPELL.suggest(w.text).then(list => {
    if (spellMenu.hidden || !sugg.isConnected) return;
    sugg.replaceChildren(...(list.length ? list.map(s => item(s, () => {
      if (!cm.isReadOnly() && cm.getRange(from, to) === w.text) cm.replaceRange(s, from, to, '+spell');
    }, 'sugg')) : [Object.assign(document.createElement('span'), { className: 'muted', textContent: 'No suggestions' })]));
  });
});
document.addEventListener('mousedown', e => { if (!spellMenu.contains(e.target)) hideSpellMenu(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') hideSpellMenu(); });
cm.on('scroll', hideSpellMenu);

$('#toolbar').addEventListener('mousedown', e => e.preventDefault());   // keep editor selection
$('#toolbar').addEventListener('click', e => {
  const b = e.target.closest('button[data-cmd]');
  if (b) cmd(b.dataset.cmd);
});

// ------------------------------------------------------------------ rendering

let renderT;
function scheduleRender() { clearTimeout(renderT); renderT = setTimeout(renderPreview, 120); }

function renderPreview() {
  const pane = $('#preview-pane');
  if (!cur.md) {   // only markdown has a preview
    clearTimeout(renderT);
    blocks = [];
    blockLines = [];
    $('#preview').replaceChildren();
    updateStats();
    return;
  }
  const top = pane.scrollTop;
  blocks = MD.render(cm.getValue(), $('#preview'));
  blockLines = blocks.filter(b => b.s >= 0).map(b => ({ el: b.el, line: cm.posFromIndex(b.s).line }));
  pane.scrollTop = top;
  updateStats();
}

function updateStats() {
  if (!cur.model) { $('#stat-words').textContent = ''; $('#stat-cursor').textContent = ''; return; }
  if (!cur.md) {
    $('#stat-words').textContent = `${cm.lineCount().toLocaleString()} lines · ${cm.getValue().length.toLocaleString()} chars`;
    return;
  }
  const text = cm.getValue();
  const words = (text.match(/[\p{L}\p{N}'’-]+/gu) || []).length;
  $('#stat-words').textContent = `${words.toLocaleString()} words · ${text.length.toLocaleString()} chars`;
}
cm.on('cursorActivity', () => {
  const c = cm.getCursor();
  $('#stat-cursor').textContent = `Ln ${c.line + 1}, Col ${c.ch + 1}`;
  trackSelection();
});

// What is on screen, for Claude (Term.select; over the IDE link Claude Code shows "⧉ 3 lines
// selected" and attaches the lines to the next prompt): the file tab's selection (a preview
// selection is set as the editor's), or just the file. scm.js reports its diff editors.
const lspPos = p => ({ line: p.line, character: p.ch });
function editorSelection(model = cur.model) {
  if (!model) return null;
  const doc = model.doc;
  const from = doc.getCursor('from'), to = doc.getCursor('to');
  return { path: model.path, start: lspPos(from), end: lspPos(to), text: doc.getRange(from, to) };
}
function trackSelection() {
  const t = Tabs.active();
  if (!t) return;
  if (t.type === 'file') Term.select(editorSelection(t.model));
  else if (t.type !== 'custom' && t.path) Term.select({ path: t.path, start: { line: 0, character: 0 }, end: { line: 0, character: 0 }, text: '' });
}

// task-list checkboxes toggle the source
$('#preview').addEventListener('change', e => {
  const b = e.target;
  if (b.type !== 'checkbox' || b.dataset.off === undefined) return;
  if (cm.isReadOnly()) { b.checked = !b.checked; return; }   // a file Claude has proposed a change to
  const p = cm.posFromIndex(+b.dataset.off);
  cm.replaceRange(b.checked ? 'x' : ' ', p, { line: p.line, ch: p.ch + 1 }, '+task');
});

// ------------------------------------------------------------------ scroll sync

let scrollLead = 'editor';
$('#editor-pane').addEventListener('mouseenter', () => { scrollLead = 'editor'; });
$('#preview-pane').addEventListener('mouseenter', () => { scrollLead = 'preview'; });

cm.on('scroll', () => {
  if (scrollLead !== 'editor' || !blockLines.length) return;
  const info = cm.getScrollInfo();
  const pane = $('#preview-pane');
  if (info.top + info.clientHeight >= info.height - 4) { pane.scrollTop = pane.scrollHeight; return; }
  const topLine = cm.lineAtHeight(info.top, 'local');
  let i = 0;
  while (i + 1 < blockLines.length && blockLines[i + 1].line <= topLine) i++;
  const a = blockLines[i], b = blockLines[i + 1];
  const ya = cm.heightAtLine(a.line, 'local');
  const yb = b ? cm.heightAtLine(b.line, 'local') : cm.getScrollInfo().height;
  const frac = Math.max(0, Math.min(1, (info.top - ya) / Math.max(1, yb - ya)));
  const pa = a.el.offsetTop, pb = b ? b.el.offsetTop : pane.scrollHeight;
  pane.scrollTop = pa + frac * (pb - pa) - 16;
});

$('#preview-pane').addEventListener('scroll', () => {
  if (scrollLead !== 'preview' || !blockLines.length) return;
  const pane = $('#preview-pane');
  const y = pane.scrollTop + 16;
  let i = 0;
  while (i + 1 < blockLines.length && blockLines[i + 1].el.offsetTop <= y) i++;
  const a = blockLines[i], b = blockLines[i + 1];
  const pa = a.el.offsetTop, pb = b ? b.el.offsetTop : pane.scrollHeight;
  const frac = Math.max(0, Math.min(1, (y - pa) / Math.max(1, pb - pa)));
  const ya = cm.heightAtLine(a.line, 'local');
  const yb = b ? cm.heightAtLine(b.line, 'local') : cm.getScrollInfo().height;
  cm.scrollTo(null, ya + frac * (yb - ya));
});

// ------------------------------------------------------------------ files & saving
// Opening, saving, autosave and on-disk changes live in tabs.js; the tree in explorer.js.

async function api(method, url, body) {
  const r = await fetch(url, {
    method, headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) { const err = new Error(data.error || r.statusText); err.status = r.status; err.data = data; throw err; }
  return data;
}

function setSaveState(text, cls = '') { Tabs.setStatus(text, cls); }
const save = (force = false) => Tabs.save(undefined, force);

cm.on('change', () => scheduleRender());

let fileList = [];   // the markdown files (/api/config, /api/root): candidates for a first file

// Nothing remembered or asked for: open a markdown file, as the editor always has.
async function openFirst() {
  const want = [store.get('mdedit.last', null), fileList[0]?.path].find(p => p && fileList.some(f => f.path === p));
  if (want) return Tabs.open(want, { preview: false });
  // a new, empty folder gets an untitled.md to start with
  const top = await api('GET', '/api/tree?dir=&all=0').catch(() => null);
  if (top && Array.isArray(top.entries) && !top.entries.length) {
    const r = await api('POST', '/api/new', { path: 'untitled.md', text: '# Untitled\n\n' }).catch(() => null);
    if (r) return Tabs.open(r.path, { preview: false });
  }
  return null;
}

// The active tab changed (or its file was renamed): fit the layout to its kind.
Tabs.on('deactivate', t => {
  if (t && t.type === 'file' && t.model.category === 'md') t.model.previewTop = $('#preview-pane').scrollTop;
});
Tabs.on('activate', t => {
  const md = cur.md;
  for (const b of document.querySelectorAll('#view-mode button')) {
    b.disabled = !md;
    b.title = md ? '' : 'The preview is only for markdown files';
  }
  const pdf = $('#export-pdf');
  pdf.disabled = !md;
  pdf.title = md ? 'Export as PDF: saved next to the .md file and downloaded (the browser\'s Print prints the preview)' : 'PDF export is only for markdown files';
  const askBtn = $('#toolbar [data-cmd="ask"]');
  askBtn.disabled = !askConf.available || !cur.model;
  if (spellPref() !== spellOn) {   // the overlay belongs to the editor, so it survives swapDoc
    spellOn = !spellOn;
    $('#spell').classList.toggle('on', spellOn);
    refreshSpell();
  }
  closeAsk();
  pill.hidden = true;
  previewRange = null;
  if (cur.model && $('#main').classList.contains('preview')) cm.refresh();   // the editor was hidden
  renderPreview();
  if (md) $('#preview-pane').scrollTop = cur.model.previewTop || 0;
  if (cur.model) { const c = cm.getCursor(); $('#stat-cursor').textContent = `Ln ${c.line + 1}, Col ${c.ch + 1}`; }
  trackSelection();
});

// ------------------------------------------------------------------ selection → source range

let lastSelSource = 'editor';
let previewRange = null;   // {from, to} source indices from the last preview selection

function wordsOf(s) { return s.match(/[\p{L}\p{N}]+/gu) || []; }
function reEsc(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

// Map the current DOM selection inside the preview to a source range.
function previewSelectionToSource() {
  const sel = window.getSelection();
  if (!sel.rangeCount || sel.isCollapsed) return null;
  const r = sel.getRangeAt(0);
  if (!$('#preview').contains(r.commonAncestorContainer)) return null;
  const blockOf = n => (n.nodeType === 1 ? n : n.parentElement)?.closest('[data-s]');
  let a = blockOf(r.startContainer), b = blockOf(r.endContainer);
  if (!a || !b) return null;
  let s = +a.dataset.s, e = +b.dataset.e;
  if (s < 0 || e < 0 || e < s) return null;
  const src = cm.getValue();
  const chunk = src.slice(s, e);
  // Narrow the block range to the selected words, allowing markdown syntax between them.
  const words = wordsOf(sel.toString());
  if (words.length) {
    const tries = [words.map(reEsc).join('[^\\p{L}\\p{N}]+'), words.map(reEsc).join('[\\s\\S]*?')];
    for (const pat of tries) {
      const m = new RegExp(pat, 'u').exec(chunk);
      if (m) {
        let ms = s + m.index, me = ms + m[0].length;
        // pull in emphasis/code markers hugging the ends so markup stays balanced
        while (ms > s && /[*_~`]/.test(src[ms - 1])) ms--;
        while (me < e && /[*_~`.,;:!?)\]]/.test(src[me])) me++;
        return { from: ms, to: me };
      }
    }
  }
  while (e > s && /\s/.test(src[e - 1])) e--;
  return { from: s, to: e };
}

$('#preview-pane').addEventListener('mouseup', () => {
  setTimeout(() => {
    const r = previewSelectionToSource();
    if (!r || r.to <= r.from) return;
    previewRange = r;
    lastSelSource = 'preview';
    cm.getInputField().blur();   // a focused editor would re-read its input field as typing
    cm.setSelection(cm.posFromIndex(r.from), cm.posFromIndex(r.to));
    if (cm.getWrapperElement().offsetParent) cm.scrollIntoView({ from: cm.posFromIndex(r.from), to: cm.posFromIndex(r.to) }, 60);
    openAsk('preview', false);
  }, 0);
});

// Click (not drag) in the preview moves the editor cursor to that block.
$('#preview').addEventListener('click', e => {
  if (!window.getSelection().isCollapsed || e.target.closest('a,input')) return;
  const el = e.target.closest('[data-s]');
  if (!el || +el.dataset.s < 0 || !$('#main').classList.contains('split')) return;
  const p = cm.posFromIndex(+el.dataset.s);
  cm.setCursor(p);
  cm.scrollIntoView(p, 80);
});

// A small pill offers Claude when text is mouse-selected in the editor.
const pill = Object.assign(document.createElement('button'), { id: 'ask-pill', textContent: '✦ Ask Claude', hidden: true });
document.body.appendChild(pill);
pill.addEventListener('mousedown', e => e.preventDefault());
pill.onclick = () => { pill.hidden = true; openAsk('editor', true); };
cm.getWrapperElement().addEventListener('mouseup', () => setTimeout(() => {
  lastSelSource = 'editor';
  if (!cm.somethingSelected() || !askConf.available) { pill.hidden = true; return; }
  const c = cm.cursorCoords(cm.getCursor('to'), 'window');
  placeFloating(pill, c.left, c.bottom + 6);
  pill.hidden = false;
}, 0));
cm.on('keydown', () => { pill.hidden = true; });
cm.on('blur', () => setTimeout(() => { if (document.activeElement !== pill) pill.hidden = true; }, 150));

function placeFloating(el, x, y) {
  el.style.left = '0px'; el.style.top = '0px';
  const w = el.offsetWidth || 300, h = el.offsetHeight || 40;
  el.style.left = Math.max(8, Math.min(x, innerWidth - w - 8)) + 'px';
  el.style.top = (y + h > innerHeight - 8 ? Math.max(8, y - h - 40) : y) + 'px';
}

// ------------------------------------------------------------------ ask bar
// A question goes to the Claude session in the side panel (Term.ask), about the selection or,
// with nothing selected, the whole file; Claude answers there and makes any edit itself.

const PRESETS = [
  { label: 'Improve', instr: 'Rewrite this so it reads better: clearer and more fluent, with the same meaning and roughly the same length.' },
  { label: 'Tighten', instr: 'Make this more concise. Cut redundancy and filler without losing any meaning.' },
  { label: 'Expand', instr: 'Expand this with more detail and explanation, in the same style.' },
  { label: 'Simplify', instr: 'Rewrite this in plainer language for a non-specialist reader.' },
  { label: 'Fix grammar', instr: 'Fix spelling, grammar and punctuation only. Change nothing else.' },
  { label: 'More formal', instr: 'Rewrite this in a more formal, professional register.' },
  { label: 'More casual', instr: 'Rewrite this in a friendlier, more conversational tone.' },
  { label: 'To bullets', instr: 'Restructure this as a markdown bulleted list.' },
  { label: 'To prose', instr: 'Rewrite this as flowing prose paragraphs.' },
  { label: 'Critique', instr: 'Critique this passage: clarity, structure, argument and style. Be specific and brief, and change nothing.' },
];

let askTarget = null;   // {from, to, whole, model} captured when the bar opened

function currentRange(source) {
  if (source === 'preview' && previewRange) return previewRange;
  if (cm.somethingSelected()) return { from: cm.indexFromPos(cm.getCursor('from')), to: cm.indexFromPos(cm.getCursor('to')) };
  return { from: 0, to: cm.getValue().length, whole: true };
}

function buildPresets() {
  const box = $('#ask-presets');
  box.replaceChildren();
  const all = [
    ...skills.map(s => ({
      label: /writing-style|voice/.test(s.name) ? '✎ My style' : `Skill: ${s.name}`,
      title: s.description,
      instr: `Rewrite this using the \`${s.name}\` skill.`,
      cls: 'skill',
    })),
    ...PRESETS,
  ];
  for (const p of all) {
    const b = Object.assign(document.createElement('button'), { type: 'button', textContent: p.label, title: p.title || p.instr });
    if (p.cls) b.classList.add(p.cls);
    b.onclick = () => submitAsk(p.instr);
    box.appendChild(b);
  }
}

// Ask Claude can't run (the terminal is off, see askConf.reason): show the panel, which says why.
function showAskUnavailable() {
  App.showTerminal();
  UI.toast(`Ask Claude needs the Claude terminal, which is unavailable: ${askConf.reason || 'unknown reason.'}`, { kind: 'err', ms: 6000 });
}

function openAsk(source, focus) {
  if (!askConf.available) { if (focus) showAskUnavailable(); return; }
  if (!cur.model) return;
  askTarget = Object.assign(currentRange(source), { model: cur.model });
  if (source !== 'preview') previewRange = null;
  const bar = $('#askbar');
  bar.hidden = false;
  bar.classList.toggle('whole', !!askTarget.whole);
  $('#ask-input').placeholder = askTarget.whole
    ? 'Nothing selected: ask Claude about the whole file… (Enter sends it to the Claude terminal)'
    : 'Ask Claude… (Enter sends it to the Claude terminal, Esc closes)';
  let x, y;
  const sel = window.getSelection();
  if (source === 'preview' && sel.rangeCount && !sel.isCollapsed) {
    const rect = sel.getRangeAt(0).getBoundingClientRect();
    x = rect.left; y = rect.bottom + 8;
  } else if (!askTarget.whole) {
    const c = cm.cursorCoords(cm.posFromIndex(askTarget.to), 'window');
    x = c.left; y = c.bottom + 8;
  } else {
    const r = $('#editor-pane').getBoundingClientRect();
    x = r.left + 40; y = r.top + 60;
  }
  placeFloating(bar, x, y);
  pill.hidden = true;
  if (focus) $('#ask-input').focus();
}

function closeAsk() { $('#askbar').hidden = true; askTarget = null; }

$('#ask-form').onsubmit = e => {
  e.preventDefault();
  const v = $('#ask-input').value.trim();
  if (!v) return;
  submitAsk(v);
  $('#ask-input').value = '';
};
$('#ask-input').addEventListener('keydown', e => { if (e.key === 'Escape') closeAsk(); });
document.addEventListener('mousedown', e => {
  if (!$('#askbar').hidden && !e.target.closest('#askbar')) closeAsk();
});
document.addEventListener('keydown', e => {
  if (e.target instanceof Element && e.target.closest('#term')) return;   // Claude Code uses these keys
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'j' && !cm.hasFocus()) { e.preventDefault(); openAsk(lastSelSource, true); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's' && !cm.hasFocus()) { e.preventDefault(); save(); }
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'o' && !cm.hasFocus()) { e.preventDefault(); openBrowser(); }
  if (e.key === 'Escape' && !$('#askbar').hidden) closeAsk();
});

// The question goes to the Claude session, with the selection (or the whole file). A file with
// unsaved changes is saved first, so Claude reads what is on screen.
async function submitAsk(instr) {
  if (!askTarget) return;
  const t = askTarget;
  closeAsk();
  if (!cm.hasFocus()) window.getSelection().removeAllRanges();
  const m = t.model;
  if (!m || Tabs.model(m.path) !== m) return;
  const name = UI.basename(m.path);
  if (m.dirty) {
    if (await Tabs.save(m)) UI.toast(`Saved ${name} first, so that Claude reads what you see.`, { kind: 'ok' });
    else UI.toast(`${name} could not be saved: Claude will read the version on disk.`, { kind: 'err' });
  }
  const doc = m.doc;
  const len = doc.getValue().length;
  const from = doc.posFromIndex(Math.min(t.from, len)), to = doc.posFromIndex(Math.min(t.to, len));
  const sel = t.whole
    ? { path: m.path, start: lspPos(doc.getCursor()), end: lspPos(doc.getCursor()), text: '' }
    : { path: m.path, start: lspPos(from), end: lspPos(to), text: doc.getRange(from, to) };
  Term.ask(instr, sel);
}

// ------------------------------------------------------------------ file browser

let rootDir = '';
let browseState = null;   // last /api/browse result
let browseSel = -1;

async function openBrowser() {
  $('#browser').hidden = false;
  await browseTo(browseState?.dir || rootDir);
  $('#browser-path').focus();
  $('#browser-path').select();
}
function closeBrowser() { $('#browser').hidden = true; cm.focus(); }

async function browseTo(dir) {
  const msg = $('#browser-msg');
  try {
    browseState = await api('GET', `/api/browse?dir=${UI.encPath(dir)}`);
  } catch (e) {
    msg.textContent = e.message;
    msg.className = 'err';
    return;
  }
  msg.textContent = 'Click a folder to enter it; click a .md file to open it.';
  msg.className = 'muted';
  $('#browser-path').value = browseState.dir;
  $('#browser-up').disabled = !browseState.parent;
  browseSel = -1;
  const ul = $('#browser-list');
  ul.replaceChildren();
  if (!browseState.entries.length) {
    ul.innerHTML = '<li class="empty">No folders or markdown files here.</li>';
    return;
  }
  browseState.entries.forEach((en, i) => {
    const li = document.createElement('li');
    li.className = en.dir ? 'dir' : 'md';
    li.innerHTML = `<span class="ico">${en.dir ? '📁' : '📄'}</span>${esc(en.name)}`;
    li.title = en.name;
    li.onclick = () => chooseEntry(i);
    ul.appendChild(li);
  });
}

function joinPath(dir, name) { return dir.replace(/\/+$/, '') + '/' + name; }

function chooseEntry(i) {
  const en = browseState.entries[i];
  const full = joinPath(browseState.dir, en.name);
  if (en.dir) browseTo(full);
  else switchRoot(full);
}

function selectEntry(i) {
  const items = [...document.querySelectorAll('#browser-list li:not(.empty)')];
  if (!items.length) return;
  browseSel = Math.max(0, Math.min(items.length - 1, i));
  items.forEach((li, k) => li.classList.toggle('sel', k === browseSel));
  items[browseSel].scrollIntoView({ block: 'nearest' });
}

// Point the server at a folder, or at a file's folder, then open the file.
// Unsaved files are saved or dropped first (paths are relative to the root), then the tabs
// of the old root are closed and those remembered for the new one reopened.
async function switchRoot(path) {
  if (!(await Tabs.confirmLeave('Save your changes before opening another folder?'))) return;
  UI.stat.bump();   // drop polls of the old root's files
  let r;
  try { r = await api('POST', '/api/root', { path }); }
  catch (e) { $('#browser-msg').textContent = e.message; $('#browser-msg').className = 'err'; return; }
  $('#browser').hidden = true;
  UI.stat.bump();
  Tabs.closeAll();
  setRoot(r.root);
  fileList = r.files;
  await Tabs.restore();
  if (r.initial) await Tabs.open(r.initial, { preview: false });
  else if (!Tabs.active()) await openFirst();
  if (cur.model) cm.focus();
}

function setRoot(root) {
  rootDir = root;
  $('#file-root').textContent = root;
  $('#stat-root').textContent = root;
  Term.onRootChange(root);
  UI.emit('root', root);
}

$('#open-browse').onclick = openBrowser;
$('#browse-btn').onclick = openBrowser;
$('#browser-close').onclick = closeBrowser;
$('#browser').addEventListener('mousedown', e => { if (e.target.id === 'browser') closeBrowser(); });
$('#browser-up').onclick = () => browseState?.parent && browseTo(browseState.parent);
$('#browser-home').onclick = () => browseTo(browseState?.home || '~');
$('#browser-use-folder').onclick = () => browseState && switchRoot(browseState.dir);
$('#browser-path-form').onsubmit = e => {
  e.preventDefault();
  const v = $('#browser-path').value.trim();
  if (!v) return;
  if (/\.(md|markdown)$/i.test(v)) switchRoot(v);
  else browseTo(v);
};
$('#browser').addEventListener('keydown', e => {
  if (e.key === 'Escape') { e.preventDefault(); closeBrowser(); }
  else if (e.key === 'ArrowDown') { e.preventDefault(); selectEntry(browseSel + 1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); selectEntry(browseSel - 1); }
  else if (e.key === 'Enter' && browseSel >= 0) { e.preventDefault(); chooseEntry(browseSel); }
  else if (e.key === 'Backspace' && e.altKey && browseState?.parent) { e.preventDefault(); browseTo(browseState.parent); }
});

// ------------------------------------------------------------------ PDF export

async function exportPdf() {
  if (!cur.md) return;
  if (cur.dirty) await save();
  const btn = $('#export-pdf');
  btn.disabled = true;
  setSaveState('Exporting PDF…');
  try {
    // render a clean light-theme copy so dark mode stays out of the PDF
    const wasDark = document.documentElement.dataset.theme === 'dark';
    if (wasDark) MD.initMermaid(false);
    const box = document.createElement('div');
    box.style.cssText = 'position:absolute;left:-10000px;top:0;width:800px';
    box.className = 'markdown-body';
    document.body.appendChild(box);
    MD.render(cm.getValue(), box);
    await MD.mermaidDone();
    box.querySelectorAll('[data-s]').forEach(el => { delete el.dataset.s; delete el.dataset.e; });
    box.querySelectorAll('input[type=checkbox]').forEach(b => { b.setAttribute('disabled', ''); if (b.checked) b.setAttribute('checked', ''); });
    const html = box.innerHTML;
    box.remove();
    if (wasDark) { MD.initMermaid(true); renderPreview(); }
    const r = await api('POST', '/api/pdf', { path: cur.path, html });
    setSaveState(`Exported ${r.pdf} (${Math.round(r.bytes / 1024)} KB)`, 'ok');
    const a = Object.assign(document.createElement('a'), { href: `/api/download?path=${UI.encPath(r.pdf)}`, download: r.pdf.split('/').pop() });
    document.body.appendChild(a);
    a.click();
    a.remove();
  } catch (e) {
    setSaveState(`PDF export failed: ${e.message}`, 'err');
  } finally {
    btn.disabled = false;
  }
}
$('#export-pdf').onclick = exportPdf;

// ------------------------------------------------------------------ layout & theme

document.querySelectorAll('#view-mode button').forEach(b => b.onclick = () => setView(b.dataset.mode));
function setView(mode) {
  $('#main').classList.remove('edit', 'split', 'preview');
  $('#main').classList.add(mode);
  document.querySelectorAll('#view-mode button').forEach(b => b.classList.toggle('on', b.dataset.mode === mode));
  store.set('mdedit.view', mode);
  setTimeout(() => { cm.refresh(); renderPreview(); }, 0);
}
$('#toggle-files').onclick = () => Activity.setSidebar(!Activity.sidebarOn());
Activity.onLayout(() => { fitColumns(); cm.refresh(); });
// A narrow window has room for one overlay: showing this panel hides the side bar (and
// activity.js does the reverse).
function showClaudePanel() {
  document.body.classList.remove('no-claude');
  if (matchMedia('(max-width: 800px)').matches && Activity.sidebarOn()) Activity.setSidebar(false, false);
  fitColumns();
}
$('#toggle-claude').onclick = () => {
  if (document.body.classList.contains('no-claude')) showClaudePanel();
  else document.body.classList.add('no-claude');
  cm.refresh();
  Term.visible();
};

function setTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  $('#gh-css').href = `https://cdnjs.cloudflare.com/ajax/libs/github-markdown-css/5.5.1/github-markdown-${dark ? 'dark' : 'light'}.min.css`;
  $('#hl-css').href = `https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github${dark ? '-dark' : ''}.min.css`;
  MD.initMermaid(dark);
  Term.setTheme(dark);
  store.set('mdedit.theme', dark ? 'dark' : 'light');
  renderPreview();
  UI.emit('theme', dark);
}
$('#theme').onclick = () => setTheme(document.documentElement.dataset.theme !== 'dark');

// unsaved files: autosaved ones are saved now, and the browser asks before leaving
window.addEventListener('beforeunload', e => {
  if (!Tabs.anyDirty()) return;
  for (const m of Tabs.models()) if (m.dirty && Tabs.autosave(m.category)) Tabs.save(m, false, true);   // not deleted ones
  e.preventDefault();
  e.returnValue = '';
});

// ------------------------------------------------------------------ Claude panel: the terminal and its width

const panel = $('#claude');

// for the other modules (term.js: Ask Claude and Send to Claude terminal show the panel)
window.App = {
  cm,
  showTerminal() {
    showClaudePanel();
    cm.refresh();
    Term.visible();
  },
};

const PANEL_MIN = 280;
const EDITOR_MIN = 240;   // what the Claude panel and the side bar always leave the editor
const defaultPanelWidth = () => Math.max(360, Math.min(innerWidth * 0.4, 640));
function setPanelWidth(w, persist) {
  // a narrow window shows the side bar or the panel over the editor, one at a time
  const side = !matchMedia('(max-width: 800px)').matches && Activity.sidebarOn() ? $('#sidebar').getBoundingClientRect().width : 0;
  const max = Math.min(innerWidth * 0.75, innerWidth - $('#activity-bar').getBoundingClientRect().width - side - EDITOR_MIN);
  w = Math.round(Math.max(PANEL_MIN, Math.min(max, w)));
  panel.style.width = w + 'px';
  if (persist) store.set('mdedit.panelWidth', String(w));
}
const applyPanelWidth = () => setPanelWidth(+store.get('mdedit.panelWidth', 0) || defaultPanelWidth());
// the remembered widths, as far as the window has room: the panel's first, then the side bar's
function fitColumns() {
  applyPanelWidth();
  Activity.fit();
}
applyPanelWidth();
window.addEventListener('resize', applyPanelWidth);

// Drag the panel's left edge to resize it; the terminal is refitted once, on release.
$('#panel-resize').addEventListener('pointerdown', e => {
  if (e.button !== 0) return;
  e.preventDefault();
  const handle = e.currentTarget;
  const right = panel.getBoundingClientRect().right;
  handle.setPointerCapture(e.pointerId);
  document.body.classList.add('panel-resizing');
  const move = ev => setPanelWidth(right - ev.clientX);
  const up = () => {
    handle.removeEventListener('pointermove', move);
    handle.removeEventListener('pointerup', up);
    handle.removeEventListener('pointercancel', up);
    document.body.classList.remove('panel-resizing');
    setPanelWidth(panel.getBoundingClientRect().width, true);
    cm.refresh();
    Term.fit();
  };
  handle.addEventListener('pointermove', move);
  handle.addEventListener('pointerup', up);
  handle.addEventListener('pointercancel', up);
});
$('#panel-resize').addEventListener('dblclick', () => {
  store.set('mdedit.panelWidth', '');
  applyPanelWidth();
  cm.refresh();
});

// ------------------------------------------------------------------ start

(async function init() {
  const saved = store.get('mdedit.theme', null);
  setTheme(saved ? saved === 'dark' : matchMedia('(prefers-color-scheme: dark)').matches);
  setView(store.get('mdedit.view', innerWidth < 800 ? 'edit' : 'split'));
  if (innerWidth < 1100) document.body.classList.add('no-claude');   // activity.js decides the side bar
  fitColumns();

  const conf = await api('GET', '/api/config');
  skills = conf.skills || [];
  fileList = conf.files;
  Term.init(conf.agent);
  const agentOk = !!conf.agent?.available;
  askConf = { available: agentOk, reason: conf.agent?.reason || 'this md-editor server has no terminal support.' };
  const askBtn = $('#toolbar [data-cmd="ask"]');
  askBtn.disabled = !askConf.available || !cur.model;
  askBtn.title = askConf.available ? 'Ask Claude about the selection, in the Claude terminal (Ctrl+J)' : `Ask Claude unavailable: ${askConf.reason}`;
  $('#panel-title').title = agentOk ? `Claude Code (${conf.agent.cmd}) in the open folder` : 'Claude Code terminal (unavailable)';
  setRoot(conf.root);
  Term.visible();   // starts the terminal if the panel is on screen
  buildPresets();
  // read before the remembered tabs come back: activating one rewrites the URL
  let fromHash = '';
  try { fromHash = UI.decPath(location.hash.slice(1)); } catch {}
  // the file named on the command line, once per browser tab (a reload keeps the tab on screen)
  let initial = conf.initial;
  try {
    const k = 'mdedit.initial:' + conf.root;
    if (initial && sessionStorage.getItem(k) === initial) initial = null;
    else if (initial) sessionStorage.setItem(k, initial);
  } catch {}
  await Tabs.restore();   // the tabs remembered for this folder
  for (const p of [initial, fromHash]) {   // pinned, unless it is already open
    if (p && await Tabs.open(p, { preview: Tabs.list().some(t => t.path === p) })) break;
  }
  if (!Tabs.active()) await openFirst();
  UI.emit('ready');
  if (cur.model) cm.focus();
})();
