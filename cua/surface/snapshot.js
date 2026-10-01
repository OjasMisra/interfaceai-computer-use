// Semantic snapshot of one frame: what an operator can see and act on.
//
// Runs inside the page. Used both to build the planner's observation during
// discovery and to resolve recorded targets during replay, so "what is this
// control's label" is computed the same way at record time and replay time.
//
// Legacy screens rarely have accessible names (no <label for>, no aria), so
// besides the accessible name we derive a *visual label*: the text a human
// would read next to the control -- typically the preceding <td> in the row.
(opts) => {
  const MAX_TEXT = 120;
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const clip = (s) => (s.length > MAX_TEXT ? s.slice(0, MAX_TEXT) + "…" : s);
  const sensitiveRe = (opts.sensitivePatterns || []).map((p) => new RegExp(p));
  const sensitiveLabels = new Set((opts.sensitiveLabels || []).map((l) => l.toLowerCase()));
  const sensitiveColumns = new Set((opts.sensitiveColumns || []).map((l) => l.toLowerCase()));

  document.querySelectorAll("[data-cua-ref]").forEach((e) => e.removeAttribute("data-cua-ref"));
  document.querySelectorAll("[data-cua-sensitive]").forEach((e) => e.removeAttribute("data-cua-sensitive"));

  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return false;
    const cs = getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none";
  };

  // True when something else (e.g. a modal overlay) sits on top of the element.
  const occluded = (el) => {
    const r = el.getBoundingClientRect();
    const x = r.left + Math.min(r.width / 2, 8), y = r.top + r.height / 2;
    if (x < 0 || y < 0 || x > innerWidth || y > innerHeight) return false;
    const top = document.elementFromPoint(x, y);
    return !!top && top !== el && !el.contains(top) && !top.contains(el);
  };

  const roleOf = (el) => {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "text").toLowerCase();
    if (tag === "a") return "link";
    if (tag === "button") return "button";
    if (tag === "select") return "combobox";
    if (tag === "textarea") return "textbox";
    if (tag === "input") {
      if (["submit", "button", "reset", "image"].includes(type)) return "button";
      if (type === "checkbox") return "checkbox";
      if (type === "radio") return "radio";
      return "textbox";
    }
    if (tag === "th") return "columnheader";
    if (tag === "td") return "cell";
    if (el.hasAttribute("onclick")) return "button";
    return null;
  };

  const accName = (el, role) => {
    const aria = el.getAttribute("aria-label");
    if (aria) return norm(aria);
    const lb = el.getAttribute("aria-labelledby");
    if (lb) return norm(lb.split(/\s+/).map((id) => document.getElementById(id)?.innerText || "").join(" "));
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return norm(l.innerText);
    }
    const wrap = el.closest("label");
    if (wrap) return norm(wrap.innerText);
    if (el.tagName === "INPUT" && role === "button") return norm(el.value || el.getAttribute("alt") || "");
    if (["link", "button", "cell", "columnheader"].includes(role)) return norm(el.innerText);
    return norm(el.getAttribute("title") || el.getAttribute("placeholder") || "");
  };

  // Text a human reads as this control's label: previous cell in the row, or
  // the nearest preceding text in the same container.
  const visualLabel = (el) => {
    const cell = el.closest("td,th");
    if (cell) {
      const row = cell.parentElement;
      const cells = Array.from(row.children);
      for (let i = cells.indexOf(cell) - 1; i >= 0; i--) {
        const t = norm(cells[i].innerText).replace(/[:*]+$/, "").trim();
        if (t) return t;
      }
    }
    let n = el.previousSibling;
    while (n) {
      const t = norm(n.textContent).replace(/[:*]+$/, "").trim();
      if (t) return t;
      n = n.previousSibling;
    }
    return "";
  };

  const cssPath = (el) => {
    const parts = [];
    for (let e = el; e && e.nodeType === 1 && e !== document.body; e = e.parentElement) {
      const tag = e.tagName.toLowerCase();
      const sibs = Array.from(e.parentElement.children).filter((s) => s.tagName === e.tagName);
      parts.unshift(sibs.length > 1 ? `${tag}:nth-of-type(${sibs.indexOf(e) + 1})` : tag);
    }
    return "body > " + parts.join(" > ");
  };

  // A table's first row counts as a header row when it is styled like one.
  const tableInfo = new Map();
  const tableIndex = new Map();
  Array.from(document.querySelectorAll("table")).forEach((t, i) => tableIndex.set(t, i));
  const headersOf = (table) => {
    if (tableInfo.has(table)) return tableInfo.get(table);
    const first = table.rows[0];
    let headers = null;
    if (first && table.rows.length > 1) {
      const cells = Array.from(first.cells);
      const styled = first.hasAttribute("bgcolor") ||
        cells.every((c) => c.tagName === "TH" || c.querySelector("b,strong"));
      if (styled && cells.every((c) => norm(c.innerText))) headers = cells.map((c) => norm(c.innerText));
    }
    tableInfo.set(table, headers);
    return headers;
  };

  const out = [];
  let n = 0;
  const candidates = document.querySelectorAll(
    "a[href], button, input:not([type=hidden]), select, textarea, [onclick], [role], td, th");
  for (const el of candidates) {
    const role = roleOf(el);
    if (!role || !visible(el)) continue;
    if ((role === "cell" || role === "columnheader") && el.querySelector("a,input,select,button,textarea,table")) continue;
    const name = accName(el, role);
    if ((role === "cell" || role === "columnheader") && !name) continue;
    const rec = {
      ref: String(n++),
      role,
      name: clip(name),
      label: ["textbox", "combobox", "checkbox", "radio", "cell"].includes(role) ? visualLabel(el) : "",
      tag: el.tagName.toLowerCase(),
      attrs: {},
      css: cssPath(el),
      enabled: !el.disabled,
      blocked: occluded(el),
    };
    for (const a of ["name", "type", "href", "value"]) {
      const v = el.getAttribute(a);
      if (v !== null && !(a === "value" && role === "textbox")) rec.attrs[a] = a === "href" ? new URL(v, location.href).pathname : v;
    }
    if (role === "textbox") rec.value = el.type === "password" ? (el.value ? "•••" : "") : el.value;
    if (role === "combobox") {
      rec.value = el.options[el.selectedIndex]?.text || "";
      rec.options = Array.from(el.options).map((o) => norm(o.text)).filter(Boolean);
    }
    if (role === "cell" || role === "columnheader") {
      const table = el.closest("table");
      const row = el.parentElement;
      const headers = headersOf(table);
      rec.table = {
        index: tableIndex.get(table),
        row: row.rowIndex,
        col: el.cellIndex,
        header: headers && row.rowIndex > 0 ? headers[el.cellIndex] || null : null,
        headers,
        row_values: headers && row.rowIndex > 0 ? Array.from(row.cells).map((c) => norm(c.innerText)) : null,
      };
      const isSensitive = sensitiveRe.some((re) => re.test(name)) ||
        sensitiveLabels.has((rec.label || "").toLowerCase()) ||
        sensitiveColumns.has((rec.table.header || "").toLowerCase());
      if (isSensitive) { el.setAttribute("data-cua-sensitive", "1"); rec.sensitive = true; }
    }
    el.setAttribute("data-cua-ref", rec.ref);
    out.push(rec);
  }

  const text = (document.body?.innerText || "").split("\n").map(norm).filter(Boolean);
  return { url: location.pathname, title: document.title, text, elements: out };
}
