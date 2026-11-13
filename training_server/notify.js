/*
 * Meldungen oben rechts im Stil der Mitteilungen von iPhone/Mac - fuer Spiel-, Admin- und
 * Pruefer-Seite. Kein Framework, einfach einbinden:  <script src="/notify.js"></script>
 *
 *   Notify.show("Gespeichert")                         kurze Meldung (verschwindet von selbst)
 *   Notify.show("Fehler", "Kamera fehlt", "error")     mit Text und Art: error|warning|info|success
 *   Notify.sync(state.notices)                         dauerhafte Meldungen vom Pi (Kamera weg, ...)
 *
 * Dauerhafte Meldungen klappen nach ein paar Sekunden (oder per Antippen) zu einem kleinen
 * Knopf oben rechts zusammen ("2 Meldungen"); Antippen zeigt sie wieder.
 */
(function () {
  "use strict";
  const COLORS = { error: "#FF453A", warning: "#FF9F0A", info: "#0A84FF", success: "#30D158" };
  const GLYPH = {
    error: '<path d="M12 6.5v7" /><circle cx="12" cy="17.2" r="1.2" fill="#fff" stroke="none"/>',
    warning: '<path d="M12 6.5v7" /><circle cx="12" cy="17.2" r="1.2" fill="#fff" stroke="none"/>',
    info: '<circle cx="12" cy="7.2" r="1.2" fill="#fff" stroke="none"/><path d="M12 10.5v7" />',
    success: '<path d="M7 12.5l3.2 3.2L17.2 8.6" />',
  };
  const css = `
  .ntf-stack { position: fixed; top: max(12px, env(safe-area-inset-top)); right: max(12px, env(safe-area-inset-right));
    z-index: 100; display: flex; flex-direction: column; gap: 10px; width: min(380px, calc(100vw - 24px));
    pointer-events: none; }
  .ntf { pointer-events: auto; display: grid; grid-template-columns: 22px 1fr auto; column-gap: 8px; row-gap: 2px;
    align-items: center; padding: 11px 14px 13px; border-radius: 20px; color: #fff; cursor: pointer;
    background: rgba(40, 40, 44, .72); -webkit-backdrop-filter: blur(24px) saturate(180%); backdrop-filter: blur(24px) saturate(180%);
    border: 1px solid rgba(255, 255, 255, .12); box-shadow: 0 12px 32px rgba(0, 0, 0, .35);
    font: 15px/1.3 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", system-ui, sans-serif;
    -webkit-font-smoothing: antialiased;
    transform: translateX(calc(100% + 24px)); opacity: 0;
    transition: transform .45s cubic-bezier(.2, .9, .25, 1), opacity .3s ease; }
  .ntf.in { transform: none; opacity: 1; }
  .ntf.out { transform: translateX(calc(100% + 24px)); opacity: 0; transition-duration: .3s; }
  .ntf .ic { width: 22px; height: 22px; border-radius: 6px; display: grid; place-items: center; }
  .ntf .ic svg { width: 22px; height: 22px; stroke: #fff; stroke-width: 2.4; stroke-linecap: round; stroke-linejoin: round; fill: none; }
  .ntf .app { font-size: 12px; letter-spacing: .04em; text-transform: uppercase; color: rgba(235, 235, 245, .6); }
  .ntf .tm { font-size: 12px; color: rgba(235, 235, 245, .6); }
  .ntf .tt { grid-column: 1 / -1; font-weight: 600; margin-top: 4px; overflow-wrap: anywhere; }
  .ntf .bd { grid-column: 1 / -1; color: rgba(235, 235, 245, .78); font-size: 14px; overflow-wrap: anywhere; }
  @media (prefers-color-scheme: light) {
    .ntf { background: rgba(246, 246, 248, .78); color: #000; border-color: rgba(0, 0, 0, .06); box-shadow: 0 12px 32px rgba(0, 0, 0, .18); }
    .ntf .app, .ntf .tm { color: rgba(60, 60, 67, .6); }
    .ntf .bd { color: rgba(60, 60, 67, .85); }
  }
  .ntf-pill { pointer-events: auto; align-self: flex-end; display: inline-flex; align-items: center; gap: 8px;
    min-height: 36px; padding: 6px 14px 6px 8px; border-radius: 18px; border: 1px solid rgba(255, 255, 255, .12);
    background: rgba(40, 40, 44, .72); -webkit-backdrop-filter: blur(24px) saturate(180%); backdrop-filter: blur(24px) saturate(180%);
    color: #fff; font: 600 14px/1 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Helvetica Neue", system-ui, sans-serif;
    box-shadow: 0 8px 24px rgba(0, 0, 0, .3); cursor: pointer; }
  .ntf-pill .ic { width: 22px; height: 22px; border-radius: 6px; display: grid; place-items: center; }
  .ntf-pill .ic svg { width: 22px; height: 22px; stroke: #fff; stroke-width: 2.4; stroke-linecap: round; fill: none; }
  @media (prefers-color-scheme: light) {
    .ntf-pill { background: rgba(246, 246, 248, .82); color: #000; border-color: rgba(0, 0, 0, .06); }
  }
  @media (prefers-reduced-motion: reduce) { .ntf, .ntf.out { transition: opacity .2s; transform: none; } }`;
  const TUCK_MS = 7000;

  let stack = null;
  const live = new Map();          // key -> Element (dauerhafte Meldungen vom Pi)
  const tucked = new Set();        // zusammengeklappt: nur noch im Knopf "n Meldungen"
  let lastList = [];
  let pill = null;

  function root() {
    if (stack) return stack;
    const st = document.createElement("style");
    st.textContent = css;
    document.head.appendChild(st);
    stack = document.createElement("div");
    stack.className = "ntf-stack";
    stack.setAttribute("role", "status");
    stack.setAttribute("aria-live", "polite");
    document.body.appendChild(stack);
    return stack;
  }

  function ago(t) {
    const s = Math.max(0, Date.now() / 1000 - t);
    return s < 60 ? "jetzt" : s < 3600 ? "vor " + Math.floor(s / 60) + " Min." : "vor " + Math.floor(s / 3600) + " Std.";
  }

  function build(title, body, level) {
    level = COLORS[level] ? level : "info";
    const el = document.createElement("div");
    el.className = "ntf";
    const ic = document.createElement("span");
    ic.className = "ic";
    ic.style.background = COLORS[level];
    ic.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + GLYPH[level] + "</svg>";
    const app = document.createElement("span"); app.className = "app"; app.textContent = "Billard";
    const tm = document.createElement("span"); tm.className = "tm"; tm.textContent = "jetzt";
    const tt = document.createElement("div"); tt.className = "tt"; tt.textContent = title;
    el.append(ic, app, tm, tt);
    if (body) { const bd = document.createElement("div"); bd.className = "bd"; bd.textContent = body; el.appendChild(bd); }
    el.addEventListener("click", () => { if (el.dataset.key) tuck(el.dataset.key); else remove(el); });
    return el;
  }

  function insert(el, top) {
    const r = root();
    if (top) r.insertBefore(el, r.firstChild); else r.appendChild(el);
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.add("in")));
  }

  function remove(el) {
    if (!el || el.classList.contains("out")) return;
    el.classList.add("out");
    setTimeout(() => el.remove(), 320);
    for (const [k, v] of live) if (v === el) live.delete(k);
  }

  function tuck(key) {
    tucked.add(key);
    remove(live.get(key));
    renderPill();
  }

  function renderPill() {
    const open = lastList.filter((n) => tucked.has(n.key));
    if (!open.length) { if (pill) { pill.remove(); pill = null; } return; }
    if (!pill) {
      pill = document.createElement("button");
      pill.type = "button";
      pill.className = "ntf-pill";
      pill.addEventListener("click", () => { tucked.clear(); renderPill(); Notify.sync(lastList); });
      root().appendChild(pill);
    }
    const order = ["error", "warning", "info", "success"];
    const worst = open.map((n) => n.level).sort((a, b) => order.indexOf(a) - order.indexOf(b))[0] || "info";
    pill.innerHTML = '<span class="ic" style="background:' + COLORS[worst] + '"><svg viewBox="0 0 24 24" aria-hidden="true">' +
      GLYPH[worst] + "</svg></span>";
    pill.appendChild(document.createTextNode(open.length === 1 ? open[0].title : open.length + " Meldungen"));
    pill.setAttribute("aria-label", "Meldungen anzeigen: " + open.map((n) => n.title).join(", "));
    root().appendChild(pill);         // immer unter den Karten
  }

  const Notify = {
    show(title, body, level, ms) {
      if (level === undefined && (body === "error" || body === "success" || body === "warning" || body === "info")) {
        level = body; body = "";
      }
      level = level || "success";
      const el = build(title, body || "", level);
      insert(el, false);
      const extra = stack.querySelectorAll(".ntf:not(.out):not([data-key])");
      if (extra.length > 3) remove(extra[0]);
      setTimeout(() => remove(el), ms || (level === "error" ? 6000 : 3800));
      return el;
    },
    sync(list) {
      list = list || [];
      lastList = list;
      const keys = new Set(list.map((n) => n.key));
      for (const k of [...tucked]) if (!keys.has(k)) tucked.delete(k);
      for (const [k, el] of live) if (!keys.has(k)) remove(el);
      for (const n of list) {
        if (tucked.has(n.key)) continue;
        let el = live.get(n.key);
        if (!el) {
          el = build(n.title, n.body, n.level);
          el.dataset.key = n.key;
          live.set(n.key, el);
          insert(el, true);
          const key = n.key;
          setTimeout(() => { if (live.get(key) === el) tuck(key); }, TUCK_MS);
        } else {
          el.querySelector(".tt").textContent = n.title;
          const bd = el.querySelector(".bd");
          if (bd) bd.textContent = n.body || "";
        }
        el.querySelector(".tm").textContent = ago(n.t);
      }
      renderPill();
    },
  };
  window.Notify = Notify;
})();
