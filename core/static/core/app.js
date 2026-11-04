/* Keyboard-first behaviour: command palette (⌘K), g-shortcuts, ? sheet, j/k row navigation, chart theme. */
(function () {
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const typing = (e) => {
    const t = e.target;
    return t.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(t.tagName);
  };
  const csrf = () => (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || ($('input[name=csrfmiddlewaretoken]') || {}).value;

  // ---------- command palette
  const commands = JSON.parse(($("#palette-data") || {}).textContent || "[]");
  const overlay = $("#palette"), input = $("#palette input"), list = $("#palette ul");
  let filtered = [], sel = 0;

  function score(label, q) {
    label = label.toLowerCase(); q = q.toLowerCase().trim();
    if (!q) return 1;
    if (label.includes(q)) return 100 - label.indexOf(q);
    let i = 0;
    for (const ch of label) if (ch === q[i]) i++;
    return i === q.length ? 10 : 0;
  }
  function render() {
    const q = input.value;
    filtered = commands.map(c => [score(c.label, q), c]).filter(([s]) => s > 0).sort((a, b) => b[0] - a[0]).map(([, c]) => c);
    sel = Math.min(sel, Math.max(filtered.length - 1, 0));
    list.innerHTML = filtered.length ? filtered.map((c, i) =>
      `<li class="${i === sel ? "sel" : ""}" data-i="${i}"><span>${c.label}</span>${c.key ? `<span class="kbd">${c.key}</span>` : ""}</li>`
    ).join("") : `<div class="empty">No matching command</div>`;
  }
  function run(c) {
    if (!c) return;
    if (c.url) { location.href = c.url; return; }
    const f = document.createElement("form");
    f.method = "post"; f.action = c.post;
    const data = Object.assign({ csrfmiddlewaretoken: csrf() }, c.data || {});
    for (const [k, v] of Object.entries(data)) {
      const i = document.createElement("input"); i.type = "hidden"; i.name = k; i.value = v; f.appendChild(i);
    }
    document.body.appendChild(f); f.submit();
  }
  function openPalette() { if (!overlay) return; overlay.classList.add("open"); input.value = ""; sel = 0; render(); input.focus(); }
  function closeAll() { $$(".overlay").forEach(o => o.classList.remove("open")); }
  if (overlay) {
    input.addEventListener("input", () => { sel = 0; render(); });
    input.addEventListener("keydown", e => {
      if (e.key === "ArrowDown") { sel = Math.min(sel + 1, filtered.length - 1); render(); e.preventDefault(); }
      if (e.key === "ArrowUp") { sel = Math.max(sel - 1, 0); render(); e.preventDefault(); }
      if (e.key === "Enter") { run(filtered[sel]); e.preventDefault(); }
    });
    list.addEventListener("click", e => { const li = e.target.closest("li"); if (li) run(filtered[+li.dataset.i]); });
    $$(".overlay").forEach(o => o.addEventListener("click", e => { if (e.target === o) closeAll(); }));
    $$("[data-open-palette]").forEach(b => b.addEventListener("click", openPalette));
  }

  // ---------- global shortcuts
  const goKeys = {};
  commands.forEach(c => { if (c.key && c.key.startsWith("g ")) goKeys[c.key.slice(2)] = c.url; });
  let pendingG = 0;
  document.addEventListener("keydown", e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
    if (e.key === "Escape") { closeAll(); if (typing(e)) e.target.blur(); return; }
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      const f = $("form[data-primary]") || (typing(e) && e.target.form);
      if (f) { e.preventDefault(); f.requestSubmit ? f.requestSubmit() : f.submit(); }
      return;
    }
    if (typing(e) || e.metaKey || e.ctrlKey || e.altKey) return;
    if (pendingG && Date.now() - pendingG < 1200) {
      pendingG = 0;
      if (goKeys[e.key]) { location.href = goKeys[e.key]; e.preventDefault(); }
      return;
    }
    if (e.key === "g") { pendingG = Date.now(); return; }
    if (e.key === "?") { $("#shortcuts") && $("#shortcuts").classList.add("open"); return; }
    if (e.key === "/") { const f = $("[data-search]"); if (f) { f.focus(); e.preventDefault(); } return; }
    rowKeys(e);
  });

  // ---------- j/k row navigation on tables marked data-rows
  let focusIdx = -1;
  function rows() { return $$("table[data-rows] tbody tr").filter(r => !r.querySelector(".empty-row")); }
  function setFocus(i) {
    const r = rows(); if (!r.length) return;
    focusIdx = Math.max(0, Math.min(i, r.length - 1));
    r.forEach(x => x.classList.remove("kbd-focus"));
    r[focusIdx].classList.add("kbd-focus");
    r[focusIdx].scrollIntoView({ block: "nearest" });
  }
  function rowKeys(e) {
    const r = rows(); if (!r.length) return;
    const cur = r[focusIdx];
    if (e.key === "j") { setFocus(focusIdx + 1); e.preventDefault(); }
    else if (e.key === "k") { setFocus(focusIdx - 1); e.preventDefault(); }
    else if (e.key === "x" && cur) { const cb = cur.querySelector("input[type=checkbox].ok"); if (cb) cb.checked = !cb.checked; e.preventDefault(); }
    else if (e.key === "c" && cur) { const s = cur.querySelector("select"); if (s) { s.focus(); e.preventDefault(); } }
    else if (e.key === "Enter" && cur) { const a = cur.querySelector("a[href]"); if (a) location.href = a.href; }
  }

  // ---------- flash messages fade
  setTimeout(() => $$(".flash .alert-success, .flash .alert-info").forEach(a => a.remove()), 6000);

  // ---------- Chart.js theme (applied if a page loads Chart.js)
  window.ffTheme = function () {
    if (!window.Chart) return;
    const css = getComputedStyle(document.documentElement);
    const v = n => css.getPropertyValue(n).trim();
    const C = window.Chart;
    C.defaults.font.family = v("--font"); C.defaults.font.size = 11;
    C.defaults.color = v("--muted"); C.defaults.borderColor = v("--line");
    C.defaults.maintainAspectRatio = false;
    C.defaults.elements.point.radius = 0; C.defaults.elements.point.hoverRadius = 3;
    C.defaults.elements.line.borderWidth = 1.75; C.defaults.elements.line.tension = 0.2;
    C.defaults.elements.bar.borderRadius = 2;
    C.defaults.plugins.legend.labels.boxWidth = 8; C.defaults.plugins.legend.labels.boxHeight = 8;
    C.defaults.plugins.legend.labels.usePointStyle = true; C.defaults.plugins.legend.align = "end";
    C.defaults.plugins.tooltip.backgroundColor = v("--text"); C.defaults.plugins.tooltip.titleColor = v("--bg");
    C.defaults.plugins.tooltip.bodyColor = v("--bg"); C.defaults.plugins.tooltip.padding = 8;
    C.defaults.plugins.tooltip.cornerRadius = 4; C.defaults.plugins.tooltip.boxPadding = 4;
    C.defaults.scale.grid.color = v("--line"); C.defaults.scale.border = { display: false };
    C.defaults.scale.ticks.maxTicksLimit = 7;
    window.ffColors = {
      accent: v("--accent"), pos: v("--pos"), neg: v("--neg"), muted: v("--faint"), text: v("--text"),
      series: [v("--accent"), "#e07a2f", v("--pos"), "#c2417a", "#3b9bb5", "#b88a1b", "#7a6fd0", "#5f7f3f", "#a1a1a8"],
    };
  };
})();
