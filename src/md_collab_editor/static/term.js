// Claude Code in the side panel: an xterm.js terminal joined by a WebSocket (/api/term)
// to a PTY on the server, which runs the agent command (by default claude-sandbox).
// The terminal and its socket are created the first time the Terminal tab is really on
// screen, so a hidden panel never starts a sandbox. app.js calls the hooks at the bottom.

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

  let conf = null;            // {available, cmd, reason} from /api/config
  let wrap, box, notice, restartBtn;
  let term = null, fitter = null, broken = false;
  let ws = null, retries = 0, retryT = null, announced = false;
  let fresh = false;          // the next status is the first on this connection
  let expectReplay = false;   // the next binary frame is the scrollback replay
  let replaying = 0;          // replies xterm makes to queries in a replay are not sent
  let pending = [], pendingLen = 0;   // input typed before the session can take it
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
  // on screen: the panel is shown and the Terminal tab is active
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
    restartBtn.onclick = () => restart();
    if (!conf.available) unavailable(conf.reason || 'unknown reason.');
  }

  function unavailable(reason) {
    restartBtn.hidden = true;
    wrap.classList.add('off');
    box.innerHTML = `<div class="term-off muted">
      <p><strong>Terminal unavailable</strong></p>
      <p>${esc(reason)}</p>
      <p>Choose the command with <code>md-editor --agent CMD</code> (or the <code>MDEDIT_AGENT</code> environment variable),
      e.g. <code>--agent claude</code> for Claude Code without the sandbox. The Suggestions tab works either way.</p>
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
    s.onopen = () => { if (s !== ws) return; retries = 0; announced = false; syncSize(); };
    s.onmessage = ev => { if (s === ws) receive(ev.data); };
    s.onclose = () => {
      if (s !== ws) return;
      ws = null;
      if (!announced) {
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
      if (m && m.type === 'status') onStatus(m);
      return;
    }
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
    // a new connection replays the scrollback (the server puts the terminal modes in force
    // where it begins first), and a new session starts clean
    if (first || m.id !== shownId) { term.reset(); shownId = m.id; }
    expectReplay = first && (m.state === 'running' || m.state === 'exited');
    status = m;
    if (m.state === 'exited' || m.state === 'failed') pending = [], pendingLen = 0;
    renderNotice();
    flush();
  }

  // ---------------------------------------------------------------- input

  const ended = () => status.state === 'exited' || status.state === 'failed';
  // the running session can take input: connected, its status and any replay are in
  const ready = () => open() && !fresh && !expectReplay && !replaying && status.state === 'running';
  // xterm's own answers to terminal queries (device attributes, cursor position, mode and
  // colour reports, focus events); those made while drawing a replay answer old queries
  const REPLY = /^\x1b(\[[?>]?[\d;]*(\$y|[cnRtIO])|\][\s\S]*(\x07|\x1b\\)|P[\s\S]*\x1b\\)$/;
  const PENDING_MAX = 1 << 20;

  // Keys typed while connecting, or while a replay is drawn, wait here rather than being lost.
  function flush() {
    if (!ready() || !pending.length) return;
    const queued = pending.join('');
    pending = [], pendingLen = 0;
    sendBytes(enc.encode(queued));
  }

  function onInput(s) {
    if (ended()) { if (s === '\r') restart(); return; }
    if (ready()) { flush(); sendBytes(enc.encode(s)); return; }
    if (replaying && REPLY.test(s)) return;
    if (pendingLen + s.length <= PENDING_MAX) { pending.push(s); pendingLen += s.length; }
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
    // the panel or tab may have been shown: start the terminal on first sight, else refit
    visible() {
      if (!shown()) return false;
      if (!term) create(); else syncSize();
      return shown();
    },
    fit: syncSize,
    focus() { if (term && shown()) term.focus(); },
  };
})();
window.Term = Term;
