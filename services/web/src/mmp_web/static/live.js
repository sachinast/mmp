// The live view.
//
// Polls /live/feed every two seconds and renders what arrives. Deliberately
// small and dependency-free: the page's content-security policy allows script
// from this origin only, so there is no library to load, and none is needed.
//
// Every value rendered here came from an event, and events can be sent by anyone
// holding an SDK key — which ships inside every copy of an app. So nothing from
// the feed is ever interpreted as markup: it reaches the page through
// textContent and nowhere else. An event named <img src=x onerror=...> is shown
// as those characters. A test asserts this file never uses innerHTML or its
// relatives.

(() => {
  "use strict";

  const root = document.getElementById("live");
  if (!root || !root.dataset.appId) return;

  const APP_ID = root.dataset.appId;
  const FEED = root.dataset.feed;
  const POLL_MS = 2000;
  const MAX_BACKOFF_MS = 30000;
  const MAX_ROWS = 500;
  // Remembered separately from what is drawn. Forgetting a trimmed row would let
  // the server's late-write margin draw it again on a busy app; remembering
  // everything forever would grow for as long as the tab is open.
  const MAX_REMEMBERED = 5000;
  const MAX_NAME_CHIPS = 24;
  const KINDS = ["click", "install", "event", "postback", "rejected"];

  const rows = document.getElementById("live-rows");
  const empty = document.getElementById("live-empty");
  const status = document.getElementById("live-status");
  const pauseButton = document.getElementById("live-pause");
  const clearButton = document.getElementById("live-clear");
  const search = document.getElementById("live-search");
  const names = document.getElementById("live-names");
  const lastSeen = document.getElementById("live-last");
  const drawerTitle = document.getElementById("drawer-title");
  const drawerKind = document.getElementById("drawer-kind");
  const drawerFacts = document.getElementById("drawer-facts");
  const drawerJson = document.getElementById("drawer-json");

  // kind:id of everything drawn, so an item repeated by the server's late-write
  // margin is recognised and not drawn twice.
  const shown = new Map();
  const counts = Object.fromEntries(KINDS.map((kind) => [kind, 0]));
  const visibleKinds = new Set(KINDS);
  // Event names with a chip, and the subset switched off. Empty "off" set means
  // every name is shown, so the default needs no chips at all.
  const knownNames = new Map();
  const hiddenNames = new Set();
  const items = new Map();

  let since = null;
  let paused = false;
  let ended = false;
  let timer = null;
  let delay = POLL_MS;
  let filterText = "";
  let selected = null;

  // --- rendering --------------------------------------------------------
  function cell(text, className) {
    const td = document.createElement("td");
    if (className) td.className = className;
    td.textContent = text == null ? "" : String(text);
    return td;
  }

  function time(iso) {
    if (!iso) return "";
    const at = new Date(iso);
    return at.toLocaleTimeString(undefined, { hour12: false }) +
      "." + String(at.getMilliseconds()).padStart(3, "0");
  }

  function money(minor, currency) {
    if (minor == null || !currency) return "";
    try {
      // The currency's own number of decimal places: 499 is 4.99 USD but 499 JPY.
      const format = new Intl.NumberFormat(undefined, { style: "currency", currency });
      const places = format.resolvedOptions().maximumFractionDigits;
      return format.format(minor / Math.pow(10, places));
    } catch (_error) {
      return `${minor} ${currency} (minor units)`;
    }
  }

  function describe(item) {
    switch (item.kind) {
      case "click":
        return [item.campaign, item.platform, item.country, item.is_bot ? "bot" : null]
          .filter(Boolean).join(" · ");
      case "install":
        return [item.campaign || "organic",
          item.fraud_verdict && item.fraud_verdict !== "clean" ? `flagged ${item.fraud_verdict}` : null]
          .filter(Boolean).join(" · ");
      case "event": {
        const props = item.details && item.details.properties ? Object.keys(item.details.properties) : [];
        return [money(item.revenue_minor, item.currency), item.platform,
          item.user_id ? `user ${item.user_id}` : null,
          props.length ? `${props.length} propert${props.length === 1 ? "y" : "ies"}` : null]
          .filter(Boolean).join(" · ");
      }
      case "postback":
        return [item.status, item.response_status ? `HTTP ${item.response_status}` : null,
          item.details && item.details.destination_host]
          .filter(Boolean).join(" · ");
      case "rejected": {
        const details = item.details || {};
        // One bad event refuses the whole request. Saying how many went with it
        // is the difference between "my event was rejected" and "the SDK is
        // losing data" — the second being what people conclude otherwise.
        const batch = details.events_in_batch > 1
          ? `whole batch of ${details.events_in_batch} refused`
          : null;
        return [`HTTP ${item.status}`, details.detail, batch, item.source === "s2s" ? "server-to-server" : null]
          .filter(Boolean).join(" · ");
      }
      default:
        return "";
    }
  }

  // The drawer: a few facts as a list, then everything as JSON. Both through
  // textContent; the facts are built element by element.
  function fact(label, value) {
    if (value == null || value === "") return;
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.textContent = String(value);
    drawerFacts.append(dt, dd);
  }

  function showDetails(item, row) {
    if (selected) selected.classList.remove("selected");
    selected = row;
    row.classList.add("selected");
    drawerTitle.textContent = item.title || item.kind;
    drawerKind.textContent = item.kind;
    drawerKind.className = `live-kind live-kind-${item.kind}`;
    drawerFacts.replaceChildren();
    fact("Time", item.at ? new Date(item.at).toLocaleString() : null);
    fact("Device", item.device);
    fact("User", item.user_id);
    fact("Platform", item.platform);
    fact("Campaign", item.campaign);
    fact("Country", item.country);
    fact("Revenue", money(item.revenue_minor, item.currency));
    fact("Status", item.status);
    fact("Verdict", item.fraud_verdict);
    const details = item.details || {};
    for (const key of ["event_id", "click_id", "attribution_id", "occurred_at", "clock_skew_ms",
      "app_version", "os_version", "device_model", "tracking_code", "deep_link",
      "sub1", "sub2", "sub3", "destination_host", "attempts", "error", "detail", "events_in_batch"]) {
      fact(key.replace(/_/g, " "), details[key]);
    }
    drawerJson.textContent = JSON.stringify(item, null, 2);
    document.dispatchEvent(new CustomEvent("mmp:drawer"));
  }

  function build(item) {
    const row = document.createElement("tr");
    row.className = "live-row";
    row.dataset.kind = item.kind;
    row.dataset.name = item.kind === "event" ? String(item.title || "") : "";
    // Compared as numbers, not strings: an ISO timestamp with zero microseconds
    // has no fractional part at all, so string order is order by luck.
    row.dataset.at = String(Date.parse(item.at) || 0);
    row.dataset.search = `${item.title || ""} ${item.device || ""} ${item.user_id || ""}`.toLowerCase();
    row.tabIndex = 0;

    const kind = document.createElement("td");
    const badge = document.createElement("span");
    badge.className = `live-kind live-kind-${item.kind}`;
    badge.textContent = item.kind;
    kind.appendChild(badge);

    row.append(
      cell(time(item.at), "live-time"),
      kind,
      cell(item.title, "live-title"),
      cell(item.device, "live-device"),
      cell(describe(item), "live-detail"),
    );
    // Sandbox and in-flight deliveries are not problems: only a delivery that
    // failed, was abandoned or was blocked needs someone to look at it.
    if (item.kind === "rejected" ||
        (item.kind === "postback" && ["failed", "abandoned"].includes(item.status))) {
      row.classList.add("live-problem");
    }

    const open = () => showDetails(item, row);
    row.addEventListener("click", open);
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
    return row;
  }

  function matches(row) {
    if (!visibleKinds.has(row.dataset.kind)) return false;
    if (row.dataset.kind === "event" && hiddenNames.has(row.dataset.name)) return false;
    return !filterText || row.dataset.search.includes(filterText);
  }

  function applyFilters() {
    for (const row of rows.querySelectorAll("tr.live-row")) {
      row.hidden = !matches(row);
    }
  }

  // One chip per event name seen, so a noisy heartbeat event can be switched
  // off while watching for the one that matters. Chips are made from
  // textContent like everything else; the name is attacker-controlled text.
  function noteName(item) {
    if (item.kind !== "event" || !names) return;
    const name = String(item.title || "");
    if (!name) return;
    if (knownNames.has(name)) {
      const chip = knownNames.get(name);
      chip.dataset.n = String(Number(chip.dataset.n) + 1);
      chip.lastElementChild.textContent = chip.dataset.n;
      return;
    }
    if (knownNames.size >= MAX_NAME_CHIPS) return;
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.dataset.n = "1";
    chip.setAttribute("aria-pressed", "true");
    const label = document.createElement("span");
    label.textContent = name;
    label.className = "chip-name";
    const n = document.createElement("span");
    n.textContent = "1";
    chip.append(label, n);
    chip.addEventListener("click", () => {
      const on = chip.getAttribute("aria-pressed") !== "true";
      chip.setAttribute("aria-pressed", String(on));
      if (on) hiddenNames.delete(name);
      else hiddenNames.add(name);
      applyFilters();
    });
    knownNames.set(name, chip);
    names.appendChild(chip);
    names.hidden = false;
    const wrap = document.getElementById("live-names-wrap");
    if (wrap) wrap.hidden = false;
  }

  // Newest first by the item's own time. Usually that is the top, but the
  // server re-reads a margin for late writes, so an item can legitimately arrive
  // older than something already shown — it goes where its time says.
  function insert(item) {
    const row = build(item);
    const at = Date.parse(item.at) || 0;
    let before = null;
    for (const existing of rows.querySelectorAll("tr.live-row")) {
      if (Number(existing.dataset.at) < at) {
        before = existing;
        break;
      }
    }
    rows.insertBefore(row, before);
    row.hidden = !matches(row);
    row.classList.add("live-new");
    setTimeout(() => row.classList.remove("live-new"), 1600);
    noteName(item);
    return row;
  }

  function trim() {
    const all = rows.querySelectorAll("tr.live-row");
    for (let index = MAX_ROWS; index < all.length; index += 1) {
      all[index].remove();
    }
    // A Map iterates in insertion order, so the first keys are the oldest.
    for (const key of shown.keys()) {
      if (shown.size <= MAX_REMEMBERED) break;
      shown.delete(key);
      items.delete(key);
    }
  }

  function refreshCounts() {
    for (const kind of KINDS) {
      const target = root.querySelector(`[data-count="${kind}"]`);
      if (target) target.textContent = String(counts[kind]);
    }
    const any = rows.querySelector("tr.live-row") !== null;
    empty.hidden = any;
    if (lastSeen && any) {
      const newest = rows.querySelector("tr.live-row");
      lastSeen.textContent = `last activity ${time(new Date(Number(newest.dataset.at)).toISOString())}`;
    }
  }

  function setStatus(state, text) {
    status.dataset.state = state;
    status.textContent = text;
  }

  // --- polling ----------------------------------------------------------
  function schedule(ms) {
    clearTimeout(timer);
    if (!ended) timer = setTimeout(poll, ms);
  }

  async function poll() {
    if (paused || ended) return;
    // A hidden tab polling every two seconds is load for nobody's benefit.
    if (document.hidden) return;

    const params = new URLSearchParams({ app_id: APP_ID });
    if (since) params.set("since", since);

    try {
      const response = await fetch(`${FEED}?${params}`, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { accept: "application/json" },
      });
      if (response.status === 401) {
        ended = true;
        setStatus("ended", "Session ended — reload the page to sign in again");
        return;
      }
      if (response.status === 404) {
        ended = true;
        setStatus("ended", "This app is not part of your current organisation");
        return;
      }
      if (response.status === 403) {
        ended = true;
        setStatus("ended", "Your role cannot view live events (member or above)");
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const body = await response.json();
      // Checked again after the awaits, not only before them. Pausing while a
      // request is in flight used to let its response redraw the table, set the
      // status back to "Live" and schedule the next poll — a paused view that
      // kept updating and said it was live. The response is dropped and `since`
      // left where it was, so resuming loses nothing.
      if (paused || ended) return;
      since = body.server_time;
      for (const item of body.items.slice().reverse()) {
        const key = `${item.kind}:${item.id}`;
        if (shown.has(key)) continue;
        insert(item);
        shown.set(key, true);
        items.set(key, item);
        if (item.kind in counts) counts[item.kind] += 1;
      }
      trim();
      refreshCounts();
      setStatus("live", "Live");
      delay = POLL_MS;
      schedule(POLL_MS);
    } catch (error) {
      if (paused || ended) return;
      // Backed off, so a dashboard left open through an outage does not become
      // part of the load that is keeping the API down.
      delay = Math.min(delay * 2, MAX_BACKOFF_MS);
      setStatus("retrying", `Reconnecting in ${Math.round(delay / 1000)}s — ${error.message}`);
      schedule(delay);
    }
  }

  // --- controls ---------------------------------------------------------
  pauseButton.addEventListener("click", () => {
    paused = !paused;
    pauseButton.textContent = paused ? "Resume" : "Pause";
    if (paused) {
      clearTimeout(timer);
      setStatus("paused", "Paused");
    } else {
      poll();
    }
  });

  clearButton.addEventListener("click", () => {
    // Rows go; what has been seen is remembered, so clearing does not bring the
    // same items straight back on the next poll.
    rows.replaceChildren();
    for (const kind of KINDS) counts[kind] = 0;
    refreshCounts();
  });

  for (const chip of root.querySelectorAll("button.chip[data-kind]")) {
    chip.addEventListener("click", () => {
      const kind = chip.dataset.kind;
      const on = chip.getAttribute("aria-pressed") !== "true";
      chip.setAttribute("aria-pressed", String(on));
      if (on) visibleKinds.add(kind);
      else visibleKinds.delete(kind);
      applyFilters();
    });
  }

  search.addEventListener("input", () => {
    filterText = search.value.trim().toLowerCase();
    applyFilters();
  });

  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && !paused && !ended) poll();
  });

  poll();
})();
