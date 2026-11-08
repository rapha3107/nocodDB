// NEXUS Home — boot, tabs, router, quick menu, profile picker, NEXUS AI overlay.

import * as api from "./api.js";
import { el, btn, clear, asList, pick } from "./dom.js";
import { nav } from "./nav.js";
import { startInput } from "./gamepad.js";
import { openKeyboard, textField, setKeyboardLayout } from "./keyboard.js";
import {
  toast, toastError, openSheet, confirmDialog, trackTask, setHints, DEFAULT_HINTS,
  recentNotifications, unavailable, errorState, loading, run,
} from "./ui.js";
import home from "./screens/home.js";
import library from "./screens/library.js";
import emulation from "./screens/emulation.js";
import store from "./screens/store.js";
import music from "./screens/music.js";
import browser from "./screens/browser.js";
import vr from "./screens/vr.js";
import settings from "./screens/settings.js";

const SCREENS = [home, library, emulation, store, music, browser, vr, settings];
const byId = Object.fromEntries(SCREENS.map((s) => [s.id, s]));

const state = { tab: "home", stack: [], profile: null, settings: {} };

// ---- router -------------------------------------------------------------------
export const app = {
  state,
  /** Switch to a top-level tab (resets its view stack). */
  go(id, opts = {}) {
    if (!byId[id]) return;
    state.tab = id;
    for (const e of state.stack) e.handlers?.dispose?.();
    state.stack = [];
    renderTabs();
    app.push(byId[id], { root: true, ...opts });
  },
  /** Push a view {render(box, app, opts) -> {onIntent?}?, title?}. */
  push(view, opts = {}) {
    const content = document.getElementById("content");
    const prev = state.stack[state.stack.length - 1];
    if (prev) { prev.focus = nav.current; prev.scroll = content.scrollTop; prev.box.remove(); }
    if (opts.root) clear(content);
    const box = el("section.screen-enter", { "data-screen": view.id || "detail", "aria-label": view.title || "" });
    content.append(box);
    content.scrollTop = 0;
    const entry = { view, box, handlers: null };
    state.stack.push(entry);
    entry.handlers = view.render(box, app, opts) || null;
    document.body.dataset.screen = view.id || state.tab;
    queueMicrotask(() => { if (nav.layerCount === 0) nav.focusFirst(box); });
  },
  back() {
    if (state.stack.length > 1) {
      const top = state.stack.pop();
      top.box.remove();
      top.handlers?.dispose?.();
      const prev = state.stack[state.stack.length - 1];
      const content = document.getElementById("content");
      content.append(prev.box);
      content.scrollTop = prev.scroll || 0;
      if (prev.focus && prev.box.contains(prev.focus)) nav.focus(prev.focus); else nav.focusFirst(prev.box);
      prev.handlers?.onResume?.();
      return true;
    }
    if (state.tab !== "home") { app.go("home"); return true; }
    return false;
  },
  refresh() {
    const top = state.stack[state.stack.length - 1];
    if (!top) return;
    if (state.stack.length === 1) app.go(state.tab);
    else top.handlers?.refresh?.();
  },
  call: api.call,
  openAi: () => openAiOverlay(),
  applyProfile,
  applySettings,
};

function renderTabs() {
  const list = document.getElementById("tab-list");
  clear(list);
  for (const s of SCREENS) {
    list.append(btn(s.title, () => app.go(s.id), {
      cls: "tab", attrs: { role: "tab", "aria-selected": String(s.id === state.tab), "data-zone": "top", "data-tab": s.id },
    }));
  }
  const sel = list.querySelector('[aria-selected="true"]');
  sel?.scrollIntoView({ inline: "nearest", block: "nearest" });
}

function cycleTab(delta) {
  const i = SCREENS.findIndex((s) => s.id === state.tab);
  app.go(SCREENS[(i + delta + SCREENS.length) % SCREENS.length].id);
}

// ---- global intents ---------------------------------------------------------------
nav.onIntent((intent) => {
  const top = state.stack[state.stack.length - 1];
  if (top?.handlers?.onIntent?.(intent) === true) return true;
  switch (intent) {
    case "back": return app.back();
    case "prev": cycleTab(-1); return true;
    case "next": cycleTab(1); return true;
    case "home": nav.closeAll(); app.go("home"); return true;
    case "menu": openQuickMenu(); return true;
    case "options": openOptions(); return true;
    case "search": {
      const f = nav.current;
      if (f?.classList.contains("field")) { f.click(); return true; }
      openKeyboard({ title: "Rechercher un jeu", value: "" }).then((q) => { if (q) app.go("library", { query: q }); });
      return true;
    }
    case "ptt-start": startTalking(); return true;
    case "ptt-end": stopTalking(); return true;
  }
  return false;
});


function openOptions() {
  const f = nav.current;
  const opts = (f && f._options) || [];
  openSheet((s, close) => {
    s.append(el("h2", { text: "Options" }));
    const list = el("div.list", { "data-zone": "options" });
    for (const o of opts) list.append(btn(o.label, () => { close(); o.fn(); }, { cls: "row-item" }));
    list.append(
      btn("Actualiser cet écran", () => { close(); app.refresh(); }, { cls: "row-item" }),
      btn("Retour à l'accueil", () => { close(); app.go("home"); }, { cls: "row-item" }),
    );
    s.append(list);
  }, { label: "Options" });
}

// ---- quick menu (Options button) ----------------------------------------------------
function openQuickMenu() {
  if (document.querySelector("[data-quick-menu]")) return;
  openSheet((s, close) => {
    s.dataset.quickMenu = "";
    s.append(el("h2", { text: "Menu rapide" }));

    const vol = el("div.section");
    const volLabel = el("span.muted", { text: "…" });
    const setVol = async (delta) => {
      try {
        const r = await api.call("music.volume_step", { delta });
        const lvl = pick(r, "volume", "level");
        volLabel.textContent = lvl !== undefined ? `${lvl} %` : "modifié";
      } catch (e) { toastError(e, "Volume"); }
    };
    vol.append(el("div.section-head", {}, el("h3", { text: "Volume" }), volLabel),
      el("div.btn-row", { "data-zone": "qm-vol" },
        btn("− 10", () => setVol(-10), { cls: "icon-btn", attrs: { "aria-label": "Baisser le volume" } }),
        btn("+ 10", () => setVol(10), { cls: "icon-btn", attrs: { "aria-label": "Monter le volume" } })));
    if (!api.hasAction("music.volume_step")) { vol.querySelector(".btn-row").replaceWith(unavailable(api.moduleProblem("music") || "réglage du volume absent")); }
    else api.call("music.status").then((r) => { const l = pick(r, "volume"); if (l !== undefined) volLabel.textContent = `${l} %`; }).catch(() => {});

    const music = el("div.section", {}, el("h3", { text: "Musique" }));
    if (api.hasAction("music.pause")) {
      music.append(el("div.btn-row", { "data-zone": "qm-music" },
        btn("⏮", () => run(() => api.call("music.previous")), { cls: "icon-btn", attrs: { "aria-label": "Précédent" } }),
        btn("⏸", () => run(() => api.call("music.pause"), "Pause"), { cls: "icon-btn", attrs: { "aria-label": "Pause" } }),
        btn("▶", () => run(() => api.call("music.resume"), "Lecture"), { cls: "icon-btn primary", attrs: { "aria-label": "Reprendre" } }),
        btn("⏭", () => run(() => api.call("music.next")), { cls: "icon-btn", attrs: { "aria-label": "Suivant" } })));
    } else music.append(unavailable(api.moduleProblem("music") || "lecteur absent"));

    const perf = el("div.section", {}, el("h3", { text: "Profil de performances" }));
    const perfBox = el("div.chips", { "data-zone": "qm-perf" }, loading());
    perf.append(perfBox);
    if (!api.hasAction("hardware.performance_profiles")) perfBox.replaceWith(unavailable(api.moduleProblem("hardware") || "profils absents"));
    else api.call("hardware.performance_profiles").then((r) => {
      clear(perfBox);
      const LABELS = { "power-saver": "Économie", balanced: "Équilibré", performance: "Performance" };
      for (const p of asList(r, "profiles")) {
        const id = typeof p === "string" ? p : pick(p, "id", "name");
        perfBox.append(btn(LABELS[id] || String(id), () => run(() => api.call("hardware.set_performance_profile", { profile: id }), `Profil ${LABELS[id] || id}`),
          { cls: "chip", attrs: { "aria-pressed": String(id === r.active) } }));
      }
      if (!perfBox.children.length) perfBox.append(el("span.muted", { text: "Aucun profil d'énergie disponible" }));
      nav.ensure();
    }).catch((e) => perfBox.replaceWith(errorState(e)));

    const power = el("div.section", {}, el("h3", { text: "Alimentation" }));
    if (api.hasAction("system.power")) {
      const spec = api.actionSpec("system.power");
      const choices = spec?.params?.action?.choices || ["suspend", "reboot", "poweroff"];
      const L = { suspend: "Mettre en veille", reboot: "Redémarrer", poweroff: "Éteindre", logout: "Quitter la session", desktop: "Mode bureau" };
      power.append(el("div.btn-row", { "data-zone": "qm-power" },
        ...choices.map((c) => btn(L[c] || c, () => { close(); run(() => api.call("system.power", { action: c }), L[c] || c); }, { danger: c !== "suspend" }))));
    } else power.append(unavailable(api.moduleProblem("system") || "alimentation absente"));

    const notifs = el("div.section", {}, el("h3", { text: "Notifications récentes" }));
    const n = recentNotifications();
    if (!n.length) notifs.append(el("p.muted", { text: "Aucune notification depuis le démarrage." }));
    for (const x of n.slice(0, 6)) notifs.append(el("div.row-item", {}, el("div.grow", {}, el("div.t", { text: x.title }), x.body ? el("div.s", { text: x.body }) : null)));

    s.append(vol, music, perf, power, notifs, el("div.btn-row", {}, btn("Fermer", () => close())));
  }, { side: true, label: "Menu rapide" });
}

// ---- profiles --------------------------------------------------------------------
function inkFor(hex) {
  const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex || "");
  if (!m) return "#08142b";
  const [r, g, b] = m.slice(1).map((x) => parseInt(x, 16) / 255);
  const lum = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  return lum > 0.5 ? "#08142b" : "#ffffff";
}

function applyProfile(p) {
  state.profile = p;
  if (!p) return;
  document.documentElement.style.setProperty("--accent", p.color || "#4cc9f0");
  document.documentElement.style.setProperty("--accent-ink", inkFor(p.color));
  document.getElementById("me-avatar").textContent = p.avatar || "🎮";
  document.getElementById("me-name").textContent = p.name;
}

function applySettings(s) {
  state.settings = s || {};
  document.documentElement.dataset.theme = state.settings["ui.theme"] || "dark";
  setKeyboardLayout(state.settings["ui.keyboard_layout"] || "azerty");
}

export function openProfilePicker({ force = false } = {}) {
  return api.call("profiles.list").then((r) => {
    const list = asList(r, "profiles");
    if (list.length <= 1 && !force) return null;
    const { done } = openSheet((s, close) => {
      s.classList.add("picker");
      s.append(el("h1", { text: "Qui joue ?" }));
      const grid = el("div.grid", { "data-zone": "profiles" });
      for (const p of list) {
        const card = btn(null, async () => {
          try { const cur = await api.call("profiles.switch", { id: p.id }); applyProfile(cur); close(p); app.go("home"); }
          catch (e) { toastError(e, "Changement de profil"); }
        }, { cls: "profile-card", attrs: { "data-profile": p.id, ...(p.active ? { "data-autofocus": true } : {}) } });
        card.append(el("span.avatar.big", { style: { background: p.color }, text: p.avatar || "🎮" }), el("strong", { text: p.name }));
        grid.append(card);
      }
      s.append(grid);
    }, { label: "Choix du profil" });
    return done;
  }).catch((e) => { toastError(e, "Profils"); return null; });
}

// ---- NEXUS AI overlay ----------------------------------------------------------
let ai = null; // {log, close, busy}

function sourceBadges(r) {
  const out = [];
  const prov = pick(r, "provider");
  if (prov) {
    const local = ["local", "ollama"].includes(String(prov).split(":")[0]);
    out.push(el(`span.badge.${local ? "ok" : "warn"}`, { "data-ai-source": local ? "local" : "remote", text: local ? `Local · ${prov}` : `Distant · ${prov}` }));
  }
  if (r?.fallback) out.push(el("span.badge.warn", { text: typeof r.fallback === "string" ? `Repli : ${r.fallback}` : "Repli local" }));
  if (typeof r?.cost_estimate === "number") out.push(el("span.badge", { text: `≈ ${r.cost_estimate.toFixed(4)} $ (estimation)` }));
  return out;
}

function aiBubble(r) {
  const reply = pick(r, "reply", "text", "message") || "(pas de réponse)";
  const b = el("div.bubble", { "data-ai-reply": true }, el("div", { text: String(reply) }));
  const acts = asList(r?.actions);
  if (acts.length) {
    const ul = el("ul");
    for (const a of acts) {
      const name = typeof a === "string" ? a : pick(a, "action", "name");
      const ok = typeof a === "object" ? (a.error ? `échec : ${pick(a.error, "message") || a.error}` : a.confirmation_id ? "en attente de confirmation" : "exécutée") : "exécutée";
      ul.append(el("li", { text: `${name} — ${ok}` }));
    }
    b.append(ul);
  }
  const badges = sourceBadges(r);
  if (badges.length) b.append(el("div.meta", {}, ...badges));
  return b;
}

async function handleAiConfirmations(r) {
  const ids = [r?.confirmation_id, ...asList(r?.actions).map((a) => a?.confirmation_id)].filter(Boolean);
  if (!ids.length) return;
  let pending = [];
  try { pending = await api.get("/api/confirmations"); } catch { pending = []; }
  for (const id of ids) {
    if (api.isHandled(id)) continue;
    const p = pending.find((x) => x.id === id) || { id, summary: "Action demandée par NEXUS AI", origin: "ai" };
    try { const res = await api.resolveConfirmation(p); toast("Action confirmée", p.summary || ""); if (ai) ai.log.append(aiBubble({ reply: "Action exécutée après votre confirmation.", result: res })); }
    catch (e) { toastError(e); }
  }
}

async function askAi(text) {
  if (!ai) return;
  ai.log.append(el("div.bubble.me", { text }));
  const wait = el("div.bubble.loading", { text: "NEXUS AI réfléchit" });
  ai.log.append(wait);
  ai.busy = true;
  try {
    const r = await api.call("ai.ask", { text });
    wait.replaceWith(aiBubble(r));
    handleAiConfirmations(r);
  } catch (e) {
    wait.replaceWith(errorState(e));
  } finally { if (ai) ai.busy = false; nav.ensure(); }
}

function openAiOverlay({ listening = false } = {}) {
  if (ai) return ai;
  const handle = openSheet((s, close) => {
    s.dataset.aiOverlay = "";
    s.append(el("h2", { text: "NEXUS AI" }));
    const statusLine = el("p.muted.small", { text: "" });
    s.append(statusLine);
    if (!api.hasAction("ai.ask")) {
      s.append(unavailable(api.moduleProblem("ai") || "assistant absent"), el("div.btn-row", {}, btn("Fermer", () => close())));
      ai = { log: el("div"), close, busy: false };
      return;
    }
    api.call("ai.status").then((st) => {
      const prov = pick(st, "provider");
      const remote = st?.remote_enabled;
      statusLine.textContent = `${prov ? "Fournisseur : " + prov : ""}${remote === false ? " · API distantes désactivées (tout reste local)" : remote ? " · API distantes autorisées" : ""}`;
    }).catch(() => {});
    const log = el("div.ai-log", { "aria-live": "polite" });
    const field = textField({ label: "Écrire à NEXUS AI", placeholder: "Ex. : lance ma musique, installe RetroArch…", onChange: (v) => { if (v.trim()) { askAi(v.trim()); field.setValue(""); } }, attrs: { "data-autofocus": true, "data-ai-input": true } });
    const mic = btn("🎤 Parler", () => listenOnce(), { attrs: { "data-ai-mic": true } });
    s.append(log, el("div.list", { "data-zone": "ai-input" }, field), el("div.btn-row", {}, mic, btn("Fermer", () => close())),
      el("p.small.muted", { style: { marginTop: "1rem" }, text: "Maintenez R3 (ou PS) pour parler. Les actions sensibles demandent toujours votre confirmation." }));
    ai = { log, close, busy: false };
  }, { side: true, label: "NEXUS AI" });
  handle.done.then(() => { ai = null; document.getElementById("ai-orb").classList.remove("listening"); });
  if (listening) listenOnce();
  return ai;
}

let talking = false;
async function listenOnce() {
  if (!ai) openAiOverlay();
  if (!api.hasAction("ai.listen")) { toast("Micro indisponible", api.moduleProblem("ai") || "écoute vocale absente", "warning"); return; }
  if (talking) return;
  talking = true;
  document.getElementById("ai-orb").classList.add("listening");
  const wait = el("div.bubble.me.loading", { text: "Écoute" });
  ai?.log.append(wait);
  try {
    const r = await api.call("ai.listen", {});
    const heard = pick(r, "text", "transcript");
    if (pick(r, "reply")) { wait.replaceWith(el("div.bubble.me", { text: heard || "(voix)" })); ai?.log.append(aiBubble(r)); handleAiConfirmations(r); }
    else if (heard) { wait.remove(); askAi(String(heard)); }
    else wait.replaceWith(el("div.bubble.me", { text: "Rien entendu." }));
  } catch (e) { wait.replaceWith(errorState(e)); }
  finally { talking = false; document.getElementById("ai-orb").classList.remove("listening"); }
}
function startTalking() { openAiOverlay(); listenOnce(); }
function stopTalking() { /* listening duration is decided by ai.listen; release only updates the UI */ document.getElementById("ai-orb").classList.remove("listening"); }

// ---- events -----------------------------------------------------------------------
function wireEvents() {
  api.on("connection", ({ online }) => {
    const c = document.getElementById("conn");
    c.classList.toggle("online", online);
    c.textContent = online ? "connecté" : "reconnexion…";
  });
  api.on("notification", (d) => toast(d?.title || "Notification", d?.body || "", d?.level || "info"));
  api.on("task.update", (t) => trackTask(t));
  api.on("input.mode", (d) => {
    const mode = pick(d, "label", "mode") || "manette";
    const help = pick(d, "help", "hint", "message") || "";
    toast(`Manette : ${mode}`, String(help), "info", 6000);
  });
  api.on("confirmation.requested", (d) => {
    if (!d || d.origin !== "ai" || api.isHandled(d.id)) return;
    api.resolveConfirmation(d).then(() => toast("Action confirmée", d.summary || "")).catch((e) => toastError(e));
  });
  api.on("ai.reply", (d) => {
    if (ai && !ai.busy) ai.log.append(aiBubble(d));
    else if (!ai) toast("NEXUS AI", String(pick(d, "reply", "text") || ""));
  });
  api.on("profiles.changed", () => api.call("profiles.current").then(applyProfile).catch(() => {}));
  api.on("settings.changed", (d) => { if (d?.key) { state.settings[d.key] = d.value; applySettings(state.settings); } });
}

function clock() {
  const c = document.getElementById("clock");
  const tick = () => { c.textContent = new Date().toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" }); };
  tick();
  setInterval(tick, 10_000);
}

// ---- boot -------------------------------------------------------------------------
async function boot() {
  api.setConfirmHandler(confirmDialog);
  clock();
  setHints(DEFAULT_HINTS);
  startInput();
  wireEvents();
  document.getElementById("me").addEventListener("click", () => openProfilePicker({ force: true }));
  document.getElementById("ai-orb").addEventListener("click", () => openAiOverlay());
  try { await api.refreshMeta(); }
  catch (e) {
    clear(document.getElementById("content")).append(errorState(e));
    toastError(e, "Connexion à NEXUS");
  }
  try { applySettings((await api.call("profiles.settings_get")).settings); } catch { applySettings({}); }
  try { applyProfile(await api.call("profiles.current")); } catch { /* profiles module missing: keep defaults */ }
  api.connectEvents();
  renderTabs();
  app.go("home");
  document.documentElement.dataset.ready = "1";
  if (api.hasAction("profiles.list")) await openProfilePicker();
}

window.nexus = { app, nav, api };  // debugging & tests
boot();
