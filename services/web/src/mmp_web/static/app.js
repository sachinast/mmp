// The dashboard's own script: theme, copy buttons, the detail drawer, tabs.
//
// Loaded synchronously in <head> so the saved theme is applied before the first
// paint — deferred, a dark-theme user would see a white flash on every page.
// Everything else waits for the document.
//
// Like live.js, nothing here turns data into markup: values reach the page
// through textContent only. A test asserts the file never uses innerHTML or its
// relatives, for the same reason — event names and properties are written by
// whoever holds an SDK key, and the key ships inside every copy of the app.

(() => {
  "use strict";

  const THEME_KEY = "mmp.theme";
  const root = document.documentElement;

  function savedTheme() {
    try { return localStorage.getItem(THEME_KEY); } catch (_e) { return null; }
  }
  function applyTheme(theme) {
    if (theme === "dark" || theme === "light") root.dataset.theme = theme;
    else delete root.dataset.theme;
  }
  function systemTheme() {
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  // No saved choice: follow the system, but still stamp the attribute so every
  // token resolves through one path rather than two.
  applyTheme(savedTheme() || systemTheme());

  function toggleTheme() {
    const next = root.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (_e) { /* private mode */ }
  }

  // --- copy buttons -------------------------------------------------------
  // <button data-copy="#id"> copies the text content of that element.
  function wireCopy(button) {
    button.addEventListener("click", async () => {
      const target = document.querySelector(button.dataset.copy);
      if (!target) return;
      const text = target.dataset.copyText != null ? target.dataset.copyText : target.textContent;
      const label = button.textContent;
      try {
        await navigator.clipboard.writeText(text);
        button.textContent = "Copied";
      } catch (_e) {
        // Clipboard refused (an insecure context, or an old browser): select the
        // text so a keyboard copy works.
        const range = document.createRange();
        range.selectNodeContents(target);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        button.textContent = "Selected";
      }
      setTimeout(() => { button.textContent = label; }, 1400);
    });
  }

  // --- drawer -------------------------------------------------------------
  // One drawer per page, opened by anything with data-drawer-open and closed by
  // the scrim, the close button, or Escape. Content is filled by the page's own
  // script (live.js) through the `mmp:drawer` event.
  function wireDrawer() {
    const drawer = document.querySelector(".drawer");
    const scrim = document.querySelector(".scrim");
    if (!drawer || !scrim) return;
    let lastFocus = null;

    function open() {
      lastFocus = document.activeElement;
      drawer.setAttribute("aria-hidden", "false");
      scrim.classList.add("on");
      const close = drawer.querySelector("[data-drawer-close]");
      if (close) close.focus();
    }
    function close() {
      drawer.setAttribute("aria-hidden", "true");
      scrim.classList.remove("on");
      if (lastFocus && lastFocus.focus) lastFocus.focus();
    }
    document.addEventListener("mmp:drawer", open);
    scrim.addEventListener("click", close);
    drawer.querySelectorAll("[data-drawer-close]").forEach((b) => b.addEventListener("click", close));
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && drawer.getAttribute("aria-hidden") === "false") close();
    });
  }

  // --- tabs ---------------------------------------------------------------
  // <div class="tabs" data-tabs> with buttons carrying data-tab="panel-id".
  function wireTabs(group) {
    const buttons = Array.from(group.querySelectorAll("[data-tab]"));
    function select(button) {
      buttons.forEach((b) => {
        const on = b === button;
        b.setAttribute("aria-selected", on ? "true" : "false");
        const panel = document.getElementById(b.dataset.tab);
        if (panel) panel.hidden = !on;
      });
      try { localStorage.setItem("mmp.tab." + group.dataset.tabs, button.dataset.tab); } catch (_e) { /* ignore */ }
    }
    buttons.forEach((b) => b.addEventListener("click", () => select(b)));
    let remembered = null;
    try { remembered = localStorage.getItem("mmp.tab." + group.dataset.tabs); } catch (_e) { /* ignore */ }
    const initial = buttons.find((b) => b.dataset.tab === remembered) || buttons[0];
    if (initial) select(initial);
  }

  // <select data-autosubmit> submits its form on change — the app switcher.
  // Without script the form keeps a visible Go button, so nothing depends on
  // this running.
  function wireAutosubmit(select) {
    select.addEventListener("change", () => {
      if (select.value && select.form) select.form.requestSubmit();
    });
  }

  // --- catalogue filters ---------------------------------------------------
  // Hides rows already on the page by the text, category and status they
  // carry in data attributes. Nothing is fetched and no markup is made: rows
  // are shown or hidden, and the visible count is written as text.
  function wireCatalogueFilters(root) {
    const search = root.querySelector("[data-filter-search]");
    const category = root.querySelector("[data-filter-category]");
    const status = root.querySelector("[data-filter-status]");
    const rows = Array.from(document.querySelectorAll("table.catalogue tbody tr[data-name]"));
    const empty = document.getElementById("catalogue-empty");
    const count = document.querySelector("[data-catalogue-count]");
    function apply() {
      const text = (search && search.value || "").trim().toLowerCase();
      const cat = category ? category.value : "";
      const st = status ? status.value : "";
      let visible = 0;
      for (const row of rows) {
        const show = (!text || row.dataset.name.includes(text)) &&
          (!cat || row.dataset.category === cat) &&
          (!st || row.dataset.status === st);
        row.hidden = !show;
        if (show) visible += 1;
      }
      if (empty) empty.hidden = visible > 0 || rows.length === 0;
      if (count) count.textContent = String(visible);
    }
    [search, category, status].forEach((el) => el && el.addEventListener("input", apply));
    [category, status].forEach((el) => el && el.addEventListener("change", apply));
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => b.addEventListener("click", toggleTheme));
    document.querySelectorAll("[data-catalogue-filters]").forEach(wireCatalogueFilters);
    document.querySelectorAll("select[data-autosubmit]").forEach(wireAutosubmit);
    document.querySelectorAll("[data-copy]").forEach(wireCopy);
    document.querySelectorAll("[data-tabs]").forEach(wireTabs);
    wireDrawer();
  });
})();
