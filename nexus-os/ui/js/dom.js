// Tiny DOM helpers. Never uses innerHTML with data: every text goes through textContent.

export function el(tag, attrs = {}, ...children) {
  const [name, ...classes] = tag.split(".");
  const node = document.createElement(name || "div");
  if (classes.length) node.className = classes.join(" ");
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className += (node.className ? " " : "") + v;
    else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "text") node.textContent = v;
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  append(node, children);
  return node;
}

export function append(node, children) {
  for (const c of children.flat(Infinity)) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function clear(node) { node.replaceChildren(); return node; }

/** A focusable button. `onPress` is required: no button without an action. */
export function btn(label, onPress, { cls = "btn", primary = false, danger = false, attrs = {}, icon } = {}) {
  if (typeof onPress !== "function") throw new Error("btn() requires an action");
  const b = el(`button.${cls}`, { "data-nav": true, type: "button", ...attrs });
  if (primary) b.classList.add("primary");
  if (danger) b.classList.add("danger");
  if (icon) b.append(el("span", { "aria-hidden": "true", text: icon }));
  if (label !== null && label !== undefined) b.append(label instanceof Node ? label : document.createTextNode(label));
  b.addEventListener("click", (e) => { e.preventDefault(); onPress(b, e); });
  return b;
}

// ---- tolerant readers for module results (modules are developed in parallel) ----

/** Returns the first array found in `r` (r itself, or r[key] for known keys). */
export function asList(r, ...keys) {
  if (Array.isArray(r)) return r;
  if (!r || typeof r !== "object") return [];
  for (const k of [...keys, "items", "results", "list", "data"]) if (Array.isArray(r[k])) return r[k];
  for (const v of Object.values(r)) if (Array.isArray(v)) return v;
  return [];
}

export function pick(o, ...keys) {
  if (!o || typeof o !== "object") return undefined;
  for (const k of keys) if (o[k] !== undefined && o[k] !== null && o[k] !== "") return o[k];
  return undefined;
}

export const nameOf = (o) => String(pick(o, "title", "name", "label", "id") ?? "Sans titre");
export const idOf = (o) => pick(o, "id", "app_id", "rom_id", "station_id", "track_id", "playlist_id", "key");

export function fmtBytes(n) {
  if (typeof n !== "number" || !isFinite(n)) return "—";
  const u = ["o", "Ko", "Mo", "Go", "To"];
  let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i ? 1 : 0)} ${u[i]}`;
}

/** Renders a plain object as a <dl>, skipping nested structures unless simple. */
export function kv(obj, labels = {}) {
  const dl = el("dl.kv");
  for (const [k, v] of Object.entries(obj || {})) {
    if (v === null || v === undefined || v === "") continue;
    let text;
    if (typeof v === "boolean") text = v ? "oui" : "non";
    else if (typeof v === "object") {
      if (Array.isArray(v) && v.every((x) => typeof x !== "object")) text = v.join(", ");
      else continue;
    } else text = String(v);
    dl.append(el("dt", { text: labels[k] || k }), el("dd", { text }));
  }
  return dl;
}
