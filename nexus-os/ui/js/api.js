// NEXUS API client: actions, confirmations, module status, SSE events.

const meta = document.querySelector('meta[name="nexus-token"]');
export const TOKEN = meta ? meta.content : "";

export class ApiError extends Error {
  constructor(code, message, status = 0, details = {}) {
    super(message || code);
    this.code = code;
    this.status = status;
    this.details = details || {};
  }
}

let confirmHandler = async () => false;
/** fn(details) -> Promise<boolean>; shows the confirmation dialog. */
export function setConfirmHandler(fn) { confirmHandler = fn; }

async function request(method, path, body) {
  let res;
  try {
    res = await fetch(path, {
      method,
      headers: { "Content-Type": "application/json", "X-Nexus-Token": TOKEN },
      body: body === undefined ? undefined : JSON.stringify(body),
      cache: "no-store",
    });
  } catch (e) {
    throw new ApiError("network", "NEXUS ne répond pas (service nexusd arrêté ?)");
  }
  let data = null;
  try { data = await res.json(); } catch { data = null; }
  if (!res.ok) {
    throw new ApiError(data?.error || `http_${res.status}`, data?.message || `Erreur ${res.status}`, res.status, data?.details);
  }
  return data;
}

export const get = (path) => request("GET", path);

// ---- catalog & module status ------------------------------------------------
let catalog = null;   // Map action -> spec
let status = null;    // {modules, load_errors}

export async function refreshMeta() {
  const [acts, st] = await Promise.all([get("/api/actions"), get("/api/status")]);
  catalog = new Map(acts.map((a) => [a.action, a]));
  status = st;
  return { catalog, status };
}
export const hasAction = (name) => !!catalog?.has(name);
export const actionSpec = (name) => catalog?.get(name);

/** Why a module can't be used, or null if it is loaded. */
export function moduleProblem(module) {
  if (!status) return null;
  if (status.modules?.[module]) return null;
  const err = status.load_errors?.[module];
  return err ? `erreur de chargement (${err})` : "module non installé ou non chargé";
}
export function moduleStatus(module) { return status?.modules?.[module] || null; }

/**
 * Calls an action. Handles 409 confirmation_required by showing the
 * confirmation dialog; approve executes the action server-side.
 */
export async function call(action, params = {}) {
  if (catalog && !catalog.has(action)) {
    const mod = action.split(".")[0];
    const why = moduleProblem(mod) || `action ${action} absente`;
    throw new ApiError("unavailable", why, 503);
  }
  const spec = catalog?.get(action);
  const clean = {};
  for (const [k, v] of Object.entries(params || {})) {
    if (v === undefined || v === null || v === "") continue;
    if (spec && spec.params && !(k in spec.params)) { console.warn(`[nexus] paramètre ${k} ignoré pour ${action}`); continue; }
    clean[k] = v;
  }
  try {
    const r = await request("POST", "/api/call", { action, params: clean });
    return r.result;
  } catch (e) {
    if (e instanceof ApiError && e.code === "confirmation_required" && e.details?.confirmation_id) {
      return resolveConfirmation({ ...e.details, id: e.details.confirmation_id, origin: "user" });
    }
    throw e;
  }
}

const handledConfirmations = new Set();
export const isHandled = (id) => handledConfirmations.has(id);

/** Shows the dialog for a pending confirmation and approves/rejects it. */
export async function resolveConfirmation(details) {
  const id = details.id || details.confirmation_id;
  handledConfirmations.add(id);
  const ok = await confirmHandler(details);
  if (!ok) {
    await request("POST", `/api/confirmations/${encodeURIComponent(id)}/reject`, {}).catch(() => {});
    throw new ApiError("rejected", "Action annulée");
  }
  const r = await request("POST", `/api/confirmations/${encodeURIComponent(id)}/approve`, {});
  return r.result;
}

// ---- events (SSE) --------------------------------------------------------------
const listeners = new Map();
export function on(topic, fn) {
  if (!listeners.has(topic)) listeners.set(topic, new Set());
  listeners.get(topic).add(fn);
  return () => listeners.get(topic).delete(fn);
}
function emit(topic, data) {
  for (const key of [topic, "*"]) for (const fn of listeners.get(key) || []) {
    try { fn(data, topic); } catch (e) { console.error(e); }
  }
}

const TOPICS = ["notification", "task.update", "music.state", "confirmation.requested", "ai.reply",
  "ai.listening", "input.mode", "profiles.changed", "settings.changed", "games.changed", "store.changed"];
let es = null;
let retry = 1000;
export function connectEvents() {
  if (es) es.close();
  es = new EventSource(`/api/events?token=${encodeURIComponent(TOKEN)}`);
  es.onopen = () => { retry = 1000; emit("connection", { online: true }); };
  es.onerror = () => {
    emit("connection", { online: false });
    es.close();
    es = null;
    setTimeout(connectEvents, retry);
    retry = Math.min(retry * 2, 15000);
  };
  const handler = (ev) => {
    let d = null;
    try { d = JSON.parse(ev.data); } catch { return; }
    emit(d.topic || ev.type, d.data);
  };
  for (const t of TOPICS) es.addEventListener(t, handler);
  es.onmessage = handler;
}
