import { el, btn, clear, asList, pick, nameOf } from "../dom.js";
import * as api from "../api.js";
import { loadInto, empty, run } from "../ui.js";
import { openKeyboard } from "../keyboard.js";
import { miniPlayer } from "./home.js";

const after = (p) => p.then((r) => { api.call("music.status").catch(() => {}); return r; });

function trackRow(t) {
  const id = pick(t, "track_id", "id");
  const b = btn(null, () => run(() => after(api.call("music.play", { track_id: id })), `Lecture : ${nameOf(t)}`), { cls: "row-item", attrs: { "data-track": id } });
  b.append(el("div.grow", {}, el("div.t", { text: nameOf(t) }), el("div.s", { text: [pick(t, "artist"), pick(t, "album")].filter(Boolean).join(" — ") })));
  return b;
}

export default {
  id: "music",
  title: "Musique",
  render(root, app) {
    const player = miniPlayer();
    const controls = el("div.btn-row", { "data-zone": "music-controls" },
      btn("Arrêter", () => run(() => after(api.call("music.stop")), "Lecture arrêtée")),
      btn("Volume −", () => run(() => api.call("music.volume_step", { delta: -10 }), "Volume baissé")),
      btn("Volume +", () => run(() => api.call("music.volume_step", { delta: 10 }), "Volume monté")),
      btn("Jouer…", async () => { const q = await openKeyboard({ title: "Que voulez-vous écouter ?" }); if (q) run(() => after(api.call("music.play", { query: q })), `Lecture : ${q}`); }, { attrs: { "data-act": "play-query" } }),
      btn("Analyser la bibliothèque", () => run(() => api.call("music.scan"), "Analyse de la musique")));
    root.append(el("div.screen-head", {}, el("div", {}, el("h1", { text: "Musique" }), el("p.muted", { text: "Votre musique locale, vos playlists et les radios web." }))),
      el("section.section", {}, player, controls));

    const tracks = el("div.list", { "data-zone": "tracks" });
    const pls = el("div.list", { "data-zone": "playlists" });
    const radio = el("div.list", { "data-zone": "radio" });
    root.append(el("div.cols", {},
      el("section.section", {}, el("h2", { text: "Bibliothèque" }), tracks),
      el("section.section", {}, el("div.section-head", {}, el("h2", { text: "Playlists" }),
        btn("Nouvelle playlist", async () => { const n = await openKeyboard({ title: "Nom de la playlist" }); if (n) { await run(() => api.call("music.playlist_create", { name: n }), `Playlist « ${n} » créée`); loadPls(); } })), pls),
      el("section.section", {}, el("div.section-head", {}, el("h2", { text: "Radio" }),
        btn("Chercher une radio", async () => { const q = await openKeyboard({ title: "Rechercher une station" }); if (q) loadRadio(q); }, { attrs: { "data-act": "radio-search" } })), radio)));

    loadInto(tracks, () => api.call("music.library"), (r) => {
      const l = asList(r, "tracks");
      return l.length ? l.slice(0, 200).map(trackRow) : empty("Aucun morceau", "Ajoutez de la musique dans votre dossier Musique puis analysez la bibliothèque.");
    }, { module: "music" });
    const loadPls = () => loadInto(pls, () => api.call("music.playlists"), (r) => {
      const l = asList(r, "playlists");
      return l.length ? l.map((p) => {
        const id = pick(p, "playlist_id", "id");
        const n = asList(p, "tracks").length || pick(p, "count", "track_count");
        const b = btn(null, () => run(() => after(api.call("music.play", { playlist_id: id })), `Lecture de ${nameOf(p)}`), { cls: "row-item", attrs: { "data-playlist": id } });
        b.append(el("div.grow", {}, el("div.t", { text: nameOf(p) }), el("div.s", { text: n ? `${n} titres` : "" })));
        return b;
      }) : empty("Aucune playlist");
    }, { module: "music" });
    loadPls();
    const loadRadio = (q) => loadInto(radio, () => api.call("music.radio_search", { query: q }), (r) => {
      const l = asList(r, "stations", "results");
      return l.length ? l.slice(0, 40).map((s) => {
        const id = pick(s, "station_id", "id", "stationuuid");
        const b = btn(null, () => run(() => after(api.call("music.radio_play", { station_id: id })), `Radio : ${nameOf(s)}`), { cls: "row-item", attrs: { "data-station": id } });
        b.append(el("div.grow", {}, el("div.t", { text: nameOf(s) }), el("div.s", { text: [pick(s, "country"), pick(s, "tags", "genre")].filter(Boolean).join(" — ") })));
        return b;
      }) : empty(`Aucune station pour « ${q} »`);
    }, { module: "music" });
    clear(radio).append(empty("Recherchez une station", "Par nom, genre ou pays (ex. jazz, France Inter)."));
    return { dispose: () => player._dispose?.() };
  },
};
