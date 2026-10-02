// Claude Code in the side panel: an xterm.js terminal joined by a WebSocket (/api/term)
// to a PTY on the server, which runs the agent command (by default claude-sandbox).
// The terminal is created the first time the panel is really on screen, and its first resize
// starts the session, so a hidden panel never starts a sandbox. Until then the page is joined
// all the same (without a terminal, so the output is dropped), for the IDE link's messages:
// the terminal makes a connection of its own when it is created, which replays the output.
// app.js calls the hooks at the bottom.
//
// The same socket carries the editor's side of md-editor's IDE link to that session (the
// server speaks Claude Code's IDE protocol to it). Text JSON from here: `selection` (the
// file and selection on screen, debounced; the server passes it on when the link is up),
// `ask` (a question about a selection: the server sends the selection over the link, then
// types the question and Enter, or types "@path#L1-5 question" when there is no link),
// `mention` (Send to Claude terminal: an at-mention over the link, else typed) and
// `diff-decision`. From the server: `status` (with `ide`: off | waiting | connected, shown
// in the panel's header, and `diffs`, the proposals still waiting), `diff` (Claude asks to
// show a proposed edit: proposal.js opens it as a tab), `diff-close` (that proposal was
// answered or withdrawn), `waiting` (an ask waits for a session that is still starting) and
// `sent` (how an ask or mention went: {ok, via: 'ide' | 'typed'} or {ok: false, error}, shown
// as a toast).
//
//   Term.select({path, start, end, text})   the selection on screen (ROOT-relative path;
//                                            start/end {line, character}, 0-based)
//   Term.ask(question, {path, start, end, text}) → Promise<bool>   zero-width (or no)
//                                            start/end: about the whole file
//   Term.mention(path, {from, to}?) → Promise<bool>   1-based lines; path '' is the folder
//   Term.ide() → 'off' | 'waiting' | 'connected' | null

const Term = (() => {
  const RECONNECT_MAX = 10000;
  const enc = new TextEncoder();
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform);
  // GitHub's terminal colours, to sit with the rest of the page
  const ANSI = {
    light: {
      black: '#24292f', red: '#cf222e', green: '#116329', yellow: '#4d2d00',
      blue: '#0969da', magenta: '#8250df', cyan: '#1b7c83', white: '#6e7781',
      brightBlack: '#57606a', brightRed: '#a40e26', brightGreen: '#1a7f37', brightYellow: '#633c01',
      brightBlue: '#218bff', brightMagenta: '#a475f9', brightCyan: '#3192aa', brightWhite: '#8c959f',
    },
    dark: {
      black: '#484f58', red: '#ff7b72', green: '#3fb950', yellow: '#d29922',
      blue: '#58a6ff', magenta: '#bc8cff', cyan: '#39c5cf', white: '#b1bac4',
      brightBlack: '#6e7681', brightRed: '#ffa198', brightGreen: '#56d364', brightYellow: '#e3b341',
      brightBlue: '#79c0ff', brightMagenta: '#d2a8ff', brightCyan: '#56d4dd', brightWhite: '#ffffff',
    },
  };

  const SEL_DELAY = 150;      // ms: selection messages are debounced
  const SEL_TEXT_MAX = 65536; // characters of a selection sent (Claude Code keeps 2,000)
  let conf = null;            // {available, cmd, reason} from /api/config
  let wrap, box, notice, restartBtn, ideEl;
  let sel = null, selSent = null, selT = null;   // the selection message, the last one sent
  let outbox = [];            // diff decisions made while disconnected
  let term = null, fitter = null, broken = false;
  let ws = null, retries = 0, retryT = null, announced = false;
  let fresh = false;          // the next status is the first on this connection
  let expectReplay = false;   // the next binary frame is the scrollback replay
  let replaying = 0;          // replies xterm makes to queries in a replay are not sent
  let pending = [], pendingLen = 0;   // input before the session can take it: text, or a message (ask, mention)
  let lost = false;           // the connection to a session on screen dropped; keys go nowhere
  let shownId;                // id of the session whose output the terminal holds
  let status = { state: 'none', id: null, cwd: null };
  let sent = { cols: 0, rows: 0 };
  let root = '';
  let dark = false;
  let fitQueued = false;

  const cssVar = name => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const samePath = (a, b) => a.replace(/\/+$/, '') === b.replace(/\/+$/, '');
  const open = () => ws && ws.readyState === WebSocket.OPEN;
  // on screen: the Claude panel is shown
  const shown = () => !!(conf && conf.available && !broken && wrap && wrap.getClientRects().length);

  function theme() {
    const bg = cssVar('--bg') || (dark ? '#0d1117' : '#ffffff');
    const fg = cssVar('--fg') || (dark ? '#f0f6fc' : '#1f2328');
    return {
      background: bg, foreground: fg, cursor: fg, cursorAccent: bg,
      selectionBackground: dark ? 'rgba(68, 147, 248, 0.35)' : 'rgba(9, 105, 218, 0.22)',
      ...ANSI[dark ? 'dark' : 'light'],
    };
  }

  // ---------------------------------------------------------------- setup

  function init(agentConf) {
    conf = agentConf || { available: false, cmd: null, reason: 'this md-editor server has no terminal support.' };
    wrap = document.getElementById('term-wrap');
    box = document.getElementById('term');
    notice = document.getElementById('term-notice');
    restartBtn = document.getElementById('term-restart');
    ideEl = document.getElementById('term-ide');
    restartBtn.onclick = () => restart();
    if (!conf.available) unavailable(conf.reason || 'unknown reason.');
    // with several pages open, the one in front says what is selected
    window.addEventListener('focus', () => sendSelection(true));
  }

  function unavailable(reason) {
    restartBtn.hidden = true;
    wrap.classList.add('off');
    box.innerHTML = `<div class="term-off muted">
      <p><strong>Terminal unavailable</strong></p>
      <p>${esc(reason)}</p>
      <p>Choose the command with <code>md-editor --agent CMD</code> (or the <code>MDEDIT_AGENT</code> environment variable),
      e.g. <code>--agent claude</code> for Claude Code without the sandbox. <em>Ask Claude</em> sends its questions to this terminal, so it is off too.</p>
    </div>`;
  }

  function create() {
    if (typeof Terminal === 'undefined' || typeof FitAddon === 'undefined') {
      broken = true;
      unavailable('xterm.js could not be loaded from the CDN (the page needs internet access). Reload to try again.');
      return;
    }
    term = new Terminal({
      fontFamily: cssVar('--mono') || 'monospace', fontSize: 13, scrollback: 5000,
      cursorBlink: true, macOptionIsMeta: true, theme: theme(),
    });
    fitter = new FitAddon.FitAddon();
    term.loadAddon(fitter);
    if (typeof WebLinksAddon !== 'undefined') term.loadAddon(new WebLinksAddon.WebLinksAddon());
    term.open(box);
    term.attachCustomKeyEventHandler(onKey);
    term.onData(onInput);
    term.onBinary(s => {
      if (!ready()) return;   // mouse reports; stale ones are not worth queueing
      const b = new Uint8Array(s.length);
      for (let i = 0; i < s.length; i++) b[i] = s.charCodeAt(i) & 0xff;
      sendBytes(b);
    });
    new ResizeObserver(scheduleFit).observe(box);
    try { fitter.fit(); } catch {}
    // a connection made while the panel was hidden had no terminal for the replay: start afresh
    if (ws) { const old = ws; ws = null; try { old.close(); } catch {} }
    connect();
  }

  // ---------------------------------------------------------------- size

  function scheduleFit() {
    if (fitQueued) return;
    fitQueued = true;
    requestAnimationFrame(() => { fitQueued = false; syncSize(); });
  }

  // Fit the terminal to its box, then tell the server if the size changed. The first
  // resize on a connection also starts the session, so nothing is sent while hidden.
  function syncSize() {
    if (!term || document.body.classList.contains('panel-resizing')) return;
    if (box.clientWidth > 0 && box.clientHeight > 0) { try { fitter.fit(); } catch {} }
    if (!open() || !shown()) return;
    if (term.cols === sent.cols && term.rows === sent.rows) return;
    sent = { cols: term.cols, rows: term.rows };
    send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
  }

  // ---------------------------------------------------------------- socket

  function connect() {
    clearTimeout(retryT);
    const s = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/term');
    s.binaryType = 'arraybuffer';
    ws = s;
    fresh = true;
    expectReplay = false;
    sent = { cols: 0, rows: 0 };
    selSent = null;   // a new connection (perhaps a new server) is told the selection again
    s.onopen = () => { if (s !== ws) return; retries = 0; announced = false; syncSize(); };
    s.onmessage = ev => { if (s === ws) receive(ev.data); };
    s.onclose = () => {
      if (s !== ws) return;
      ws = null;
      // keys typed at a dead panel must not reach whatever session answers the reconnect
      // (a restarted server's, or one another tab restarted)
      if (shownId !== undefined) { lost = true; clearPending(); }
      if (!announced && term) {
        announced = true;
        term.write('\r\n\x1b[2m[disconnected — reconnecting…]\x1b[22m\r\n');
      }
      retryT = setTimeout(connect, Math.min(RECONNECT_MAX, 1000 * 2 ** Math.min(retries++, 4)));
    };
  }

  function send(data) { if (open()) ws.send(data); }
  // input bytes; the server closes the socket on messages over 4 MiB, so a huge paste goes in pieces
  function sendBytes(b) { for (let i = 0; i < b.length; i += 1 << 20) send(b.subarray(i, i + (1 << 20))); }

  function receive(data) {
    if (typeof data === 'string') {
      let m;
      try { m = JSON.parse(data); } catch { return; }
      if (!m) return;
      if (m.type === 'status') onStatus(m);
      else if (m.type === 'diff' && m.id != null) {
        window.Proposals?.open(m, d => decide(Object.assign({ type: 'diff-decision', id: m.id }, d)));
      } else if (m.type === 'diff-close' && m.id != null) window.Proposals?.close(m.id);
      else if (m.type === 'waiting') toast('Claude Code is still starting: the question goes in when it is ready.', 'ok');
      else if (m.type === 'sent' && m.ok === false) {   // the server's answer to an ask or a mention
        toast(m.error || `The Claude terminal could not take the ${m.what === 'ask' ? 'question' : 'text'}.`);
      }
      return;
    }
    if (!term) { expectReplay = false; return; }   // joined while the panel is hidden: no terminal yet
    const bytes = new Uint8Array(data);
    if (expectReplay) {   // the server sends one, possibly empty, after the first status
      expectReplay = false;
      if (!bytes.length) { flush(); return; }
      replaying++;
      term.write(bytes, () => { replaying--; flush(); });
    } else term.write(bytes);
  }

  function onStatus(m) {
    const first = fresh;
    fresh = false;
    const newSession = m.id !== status.id;
    // a new connection replays the scrollback (the server puts the terminal modes in force
    // where it begins first), and a new session starts clean
    if (term && (first || m.id !== shownId)) { term.reset(); shownId = m.id; }
    expectReplay = !!term && first && (m.state === 'running' || m.state === 'exited');
    status = m;
    lost = false;
    if (m.state === 'exited' || m.state === 'failed') clearPending();
    renderNotice();
    renderIde();
    // the proposals the server still waits on: any other shown here is an earlier server's
    // (md-editor restarted) or was answered while this page was away
    window.Proposals?.sync(Array.isArray(m.diffs) ? m.diffs : []);
    // a new connection (perhaps to a new server), or a new session (whose link starts with no
    // selection), is told what is selected
    if (first || (newSession && m.state === 'running')) sendSelection(true);
    if (first) {
      const out = outbox;
      outbox = [];
      for (const d of out) decide(d);   // the server answers one it no longer waits on with diff-close
    }
    flush();
  }

  // ---------------------------------------------------------------- input

  const ended = () => status.state === 'exited' || status.state === 'failed';
  // the running session can take input: connected, its status and any replay are in
  const ready = () => open() && !fresh && !expectReplay && !replaying && status.state === 'running';
  // dropped while a replay is drawn: xterm's own answers to terminal queries (device
  // attributes, cursor position, mode and colour reports), which answer old queries, and
  // mouse reports (SGR and X10), which point at a screen still being drawn
  const REPLY = /^\x1b(\[[?>]?[\d;]*(\$y|[cnRt])|\[<[\d;]*[Mm]|\[M[\s\S]{3}|\][\s\S]*(\x07|\x1b\\)|P[\s\S]*\x1b\\)$/;
  const PENDING_MAX = 1 << 20;

  // Keys typed while connecting, or while a replay is drawn, wait here rather than being lost;
  // so do asks and mentions, which the server types into the session, so they stay in order.
  function flush() {
    if (!ready() || !pending.length) return;
    const queued = pending;
    clearPending();
    let text = '';
    for (const p of queued) {
      if (typeof p === 'string') { text += p; continue; }
      if (text) { sendBytes(enc.encode(text)); text = ''; }
      send(JSON.stringify(p));
    }
    if (text) sendBytes(enc.encode(text));
  }

  function clearPending() { pending = []; pendingLen = 0; }

  // Input for the session (typed keys or text as a string; an ask or mention as a message for
  // the server): sent now if it can take it, else queued behind any earlier input. False if it
  // goes nowhere: the connection to the session on screen dropped, or the queue is full.
  function input(s) {
    if (lost) return false;
    const msg = typeof s === 'string' ? null : JSON.stringify(s);
    if (ready()) { flush(); if (msg) send(msg); else sendBytes(enc.encode(s)); return true; }
    const n = msg ? msg.length : s.length;
    if (pendingLen + n > PENDING_MAX) return false;
    pending.push(s); pendingLen += n;
    return true;
  }

  function onInput(s) {
    if (ended()) { if (s === '\r') restart(); return; }
    if (replaying && REPLY.test(s)) return;   // never when ready()
    input(s);
  }

  // Ctrl+Shift+C copies; Ctrl+Shift+V (and Ctrl+V off the Mac) is left to the browser,
  // whose paste event xterm turns into input.
  function onKey(e) {
    const k = (e.key || '').toLowerCase();
    if (e.ctrlKey && e.shiftKey && !e.altKey && !e.metaKey && k === 'c') {
      if (e.type === 'keydown') { e.preventDefault(); copy(); }
      return false;
    }
    if (e.ctrlKey && !e.altKey && !e.metaKey && k === 'v' && (e.shiftKey || !isMac)) return false;
    return true;
  }

  function copy() {
    const text = term.getSelection();
    if (!text) return;
    const fallback = () => {
      const ta = Object.assign(document.createElement('textarea'), { value: text });
      ta.style.cssText = 'position:fixed;top:0;left:0;opacity:0';
      document.body.append(ta);
      ta.select();
      try { document.execCommand('copy'); } catch {}
      ta.remove();
      term.focus();
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).catch(fallback);
    else fallback();
  }

  function restart() {
    if (!term) return;
    if (!ws) { connect(); return; }   // disconnected: try again now rather than wait
    if (!open()) return;
    if (status.state === 'running' && !confirm('Restart the Claude session? The current conversation in the terminal will end.')) return;
    syncSize();
    send(JSON.stringify({ type: 'restart' }));
    term.focus();
  }

  // ---------------------------------------------------------------- the editor's side of the IDE link

  const running = () => open() && !fresh && status.state === 'running';
  const toast = (msg, kind = 'err') => window.UI?.toast(msg, { kind });

  // The selection on screen; the server keeps the last one and passes it on over the link.
  function select(s) {
    clearTimeout(selT);
    if (!s || typeof s.path !== 'string' || !s.start || !s.end) return;
    const text = String(s.text || '');
    sel = { type: 'selection', path: s.path, start: s.start, end: s.end, text: text.length > SEL_TEXT_MAX ? text.slice(0, SEL_TEXT_MAX) : text };
    selT = setTimeout(sendSelection, SEL_DELAY);
  }

  function sendSelection(force = false) {
    clearTimeout(selT);
    if (!sel || !open()) return;
    const j = JSON.stringify(sel);
    if (j === selSent && force !== true) return;
    selSent = j;
    send(j);
  }

  // a diff decision is never lost to a dropped connection: it goes when the socket is back
  function decide(d) {
    if (open() && !fresh) send(JSON.stringify(d));
    else outbox.push(d);
  }

  // Show the panel and wait (up to 5 s) for a session that is still connecting.
  async function reach() {
    if (!conf || !conf.available || broken) { toast('The Claude terminal is not available.'); return false; }
    window.App?.showTerminal?.();
    if (!term) { toast('The Claude terminal is not available.'); return false; }
    for (let i = 0; i < 50 && !running() && !ended(); i++) await new Promise(r => setTimeout(r, 100));
    if (!running()) {
      toast(ended() ? 'The Claude session has ended: restart it in the terminal first.' : 'The Claude terminal is not running yet; try again when it has started.');
      return false;
    }
    return true;
  }

  async function ask(question, s) {
    question = String(question || '').trim();
    if (!question || !(await reach())) return false;
    const msg = { type: 'ask', text: question };
    if (s && typeof s.path === 'string') {
      select(s);
      sendSelection(true);   // the selection the server sends with the question
      msg.path = s.path;
      const a = s.start, b = s.end;
      if (a && b && (a.line !== b.line || a.character !== b.character)) Object.assign(msg, { start: a, end: b });
    }
    if (!input(msg)) { toast('The Claude terminal could not take the question.'); return false; }
    term?.focus();
    return true;
  }

  // an @-mention of a file or folder (and lines) in Claude Code's prompt, without Enter
  async function mention(path, range) {
    if (!(await reach())) return false;
    const msg = { type: 'mention', path: path || '' };
    if (range && range.from) {   // whole lines: the end is the start of the line after the last
      msg.start = { line: range.from - 1, character: 0 };
      msg.end = { line: Math.max(range.to || range.from, range.from), character: 0 };
    }
    if (!input(msg)) { toast('The Claude terminal could not take the text.'); return false; }
    term?.focus();
    return true;
  }

  const IDE = {
    connected: ['IDE linked', 'Claude Code is connected to md-editor: it sees the file and the lines you select, Ask Claude sends your selection with the question, and an edit it asks permission for opens here as a diff to accept or reject.'],
    waiting: ['IDE link…', 'Waiting for Claude Code to connect to md-editor (it looks when it starts; /ide in Claude Code connects by hand). Until then, files and lines are typed into the terminal as @path#L1-5.'],
    off: ['No IDE link', 'Claude Code is not connected to md-editor: files and lines are typed into the terminal as @path#L1-5.'],
  };

  function renderIde() {
    if (!ideEl) return;
    const st = status.state === 'running' && IDE[status.ide] ? status.ide : null;
    ideEl.hidden = !st;
    if (!st) return;
    ideEl.dataset.state = st;
    ideEl.textContent = IDE[st][0];
    const why = status.ide_reason ? ` (${status.ide_reason})` : '';
    ideEl.title = IDE[st][1] + (st === 'off' ? why : '');
  }

  // ---------------------------------------------------------------- notices

  function renderNotice() {
    const rows = [];
    const s = status;
    const row = (html, cls, action) => {
      const d = document.createElement('div');
      if (cls) d.className = cls;
      d.innerHTML = `<span>${html}</span>`;
      if (action) {
        const b = Object.assign(document.createElement('button'), { type: 'button', textContent: action[0] });
        b.onclick = action[1];
        d.append(b);
      }
      rows.push(d);
    };
    if (s.state === 'exited') {
      const how = s.code == null ? '' : s.code < 0 ? ` (killed by signal ${-s.code})` : ` (exit code ${s.code})`;
      row(`Session ended${how}. Press Enter or click Restart to start a new one.`);
    } else if (s.state === 'failed') {
      // the server's message already reads "Could not start <cmd>: <error>"
      const msg = (s.message || `Could not start ${s.cmd || conf.cmd || 'the agent'}`).trim();
      row(`${esc(msg)}${/[.!?]$/.test(msg) ? '' : '.'} Press Enter or click Restart to try again.`, 'err');
    }
    if ((s.state === 'running' || s.state === 'exited') && s.cwd && root && !samePath(s.cwd, root)) {
      const verb = s.state === 'running' ? 'is running' : 'ran';
      row(`Terminal ${verb} in <span class="path">${esc(s.cwd)}</span>; the editor is now on <span class="path">${esc(root)}</span>.`,
        '', ['Restart here', restart]);
    }
    notice.replaceChildren(...rows);
    notice.hidden = !rows.length;
  }

  // ---------------------------------------------------------------- hooks for app.js

  return {
    init,
    // light/dark changed (the data-theme attribute is already set)
    setTheme(isDark) {
      dark = !!isDark;
      if (term) term.options.theme = theme();
    },
    // the editor's document root changed
    onRootChange(r) {
      root = r || '';
      if (term) renderNotice();
    },
    // the panel may have been shown: start the terminal on first sight, else refit. While it is
    // hidden the page is joined all the same (no terminal, no session started), so a proposed
    // edit waiting for an answer still opens, and Claude is told the selection.
    visible() {
      if (!shown()) {
        if (!term && !ws && conf && conf.available) connect();
        return false;
      }
      if (!term) create(); else syncSize();
      return shown();
    },
    fit: syncSize,
    focus() { if (term && shown()) term.focus(); },
    // has this connection's status reported a running session (so its folder is known), its
    // folder, and typing into it (bracketed: as one paste, so Claude Code takes it literally).
    // Text, asks and mentions go through the same queue as typed keys, so what is sent while
    // the replay is drawn, or while reconnecting, is not lost and stays in order.
    available: () => !!(conf && conf.available && !broken),
    running,
    state: () => status.state,   // none | running | exited | failed
    cwd: () => status.cwd || null,
    ide: () => (status.state === 'running' && status.ide) || null,
    sendText(text, { bracketed = false } = {}) {
      if (!term || ended()) return false;
      return input(bracketed ? `\x1b[200~${text}\x1b[201~` : text);
    },
    select, ask, mention,
  };
})();
window.Term = Term;
