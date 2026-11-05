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
    if (c.action === "toggle-sidebar") { closeAll(); toggleSidebar(); return; }
    if (c.url && c.new_tab) { closeAll(); window.open(c.url, "_blank", "noopener"); return; }
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

  // ---------- collapsible menu (remembered per browser; base.html applies it before first paint)
  function toggleSidebar() {
    const on = document.documentElement.classList.toggle("nav-collapsed");
    try { localStorage.setItem("ff-nav", on ? "collapsed" : "open"); } catch (e) {}
    $$("[data-toggle-sidebar]").forEach(b => b.title = on ? "Expand menu ( [ )" : "Collapse menu ( [ )");
  }
  $$("[data-toggle-sidebar]").forEach(b => {
    if (document.documentElement.classList.contains("nav-collapsed")) b.title = "Expand menu ( [ )";
    b.addEventListener("click", toggleSidebar);
  });

  // ---------- global shortcuts
  const goKeys = {};
  commands.forEach(c => { if (c.key && c.key.startsWith("g ")) goKeys[c.key.slice(2)] = c.url; });
  let pendingG = 0;
  document.addEventListener("keydown", e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
    if (e.key === "Escape") {
      const ed = $("tr.editing");
      if (ed) { cancelEdit(ed); e.preventDefault(); return; }
      closeAll(); if (typing(e)) e.target.blur(); return;
    }
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
    if (e.key === "[") { toggleSidebar(); e.preventDefault(); return; }
    if (e.key === "?") { $("#shortcuts") && $("#shortcuts").classList.add("open"); return; }
    if (e.key === "/") { const f = $("[data-search]"); if (f) { f.focus(); e.preventDefault(); } return; }
    rowKeys(e);
  });

  // ---------- j/k row navigation on tables marked data-rows
  let focusIdx = -1;
  function rows() { return $$("table[data-rows] tbody tr").filter(r => !r.querySelector(".empty-row") && !r.hidden); }
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
    else if (e.key === "e" && cur && cur.matches("[data-inline]")) { startEdit(cur); e.preventDefault(); }
    else if (e.key === "Enter" && cur) { const a = cur.querySelector("a[href]"); if (a) location.href = a.href; }
  }

  // ---------- inline row editing (rows marked data-inline; inputs .e, display .v)
  function formsOf(tr) { return [...new Set($$("[form]", tr).map(i => i.form).filter(Boolean))]; }
  function startEdit(tr) {
    $$("tr.editing").forEach(r => r !== tr && cancelEdit(r));
    tr.classList.add("editing");
    const i = rows().indexOf(tr); if (i >= 0) setFocus(i);
    const first = tr.querySelector("input.e"); if (first) { first.focus(); first.select(); }
  }
  function cancelEdit(tr) {
    formsOf(tr).forEach(f => f.reset());
    tr.classList.remove("editing");
    if (document.activeElement && tr.contains(document.activeElement)) document.activeElement.blur();
  }
  document.addEventListener("click", e => {
    const tr = e.target.closest("tr[data-inline]"); if (!tr) return;
    if (e.target.closest("[data-cancel]")) { cancelEdit(tr); return; }
    if (tr.classList.contains("editing") || e.target.closest("a, input, label, select, textarea, button:not([data-edit])")) return;
    startEdit(tr);
  });
  const hashRow = location.hash && document.getElementById(location.hash.slice(1));
  if (hashRow && hashRow.matches("tr[data-inline]")) { const i = rows().indexOf(hashRow); if (i >= 0) setFocus(i); }

  // ---------- sortable / filterable tables
  // <table data-sort>: click a header to sort (th[data-nosort] opts out). A cell's data-v, if set, is its sort key.
  // <table data-filter>: adds a filter bar above the table (text search + a dropdown per th[data-facet]).
  //   A cell's data-f, if set, is its facet value; table[data-row-facets="status:Status"] adds a facet read from tr[data-status].
  const MINUS = /[−–]/g;
  function cellValue(td) {
    if (!td) return "";
    if (td.dataset.v !== undefined) return td.dataset.v;
    const field = td.querySelector("select, input:not([type=hidden]):not([type=checkbox])");
    if (field && !td.querySelector(".v")) return field.tagName === "SELECT" ? field.selectedOptions[0]?.text || "" : field.value;
    const shown = td.querySelector(".v");
    return (shown || td).innerText.trim();
  }
  function asNumber(s) {
    const t = String(s).replace(MINUS, "-").replace(/[₹,+\s]/g, "");
    const m = t.match(/^(-?\d+(?:\.\d+)?)(Cr|L|%)?$/);
    if (!m) return null;
    return parseFloat(m[1]) * ({ Cr: 1e7, L: 1e5 }[m[2]] || 1);
  }
  const isBlank = s => s === "" || s === "—";
  function compare(a, b) {
    const na = asNumber(a), nb = asNumber(b);
    if (na !== null && nb !== null) return na - nb;
    return a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" });
  }
  function bodyRows(table) { return $$(":scope > tbody > tr", table).filter(r => !r.querySelector(".empty-row")); }
  function colIndex(th) { return [...th.parentElement.children].indexOf(th); }

  $$("table[data-sort]").forEach(table => {
    const ths = $$(":scope > thead th", table);
    ths.forEach(th => {
      if (th.hasAttribute("data-nosort") || !th.textContent.trim()) return;
      th.classList.add("sortable");
      th.tabIndex = 0;
      const go = () => {
        const dir = th.dataset.dir === "asc" ? "desc" : "asc";
        ths.forEach(h => { delete h.dataset.dir; h.removeAttribute("aria-sort"); });
        th.dataset.dir = dir;
        th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
        const i = colIndex(th), sign = dir === "asc" ? 1 : -1;
        const body = table.tBodies[0];
        // rows keep their original order as the tie-breaker (stable sort)
        bodyRows(table).map((r, n) => [r, cellValue(r.children[i]), n])
          .sort((x, y) => (isBlank(x[1]) - isBlank(y[1])) || sign * compare(x[1], y[1]) || x[2] - y[2])  // blanks always last
          .forEach(([r]) => body.appendChild(r));
        focusIdx = -1; $$("tr.kbd-focus").forEach(r => r.classList.remove("kbd-focus"));
      };
      th.addEventListener("click", go);
      th.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); go(); } });
    });
  });

  $$("table[data-filter]").forEach((table, tIdx) => {
    const ths = $$(":scope > thead th", table);
    const facets = ths.filter(th => th.hasAttribute("data-facet")).map(th => ({
      label: th.dataset.facet || th.textContent.trim(), get: r => { const td = r.children[colIndex(th)]; return td ? (td.dataset.f ?? cellValue(td)) : ""; },
    }));
    (table.dataset.rowFacets || "").split(",").filter(Boolean).forEach(spec => {
      const [key, label] = spec.split(":");
      facets.push({ label: label || key, get: r => r.dataset[key] || "", initial: table.dataset[`default${key[0].toUpperCase()}${key.slice(1)}`] });
    });
    const bar = document.createElement("div");
    bar.className = "filter-bar";
    const q = document.createElement("input");
    q.type = "search"; q.className = "form-control form-control-sm"; q.placeholder = "Filter…  /";
    q.setAttribute("aria-label", "Filter rows");
    if (tIdx === 0) q.setAttribute("data-search", "");
    bar.appendChild(q);
    const all = bodyRows(table);
    const selects = facets.map(f => {
      const values = [...new Set(all.map(f.get).filter(Boolean))].sort((a, b) => a.localeCompare(b));
      const s = document.createElement("select");
      s.className = "form-select form-select-sm";
      s.setAttribute("aria-label", f.label);
      s.innerHTML = `<option value="">${f.label}: all</option>` + values.map(v => `<option>${v.replace(/</g, "&lt;")}</option>`).join("");
      if (f.initial && values.includes(f.initial)) s.value = f.initial;
      bar.appendChild(s);
      return s;
    });
    const count = document.createElement("span");
    count.className = "filter-count";
    bar.appendChild(count);
    const clear = document.createElement("button");
    clear.type = "button"; clear.className = "btn btn-sm btn-link"; clear.textContent = "Clear";
    bar.appendChild(clear);
    let empty = null;
    function apply() {
      const words = q.value.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      bodyRows(table).forEach(r => {
        const text = r.innerText.toLowerCase() + " " + $$("input:not([type=hidden])", r).map(i => i.value.toLowerCase()).join(" ");
        const ok = words.every(w => text.includes(w)) && facets.every((f, i) => !selects[i].value || f.get(r) === selects[i].value);
        r.hidden = !ok; shown += ok;
      });
      const filtering = words.length || selects.some(s => s.value);
      count.textContent = filtering ? `${shown} of ${all.length}` : `${all.length}`;
      clear.hidden = !filtering;
      if (!shown && all.length) {
        if (!empty) { empty = document.createElement("tr"); empty.innerHTML = `<td colspan="${ths.length || 1}" class="text-muted empty-row">No rows match.</td>`; }
        table.tBodies[0].appendChild(empty);
      } else if (empty) empty.remove();
      focusIdx = -1;
    }
    q.addEventListener("input", apply);
    q.addEventListener("keydown", e => { if (e.key === "Escape" && q.value) { q.value = ""; apply(); e.stopPropagation(); } });
    selects.forEach(s => s.addEventListener("change", apply));
    clear.addEventListener("click", () => { q.value = ""; selects.forEach(s => s.value = ""); apply(); });
    const host = table.dataset.filterBar ? $(table.dataset.filterBar) : null;
    if (host) host.appendChild(bar);
    else (table.closest(".table-responsive") || table).before(bar);
    apply();
  });

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
