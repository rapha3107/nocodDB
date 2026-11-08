import { el, btn, clear, asList, pick, nameOf, fmtBytes } from "../dom.js";
import * as api from "../api.js";
import { loadInto, empty, run } from "../ui.js";
import { textField, openKeyboard } from "../keyboard.js";
import { levelBadge } from "../view.js";

const suggest = async (text) => {
  if (!api.hasAction("browser.suggest")) return [];
  const r = await api.call("browser.suggest", { text });
  return asList(r, "suggestions").map((s) => ({ label: pick(s, "title") ? `${s.title} — ${s.url}` : s.url, value: s.url }));
};

const open = (url) => run(() => api.call("browser.open", { url }), `Ouverture de ${url}`);

export default {
  id: "browser",
  title: "Navigateur",
  render(root) {
    const address = textField({ label: "Adresse ou recherche", placeholder: "Saisir une adresse (ex. youtube.com) ou une recherche", suggest,
      onChange: (v) => { if (v.trim()) open(v.trim()); }, attrs: { "data-autofocus": true, "data-address": true } });
    root.append(el("div.screen-head", {}, el("div", {}, el("h1", { text: "Navigateur" }),
      el("p.muted", { text: "Le navigateur s'ouvre en plein écran ; ○ ou PS pour revenir à NEXUS." })),
      el("div.btn-row", {}, btn("Fermer le navigateur", () => run(() => api.call("browser.close"), "Navigateur fermé")))),
      el("div.list", { "data-zone": "address", style: { marginBottom: "2rem" } }, address));

    const services = el("div.grid", { "data-zone": "services" });
    const note = el("p.small.muted", { style: { marginTop: ".8rem" } });
    const marks = el("div.list", { "data-zone": "bookmarks" });
    const downloads = el("div.list", { "data-zone": "downloads" });
    root.append(el("section.section", {}, el("h2", { text: "Services" }), services, note),
      el("div.cols", {},
        el("section.section", {}, el("div.section-head", {}, el("h2", { text: "Favoris" }),
          btn("Ajouter un favori", async () => {
            const url = await openKeyboard({ title: "Adresse du favori" });
            if (!url) return;
            const title = await openKeyboard({ title: "Titre du favori", value: url.replace(/^https?:\/\//, "") });
            if (!title) return;
            await run(() => api.call("browser.bookmark_add", { title, url }), "Favori ajouté");
            loadMarks();
          })), marks),
        el("section.section", {}, el("h2", { text: "Téléchargements" }), downloads)));

    loadInto(services, () => api.call("browser.services"), (r) => {
      if (r?.controller_note) note.textContent = r.controller_note;
      const l = asList(r, "services");
      return l.length ? l.map((s) => {
        const t = btn(null, () => open(s.url), { cls: "tile", attrs: { "data-service": pick(s, "id") } });
        t.append(el("span.tile-title", { text: nameOf(s) }), el("span.tile-sub", { text: String(pick(s, "note") || s.url) }), levelBadge(pick(s, "compatibility")));
        return t;
      }) : empty("Aucun service");
    }, { module: "browser" });
    const loadMarks = () => loadInto(marks, () => api.call("browser.bookmarks"), (r) => {
      const l = asList(r, "bookmarks");
      return l.length ? l.map((b) => {
        const row = btn(null, () => open(b.url), { cls: "row-item", attrs: { "data-bookmark": b.id } });
        row.append(el("div.grow", {}, el("div.t", { text: nameOf(b) }), el("div.s", { text: b.url })));
        row._options = [{ label: "Supprimer ce favori", fn: () => run(async () => { const x = await api.call("browser.bookmark_remove", { id: b.id }); loadMarks(); return x; }, "Favori supprimé") }];
        return row;
      }) : empty("Aucun favori");
    }, { module: "browser" });
    loadMarks();
    loadInto(downloads, () => api.call("browser.downloads"), (r) => {
      const l = asList(r, "files");
      if (!l.length) return empty("Aucun téléchargement", r?.dir ? `Dossier : ${r.dir}` : "");
      return l.slice(0, 30).map((f) => el("div.row-item", {}, el("div.grow", {}, el("div.t", { text: nameOf(f) }), el("div.s", { text: fmtBytes(pick(f, "size", "bytes")) }))));
    }, { module: "browser" });
    return {};
  },
};
