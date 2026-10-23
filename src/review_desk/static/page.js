// review-desk page script (served from /static so the CSP can forbid inline script).
// Packet page: per-item PUTs (merge on the server), debounced notes, tally, and a poll so other tabs' changes show up.
(() => {
  // Hot swap: a newer copy of this script replaces the running one in place. The old copy detaches
  // everything it bound and stops polling; the DOM (notes being typed, open drafts, scroll) is left exactly as it is.
  const hot = typeof window.__deskDispose === "function";
  if (hot) window.__deskDispose();
  const ac = new AbortController();
  const listen = (target, type, fn, opts) => target.addEventListener(type, fn, { ...opts, signal: ac.signal });
  let poll = null, dead = false;
  const shareTimers = [];
  // A replaced copy must not touch the page any more (render, connection state, fetch results), or it would fight the
  // new one with its frozen state. Pending note saves still go out: they're the human's text.
  window.__deskDispose = () => { dead = true; ac.abort(); clearInterval(poll); shareTimers.forEach(clearInterval); };

  const fmt = iso => { const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(); };
  // How long ago, for the "Last changed" line and its list: seconds under a minute, then minutes, hours, days.
  const agoText = sec => {
    if (!(sec >= 0)) sec = 0; // NaN, or a clock a little behind the file's
    if (sec < 10) return "just now";
    if (sec < 60) return `${Math.floor(sec)} s ago`;
    if (sec < 3600) return `${Math.floor(sec / 60)} min ago`;
    if (sec < 86400) return `${Math.floor(sec / 3600)} h ago`;
    const d = Math.floor(sec / 86400);
    return `${d} day${d === 1 ? "" : "s"} ago`;
  };
  const stamp = iso => { const t = new Date(iso).getTime(); return isNaN(t) ? fmt(iso) : `${fmt(iso)} (${agoText((Date.now() - t) / 1000)})`; };

  // Share mode (desk started with --share): a button that opens a 4-hour window for the local network, counts it down, and
  // on a click either resets it to 4:00:00 or, right after a start or reset (3:59 or more left), ends it. Its tooltip lists
  // the URLs to open from another device. The server decides what a click does; hidden entirely when sharing isn't enabled.
  function initShare(el) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "share-btn";
    const err = document.createElement("span");
    err.className = "share-error";
    el.append(btn, err);
    let s = null, syncedAt = 0, busy = false;
    const clock = sec => {
      sec = Math.ceil(sec);
      const two = n => String(n).padStart(2, "0");
      return `${Math.floor(sec / 3600)}:${two(Math.floor(sec / 60) % 60)}:${two(sec % 60)}`;
    };
    const setTitle = t => { if (btn.title !== t) btn.title = t; };
    function paint() {
      if (dead) return;
      if (!s || !s.enabled) { el.hidden = true; return; }
      el.hidden = false;
      const left = Math.max(0, s.remaining - (Date.now() - syncedAt) / 1000);
      const on = s.active && left > 0;
      btn.setAttribute("aria-pressed", String(on));
      btn.disabled = busy || !s.canControl;
      if (!on) {
        btn.textContent = "Share on this network (4h)";
        setTitle(s.canControl ? "Let devices on your local network open this desk for 4 hours"
                              : "Sharing is switched on from the machine running the desk, at its localhost address");
        return;
      }
      btn.textContent = `Sharing ${clock(left)}`;
      const urls = s.urls.map(u => `${new URL(location.pathname, u.url).href}  (${u.label})`);
      const act = !s.canControl ? "Only the machine running the desk can change this."
        : left < s.bumpBelow ? "Click to reset the timer to 4:00:00." : "Click to stop sharing.";
      setTitle(`Open from this network:\n${urls.length ? urls.join("\n") : "(no network address found)"}\n\n${act}`);
    }
    async function sync(action) {
      try {
        const r = await fetch("/api/share", action ? {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Review-Desk": "1" },
          body: JSON.stringify({ action }),
        } : { cache: "no-store" });
        const body = await r.json().catch(() => ({}));
        if (dead) return;
        if (!r.ok) throw new Error(body.error || r.status);
        s = body;
        syncedAt = Date.now();
        err.textContent = "";
      } catch (e) {
        if (!dead && action) err.textContent = `Couldn't change sharing (${e.message})`;
      }
      paint();
    }
    listen(btn, "click", async () => {
      busy = true;
      paint();
      await sync("toggle");
      busy = false;
      paint();
    });
    shareTimers.push(setInterval(paint, 1000), setInterval(() => { if (!document.hidden) sync(); }, 5000));
    sync();
  }
  const shareEl = document.getElementById("share");
  if (shareEl) initShare(shareEl);

  if (document.body.dataset.page === "index") {
    for (const t of document.querySelectorAll("time[data-iso]")) if (t.dataset.iso !== "never") t.textContent = fmt(t.dataset.iso);
    // "Show packets with nothing pending": a per-viewer preference (localStorage), off by default.
    const doneToggle = document.getElementById("done-toggle");
    if (doneToggle) {
      const DONE_KEY = "review-desk:show-done";
      const applyDone = on => { doneToggle.checked = on; document.body.classList.toggle("show-done", on); };
      try { applyDone(localStorage.getItem(DONE_KEY) === "1"); } catch { applyDone(false); }
      listen(doneToggle, "change", () => {
        applyDone(doneToggle.checked);
        try { localStorage.setItem(DONE_KEY, doneToggle.checked ? "1" : "0"); } catch { /* not persisted */ }
      });
    }
    return;
  }
  if (document.body.dataset.page !== "packet") return;

  // "Wrap long lines": a per-viewer viewing preference, so it lives in localStorage, not in the desk.
  // Storage can be unavailable (private window, blocked site data); the toggle then just doesn't persist.
  const wrapToggle = document.getElementById("wrap-toggle");
  const WRAP_KEY = "review-desk:wrap-lines";
  const applyWrap = on => { wrapToggle.checked = on; document.body.classList.toggle("wrap-lines", on); };
  try { applyWrap(localStorage.getItem(WRAP_KEY) === "1"); } catch { applyWrap(false); }
  listen(wrapToggle, "change", () => {
    applyWrap(wrapToggle.checked);
    try { localStorage.setItem(WRAP_KEY, wrapToggle.checked ? "1" : "0"); } catch { /* not persisted */ }
  });

  // "Hide reviewed": same idea. Approved and dropped cards hide; a card decided in this page view (.just-decided) stays
  // until the toggle changes, so a mis-click doesn't make the item vanish before it can be undone.
  const hideToggle = document.getElementById("hide-reviewed-toggle");
  const HIDE_KEY = "review-desk:hide-reviewed";
  const applyHide = on => { hideToggle.checked = on; document.body.classList.toggle("hide-reviewed", on); };
  try { applyHide(localStorage.getItem(HIDE_KEY) === "1"); } catch { applyHide(false); }
  // With the toggle on, a card marked approved or dropped fades out HIDE_DELAY ms later. Deciding again before then
  // (an undo, or a switch to revise) cancels it.
  const HIDE_DELAY = 3000, FADE_MS = 450;
  const hideTimers = new Map();
  const resetCard = card => {
    card.classList.remove("fading");
    for (const p of ["height", "padding-top", "padding-bottom", "border-width", "margin-bottom", "overflow"]) card.style.removeProperty(p);
  };
  const cancelHide = card => {
    clearTimeout(hideTimers.get(card));
    hideTimers.delete(card);
    resetCard(card);
  };
  // The card fades and collapses (height, padding, border and the grid gap all animate to 0), so what follows slides up
  // instead of jumping. Meanwhile the first card still in view is held at the same screen position by scrolling the
  // difference away each frame: whatever you're looking at stays put, even when the hiding card is above it.
  function collapse(card) {
    const anchor = cards.find(c => c !== card && c.offsetParent && c.getBoundingClientRect().bottom > 0);
    let last = anchor && anchor.getBoundingClientRect().top;
    const keep = () => {
      if (!anchor) return;
      const top = anchor.getBoundingClientRect().top;
      if (top !== last) window.scrollBy({ top: top - last, behavior: "instant" });
      last = anchor.getBoundingClientRect().top;
    };
    card.querySelector("details.draft").open = false; // fold a long draft away first, so the card collapses from its short form
    keep();
    card.style.height = card.offsetHeight + "px";
    card.style.overflow = "hidden";
    card.classList.add("fading");
    void card.offsetHeight; // commit the explicit height so the transition has a start value
    for (const p of ["height", "padding-top", "padding-bottom", "border-width"]) card.style.setProperty(p, "0");
    card.style.marginBottom = "-16px"; // the .wrap grid gap
    const start = performance.now();
    const tick = now => {
      keep();
      if (now - start < FADE_MS + 50) return requestAnimationFrame(tick);
      card.classList.remove("just-decided"); // display: none now
      resetCard(card);
      hideTimers.delete(card);
      keep();
    };
    requestAnimationFrame(tick);
  }
  function scheduleHide(card) {
    cancelHide(card);
    if (!document.body.classList.contains("hide-reviewed")) return;
    hideTimers.set(card, setTimeout(() => {
      const d = card.dataset.decision; // from the DOM, so a copy that replaced this one is respected
      if (!document.body.classList.contains("hide-reviewed") || (d !== "approve" && d !== "drop")) return cancelHide(card);
      // Not while a note is being typed (focus leaves the page for <body> when the card goes). A focused Approve button
      // doesn't count: clicking it with the mouse leaves it focused.
      const note = card.querySelector("[data-note]");
      if (document.activeElement === note || note.dataset.dirty) return scheduleHide(card);
      hideTimers.delete(card);
      collapse(card);
    }, HIDE_DELAY));
  }
  listen(hideToggle, "change", () => {
    applyHide(hideToggle.checked);
    for (const c of [...hideTimers.keys()]) cancelHide(c);
    for (const c of document.querySelectorAll(".just-decided")) c.classList.remove("just-decided");
    try { localStorage.setItem(HIDE_KEY, hideToggle.checked ? "1" : "0"); } catch { /* not persisted */ }
  });

  // Instructions: a native modal <dialog>. The bar button toggles it; Esc, Close and a click on the backdrop dismiss it.
  const helpToggle = document.getElementById("help-toggle");
  const helpDialog = document.getElementById("help-dialog");
  listen(helpToggle, "click", () => {
    if (helpDialog.open) helpDialog.close(); else { helpDialog.showModal(); helpToggle.setAttribute("aria-expanded", "true"); }
  });
  listen(helpDialog, "close", () => { helpToggle.setAttribute("aria-expanded", "false"); helpToggle.focus(); });
  listen(helpDialog, "click", ev => { if (ev.target === helpDialog) helpDialog.close(); });

  const pid = document.body.dataset.pid;
  const api = `/api/p/${pid}`;
  const conn = document.getElementById("conn");
  const offline = document.getElementById("offline");
  let connected = hot; // a hot swap keeps the page usable: don't disable the fields while reconnecting
  const cards = [...document.querySelectorAll(".card")];
  const label = { approve: "Approved", revise: "Revise", drop: "Dropped", "": "Not reviewed" };
  let state = {};
  let writes = 0; // bumped when a save starts and when it finishes: a poll that overlapped one is older than the page
  const queues = {}; // per-item promise chain keeps one item's writes in order

  function render() {
    if (dead) return;
    const counts = { approve: 0, revise: 0, drop: 0, open: 0 };
    for (const card of cards) {
      const s = state[card.dataset.id] || {};
      const d = s.decision || "";
      card.dataset.decision = d;
      card.querySelector("[data-status]").textContent = label[d] ?? d;
      for (const b of card.querySelectorAll("button[data-decision]")) {
        b.setAttribute("aria-pressed", String(b.dataset.decision === d && d !== ""));
      }
      const note = card.querySelector("[data-note]");
      // Only unsaved typing is protected, not mere focus: a field the human left the cursor in must still follow the
      // server (an agent clearing the note), or its stale text would be written back on blur.
      if (!note.dataset.dirty && note.value !== (s.note || "")) note.value = s.note || "";
      card.querySelector("[data-saved]").textContent = s.updatedAt ? "Saved " + fmt(s.updatedAt) : "";
      if (counts[d] !== undefined && d) counts[d]++; else counts.open++;
    }
    document.getElementById("n-approve").textContent = counts.approve + " approved";
    document.getElementById("n-revise").textContent = counts.revise + " to revise";
    document.getElementById("n-drop").textContent = counts.drop + " dropped";
    document.getElementById("n-open").textContent = counts.open + " not reviewed";
    const reviewed = counts.approve + counts.drop;
    document.getElementById("hide-reviewed-label").textContent = reviewed ? `Hide reviewed (${reviewed})` : "Hide reviewed";
  }

  function setConn(ok, text) {
    if (dead) return;
    connected = ok;
    conn.textContent = text;
    conn.classList.toggle("bad", !ok);
    offline.hidden = ok;
    document.body.classList.toggle("is-offline", !ok);
    for (const el of document.querySelectorAll(".decide button, textarea")) el.disabled = !ok;
  }

  function put(id, patch) {
    if (!connected) return;
    if ("decision" in patch) {
      const card = cards.find(c => c.dataset.id === id);
      card.classList.add("just-decided");
      scheduleHide(card);
    }
    const before = state[id];
    writes++;
    state[id] = { ...(state[id] || {}), ...patch };
    render();
    const run = () => fetch(`${api}/items/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-Review-Desk": "1", "X-Review-Page": "1" },
      body: JSON.stringify(patch),
    }).then(async r => {
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
      state[id] = await r.json();
      writes++;
      if ("note" in patch) {
        const field = cards.find(c => c.dataset.id === id).querySelector("[data-note]");
        if (field.value === patch.note) delete field.dataset.dirty; // typed more since this save went out: still unsaved
      }
      render();
      setConn(true, "Saved");
    }).catch(e => {
      // Undo the optimistic change, so nothing looks saved that wasn't.
      writes++;
      if (before === undefined) delete state[id]; else state[id] = before;
      render();
      setConn(false, `Not saved (${e.message})`);
    });
    queues[id] = (queues[id] || Promise.resolve()).then(run);
  }

  for (const card of cards) {
    const id = card.dataset.id;
    for (const b of card.querySelectorAll("button[data-decision]")) {
      listen(b, "click", () => put(id, { decision: b.dataset.decision }));
    }
    const note = card.querySelector("[data-note]");
    let t;
    listen(note, "input", () => {
      note.dataset.dirty = "1";
      clearTimeout(t);
      t = setTimeout(() => put(id, { note: note.value }), 700);
    });
    listen(note, "blur", () => {
      clearTimeout(t);
      if ((state[id]?.note || "") !== note.value) put(id, { note: note.value });
    });
  }

  // Floating note: while a note field has focus (and its cursor), its box floats at the bottom of the window if that
  // card's draft is on screen and the note's regular place is not. The slot holds the box's height so nothing jumps.
  const inView = r => r.bottom > 0 && r.top < window.innerHeight;
  function updateFloat() {
    for (const card of cards) {
      const slot = card.querySelector(".note-slot");
      const box = slot.firstElementChild;
      const note = box.querySelector("[data-note]");
      const body = card.querySelector(".draft-body");
      const open = card.querySelector("details.draft").open;
      const want = document.activeElement === note && open && inView(body.getBoundingClientRect())
        && !inView(slot.getBoundingClientRect());
      if (want === box.classList.contains("floating")) {
        if (want) { const r = slot.getBoundingClientRect(); box.style.left = r.left + "px"; box.style.width = r.width + "px"; }
        continue;
      }
      if (want) {
        const r = slot.getBoundingClientRect();
        slot.style.height = box.offsetHeight + "px";
        box.style.left = r.left + "px";
        box.style.width = r.width + "px";
      } else {
        slot.style.removeProperty("height");
        box.style.removeProperty("left");
        box.style.removeProperty("width");
      }
      box.classList.toggle("floating", want);
    }
  }
  for (const ev of ["scroll", "resize", "focusin", "focusout", "toggle"]) listen(window, ev, updateFloat, { capture: true, passive: true });
  for (const note of document.querySelectorAll("[data-note]")) listen(note, "input", updateFloat);

  // Live drafts: when an agent edits a draft file, swap that one draft's body in place. Nothing else on the page is
  // touched (notes being typed, open drafts, scroll position, the help dialog), so there is never a reload.
  let draftsBusy = false, staticVersion = null;
  function swapStylesheet(v) {
    const old = document.querySelector('link[rel="stylesheet"][href^="/static/page.css"]');
    const next = old.cloneNode();
    next.href = `/static/page.css?v=${v}`;
    next.onload = () => old.remove(); // the old sheet stays until the new one is in: no flash of unstyled page
    old.after(next);
  }
  function loadScript(v) {
    const el = document.createElement("script");
    el.src = `/static/page.js?v=${v}`;
    document.head.append(el); // runs the new copy, which disposes this one first
  }
  async function refreshDrafts() {
    if (draftsBusy) return;
    draftsBusy = true;
    try {
      const r = await fetch(`${api}/drafts`, { cache: "no-store" });
      if (!r.ok || dead) return;
      // The manifest (items, titles, facts) changed: offer a reload rather than doing one, which would lose open drafts.
      const layout = r.headers.get("X-Layout-Version");
      if (layout && layout !== document.body.dataset.layoutVersion) document.getElementById("layout-changed").hidden = false;
      // apply page.css / page.js changes in place, never a reload (that would lose notes, open
      // drafts and scroll). The stylesheet is swapped; a changed script is loaded and replaces this running copy.
      const sv = r.headers.get("X-Static-Version");
      if (sv) {
        const [css, js] = sv.split(".");
        if (staticVersion && css !== staticVersion.css) swapStylesheet(css);
        if (staticVersion && js !== staticVersion.js) { staticVersion.js = js; return loadScript(js); } // once per change, even if the new file is broken
        staticVersion ??= { css, js };
        staticVersion.css = css;
      }
      const versions = await r.json();
      for (const card of cards) {
        const id = card.dataset.id;
        if (!versions[id] || versions[id] === card.dataset.draftVersion) continue;
        const f = await fetch(`${api}/drafts/${id}`, { cache: "no-store" });
        if (!f.ok) continue;
        const d = await f.json();
        card.querySelector(".draft-body").innerHTML = d.html;
        card.dataset.draftVersion = d.version;
        card.querySelector("[data-draft-updated]").textContent = "Draft updated " + new Date().toLocaleTimeString();
      }
    } catch { /* the next tick tries again; connectivity is shown by refresh() */ }
    finally { draftsBusy = false; }
  }

  // Earlier notes: what the human asked for before an agent revised the draft and cleared the note. Built with
  // textContent only, newest first, collapsed; it never touches the draft or the notes field.
  let historyJson = "";
  async function refreshHistory() {
    try {
      const r = await fetch(`${api}/history`, { cache: "no-store" });
      if (!r.ok) return;
      const text = await r.text();
      if (dead || text === historyJson) return;
      historyJson = text;
      const h = JSON.parse(text);
      for (const card of cards) {
        const entries = Array.isArray(h[card.dataset.id]) ? [...h[card.dataset.id]].reverse() : [];
        const box = card.querySelector("[data-earlier]");
        const list = box.querySelector("[data-earlier-list]");
        list.replaceChildren(...entries.map(en => {
          const li = document.createElement("li");
          const t = document.createElement("time");
          t.textContent = "Revision requested " + fmt(en.at);
          li.append(t, String(en.note));
          return li;
        }));
        box.querySelector("[data-earlier-count]").textContent = entries.length;
        box.hidden = entries.length === 0;
      }
    } catch { /* the next tick tries again */ }
  }

  // External changes (a draft file, the manifest entry, an agent's decision or note write), from review-changes.json. The
  // newest shows on the card; hovering or focusing it opens the full list (newest first, scrollable). textContent only.
  const CHANGE_LABEL = { draft: "Draft edited", manifest: "Item details edited", decision: "Decision set by an agent",
    note: "Note changed by an agent", "decisions-file": "Decisions file edited" };
  let changesJson = "";
  async function refreshChanges() {
    try {
      const r = await fetch(`${api}/changes`, { cache: "no-store" });
      if (!r.ok) return;
      const text = await r.text();
      if (dead || text === changesJson) return;
      changesJson = text;
      const h = JSON.parse(text);
      for (const card of cards) {
        const entries = Array.isArray(h[card.dataset.id]) ? [...h[card.dataset.id]].reverse() : [];
        const box = card.querySelector("[data-changed]");
        box.hidden = entries.length === 0;
        if (!entries.length) continue;
        // A coarse entry came from a scan that hadn't looked for a while, so it may be several changes.
        const name = en => (CHANGE_LABEL[en.kind] || String(en.kind)) + (en.coarse ? " (possibly several changes)" : "");
        const line = box.querySelector("[data-changed-text]");
        line.dataset.at = entries[0].at;
        line.dataset.label = name(entries[0]);
        line.textContent = `Last changed ${stamp(entries[0].at)} · ${line.dataset.label}`;
        box.querySelector("[data-changed-list]").replaceChildren(...entries.map(en => {
          const li = document.createElement("li");
          const t = document.createElement("time");
          t.dataset.at = en.at;
          t.textContent = stamp(en.at);
          li.append(t, ` ${name(en)}${en.detail ? ": " + String(en.detail) : ""}`);
          return li;
        }));
      }
    } catch { /* the next tick tries again */ }
  }

  // The "(5 min ago)" parts move on without another fetch.
  function tickAgo() {
    if (dead) return;
    for (const line of document.querySelectorAll("[data-changed-text][data-at]")) {
      line.textContent = `Last changed ${stamp(line.dataset.at)} · ${line.dataset.label}`;
    }
    for (const t of document.querySelectorAll("[data-changed-list] time[data-at]")) t.textContent = stamp(t.dataset.at);
  }
  shareTimers.push(setInterval(tickAgo, 5000));

  function refresh(first) {
    const w = writes;
    return fetch(`${api}/state`, { cache: "no-store" })
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(s => { if (dead || w !== writes) return; state = s || {}; render(); if (first) setConn(true, "Connected: decisions save as you click"); refreshDrafts(); refreshHistory(); refreshChanges(); })
      .catch(() => setConn(false, "Not connected")); // setConn ignores a replaced copy
  }

  if (!hot) { render(); setConn(false, "Connecting…"); }
  refresh(true);
  // A cheap local check every 3 s: a desk that stops shows the banner within seconds,
  // and the banner clears soon after it's back.
  poll = setInterval(() => { if (!document.hidden) refresh(!connected); }, 3000);
  listen(document, "visibilitychange", () => { if (!document.hidden) refresh(false); });
})();
