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
              // Only this field changes: the others keep their focus and
              // whatever is being typed into them.
              const now = (await put({ [k.env]: value })).keys?.find((x) => x.env === k.env);
              input.value = '';
              input.placeholder = now?.set ? `Saved (${now.set}). Type to replace.` : 'Not set';
              remove.hidden = !now?.set;
              state.textContent = value ? 'Saved' : 'Removed';
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

  // ── Toasts ────────────────────────────────────────────────────────
  // Bottom right, newest at the bottom. One per subject (`id`): showing it
  // again replaces its content, so a download's toast counts up in place.
  const toasts = el('div', { className: 'desktop-toasts' });
  toasts.setAttribute('role', 'status');
  toasts.setAttribute('aria-live', 'polite');
  document.body.append(toasts);
  const toast = (id, parts) => {
    let t = toasts.querySelector(`[data-toast="${id}"]`);
    if (!parts) return t?.remove();
    if (!t) {
      t = el('div', { className: 'desktop-toast' });
      t.dataset.toast = id;
      toasts.append(t);
    }
    t.replaceChildren(...parts);
  };
  const btn = (text, onClick, primary = false) => {
    const b = el('button', { type: 'button', className: primary ? 'ghost-btn desktop-primary' : 'ghost-btn', textContent: text });
    b.addEventListener('click', onClick);
    return b;
  };
  const mb = (bytes) => (bytes >= 1e9 ? `${(bytes / 1e9).toFixed(1)} GB` : `${Math.round(bytes / 1e6)} MB`);
  // Without a total the bar has no value at all: it shows that something
  // moves, not how far. (Setting value to undefined throws.)
  const bar = (done, total) => el('progress', total ? { max: total, value: Math.min(done || 0, total) } : {});

  // ── Updates ───────────────────────────────────────────────────────
  // The app looks for a newer release by itself (at start and every few
  // hours). A toast says when there is one. Install downloads it with a
  // progress bar; once it is in place, Restart now opens the new version.
  // Settings -> Updates shows the same, and Check now asks right away.
  {
    const API = '/desktop/api/update';
    const panel = strip && body ? addTab('update', 'Updates').panel : null;
    const dismissed = new Set();
    let st = {};
    let checking = false;
    let checked = false;
    let restarting = false;
    let poll = null;

    const get = () => fetch(API, { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null), () => null);
    const refresh = async () => {
      const next = await get();
      if (next) show(next);
    };
    const post = async (action) => {
      try {
        return await send(`${API}/${action}`, 'POST');
      } catch (e) {
        return { ...st, error: e.message, errorFrom: action };
      }
    };
    const install = async () => {
      dismissed.delete('error'); // a new try may say again that it failed
      show(await post('install'));
    };
    const check = async () => {
      checking = true;
      show(st);
      const next = await post('check');
      checking = false;
      checked = true;
      show(next);
    };
    const restart = async () => {
      restarting = true;
      show(st);
      const res = await post('restart');
      if (!res.restarting) {
        restarting = false;
        show(res);
      }
    };

    // What to say: { key, text, parts }, or null. `parts` makes new elements on
    // each call: the toast and the tab each need their own.
    const describe = (s) => {
      if (restarting) return { key: 'restart', text: 'Restarting Simplixity…' };
      const p = s.progress;
      if (s.busy && p) {
        const what = p.version ? `Simplixity ${p.version}` : 'the update';
        const text =
          p.phase === 'downloading'
            ? `Downloading ${what}: ${mb(p.done)} of ${mb(p.total)}`
            : p.phase === 'unpacking'
              ? `Installing ${what}…`
              : `Starting the download of ${what}…`;
        return { key: 'busy', text, parts: () => [bar(p.phase === 'downloading' ? p.done : 0, p.phase === 'downloading' ? p.total : 0)] };
      }
      if (p?.phase === 'error') {
        return { key: `error`, text: `The update did not install: ${p.error}`, parts: () => [btn('Try again', install, true)] };
      }
      if (s.staged) {
        return { key: `staged-${s.staged}`, text: `Simplixity ${s.staged} is ready.`, parts: () => [btn('Restart now', restart, true)] };
      }
      const a = s.available;
      if (a && a.installer_only) {
        const link = () => el('a', { href: a.page, target: '_blank', rel: 'noopener', textContent: 'Download', className: 'ghost-btn desktop-primary' });
        return { key: `installer-${a.version}`, text: `Simplixity ${a.version} is available. It needs a new download of the app.`, parts: () => [link()] };
      }
      if (a) return { key: `available-${a.version}`, text: `Simplixity ${a.version} is available.`, parts: () => [btn('Install', install, true)] };
      return null;
    };

    function show(next) {
      st = next || {};
      const said = describe(st);
      // The toast: everything but a finished check that found nothing.
      if (said && !dismissed.has(said.key)) {
        const later = said.key === 'busy' || said.key === 'restart' ? [] : [btn('Later', () => (dismissed.add(said.key), toast('update', null)))];
        toast('update', [el('span', { className: 'desktop-toast-text', textContent: said.text }), ...(said.parts?.() ?? []), ...later]);
      } else {
        toast('update', null);
      }
      // While something moves, ask often; otherwise now and then.
      clearTimeout(poll);
      poll = setTimeout(refresh, st.busy ? 500 : 5 * 60 * 1000);
      if (!panel) return;
      if (st.enabled === false) {
        panel.replaceChildren(el('p', { className: 'hint', textContent: 'Updates only work in the installed app.' }));
        return;
      }
      const line = checking
        ? 'Looking for a newer version…'
        : said
          ? said.text
          : st.error
            ? st.errorFrom && st.errorFrom !== 'check'
              ? st.error
              : `Could not look for updates: ${st.error}`
            : checked
              ? 'This is the newest version.'
              : 'Simplixity looks for updates by itself every few hours.';
      const checkBtn = btn(checking ? 'Checking…' : 'Check now', check);
      checkBtn.disabled = checking || !!st.busy || restarting;
      panel.replaceChildren(
        el('section', { className: 'field' },
          el('div', { className: 'field-head' }, el('label', { textContent: 'Version' })),
          el('p', { className: 'hint', textContent: st.version ?? '' }),
        ),
        el('p', { className: 'hint', textContent: line }),
        el('div', { className: 'desktop-update-actions' }, ...(said?.parts?.() ?? []), checkBtn),
      );
    }

    // The first check runs while the window opens; ask again a little later.
    refresh();
    setTimeout(refresh, 15000);
  }

  // ── Simplixity's local models ─────────────────────────────────────
  // With Ollama on this computer, each Settings picker that has a model of
  // Simplixity's own (found on Hugging Face by desktop_api.py) gets an offer
  // below it while Ollama does not have that model yet: Download, then
  // progress, then "Use it", which picks it in that picker the way a click
  // would. Models Ollama has are kept at their newest version by the app; a
  // toast shows such an update while it runs. Ollama installed but not
  // running: one line under the chat model says so. Not installed: nothing.
  {
    const API = '/desktop/api/local-models';
    const boxes = new Map();
    const SWITCH = { 'router-model': 'router-on', 'title-model': 'title-on', 'embedding-model': 'embedding-enabled' };
    const ACTIVE = new Set(['starting', 'downloading', 'creating']);
    const used = new Set();
    const announced = new Set();
    let timer = null;

    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const get = () => fetch(API, { cache: 'no-store' }).then((r) => (r.ok ? r.json() : null), () => null);
    const act = (key, verb) =>
      send(`${API}/${key}/${verb}`, 'POST').then(render, (e) => {
        boxes.get(key)?.querySelector('.desktop-model-line')?.replaceChildren(`Not started: ${e.message}`);
      });
    const name = (m) => (m.version ? `${m.label} ${m.version}` : m.label);
    const progressText = (m, job) =>
      job.state === 'creating'
        ? `Setting up ${name(m)} in Ollama…`
        : job.state === 'starting'
          ? `${job.update ? 'Updating' : 'Downloading'} ${name(m)}…`
          : `${job.update ? 'Updating' : 'Downloading'} ${name(m)}: ${mb(job.done)} of ${mb(job.total)}`;

    // Pick the new model in its picker: the switch beside it on, the
    // catalogue refreshed, then its row in the Local tab clicked, which
    // saves it like a choice made by hand. Gives up quietly.
    const use = async (m) => {
      const slot = document.getElementById(m.slot);
      if (!slot) return;
      const toggle = document.getElementById(SWITCH[m.slot]);
      if (toggle && !toggle.checked) toggle.click();
      document.getElementById('refresh-models')?.click();
      const tag = m.tag.toLowerCase();
      for (let i = 0; i < 25; i++) {
        await sleep(400);
        const trigger = slot.querySelector('.picker-trigger');
        if (!trigger) continue;
        if (trigger.getAttribute('aria-expanded') !== 'true') trigger.click();
        slot.querySelector('.picker-tab[data-tab="local"]:not([disabled])')?.click();
        const row = [...slot.querySelectorAll('.picker-row')].find((r) => {
          const n = r.querySelector('.picker-row-name')?.textContent.toLowerCase() ?? '';
          return n === tag || n.startsWith(`${tag} (`);
        });
        if (row) {
          row.click();
          used.add(m.key);
          return get().then(render);
        }
        trigger.click(); // closed again, so the next try opens the reloaded list
      }
    };

    // Below the picker.
    const describe = (m, ollama) => {
      const job = m.job;
      if (job?.quiet) return null;
      if (job && ACTIVE.has(job.state)) {
        return [
          el('span', { className: 'desktop-model-line', textContent: progressText(m, job) }),
          bar(job.done, job.total),
          btn('Cancel', () => act(m.key, 'cancel')),
        ];
      }
      if (m.installed) {
        if (job?.state !== 'done' || job.update || used.has(m.key)) return null;
        return [el('span', { className: 'desktop-model-line', textContent: `${m.label} is ready.` }), btn('Use it', () => use(m), true)];
      }
      if (!ollama.running) return null;
      return [
        el('span', { className: 'desktop-model-line' }, el('strong', { textContent: m.label }), ` · ${mb(m.size)} · ${m.about}`),
        btn(job?.state === 'error' ? 'Try again' : 'Download', () => act(m.key, 'install')),
        el('a', { href: m.page, target: '_blank', rel: 'noopener', textContent: 'About' }),
        ...(job?.state === 'error'
          ? [el('p', { className: 'hint desktop-update-error', textContent: `Download failed: ${job.error}` })]
          : []),
      ];
    };

    // Bottom right: an update the app started by itself.
    const updateToast = (m) => {
      const job = m.job;
      const id = `model-${m.key}`;
      if (!job?.update || job.quiet) return;
      if (ACTIVE.has(job.state)) {
        announced.delete(id); // this run's end is said again, also after an earlier one
        toast(id, [el('span', { className: 'desktop-toast-text', textContent: progressText(m, job) }), bar(job.done, job.total)]);
      } else if (job.state === 'cancelled') {
        toast(id, null);
      } else if (job.state === 'done' && !announced.has(id)) {
        announced.add(id);
        toast(id, [el('span', { className: 'desktop-toast-text', textContent: `${name(m)} is installed. It is used from the next question on.` })]);
        setTimeout(() => toast(id, null), 8000);
      } else if (job.state === 'error' && !announced.has(id)) {
        announced.add(id);
        toast(id, [
          el('span', { className: 'desktop-toast-text', textContent: `${m.label} could not be updated: ${job.error}` }),
          btn('Close', () => toast(id, null)),
        ]);
      }
    };

    function render(st) {
      if (!st?.models) return;
      for (const m of st.models) {
        updateToast(m);
        const slot = document.getElementById(m.slot);
        if (!slot) continue;
        let box = boxes.get(m.key);
        if (!box) {
          box = el('div', { className: 'desktop-model-offer', hidden: true });
          box.setAttribute('role', 'status');
          slot.after(box);
          boxes.set(m.key, box);
        }
        let said = describe(m, st.ollama);
        if (!said && !st.ollama.running && st.ollama.installed && m.slot === 'settings-chat-model' && !m.installed) {
          said = [
            el('span', {
              className: 'desktop-model-line',
              textContent: 'Start Ollama to download Simplixity’s own models, which run on this computer.',
            }),
          ];
        }
        box.hidden = !said;
        if (said) box.replaceChildren(...said);
      }
      // Every second while something downloads, otherwise once a minute:
      // the app starts updates by itself, and their toast should show.
      clearTimeout(timer);
      const busy = st.models.some((m) => m.job && ACTIVE.has(m.job.state) && !m.job.quiet);
      timer = setTimeout(() => get().then(render), busy ? 1000 : 60000);
    }

    get().then(render);
    document.getElementById('open-settings')?.addEventListener('click', () => get().then(render));
  }

  // Leftover from before the skin: an offline shell registered by the browser
  // build. /sw.js now answers with a worker that removes itself, this only
  // speeds that up.
  navigator.serviceWorker?.getRegistrations?.().then((all) => all.forEach((r) => r.unregister()));
})();
