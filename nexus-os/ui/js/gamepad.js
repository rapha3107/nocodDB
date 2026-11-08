// Gamepad API ("standard" mapping, DualSense via Chromium/Firefox) + PC keyboard.
//
//   D-pad / left stick  move focus (dead zone, auto-repeat)
//   ✕ (0) accept · ○ (1) back · □ (2) options · △ (3) search / keyboard
//   L1 (4) / R1 (5) previous / next tab · Options (9) quick menu
//   PS (16) tap = home, hold = push-to-talk · R3 (11) hold = push-to-talk

import { nav } from "./nav.js";

const DEAD = 0.5;
const REPEAT_DELAY = 380;
const REPEAT_RATE = 110;
const PS_HOLD = 450;

const BUTTONS = { 0: "accept", 1: "back", 2: "options", 3: "search", 4: "prev", 5: "next", 9: "menu" };
const DPAD = { 12: "up", 13: "down", 14: "left", 15: "right" };

const prev = new Map();      // `${pad}:${btn}` -> pressed
const dirState = new Map();  // pad -> {dir, since, last}
const psState = new Map();   // pad -> {since, talking}
let pttActive = false;
let running = false;
let lastInput = 0;
export const lastInputTime = () => lastInput;

function pressed(b) { return !!b && (typeof b === "object" ? b.pressed || b.value > 0.6 : b === 1.0); }

function poll(now) {
  let pads = [];
  try { pads = navigator.getGamepads ? [...navigator.getGamepads()] : []; } catch { pads = []; }
  for (const pad of pads) {
    if (!pad || pad.connected === false) continue;
    const id = pad.index ?? 0;
    const btns = pad.buttons || [];
    const isDown = (i) => pressed(btns[i]);
    const edge = (i) => {
      const k = `${id}:${i}`;
      const was = prev.get(k) || false;
      const is = isDown(i);
      prev.set(k, is);
      return is && !was ? "down" : !is && was ? "up" : null;
    };

    for (const [i, intent] of Object.entries(BUTTONS)) if (edge(+i) === "down") fire(intent);

    // R3: hold-to-talk
    const r3 = edge(11);
    if (r3 === "down") startPtt();
    if (r3 === "up") stopPtt();

    // PS: tap = home, hold = talk
    const ps = edge(16);
    const st = psState.get(id) || {};
    if (ps === "down") psState.set(id, { since: now, talking: false });
    else if (isDown(16) && st.since && !st.talking && now - st.since > PS_HOLD) { st.talking = true; startPtt(); }
    else if (ps === "up") {
      if (st.talking) stopPtt(); else fire("home");
      psState.delete(id);
    }

    // direction: d-pad has priority over the left stick
    let dir = null;
    for (const [i, d] of Object.entries(DPAD)) if (isDown(+i)) dir = d;
    edge(12); edge(13); edge(14); edge(15);
    if (!dir) {
      const [x = 0, y = 0] = pad.axes || [];
      if (Math.max(Math.abs(x), Math.abs(y)) > DEAD) dir = Math.abs(x) > Math.abs(y) ? (x > 0 ? "right" : "left") : (y > 0 ? "down" : "up");
    }
    const ds = dirState.get(id);
    if (!dir) dirState.delete(id);
    else if (!ds || ds.dir !== dir) { dirState.set(id, { dir, since: now, last: now }); fire(dir); }
    else if (now - ds.since > REPEAT_DELAY && now - ds.last > REPEAT_RATE) { ds.last = now; fire(dir, { repeat: true }); }
  }
  requestAnimationFrame(poll);
}

function fire(intent, info = {}) {
  lastInput = performance.now();
  document.documentElement.dataset.input = "pad";
  nav.dispatch(intent, { source: "gamepad", ...info });
}
function startPtt() { if (!pttActive) { pttActive = true; fire("ptt-start"); } }
function stopPtt() { if (pttActive) { pttActive = false; fire("ptt-end"); } }

const KEYS = {
  ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right",
  Enter: "accept", " ": "accept", Escape: "back", Backspace: "back",
  PageUp: "prev", PageDown: "next", Home: "home", F1: "menu", ContextMenu: "options", F2: "options", F3: "search",
};

function onKey(e) {
  if (e.altKey || e.ctrlKey || e.metaKey) return;
  document.documentElement.dataset.input = "keys";
  // Text entry layers (virtual keyboard) get first pick of printable keys.
  const top = nav.topLayer();
  if (top?.onKey && top.onKey(e) === true) { e.preventDefault(); return; }
  if (e.key === "Tab") { e.preventDefault(); nav.step(e.shiftKey ? -1 : 1); return; }
  if (e.key === "q" && !e.repeat) { e.preventDefault(); nav.dispatch("prev", { source: "keyboard" }); return; }
  if (e.key === "e" && !e.repeat) { e.preventDefault(); nav.dispatch("next", { source: "keyboard" }); return; }
  if (e.key === "v" && !e.repeat) { e.preventDefault(); startPtt(); return; }
  const intent = KEYS[e.key];
  if (!intent) return;
  e.preventDefault();
  if (e.repeat && !["up", "down", "left", "right"].includes(intent)) return;
  nav.dispatch(intent, { source: "keyboard", repeat: e.repeat });
}
function onKeyUp(e) { if (e.key === "v") stopPtt(); }

export function startInput() {
  if (running) return;
  running = true;
  window.addEventListener("keydown", onKey);
  window.addEventListener("keyup", onKeyUp);
  // Mouse / touch on PC: hovering a focusable moves the focus there.
  document.addEventListener("pointermove", (e) => {
    const t = e.target.closest?.("[data-nav]");
    if (t && t !== nav.current && nav.root().contains(t)) nav.focus(t, { scroll: false });
  });
  requestAnimationFrame(poll);
}
