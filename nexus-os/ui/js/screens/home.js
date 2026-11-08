import { el, btn, asList, pick, nameOf } from "../dom.js";
import * as api from "../api.js";
import { loadInto, empty, run, unavailable } from "../ui.js";
import { gameTile } from "../view.js";
import { openGame } from "./library.js";

const TILES = [
  { tab: "library", module: "games", icon: "🎮", title: "Jeux", sub: "Steam, Heroic, Lutris" },
  { tab: "emulation", module: "emulation", icon: "🕹️", title: "Émulation", sub: "Systèmes et ROMs personnelles" },
  { tab: "store", module: "store", icon: "🛍️", title: "Store", sub: "Applications Flathub et jeux gratuits" },
  { tab: "music", module: "music", icon: "🎧", title: "Musique", sub: "Bibliothèque et radios" },
  { tab: "browser", module: "browser", icon: "🌐", title: "Navigateur", sub: "Web et services de streaming" },
  { tab: "vr", module: "vr", icon: "🥽", title: "VR", sub: "Casques et applications" },
  { tab: "settings", module: "profiles", icon: "⚙️", title: "Paramètres", sub: "Profils, IA, système" },
];

function greeting() {
  const h = new Date().getHours();
  return h < 6 ? "Bonne nuit" : h < 18 ? "Bonjour" : "Bonsoir";
}

export function miniPlayer() {
  const box = el("div.panel.player", { "data-mini-player": true });
  if (!api.hasAction("music.status")) { box.replaceChildren(unavailable(api.moduleProblem("music") || "lecteur absent")); return box; }
  const title = el("div.t", { text: "Rien en lecture" });
  const sub = el("div.small.muted", { text: "Musique" });
  const playBtn = btn("▶", () => toggle(), { cls: "icon-btn primary", attrs: { "aria-label": "Lecture / pause" } });
  let playing = false;
  const show = (s) => {
    if (!s) return;
    playing = !!(pick(s, "playing") ?? (pick(s, "state") === "playing"));
    const tr = s.track || s.current || s.now_playing || s.station || null;
    title.textContent = tr ? nameOf(tr) : (pick(s, "title") || "Rien en lecture");
    sub.textContent = tr ? String(pick(tr, "artist", "album", "country", "source") || "") : "Choisissez un titre dans Musique";
    playBtn.textContent = playing ? "⏸" : "▶";
  };
  const toggle = () => run(async () => { const r = await api.call(playing ? "music.pause" : (title.textContent === "Rien en lecture" ? "music.play" : "music.resume")); api.call("music.status").then(show).catch(() => {}); return r; });
  box.append(el("span", { "aria-hidden": "true", style: { fontSize: "2rem" }, text: "🎵" }),
    el("div.np", {}, title, sub),
    el("div.btn-row", { style: { marginTop: 0 }, "data-zone": "player" },
      btn("⏮", () => run(() => api.call("music.previous")), { cls: "icon-btn", attrs: { "aria-label": "Précédent" } }),
      playBtn,
      btn("⏭", () => run(() => api.call("music.next")), { cls: "icon-btn", attrs: { "aria-label": "Suivant" } })));
  api.call("music.status").then(show).catch(() => { sub.textContent = "Lecteur indisponible"; });
  const off = api.on("music.state", show);
  box._dispose = off;
  return box;
}

export default {
  id: "home",
  title: "Accueil",
  render(root, app) {
    const name = app.state.profile?.name;
    root.append(el("div.screen-head", {}, el("div", {},
      el("h1", { text: `${greeting()}${name ? ", " + name : ""}` }),
      el("p.muted", { text: "Que voulez-vous lancer ?" }))));

    // Recently played
    const recentSec = el("section.section", {}, el("h2", { text: "Récemment joués" }));
    const rail = el("div.rail", { "data-zone": "recent" });
    recentSec.append(rail);
    loadInto(rail, () => api.call("games.library"), (r) => {
      const games = asList(r, "games");
      const recent = games.filter((g) => pick(g, "last_played", "lastPlayed", "last_played_at"))
        .sort((a, b) => Number(pick(b, "last_played", "lastPlayed", "last_played_at")) - Number(pick(a, "last_played", "lastPlayed", "last_played_at")))
        .slice(0, 12);
      if (!recent.length) return empty(games.length ? "Aucun jeu lancé récemment" : "Aucun jeu détecté",
        games.length ? "Lancez un jeu depuis la bibliothèque pour le retrouver ici." : "Installez Steam, Heroic ou Lutris, ou ajoutez un jeu gratuit depuis le Store.",
        btn("Ouvrir la bibliothèque", () => app.go("library"), { primary: true }));
      return recent.map((g) => gameTile(g, () => openGame(app, g)));
    }, { module: "games" });

    // Destinations
    const grid = el("div.grid", { "data-zone": "tiles" });
    TILES.forEach((t, i) => {
      const problem = api.moduleProblem(t.module);
      const tile = btn(null, () => app.go(t.tab), { cls: `tile${i === 0 ? " hero" : ""}`, attrs: { "data-tile": t.tab, ...(i === 0 ? { "data-autofocus": true } : {}) } });
      tile.append(el("span.tile-icon", { "aria-hidden": "true", text: t.icon }), el("span.tile-title", { text: t.title }),
        el("span.tile-sub", { text: problem ? "Module indisponible" : t.sub }));
      grid.append(tile);
    });

    const player = miniPlayer();
    root.append(recentSec, el("section.section", {}, el("h2", { text: "Explorer" }), grid),
      el("section.section", {}, el("h2", { text: "En écoute" }), player));
    return { dispose: () => player._dispose?.() };
  },
};
