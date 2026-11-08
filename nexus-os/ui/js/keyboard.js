// On-screen keyboard, fully navigable with the gamepad. Used for every text entry.
//   openKeyboard({title, value, masked, layout, suggest}) -> Promise<string|null>
// Gamepad shortcuts inside: □ erase, △ space, ☰ (Options) validate, L1/R1 move? no:
// L1 toggles caps, R1 toggles symbols. Physical keyboard typing also works.

import { el, btn, clear } from "./dom.js";
import { openSheet } from "./ui.js";
import { nav } from "./nav.js";

const LAYOUTS = {
  azerty: ["1234567890", "azertyuiop", "qsdfghjklm", "wxcvbn@./-"],
  qwerty: ["1234567890", "qwertyuiop", "asdfghjkl'", "zxcvbnm@./"],
};
const SYMBOLS = ["1234567890", "éèàçùêâôîï", "!?,;:'\"()_", "#&%+=*$€~\\"];
let preferredLayout = "azerty";
export function setKeyboardLayout(l) { if (LAYOUTS[l]) preferredLayout = l; }

export function openKeyboard({ title = "Saisie", value = "", masked = false, layout, maxLength = 512, suggest } = {}) {
  let text = String(value || "");
  let caps = false;
  let symbols = false;
  let lay = LAYOUTS[layout] ? layout : preferredLayout;
  let display, rowsBox, sugBox, sugTimer;

  const { done } = openSheet((s, close) => {
    display = el("div.vk-display", { "data-vk-display": true, "aria-live": "polite" });
    rowsBox = el("div.vk-rows", { "data-zone": "keys" });
    sugBox = el("div.vk-suggest", { "data-zone": "suggest" });
    s.append(el("h2", { text: title }), display, sugBox, rowsBox,
      el("p.small.muted", { style: { marginTop: ".8rem" }, text: "□ effacer · △ espace · L1 majuscules · R1 symboles · ☰ valider · ○ annuler" }));
    s._close = close;
    build(close);
    refresh();
  }, {
    cls: "vk", label: title,
    onIntent: (intent, info, close) => {
      if (intent === "options") { erase(); return true; }
      if (intent === "search") { type(" "); return true; }
      if (intent === "prev") { caps = !caps; rebuild(close); return true; }
      if (intent === "next") { symbols = !symbols; rebuild(close); return true; }
      if (intent === "menu") { close(text); return true; }
      return false;
    },
    onKey: (e, close) => {
      if (e.key === "Escape") { close(null); return true; }
      if (e.key === "Backspace") { erase(); return true; }
      if (e.key === "Enter" && e.target?.closest?.("[data-vk-key]") == null) { close(text); return true; }
      if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) { type(e.key); return true; }
      return false;
    },
  });

  function type(ch) { if (text.length < maxLength) { text += ch; refresh(); } }
  function erase() { text = text.slice(0, -1); refresh(); }

  function rebuild(close) {
    const focusedKey = nav.current?.dataset?.vkKey;
    build(close);
    const again = rowsBox.querySelector(`[data-vk-key="${CSS.escape(focusedKey || "")}"]`);
    if (again) nav.focus(again, { scroll: false }); else nav.ensure();
  }

  function build(close) {
    clear(rowsBox);
    const rows = symbols ? SYMBOLS : LAYOUTS[lay];
    rows.forEach((row, ri) => {
      const r = el("div.vk-row");
      for (const ch of row) {
        const out = caps ? ch.toUpperCase() : ch;
        r.append(btn(out, () => type(out), { cls: "key", attrs: { "data-vk-key": out, ...(ri === 1 && ch === row[0] ? { "data-autofocus": true } : {}) } }));
      }
      rowsBox.append(r);
    });
    const shift = btn("Maj", () => { caps = !caps; rebuild(close); }, { cls: "key wide", attrs: { "data-vk-key": "shift", "aria-pressed": String(caps) } });
    if (caps) shift.classList.add("on");
    const sym = btn(symbols ? "ABC" : "&?é", () => { symbols = !symbols; rebuild(close); }, { cls: "key wide", attrs: { "data-vk-key": "symbols" } });
    const layoutBtn = btn(lay === "azerty" ? "AZERTY" : "QWERTY", () => { lay = lay === "azerty" ? "qwerty" : "azerty"; rebuild(close); },
      { cls: "key wide", attrs: { "data-vk-key": "layout" } });
    rowsBox.append(el("div.vk-row", {},
      shift, sym,
      btn("Espace", () => type(" "), { cls: "key space", attrs: { "data-vk-key": "space" } }),
      btn("⌫ Effacer", erase, { cls: "key wide", attrs: { "data-vk-key": "erase" } }),
      layoutBtn,
      btn("Valider", () => close(text), { cls: "key wide go", attrs: { "data-vk-key": "ok" } })));
  }

  function refresh() {
    clear(display).append(masked ? "•".repeat(text.length) : text, el("span.caret"));
    if (suggest && !masked) {
      clearTimeout(sugTimer);
      sugTimer = setTimeout(async () => {
        let list = [];
        try { list = (await suggest(text)) || []; } catch { list = []; }
        clear(sugBox);
        for (const s of list.slice(0, 6)) {
          const label = typeof s === "string" ? s : s.label || s.url || s.title;
          const val = typeof s === "string" ? s : s.value || s.url || s.title;
          if (!label) continue;
          sugBox.append(btn(label, () => { text = String(val); refresh(); }, { cls: "chip", attrs: { "data-suggest": true } }));
        }
      }, 200);
    }
  }

  return done.then((v) => (typeof v === "string" ? v : null));
}

/** A field that opens the keyboard on accept. onChange(value) is called after validation. */
export function textField({ label, value = "", placeholder = "", masked = false, onChange, suggest, attrs = {} }) {
  let current = value;
  const valueSpan = el("span");
  const render = () => {
    clear(valueSpan);
    if (current) valueSpan.append(masked ? "•".repeat(Math.min(current.length, 12)) : current);
    else valueSpan.append(el("span.placeholder", { text: placeholder || label }));
  };
  render();
  const b = btn(null, async () => {
    const v = await openKeyboard({ title: label, value: masked ? "" : current, masked, suggest });
    if (v === null) return;
    current = v;
    render();
    onChange?.(v);
  }, { cls: "field", attrs: { "aria-label": label, ...attrs } });
  b.append(valueSpan, el("span.kbd-hint", { text: "✕ clavier" }));
  b.setValue = (v) => { current = v; render(); };
  return b;
}
