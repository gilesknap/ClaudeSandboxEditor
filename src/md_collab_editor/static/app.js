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
let cards = [];           // Ask Claude cards, each bound to its file's model (c.model)
let cardSeq = 0;
let skills = [];
let askConf = { available: false, reason: '' };   // /api/config's "ask": can Ask Claude run?

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
      if (cm.getRange(from, to) === w.text) cm.replaceRange(s, from, to, '+spell');
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
  highlightPreviewCards();
  updateStats();
}

function highlightPreviewCards() {
  for (const b of blocks) b.el.classList.remove('claude-hl', 'claude-hl-ready');
  for (const c of cards) {
    if (c.model !== cur.model) continue;
    const r = c.marker?.find();
    if (!r || !['pending', 'ready'].includes(c.status)) continue;
    const s = cm.indexFromPos(r.from), e = cm.indexFromPos(r.to);
    for (const b of blocks) {
      if (b.s >= 0 && b.s < e && b.e > s) b.el.classList.add(c.status === 'pending' ? 'claude-hl' : 'claude-hl-ready');
    }
  }
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
});

// task-list checkboxes toggle the source
$('#preview').addEventListener('change', e => {
  const b = e.target;
  if (b.type !== 'checkbox' || b.dataset.off === undefined) return;
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
  if (pill.hidden) prepareAsk();
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
  { label: 'Critique', mode: 'comment', instr: 'Critique this passage: clarity, structure, argument and style. Be specific and brief.' },
];

let askTarget = null;   // {from, to} captured when the bar opened

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
    if (p.mode === 'comment') b.classList.add('comment');
    b.onclick = () => submitAsk(p.instr, p.mode || 'replace', p.label);
    box.appendChild(b);
  }
}

// Ask the server to make the session that holds this document before the first card needs
// it (the server keeps one per document version, so an unchanged document is skipped).
let prepared = null;
function prepareAsk() {
  if (!askConf.available || !cur.path) return;
  const body = { path: cur.path, doc: cm.getValue(), model: $('#model').value };
  if (prepared && ['path', 'doc', 'model'].every(k => prepared[k] === body[k])) return;
  prepared = body;
  api('POST', '/api/ask/prepare', body).catch(() => { if (prepared === body) prepared = null; });
}

// Ask Claude can't run (see askConf.reason): show why on the Suggestions tab.
function showAskUnavailable() {
  showClaudePanel();
  setPanelTab('suggestions', false);
  renderCards();
  cm.refresh();
}

function openAsk(source, focus) {
  if (!askConf.available) { if (focus) showAskUnavailable(); return; }
  if (!cur.model) return;
  prepareAsk();
  askTarget = currentRange(source);
  if (source !== 'preview') previewRange = null;
  const bar = $('#askbar');
  bar.hidden = false;
  bar.classList.toggle('whole', !!askTarget.whole);
  $('#ask-input').placeholder = askTarget.whole
    ? 'Nothing selected: ask Claude about the whole document… (Enter)'
    : 'Ask Claude to… (Enter to send · end with ? for a comment · Esc closes)';
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
  submitAsk(v, v.endsWith('?') ? 'comment' : 'replace', v);
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

function submitAsk(instr, mode, label) {
  if (!askTarget) return;
  const t = askTarget;
  closeAsk();
  if (!cm.hasFocus()) window.getSelection().removeAllRanges();
  createCard({ from: t.from, to: t.to, whole: !!t.whole, instruction: instr, mode, label });
}

// ------------------------------------------------------------------ Claude cards

// Cards belong to their file's model: the marks live on its Doc, so a card keeps tracking its
// passage (and Accept works) while another tab is active.
function markCard(c, cls) {
  const doc = c.model.doc;
  const r = c.marker?.find();
  c.marker?.clear();
  if (!r && c.marker) { c.marker = null; return; }
  const from = r ? r.from : doc.posFromIndex(c.from), to = r ? r.to : doc.posFromIndex(c.to);
  c.marker = doc.markText(from, to, { className: `claude-mark ${cls} card-${c.id}`, clearWhenEmpty: false, inclusiveLeft: false, inclusiveRight: false });
}

function createCard({ model = cur.model, from, to, whole, instruction, mode, label }) {
  if (!model) return;
  const c = { id: ++cardSeq, model, from, to, whole, instruction, mode, label, status: 'pending', original: model.doc.getValue().slice(from, to), result: '', view: 'diff' };
  markCard(c, 'pending');
  cards.unshift(c);
  // a card lives as long as its file is open: keep a preview tab from being replaced under it
  for (const t of Tabs.list()) if (t.model === model && t.preview) Tabs.pin(t);
  showClaudePanel();
  setPanelTab('suggestions', false);   // cards don't outlive a reload, so the remembered tab stays
  run(c);
}

async function run(c, feedback) {
  const r = c.marker?.find();
  if (!r) { c.status = 'error'; c.error = 'The highlighted text was deleted.'; renderCards(); return; }
  const doc = c.model.doc.getValue();
  const start = c.model.doc.indexFromPos(r.from), end = c.model.doc.indexFromPos(r.to);
  c.original = doc.slice(start, end);
  c.status = 'pending';
  c.started = Date.now();
  c.error = null;
  markCard(c, 'pending');
  renderCards();
  highlightPreviewCards();
  const body = {
    path: c.model.path, doc, start, end, mode: c.mode, model: $('#model').value,
    instruction: feedback || c.instruction,
  };
  if (feedback && c.result) body.previous = c.result;
  if (feedback === null && c.result) {   // Retry
    body.instruction = c.instruction + '\n\n(Give a noticeably different version from the previous attempt.)';
    body.previous = c.result;
  }
  if (body.previous && c.session) body.session = c.session;   // the server forks the card's session
  try {
    const res = await api('POST', '/api/ask', body);
    if (c.status === 'cancelled') return;
    c.session = res.session || null;
    if (c.mode === 'replace') {
      const lead = c.original.match(/^\s*/)[0], trail = c.original.match(/\s*$/)[0];
      c.result = lead + res.result.trim() + trail;
    } else c.result = res.result.trim();
    c.meta = `${res.seconds}s${res.cost ? ` · $${res.cost.toFixed(3)}` : ''}`;
    c.status = 'ready';
    c.view = 'diff';
    if (feedback) c.history = [...(c.history || []), feedback];
    markCard(c, 'ready');
  } catch (e) {
    if (c.status === 'cancelled') return;
    c.status = 'error';
    c.error = e.message;
    markCard(c, 'error');
  }
  renderCards();
  highlightPreviewCards();
}

function finish(c, status) {
  c.status = status;
  c.marker?.clear();
  c.marker = null;
  renderCards();
  highlightPreviewCards();
}

function accept(c) {
  const r = c.marker?.find();
  if (!r) { c.status = 'error'; c.error = 'The highlighted text was deleted, so there is nowhere to put the result.'; renderCards(); return; }
  const text = c.editing ?? c.result;
  const doc = c.model.doc;
  doc.replaceRange(text, r.from, r.to, '+claude');
  const end = doc.posFromIndex(doc.indexFromPos(r.from) + text.length);
  const m = doc.markText(r.from, end, { className: 'accepted-flash' });
  setTimeout(() => m.clear(), 2000);
  c.editing = undefined;
  finish(c, 'accepted');
}

function renderCards() {
  const open = cards.filter(c => ['pending', 'ready'].includes(c.status)).length;
  $('#cards-count').textContent = open;
  $('#cards-count').hidden = !open;
  const box = $('#cards');
  box.replaceChildren();
  if (!cards.length) {
    box.innerHTML = askConf.available
      ? `<p class="muted hint">Highlight text in the editor or the preview, then pick an action from the pop-up (or press <kbd>Ctrl</kbd>+<kbd>J</kbd>). With nothing selected, the request applies to the whole document.</p>`
      : `<div class="muted hint"><p><strong>Ask Claude unavailable</strong></p><p>${esc(askConf.reason || 'unknown reason.')}</p>
         <p>Choose how it runs with <code>md-editor --ask-agent CMD</code> (or the <code>MDEDIT_ASK_AGENT</code> environment variable).</p></div>`;
    return;
  }
  for (const c of cards) box.appendChild(cardEl(c));
}

function cardEl(c) {
  const el = document.createElement('div');
  el.className = `card ${c.status} ${c.mode}`;
  const excerpt = c.whole ? 'Whole document' : c.original.trim().replace(/\s+/g, ' ');
  const statusText = { pending: 'Thinking…', ready: c.mode === 'comment' ? 'Comment' : 'Suggestion', accepted: 'Accepted', kept: 'Kept original', dismissed: 'Dismissed', error: 'Error', cancelled: 'Cancelled' }[c.status];
  el.innerHTML = `
    <div class="card-head">
      <span class="badge">${statusText}</span>
      <span class="card-label" title="${esc(c.instruction)}">${esc(c.label)}</span>
      <span class="card-file muted" title="${esc(c.model.path)}">${esc(c.model.path.split('/').pop())}</span>
      <span class="muted card-meta">${c.status === 'pending' ? '<span class="spinner"></span>' : esc(c.meta || '')}</span>
    </div>
    <div class="card-excerpt muted" title="Click to show in the editor">“${esc(excerpt.length > 140 ? excerpt.slice(0, 140) + '…' : excerpt)}”</div>
    ${(c.history || []).length ? `<div class="card-history muted">↳ ${c.history.map(esc).join('<br>↳ ')}</div>` : ''}
    <div class="card-body"></div>
    <div class="card-actions"></div>`;
  const body = el.querySelector('.card-body');
  const actions = el.querySelector('.card-actions');
  const btn = (label, fn, cls = '') => {
    const b = Object.assign(document.createElement('button'), { textContent: label, className: cls });
    b.onclick = fn;
    actions.appendChild(b);
    return b;
  };

  if (c.status === 'error') body.innerHTML = `<div class="err">${esc(c.error)}</div>`;

  if (c.status === 'pending') {
    btn('Cancel', () => finish(c, 'cancelled'));
  } else if (c.status === 'ready' && c.mode === 'replace') {
    const tabs = document.createElement('div');
    tabs.className = 'seg small';
    for (const [v, t] of [['diff', 'Changes'], ['render', 'Preview'], ['edit', 'Edit']]) {
      const b = Object.assign(document.createElement('button'), { textContent: t, className: c.view === v ? 'on' : '' });
      b.onclick = () => { c.view = v; if (v === 'edit' && c.editing === undefined) c.editing = c.result; renderCards(); };
      tabs.appendChild(b);
    }
    body.appendChild(tabs);
    const view = document.createElement('div');
    view.className = 'card-view';
    if (c.view === 'diff') view.innerHTML = `<div class="diff">${wordDiff(c.original, c.editing ?? c.result)}</div>`;
    else if (c.view === 'render') { view.className += ' markdown-body'; MD.render(c.editing ?? c.result, view); }
    else {
      const ta = Object.assign(document.createElement('textarea'), { value: c.editing ?? c.result });
      ta.rows = Math.min(16, (c.editing ?? c.result).split('\n').length + 2);
      ta.oninput = () => { c.editing = ta.value; };
      view.appendChild(ta);
    }
    body.appendChild(view);
    const r = c.marker?.find();
    if (r && c.model.doc.getRange(r.from, r.to) !== c.original) {
      body.insertAdjacentHTML('beforeend', '<div class="warn">The highlighted text has been edited since this was requested; Accept will overwrite those edits.</div>');
    }
    btn('Accept', () => accept(c), 'primary');
    btn('Keep original', () => finish(c, 'kept'));
    btn('Retry', () => run(c, null));
  } else if (c.status === 'ready' && c.mode === 'comment') {
    const view = document.createElement('div');
    view.className = 'card-view markdown-body';
    MD.render(c.result, view);
    body.appendChild(view);
    btn('Apply this feedback', () => {
      const r = c.marker?.find();
      if (!r) return;
      finish(c, 'dismissed');
      createCard({ model: c.model, from: c.model.doc.indexFromPos(r.from), to: c.model.doc.indexFromPos(r.to), whole: c.whole, mode: 'replace', label: 'Apply feedback',
        instruction: `Revise this passage to address the following feedback:\n\n${c.result}` });
    }, 'primary');
    btn('Dismiss', () => finish(c, 'dismissed'));
  } else if (c.status === 'error') {
    btn('Retry', () => run(c));
    btn('Dismiss', () => finish(c, 'dismissed'));
  }

  if (c.status === 'ready') {
    const f = document.createElement('form');
    f.className = 'refine';
    f.innerHTML = `<input placeholder="${c.mode === 'comment' ? 'Follow-up question…' : 'Refine: e.g. shorter, keep the first sentence…'}">`;
    f.onsubmit = e => { e.preventDefault(); const v = f.firstChild.value.trim(); if (v) run(c, v); };
    el.appendChild(f);
  }

  el.querySelector('.card-excerpt').onclick = async () => {
    if (!c.marker?.find()) return;
    if (cur.model !== c.model) await Tabs.open(c.model.path, { preview: false });
    const r = c.marker?.find();
    if (!r || cur.model !== c.model) return;
    cm.setSelection(r.from, r.to);
    cm.scrollIntoView({ from: r.from, to: r.to }, 80);
    cm.focus();
  };
  el.onmouseenter = () => document.querySelectorAll(`.card-${c.id}`).forEach(n => n.classList.add('focus'));
  el.onmouseleave = () => document.querySelectorAll(`.card-${c.id}`).forEach(n => n.classList.remove('focus'));
  return el;
}

$('#clear-cards').onclick = () => {
  cards = cards.filter(c => ['pending', 'ready'].includes(c.status));
  renderCards();
};

// a closed file takes its cards with it
Tabs.on('dispose', m => {
  const gone = cards.filter(c => c.model === m);
  if (!gone.length) return;
  for (const c of gone) { c.status = 'cancelled'; c.marker?.clear(); c.marker = null; }
  cards = cards.filter(c => c.model !== m);
  renderCards();
});

// Word-level diff (LCS) rendered as <del>/<ins>.
function wordDiff(a, b) {
  const tok = s => s.match(/\s+|[\p{L}\p{N}'’]+|[^\s\p{L}\p{N}]/gu) || [];
  const A = tok(a), B = tok(b), n = A.length, m = B.length;
  if (n * m > 4e6) return `<del>${esc(a)}</del><ins>${esc(b)}</ins>`;
  const dp = new Uint32Array((n + 1) * (m + 1));
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      dp[i * (m + 1) + j] = A[i] === B[j] ? dp[(i + 1) * (m + 1) + j + 1] + 1 : Math.max(dp[(i + 1) * (m + 1) + j], dp[i * (m + 1) + j + 1]);
  const out = [];
  let i = 0, j = 0;
  const push = (t, s) => { const l = out.at(-1); if (l && l[0] === t) l[1] += s; else out.push([t, s]); };
  while (i < n && j < m) {
    if (A[i] === B[j]) { push('=', A[i]); i++; j++; }
    else if (dp[(i + 1) * (m + 1) + j] >= dp[i * (m + 1) + j + 1]) push('-', A[i++]);
    else push('+', B[j++]);
  }
  while (i < n) push('-', A[i++]);
  while (j < m) push('+', B[j++]);
  return out.map(([t, s]) => t === '=' ? esc(s) : t === '-' ? `<del>${esc(s)}</del>` : `<ins>${esc(s)}</ins>`).join('');
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
  Tabs.closeAll();   // Ask Claude cards go with their files
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
    // render a clean light-theme copy so dark mode and Claude highlights stay out of the PDF
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
$('#model').value = store.get('mdedit.model', '');
$('#model').onchange = () => store.set('mdedit.model', $('#model').value);

// unsaved files: autosaved ones are saved now, and the browser asks before leaving
window.addEventListener('beforeunload', e => {
  if (!Tabs.anyDirty()) return;
  for (const m of Tabs.models()) if (m.dirty && Tabs.autosave(m.category)) Tabs.save(m, false, true);   // not deleted ones
  e.preventDefault();
  e.returnValue = '';
});

// ------------------------------------------------------------------ Claude panel: tabs and width

const panel = $('#claude');

// 'terminal' (Claude Code, term.js) or 'suggestions' (the Ask Claude cards)
function setPanelTab(tab, persist = true) {
  if (tab !== 'terminal') tab = 'suggestions';
  panel.dataset.tab = tab;
  for (const b of document.querySelectorAll('#panel-tabs .tab')) {
    b.classList.toggle('on', b.dataset.tab === tab);
    b.setAttribute('aria-selected', b.dataset.tab === tab ? 'true' : 'false');
  }
  if (persist) store.set('mdedit.panelTab', tab);
  Term.visible();
}
document.querySelectorAll('#panel-tabs .tab').forEach(b => b.onclick = () => {
  setPanelTab(b.dataset.tab);
  if (b.dataset.tab === 'terminal') Term.focus();
});
setPanelTab(store.get('mdedit.panelTab', 'terminal'), false);

// for the other modules (ui.js: Send to Claude terminal)
window.App = {
  cm,
  showTerminal() {
    showClaudePanel();
    setPanelTab('terminal');
    cm.refresh();
  },
  setPanelTab,
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
  skills = conf.skills;
  fileList = conf.files;
  Term.init(conf.agent);
  askConf = conf.ask || { available: false, reason: 'This md-editor server has no Ask Claude support.' };
  const askBtn = $('#toolbar [data-cmd="ask"]');
  askBtn.disabled = !askConf.available || !cur.model;
  askBtn.title = askConf.available ? `Ask Claude about the selection (Ctrl+J), via ${askConf.cmd}` : `Ask Claude unavailable: ${askConf.reason}`;
  renderCards();
  const agentOk = !!conf.agent?.available;
  $('#tab-terminal').title = agentOk ? `Claude Code (${conf.agent.cmd}) in the open folder` : 'Claude Code terminal (unavailable)';
  setRoot(conf.root);
  setPanelTab(agentOk ? store.get('mdedit.panelTab', 'terminal') : 'suggestions', false);
  buildPresets();
  // read before the remembered tabs come back: activating one rewrites the URL
  let fromHash = '';
  try { fromHash = decodeURIComponent(location.hash.slice(1)); } catch {}
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
