// review-desk page script (served from /static so the CSP can forbid inline script).
// Packet page: per-item PUTs (merge on the server), debounced notes, tally, and a poll so other tabs' changes show up.
(() => {
  const fmt = iso => { const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString(); };

  if (document.body.dataset.page === "index") {
    for (const t of document.querySelectorAll("time[data-iso]")) if (t.dataset.iso !== "never") t.textContent = fmt(t.dataset.iso);
    return;
  }
  if (document.body.dataset.page !== "packet") return;

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

  function refresh(first) {
    return fetch(`${api}/state`, { cache: "no-store" })
      .then(r => { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(s => { state = s || {}; render(); if (first) setConn(true, "Connected: decisions save as you click"); })
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
