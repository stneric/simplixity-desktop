// Simplixity Desktop skin, the script half. Same house rules as skin.css:
// look things up defensively, do nothing when they are missing.

(() => {
  const root = document.documentElement;
  // skin.py's <meta>: macos, windows or linux.
  const platform = document.querySelector('meta[name="simplixity-desktop"]')?.content;
  root.dataset.desktop = platform ?? '';

  if (platform === 'macos') {
    // macOS full screen hides the window buttons, so the room macos.css keeps
    // for them would be an empty gap. No media query reports it; the page
    // filling the whole screen does. (outerWidth is 0 in WKWebView.)
    const sync = () => {
      const full = innerWidth >= screen.width && innerHeight >= screen.height;
      root.toggleAttribute('data-fullscreen', full);
    };
    sync();
    addEventListener('resize', sync);

    // Asked by macos.py when the mouse goes down in the title bar: is this
    // empty space (drag the window) or something to click (hand it to the
    // page)? Coordinates are CSS pixels from the top left of the viewport.
    const INTERACTIVE =
      'button, a, input, select, textarea, label, summary, [role], [contenteditable=""], [contenteditable="true"], [tabindex], .picker, .menu';
    window.simplixityDesktop = Object.freeze({
      isDragArea(x, y) {
        const el = document.elementFromPoint(x, y);
        return el instanceof Element && !el.closest(INTERACTIVE);
      },
    });
  }

  // ── API keys ──────────────────────────────────────────────────────
  // The app has one user (desktop_api.py signs in for them silently), so the
  // profile entry in the sidebar goes (skin.css), and Settings gets an
  // "API keys" tab, built from upstream's own tab and field markup. Added
  // before upstream wires its tabs, so it is wired with them; the click
  // handler below does the same selection in case it is not. If Settings is
  // not there, nothing happens.
  const strip = document.querySelector('#settings-overlay .tabstrip');
  const body = document.querySelector('#settings-overlay .modal-body');
  const el = (tag, props = {}, ...children) => {
    const node = Object.assign(document.createElement(tag), props);
    node.append(...children);
    return node;
  };
  // A Settings tab in upstream's markup: { panel, select }.
  const addTab = (key, label) => {
    const tab = el('button', {
      className: 'tab',
      id: `tab-desktop-${key}`,
      type: 'button',
      tabIndex: -1,
      textContent: label,
    });
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-controls', `panel-desktop-${key}`);
    tab.setAttribute('aria-selected', 'false');
    strip.append(tab);

    const panel = el('div', { className: 'tab-panel', id: `panel-desktop-${key}`, hidden: true });
    panel.setAttribute('role', 'tabpanel');
    panel.setAttribute('aria-labelledby', tab.id);
    body.append(panel);

    const select = () => {
      for (const other of strip.querySelectorAll('[role="tab"]')) {
        const on = other === tab;
        other.setAttribute('aria-selected', String(on));
        other.tabIndex = on ? 0 : -1;
        const p = document.getElementById(other.getAttribute('aria-controls'));
        if (p) p.hidden = !on;
      }
    };
    tab.addEventListener('click', select);
    return { panel, select };
  };
  const send = async (url, method, payload) => {
    const res = await fetch(url, {
      method,
      headers: { 'content-type': 'application/json', 'x-simplixity-desktop': '1' },
      body: JSON.stringify(payload ?? {}),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    return data;
  };

  if (strip && body) {
    const API = '/desktop/api/keys';
    const { panel, select } = addTab('keys', 'API keys');

    const put = async (changes) => {
      const data = await send(API, 'PUT', changes);
      // The model pickers list what the keys unlock.
      document.getElementById('refresh-models')?.click();
      return data;
    };

    const render = (data) => {
      panel.replaceChildren(
        el('p', {
          className: 'hint',
          textContent:
            'Kept in this app’s data folder on this computer and sent only to the service each key belongs to. Saved when you press Enter or leave the field.',
        }),
        ...data.keys.map((k) => {
          const id = `desktop-key-${k.env.toLowerCase()}`;
          const input = el('input', {
            type: 'password',
            id,
            className: 'text-input',
            autocomplete: 'off',
            spellcheck: false,
            placeholder: k.set ? `Saved (${k.set}). Type to replace.` : 'Not set',
          });
          const state = el('span', { className: 'desktop-key-state' });
          const remove = el('button', { type: 'button', className: 'ghost-btn', textContent: 'Remove' });
          remove.hidden = !k.set;
          const save = async (value) => {
            state.textContent = 'Saving…';
            try {
              render(await put({ [k.env]: value }));
              document.getElementById(id)?.closest('.field')?.querySelector('.desktop-key-state')?.replaceChildren(value ? 'Saved' : 'Removed');
            } catch (e) {
              state.textContent = `Not saved: ${e.message}`;
            }
          };
          input.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') input.blur();
          });
          input.addEventListener('change', () => input.value.trim() && save(input.value.trim()));
          remove.addEventListener('click', () => save(''));
          const link = el('a', { href: k.url, target: '_blank', rel: 'noopener', textContent: 'Get a key' });
          return el(
            'section',
            { className: 'field' },
            el('div', { className: 'field-head' }, el('label', { htmlFor: id, textContent: k.label }), el('span', { className: 'desktop-key-actions' }, state, link, remove)),
            input,
            el('p', { className: 'hint', textContent: k.help }),
          );
        }),
      );
    };

    const load = () =>
      fetch(API, { cache: 'no-store' })
        .then((r) => r.json())
        .then((data) => (render(data), data));
    load().then((data) => {
      // First start without the key that matters: show where it goes, once.
      const ASKED = 'simplixity-desktop-keys-asked';
      try {
        if (localStorage.getItem(ASKED) === '1') return;
        localStorage.setItem(ASKED, '1');
      } catch {
        return;
      }
      if (data.keys.find((k) => k.env === 'OPENROUTER_API_KEY')?.set) return;
      // Upstream wires the Settings button during its own start; keep asking
      // until it has (or give up quietly).
      const overlay = document.getElementById('settings-overlay');
      let tries = 0;
      const show = () => {
        if (!overlay.hidden) return select();
        if (tries++ > 20) return;
        document.getElementById('open-settings')?.click();
        setTimeout(show, 150);
      };
      if (document.readyState === 'complete') show();
      else addEventListener('load', show, { once: true });
    }, () => panel.replaceChildren(el('p', { className: 'hint', textContent: 'API keys cannot be edited here right now.' })));
  }

  // ── Updates ───────────────────────────────────────────────────────
  // The app looks for a newer release by itself (updater.py, at start and
  // every few hours). When there is one, a notice says so; Install downloads
  // it, and it is used from the next start. Settings -> Updates shows the
  // same, with a button to look right now.
  {
    const API = '/desktop/api/update';
    const notice = el('div', { className: 'desktop-update-notice', hidden: true });
    notice.setAttribute('role', 'status');
    document.body.append(notice);
    const panel = strip && body ? addTab('update', 'Updates').panel : null;
    let dismissed = false;

    const install = async () => {
      render({ busy: true });
      try {
        render(await send(`${API}/install`, 'POST'));
      } catch (e) {
        render({ ...(await get()), error: e.message });
      }
    };
    const check = async () => {
      try {
        render(await send(`${API}/check`, 'POST'));
      } catch (e) {
        render({ ...(await get()), error: e.message });
      }
    };
    const get = () => fetch(API, { cache: 'no-store' }).then((r) => (r.ok ? r.json() : {}), () => ({}));
    const btn = (text, onClick) => {
      const b = el('button', { type: 'button', className: 'ghost-btn', textContent: text });
      b.addEventListener('click', onClick);
      return b;
    };

    // What to say, for the notice and the tab alike: [text, ...actions].
    const describe = (st) => {
      if (st.busy) return ['Installing the update…'];
      if (st.staged) return [`Version ${st.staged} is installed. Quit and reopen Simplixity to use it.`];
      const a = st.available;
      if (a && a.installer_only) {
        const link = el('a', { href: a.page, target: '_blank', rel: 'noopener', textContent: 'Download' });
        return [`Version ${a.version} is out. It needs a new download of the app.`, link];
      }
      if (a) return [`Version ${a.version} is available.`, btn('Install', install)];
      return null;
    };

    function render(st) {
      const said = describe(st);
      notice.hidden = !said || dismissed;
      if (said) {
        const close = btn('Later', () => ((dismissed = true), (notice.hidden = true)));
        notice.replaceChildren(el('span', { textContent: said[0] }), ...said.slice(1), ...(st.busy ? [] : [close]));
      }
      if (!panel) return;
      if (st.enabled === false) {
        panel.replaceChildren(el('p', { className: 'hint', textContent: 'Updates only work in the installed app.' }));
        return;
      }
      panel.replaceChildren(
        el('section', { className: 'field' },
          el('div', { className: 'field-head' }, el('label', { textContent: 'Version' })),
          el('p', { className: 'hint', textContent: st.version ?? '' }),
        ),
        el('p', { className: 'hint', textContent: said ? said[0] : 'This is the newest version.' }),
        el('div', { className: 'desktop-update-actions' }, ...(said ? said.slice(1) : []), ...(st.busy ? [] : [btn('Check now', check)])),
        ...(st.error ? [el('p', { className: 'hint desktop-update-error', textContent: st.error })] : []),
      );
    }

    // The first check runs while the window opens; ask again a little later.
    get().then(render);
    setTimeout(() => get().then(render), 15000);
    setInterval(() => get().then(render), 30 * 60 * 1000);
  }

  // Leftover from before the skin: an offline shell registered by the browser
  // build. /sw.js now answers with a worker that removes itself, this only
  // speeds that up.
  navigator.serviceWorker?.getRegistrations?.().then((all) => all.forEach((r) => r.unregister()));
})();
