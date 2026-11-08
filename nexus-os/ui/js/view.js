// Reusable views: tiles, covers, generic result rendering.

import { el, btn, kv, asList, pick, nameOf } from "./dom.js";

const SAFE_URL = /^(\/api\/m\/|https:\/\/|data:image\/)/;

/** Background-image style for a cover URL served by a module or HTTPS. */
export function coverStyle(o) {
  const url = pick(o, "cover", "cover_url", "image", "icon", "artwork", "thumbnail");
  if (typeof url !== "string" || !SAFE_URL.test(url)) return {};
  return { backgroundImage: `url("${url.replace(/["\\\n]/g, "")}")` };
}

const LEVELS = {
  stable: "ok", bon: "ok", good: "ok", verified: "ok", playable: "ok",
  "expérimental": "warn", experimental: "warn", "limité": "warn", limited: "warn", unknown: "warn", inconnu: "warn",
  "non supporté": "bad", unsupported: "bad", broken: "bad",
};
export function levelBadge(level) {
  if (!level) return null;
  const txt = typeof level === "object" ? pick(level, "level", "label", "rating") : level;
  if (!txt) return null;
  return el(`span.badge.${LEVELS[String(txt).toLowerCase()] || ""}`, { text: String(txt) });
}

export function gameTile(g, onPress, extra = {}) {
  const t = btn(null, onPress, { cls: "tile game", attrs: { "data-game": pick(g, "id", "rom_id") ?? "", ...extra } });
  Object.assign(t.style, coverStyle(g));
  const src = pick(g, "source", "system", "launcher", "platform");
  t.append(el("span.tile-title", { text: nameOf(g) }), src ? el("span.tile-sub", { text: String(src) }) : null);
  return t;
}

/** Renders any action result honestly: strings, key/values, and lists of checks. */
export function resultView(r) {
  if (r === null || r === undefined) return el("p.muted", { text: "Terminé." });
  if (typeof r !== "object") return el("p", { text: String(r) });
  const box = el("div.panel.output", { "data-result": true });
  const msg = pick(r, "message", "summary", "reply");
  if (typeof msg === "string") box.append(el("p", { style: { marginBottom: ".8rem" }, text: msg }));
  const flat = {};
  for (const [k, v] of Object.entries(r)) if (typeof v !== "object" && !["message", "summary", "reply"].includes(k)) flat[k] = v;
  if (Object.keys(flat).length) box.append(kv(flat));
  for (const [k, v] of Object.entries(r)) {
    if (!Array.isArray(v) || !v.length) continue;
    const list = el("div.list", { style: { marginTop: ".8rem" } }, el("h3", { text: k }));
    for (const item of v.slice(0, 50)) {
      if (typeof item !== "object" || item === null) { list.append(el("div.row-item", {}, el("div.grow", { text: String(item) }))); continue; }
      const status = pick(item, "status", "level", "state", "ok");
      list.append(el("div.row-item", {},
        el("div.grow", {}, el("div.t", { text: nameOf(item) }),
          pick(item, "message", "detail", "description", "fix") ? el("div.s", { text: String(pick(item, "message", "detail", "description", "fix")) }) : null),
        status !== undefined ? el(`span.badge.${status === true || status === "ok" ? "ok" : status === false || status === "error" || status === "fail" ? "bad" : "warn"}`,
          { text: status === true ? "ok" : status === false ? "problème" : String(status) }) : null));
    }
    box.append(list);
  }
  for (const [k, v] of Object.entries(r)) {
    if (v && typeof v === "object" && !Array.isArray(v)) {
      const inner = kv(v);
      if (inner.children.length) box.append(el("h3", { style: { marginTop: ".8rem" }, text: k }), inner);
    }
  }
  return box;
}

export { asList };
