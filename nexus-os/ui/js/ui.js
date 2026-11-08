// Shared UI pieces: toasts, modal sheets, confirmation dialog, empty states, tasks.

import { el, btn, clear } from "./dom.js";
import { nav } from "./nav.js";
import { ApiError, moduleProblem } from "./api.js";

// ---- toasts --------------------------------------------------------------------
const recent = [];
export const recentNotifications = () => recent.slice();

export function toast(title, body = "", level = "info", ms = 5000) {
  recent.unshift({ title, body, level, ts: Date.now() });
  recent.length = Math.min(recent.length, 20);
  const box = document.getElementById("toasts");
  const t = el(`div.toast.${level}`, { role: "status" }, el("div.t", { text: title }), body ? el("div.small.muted", { text: body }) : null);
  box.prepend(t);
  while (box.children.length > 4) box.lastChild.remove();
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 300); }, ms);
  return t;
}

export function toastError(e, prefix = "") {
  if (e instanceof ApiError && e.code === "rejected") return toast("Action annulée", "", "warning");
  const msg = e?.message || String(e);
  toast(prefix || "Échec", msg, "error", 7000);
}

// ---- modal sheets ----------------------------------------------------------------
/**
 * Opens a modal layer. `build(sheet, close)` fills it. Returns {close, sheet, done}
 * where done is a promise resolved with the value passed to close().
 */
export function openSheet(build, { cls = "", side = false, onIntent, onKey, label = "Fenêtre" } = {}) {
  let resolve;
  const done = new Promise((r) => (resolve = r));
  const sheet = el(`div.sheet${cls ? "." + cls.split(" ").join(".") : ""}`, { role: "dialog", "aria-modal": "true", "aria-label": label });
  const layer = el(`div.layer${side ? ".side" : ""}`, {}, sheet);
  let pop;
  let closed = false;
  const close = (value) => { if (closed) return; closed = true; pop(); resolve(value); };
  document.getElementById("layers").append(layer);
  pop = nav.pushLayer(layer, { onBack: () => close(undefined), onIntent: onIntent ? (i, info) => onIntent(i, info, close) : undefined });
  const top = nav.topLayer();
  if (onKey) top.onKey = (e) => onKey(e, close);
  build(sheet, close);
  return { close, sheet, done };
}

const RISK_LABELS = {
  read: "Lecture", control: "Contrôle", settings: "Réglages", install_verified: "Installation vérifiée",
  install_free_game: "Jeu gratuit", install_owned: "Jeu possédé", purchase: "Achat",
  unknown_source: "Source non vérifiée", delete_data: "Suppression de données", system: "Système",
};

/** Confirmation dialog. Resolves true (approve) or false (reject). Focus starts on « Annuler ». */
export function confirmDialog(d) {
  const fromAi = d.origin === "ai";
  const risky = ["delete_data", "system", "purchase", "unknown_source"].includes(d.risk);
  const { done } = openSheet((s, close) => {
    s.append(
      el("h2", { text: fromAi ? "NEXUS AI demande votre accord" : "Confirmer l'action" }),
      el("p.summary", { text: d.summary || d.action }),
      el("p.risk", {},
        el(`span.badge.${risky ? "bad" : "warn"}`, { text: RISK_LABELS[d.risk] || d.risk || "Action" }), " ",
        el("span.muted.small", { text: d.action || "" })),
      d.risk === "unknown_source" ? el("p.muted", { text: "Ce logiciel ne provient pas d'une source vérifiée. Installez-le seulement si vous lui faites confiance." }) : null,
      el("div.btn-row", {},
        btn("Annuler", () => close(false), { attrs: { "data-autofocus": true, "data-confirm": "reject" } }),
        btn("Confirmer", () => close(true), { primary: true, attrs: { "data-confirm": "approve" } })),
    );
  }, { cls: "confirm", label: "Confirmation" });
  return done.then((v) => v === true);
}

// ---- states --------------------------------------------------------------------
export const loading = (text = "Chargement") => el("div.loading", { text });

export function empty(title, body = "", action) {
  return el("div.empty", {}, el("strong", { text: title }), body ? el("p", { text: body }) : null,
    action ? el("div.btn-row", {}, action) : null);
}

export function unavailable(reason) {
  return el("div.empty.unavailable", { "data-state": "unavailable" },
    el("strong", { text: `Module indisponible : ${reason}` }),
    el("p", { text: "Cette fonction ne peut pas être utilisée sur cette machine pour le moment." }));
}

/** Error from a call → explicit state (never fake data). */
export function errorState(e) {
  if (e instanceof ApiError && (e.code === "unavailable" || (e.code === "not_found" && /Action inconnue/.test(e.message)))) {
    return unavailable(e.message.replace(/^Action inconnue: /, "action absente ").trim());
  }
  return el("div.empty", { "data-state": "error" }, el("strong", { text: "Impossible de charger" }), el("p", { text: e?.message || String(e) }));
}

/**
 * Loads `fn()` into `box`, rendering `render(result)`; on failure renders the
 * explicit unavailable/error state. If `module` is given and absent from
 * /api/status, the call is not even attempted.
 */
export async function loadInto(box, fn, render, { module } = {}) {
  clear(box).append(loading());
  const problem = module ? moduleProblem(module) : null;
  if (problem) { clear(box).append(unavailable(problem)); nav.ensure(); return null; }
  try {
    const r = await fn();
    clear(box);
    const out = render(r);
    if (out) box.append(...[out].flat().filter(Boolean));
    nav.ensure();
    return r;
  } catch (e) {
    clear(box).append(errorState(e));
    nav.ensure();
    return null;
  }
}

// ---- tasks progress ------------------------------------------------------------
const tasks = new Map();
export function trackTask(t) {
  if (!t?.id) return;
  const box = document.getElementById("tasks");
  let row = tasks.get(t.id);
  if (!row) {
    row = el("div.task", { "data-task": t.id }, el("span.title"), el("span.pct.muted"), el("div.meter", {}, el("i")));
    tasks.set(t.id, row);
    box.append(row);
  }
  row.className = `task ${t.state || ""}`;
  row.querySelector(".title").textContent = `${t.title}${t.message ? " — " + t.message : ""}`;
  row.querySelector(".pct").textContent = t.state === "failed" ? "échec" : `${Math.round((t.progress || 0) * 100)} %`;
  row.querySelector(".meter > i").style.transform = `scaleX(${t.progress || 0})`;
  if (t.state === "done" || t.state === "failed") {
    setTimeout(() => { row.remove(); tasks.delete(t.id); }, t.state === "done" ? 2500 : 8000);
  }
}

/** Shows the right feedback for an action result (task, message). */
export function reportResult(r, okText) {
  if (r && r.task_id) { trackTask({ id: r.task_id, title: okText || "Tâche", progress: 0, state: "queued" }); return; }
  toast(okText || "Fait", typeof r?.message === "string" ? r.message : "");
}

/** Runs an action from a button with feedback. */
export async function run(fn, okText, errText) {
  try {
    const r = await fn();
    reportResult(r, okText);
    return r;
  } catch (e) { toastError(e, errText); return null; }
}

// ---- hints bar -----------------------------------------------------------------
export function setHints(list) {
  const h = document.getElementById("hints");
  clear(h);
  for (const [g, cls, text] of list) h.append(el("span.hint", {}, el(`span.glyph.${cls}`, { text: g }), text));
}
export const DEFAULT_HINTS = [
  ["✕", "x", "Valider"], ["○", "o", "Retour"], ["△", "t", "Rechercher"], ["□", "s", "Options"],
  ["☰", "m", "Menu rapide"], ["R3", "m", "Maintenir : NEXUS AI"],
];
