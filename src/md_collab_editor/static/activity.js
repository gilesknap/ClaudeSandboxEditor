// The activity bar (thin icon strip at the far left) and the resizable left sidebar whose
// panel it picks, VS Code style. Plain script after ui.js; exposes `Activity`.
//
//   Activity.register(id, {icon, title, order, panelEl, onShow, onHide}) → handle
//       icon: HTML (an <svg>) for the button; title: its tooltip; order: position (lower first);
//       panelEl: element to show in the sidebar for this id (else an empty one is made);
//       onShow()/onHide(): its panel became visible / hidden (sidebar shown, collapsed or
//       switched). Registering an id again merges the new options into it (so scm.js and
//       search.js can fill the placeholder Source Control / Search entries made here).
//     handle = {id, button, panel, setBadge(n|string|null, title?), show(), visible()}
//   Activity.show(id)       open the sidebar on that panel
//   Activity.toggle(id?)    VS Code click: collapse if that panel is showing, else show it
//   Activity.setSidebar(on, persist = true)  show / hide the sidebar (keeps the current panel);
//                           in a narrow window showing it hides the Claude panel
//   Activity.current()      id of the current panel (even while collapsed)
//   Activity.fit()          re-apply the remembered width, within what the window and the
//                           Claude panel leave (app.js, when the Claude panel opens or resizes)
//   Activity.visible(id)    is that panel on screen
//   Activity.get(id)        handle or undefined
//   Activity.onLayout(fn)   fn() after the sidebar is shown, hidden or resized (app.js refits)

const Activity = (() => {
  const bar = document.getElementById('activity-bar');
  const sidebar = document.getElementById('sidebar');
  const views = document.getElementById('side-views');
  const entries = new Map();
  const layoutFns = new Set();
  // the chosen panel; until it is registered (scm.js and search.js come later) the first one shows
  let current = UI.store.get('mdedit.activity', 'explorer');
  const shown = () => (entries.has(current) ? current : entries.keys().next().value);
  if (UI.store.get('mdedit.files', '1') === '0' || innerWidth < 800) document.body.classList.add('no-sidebar');

  const sidebarOn = () => !document.body.classList.contains('no-sidebar');
  const layout = () => { for (const fn of layoutFns) { try { fn(); } catch (e) { console.error(e); } } };

  function register(id, opts = {}) {
    let e = entries.get(id);
    if (!e) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'activity-btn';
      button.dataset.id = id;
      button.setAttribute('role', 'tab');
      button.innerHTML = '<span class="activity-icon"></span><span class="activity-badge" hidden></span>';
      button.onclick = () => toggle(id);
      const panel = document.createElement('div');
      panel.className = 'side-view';
      panel.dataset.view = id;
      panel.hidden = true;
      views.append(panel);
      e = { id, button, panel, order: 50, opts: {} };
      e.handle = {
        id, button, panel,
        setBadge(n, title) {
          const b = button.querySelector('.activity-badge');
          const show = n !== null && n !== undefined && n !== 0 && n !== '';
          b.hidden = !show;
          b.textContent = show ? (typeof n === 'number' && n > 999 ? '999+' : String(n)) : '';
          button.setAttribute('aria-label', (e.opts.title || id) + (show ? ` (${n})` : ''));
          if (title !== undefined) b.title = title;
        },
        show: () => show(id),
        visible: () => visible(id),
      };
      entries.set(id, e);
    }
    Object.assign(e.opts, opts);
    if (opts.icon !== undefined) e.button.querySelector('.activity-icon').innerHTML = opts.icon;
    if (opts.title !== undefined) { e.button.title = opts.title; e.button.setAttribute('aria-label', opts.title); }
    if (opts.order !== undefined) e.order = opts.order;
    if (opts.panelEl) {
      opts.panelEl.classList.add('side-view-body');
      e.panel.replaceChildren(opts.panelEl);
    }
    // keep the buttons in order
    [...entries.values()].sort((a, b) => a.order - b.order).forEach(x => bar.append(x.button));
    const was = shownId;
    apply();
    // the panel was already on screen as a placeholder: it gets its real onShow, once the
    // script registering it has set itself up
    if (opts.onShow && was === id && shownId === id) {
      queueMicrotask(() => { if (shownId === id) { try { e.opts.onShow?.(); } catch (err) { console.error(err); } } });
    }
    return e.handle;
  }

  function visible(id) { return sidebarOn() && shown() === id; }

  let shownId = null;   // the panel whose onShow ran last (null while the sidebar is collapsed)
  function apply() {
    const cur = shown();
    for (const e of entries.values()) {
      const on = e.id === cur;
      e.panel.hidden = !on;
      e.button.classList.toggle('on', on && sidebarOn());
      e.button.setAttribute('aria-selected', on && sidebarOn() ? 'true' : 'false');
    }
    const now = sidebarOn() ? cur : null;
    if (now !== shownId) {
      const was = entries.get(shownId);
      shownId = now;
      try { was?.opts.onHide?.(); } catch (err) { console.error(err); }
      try { entries.get(now)?.opts.onShow?.(); } catch (err) { console.error(err); }
    }
  }

  // persist: remember the choice (not when a narrow window makes room for the Claude panel)
  function setSidebar(on, persist = true) {
    document.body.classList.toggle('no-sidebar', !on);
    if (persist) UI.store.set('mdedit.files', on ? '1' : '0');
    // a narrow window has room for one overlay: the side bar or the Claude panel
    if (on && matchMedia('(max-width: 800px)').matches && !document.body.classList.contains('no-claude')) {
      document.body.classList.add('no-claude');
    }
    apply();
    layout();
  }

  function show(id) {
    if (id && entries.has(id)) { current = id; UI.store.set('mdedit.activity', id); }
    if (!sidebarOn()) setSidebar(true); else apply();
  }

  function toggle(id) {
    id = id || shown();
    if (sidebarOn() && shown() === id) setSidebar(false);
    else show(id);
  }

  // ---------------------------------------------------------------- resizable width

  const MIN = 170;
  const EDITOR_MIN = 240;   // what the side bar and the Claude panel always leave the editor
  const defaultWidth = () => 260;
  const narrow = () => matchMedia('(max-width: 800px)').matches;   // both are overlays there
  // the width the side bar may take: some of the window, and never the editor's share
  function maxWidth() {
    const claude = document.getElementById('claude');
    const other = !narrow() && claude && !document.body.classList.contains('no-claude') ? claude.getBoundingClientRect().width : 0;
    return Math.min(innerWidth * 0.6, innerWidth - bar.getBoundingClientRect().width - other - EDITOR_MIN);
  }
  function setWidth(w, persist) {
    w = Math.round(Math.max(MIN, Math.min(maxWidth(), w)));
    sidebar.style.width = w + 'px';
    if (persist) UI.store.set('mdedit.sidebarWidth', String(w));
  }
  const applyWidth = () => setWidth(+UI.store.get('mdedit.sidebarWidth', 0) || defaultWidth());
  applyWidth();
  window.addEventListener('resize', applyWidth);

  const handle = document.getElementById('sidebar-resize');
  handle.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    e.preventDefault();
    const left = sidebar.getBoundingClientRect().left;
    handle.setPointerCapture(e.pointerId);
    document.body.classList.add('panel-resizing');
    const move = ev => setWidth(ev.clientX - left);
    const up = () => {
      handle.removeEventListener('pointermove', move);
      handle.removeEventListener('pointerup', up);
      handle.removeEventListener('pointercancel', up);
      document.body.classList.remove('panel-resizing');
      setWidth(sidebar.getBoundingClientRect().width, true);
      layout();
    };
    handle.addEventListener('pointermove', move);
    handle.addEventListener('pointerup', up);
    handle.addEventListener('pointercancel', up);
  });
  handle.addEventListener('dblclick', () => {
    UI.store.set('mdedit.sidebarWidth', '');
    applyWidth();
    layout();
  });

  // ---------------------------------------------------------------- built-in entries

  const ICONS = {
    explorer: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" aria-hidden="true"><path d="M13.5 2.5H6.5A1.5 1.5 0 0 0 5 4v12.5A1.5 1.5 0 0 0 6.5 18h9a1.5 1.5 0 0 0 1.5-1.5V6z"/><path d="M13.5 2.5V6H17"/><path d="M8 21h9.5a1.5 1.5 0 0 0 1.5-1.5V9"/></svg>',
    scm: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" aria-hidden="true"><circle cx="6.5" cy="5" r="2.2"/><circle cx="6.5" cy="19" r="2.2"/><circle cx="17.5" cy="7" r="2.2"/><path d="M6.5 7.2v9.6"/><path d="M17.5 9.2c0 4.8-6.5 3.6-10 7.3"/></svg>',
    search: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true"><circle cx="10.5" cy="10.5" r="6.2"/><path d="M15.2 15.2 21 21"/></svg>',
  };
  const placeholder = text => Object.assign(document.createElement('div'), { className: 'muted hint', textContent: text });

  register('explorer', { icon: ICONS.explorer, title: 'Explorer', order: 10 });
  register('scm', { icon: ICONS.scm, title: 'Source Control', order: 20, panelEl: placeholder('Source control is not available.') });
  register('search', { icon: ICONS.search, title: 'Search', order: 30, panelEl: placeholder('Search is not available.') });

  return {
    register, show, toggle, setSidebar, visible,
    current: shown,
    get: id => entries.get(id)?.handle,
    sidebarOn,
    fit: applyWidth,
    onLayout(fn) { layoutFns.add(fn); return () => layoutFns.delete(fn); },
    ICONS,
  };
})();
window.Activity = Activity;
