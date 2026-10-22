// review-desk page script (served from /static so the CSP can forbid inline script).
// Packet page: per-item PUTs (merge on the server), debounced notes, tally, and a poll so other tabs' changes show up.
(() => {
  const fmt = iso => { const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(); };

  if (document.body.dataset.page === "index") {
    for (const t of document.querySelectorAll("time[data-iso]")) if (t.dataset.iso !== "never") t.textContent = fmt(t.dataset.iso);
    // "Show packets with nothing pending": a per-viewer preference (localStorage), off by default.
    const doneToggle = document.getElementById("done-toggle");
    if (doneToggle) {
      const DONE_KEY = "review-desk:show-done";
      const applyDone = on => { doneToggle.checked = on; document.body.classList.toggle("show-done", on); };
      try { applyDone(localStorage.getItem(DONE_KEY) === "1"); } catch { applyDone(false); }
      doneToggle.addEventListener("change", () => {
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
  wrapToggle.addEventListener("change", () => {
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
      const d = state[card.dataset.id]?.decision;
      if (!document.body.classList.contains("hide-reviewed") || (d !== "approve" && d !== "drop")) return cancelHide(card);
      hideTimers.delete(card);
      collapse(card);
    }, HIDE_DELAY));
  }
  hideToggle.addEventListener("change", () => {
    applyHide(hideToggle.checked);
    for (const c of [...hideTimers.keys()]) cancelHide(c);
    for (const c of document.querySelectorAll(".just-decided")) c.classList.remove("just-decided");
    try { localStorage.setItem(HIDE_KEY, hideToggle.checked ? "1" : "0"); } catch { /* not persisted */ }
  });

  // Instructions: a native modal <dialog>. The bar button toggles it; Esc, Close and a click on the backdrop dismiss it.
  const helpToggle = document.getElementById("help-toggle");
  const helpDialog = document.getElementById("help-dialog");
  helpToggle.addEventListener("click", () => {
    if (helpDialog.open) helpDialog.close(); else { helpDialog.showModal(); helpToggle.setAttribute("aria-expanded", "true"); }
  });
  helpDialog.addEventListener("close", () => { helpToggle.setAttribute("aria-expanded", "false"); helpToggle.focus(); });
  helpDialog.addEventListener("click", ev => { if (ev.target === helpDialog) helpDialog.close(); });

  const pid = document.body.dataset.pid;
  const api = `/api/p/${pid}`;
  const conn = document.getElementById("conn");
  const offline = document.getElementById("offline");
  let connected = false;
  const cards = [...document.querySelectorAll(".card")];
  const label = { approve: "Approved", revise: "Revise", drop: "Dropped", "": "Not reviewed" };
  let state = {};
  const queues = {}; // per-item promise chain keeps one item's writes in order

  function render() {
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
      if (document.activeElement !== note && !note.dataset.dirty) note.value = s.note || "";
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
    state[id] = { ...(state[id] || {}), ...patch };
    render();
    const run = () => fetch(`${api}/items/${id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json", "X-Review-Desk": "1" },
      body: JSON.stringify(patch),
    }).then(async r => {
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.status);
      state[id] = await r.json();
      if ("note" in patch) delete cards.find(c => c.dataset.id === id).querySelector("[data-note]").dataset.dirty;
      render();
      setConn(true, "Saved");
    }).catch(e => {
      // Undo the optimistic change, so nothing looks saved that wasn't.
      if (before === undefined) delete state[id]; else state[id] = before;
      render();
      setConn(false, `Not saved (${e.message})`);
    });
    queues[id] = (queues[id] || Promise.resolve()).then(run);
  }

  for (const card of cards) {
    const id = card.dataset.id;
    for (const b of card.querySelectorAll("button[data-decision]")) {
      b.addEventListener("click", () => put(id, { decision: b.dataset.decision }));
    }
    const note = card.querySelector("[data-note]");
    let t;
    note.addEventListener("input", () => {
      note.dataset.dirty = "1";
      clearTimeout(t);
      t = setTimeout(() => put(id, { note: note.value }), 700);
    });
    note.addEventListener("blur", () => {
      clearTimeout(t);
      if ((state[id]?.note || "") !== note.value) put(id, { note: note.value });
    });
  }

  // Live drafts: when an agent edits a draft file, swap that one draft's body in place. Nothing else on the page is
  // touched (notes being typed, open drafts, scroll position, the help dialog), so there is never a reload.
  let draftsBusy = false;
  async function refreshDrafts() {
    if (draftsBusy) return;
    draftsBusy = true;
    try {
      const r = await fetch(`${api}/drafts`, { cache: "no-store" });
      if (!r.ok) return;
      // The manifest (items, titles, facts) changed: offer a reload rather than doing one, which would lose open drafts.
      const layout = r.headers.get("X-Layout-Version");
      if (layout && layout !== document.body.dataset.layoutVersion) document.getElementById("layout-changed").hidden = false;
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

  function refresh(first) {
    return fetch(`${api}/state`, { cache: "no-store" })
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(s => { state = s || {}; render(); if (first) setConn(true, "Connected: decisions save as you click"); refreshDrafts(); })
      .catch(() => setConn(false, "Not connected"));
  }

  render();
  setConn(false, "Connecting…");
  refresh(true);
  // A cheap local check every 3 s: a desk that stops shows the banner within seconds,
  // and the banner clears soon after it's back.
  setInterval(() => { if (!document.hidden) refresh(!connected); }, 3000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(false); });
})();
