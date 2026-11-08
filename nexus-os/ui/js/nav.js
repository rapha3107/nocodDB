// Spatial focus navigation (gamepad / keyboard / remote).
//
// Focusable elements carry [data-nav]. Navigation is geometric: from the
// focused element's rect, the best candidate in the requested direction is the
// one minimising (distance along the axis + 2 × lateral offset). Candidates in
// the same zone ([data-zone] ancestor) are preferred; when focus enters another
// zone, the element last focused in it is restored (console-like behaviour).
// Layers (modals) trap focus: only the top layer is navigable.

const DIRS = { up: [0, -1], down: [0, 1], left: [-1, 0], right: [1, 0] };
const layers = [];          // [{el, onBack, onIntent, restore}]
let current = null;
const intentHandlers = [];  // global fallbacks (app)

export const nav = {
  get current() { return current; },

  root() { return layers.length ? layers[layers.length - 1].el : document.getElementById("app"); },

  focusables(scope = this.root()) {
    return [...scope.querySelectorAll("[data-nav]")].filter((e) => !e.disabled && visible(e));
  },

  focus(el, { scroll = true } = {}) {
    if (!el) return;
    if (current && current !== el) current.classList.remove("is-focused");
    current = el;
    el.classList.add("is-focused");
    if (document.activeElement !== el) el.focus({ preventScroll: true });
    const zone = el.closest("[data-zone]");
    if (zone) zone._lastFocus = el;
    if (scroll) {
      const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
      el.scrollIntoView({ block: "nearest", inline: "nearest", behavior: reduce ? "auto" : "smooth" });
    }
    el.dispatchEvent(new CustomEvent("nav:focus", { bubbles: true }));
  },

  /** Focus the preferred element of the root (data-autofocus, else first). */
  focusFirst(scope = this.root()) {
    const list = this.focusables(scope);
    const pref = list.find((e) => e.hasAttribute("data-autofocus")) || list[0];
    if (pref) this.focus(pref);
    return pref;
  },

  /** Re-focus if the focused element disappeared or is outside the top layer. */
  ensure(scope) {
    const root = this.root();
    if (current && document.contains(current) && root.contains(current) && visible(current)) return;
    this.focusFirst(scope && root.contains(scope) ? scope : root);
  },

  move(dir) {
    const root = this.root();
    if (!current || !root.contains(current) || !document.contains(current)) { this.focusFirst(); return true; }
    const target = findTarget(current, dir, this.focusables(root));
    if (target) { this.focus(target); return true; }
    return false;
  },

  /** Tab / Shift+Tab: DOM order. */
  step(delta) {
    const list = this.focusables();
    if (!list.length) return;
    const i = list.indexOf(current);
    this.focus(list[(i + delta + list.length) % list.length]);
  },

  pushLayer(el, { onBack, onIntent } = {}) {
    const layer = { el, onBack, onIntent, restore: current };
    layers.push(layer);
    queueMicrotask(() => this.focusFirst(el));
    return () => this.popLayer(layer);
  },

  popLayer(layer) {
    const i = layers.indexOf(layer);
    if (i < 0) return;
    layers.splice(i, 1);
    layer.el.remove();
    if (current && !document.contains(current)) current = null;
    if (layer.restore && document.contains(layer.restore) && this.root().contains(layer.restore)) this.focus(layer.restore, { scroll: false });
    else this.ensure();
  },

  get layerCount() { return layers.length; },
  topLayer() { return layers[layers.length - 1] || null; },

  onIntent(fn) { intentHandlers.push(fn); },

  /** Single entry point for gamepad, keyboard and tests. */
  dispatch(intent, info = {}) {
    const top = layers[layers.length - 1];
    if (top?.onIntent && top.onIntent(intent, info) === true) return;
    if (intent in DIRS) {
      if (!this.move(intent)) current?.dispatchEvent(new CustomEvent("nav:edge", { bubbles: true, detail: intent }));
      return;
    }
    if (intent === "accept") {
      if (current && this.root().contains(current)) current.click();
      else this.focusFirst();
      return;
    }
    if (intent === "back" && top) { top.onBack ? top.onBack() : this.popLayer(top); return; }
    for (const fn of intentHandlers) if (fn(intent, info) === true) return;
  },
};

function visible(e) {
  if (e.closest("[hidden]")) return false;
  const r = e.getBoundingClientRect();
  return r.width > 0 && r.height > 0;
}

function findTarget(from, dir, all) {
  const zone = from.closest("[data-zone]");
  const inZone = zone ? all.filter((e) => e !== from && zone.contains(e)) : [];
  let best = zone ? bestIn(from, dir, inZone) : null;
  if (best) return best;
  best = bestIn(from, dir, all.filter((e) => e !== from && !(zone && zone.contains(e))));
  if (!best) return null;
  const z = best.closest("[data-zone]");
  if (z && z !== zone && z._lastFocus && z._lastFocus !== from && document.contains(z._lastFocus) && visible(z._lastFocus)
      && all.includes(z._lastFocus) && isInDirection(rect(from), rect(z._lastFocus), dir)) {
    return z._lastFocus;
  }
  return best;
}

function rect(e) {
  // Use the untransformed layout box (focused items are scaled).
  const r = e.getBoundingClientRect();
  if (e.classList.contains("is-focused")) {
    const s = parseFloat(getComputedStyle(e).transform.split(",")[3]) || 1;
    const w = r.width / s, h = r.height / s;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    return { left: cx - w / 2, right: cx + w / 2, top: cy - h / 2, bottom: cy + h / 2, cx, cy };
  }
  return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, cx: r.left + r.width / 2, cy: r.top + r.height / 2 };
}

function isInDirection(a, b, dir) {
  const [dx, dy] = DIRS[dir];
  const EPS = 2;
  if (dx > 0) return b.left >= a.right - EPS || (b.cx > a.cx + EPS && b.left > a.left + EPS);
  if (dx < 0) return b.right <= a.left + EPS || (b.cx < a.cx - EPS && b.right < a.right - EPS);
  if (dy > 0) return b.top >= a.bottom - EPS || (b.cy > a.cy + EPS && b.top > a.top + EPS);
  return b.bottom <= a.top + EPS || (b.cy < a.cy - EPS && b.bottom < a.bottom - EPS);
}

function bestIn(from, dir, cands) {
  const a = rect(from);
  const [dx] = DIRS[dir];
  let best = null, bestScore = Infinity;
  for (const e of cands) {
    const b = rect(e);
    if (!isInDirection(a, b, dir)) continue;
    let along, lateral;
    if (dx !== 0) {
      along = dx > 0 ? Math.max(0, b.left - a.right) : Math.max(0, a.left - b.right);
      const overlap = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      lateral = overlap > 0 ? 0 : Math.abs(b.cy - a.cy);
    } else {
      along = DIRS[dir][1] > 0 ? Math.max(0, b.top - a.bottom) : Math.max(0, a.top - b.bottom);
      const overlap = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      lateral = overlap > 0 ? Math.abs(b.cx - a.cx) * 0.1 : Math.abs(b.cx - a.cx);
    }
    const score = along + lateral * 2;
    if (score < bestScore) { bestScore = score; best = e; }
  }
  return best;
}
