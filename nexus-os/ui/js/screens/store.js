import { el, btn, clear, asList, pick, nameOf, kv } from "../dom.js";
import * as api from "../api.js";
import { loadInto, empty, run, errorState } from "../ui.js";
import { coverStyle } from "../view.js";
import { openKeyboard } from "../keyboard.js";
import { nav } from "../nav.js";

const appId = (a) => pick(a, "app_id", "id", "flatpak_id");

function verifiedBadge(a) {
  const v = pick(a, "verified");
  if (v === true) return el("span.badge.ok", { text: "Vérifié" });
  if (v === false) return el("span.badge.warn", { text: "Non vérifié" });
  return null;
}

function appTile(app, a) {
  const t = btn(null, () => openApp(app, a), { cls: "tile", attrs: { "data-app": appId(a) ?? "" } });
  const icon = coverStyle(a);
  t.append(icon.backgroundImage ? el("span.tile-icon", { style: { ...icon, width: "3.2rem", height: "3.2rem", backgroundSize: "cover", borderRadius: ".8rem" } })
    : el("span.tile-icon", { "aria-hidden": "true", text: pick(a, "free_game", "is_game") ? "🎮" : "📦" }),
  el("span.tile-title", { text: nameOf(a) }),
  el("span.tile-sub", { text: String(pick(a, "summary", "developer", "source") || "") }), verifiedBadge(a));
  return t;
}

export function openApp(app, a) {
  const id = appId(a);
  app.push({
    id: "app",
    title: nameOf(a),
    render(root) {
      const head = el("div");
      const facts = el("div.panel");
      const actions = el("div.btn-row", { "data-zone": "app-actions" });
      root.append(el("div.detail", {}, el("div.cover", { style: { ...coverStyle(a), aspectRatio: "1" } }, coverStyle(a).backgroundImage ? null : "📦"),
        el("div", {}, head, actions, facts)));
      const draw = (d) => {
        const verified = pick(d, "verified");
        clear(head).append(el("h1", { text: nameOf(d) }), el("p.muted", { style: { maxWidth: "60ch" }, text: String(pick(d, "summary", "description") || "") }));
        clear(facts).append(kv({
          source: pick(d, "source", "remote", "origin"), licence: pick(d, "license", "licence", "project_license"),
          "vérifié": typeof verified === "boolean" ? verified : undefined, "développeur": pick(d, "developer", "developer_name"),
          identifiant: id, version: pick(d, "version"), "taille": pick(d, "size", "download_size"),
        }));
        clear(actions);
        if (pick(d, "installed") === true) {
          actions.append(btn("Désinstaller", () => run(() => api.call("store.uninstall", { app_id: id }), `Désinstallation de ${nameOf(d)}`), { danger: true, attrs: { "data-autofocus": true } }));
        } else if (verified === false) {
          const remote = pick(d, "remote", "source");
          actions.append(btn("Installer (source non vérifiée)", () => run(() => api.call("store.install_unverified", { app_id: id, remote }), `Installation de ${nameOf(d)}`),
            { danger: true, attrs: { "data-autofocus": true, "data-act": "install" } }),
          el("p.small.muted", { style: { width: "100%" }, text: "Cette application ne vient pas d'une source vérifiée : une confirmation vous sera demandée." }));
        } else {
          actions.append(btn("Installer", () => run(() => api.call("store.install", { app_id: id }), `Installation de ${nameOf(d)}`), { primary: true, attrs: { "data-autofocus": true, "data-act": "install" } }));
        }
        nav.ensure();
      };
      draw(a);
      if (api.hasAction("store.details")) api.call("store.details", { app_id: id }).then((d) => draw({ ...a, ...(d?.app || d) })).catch((e) => facts.append(errorState(e)));
    },
  });
}

export default {
  id: "store",
  title: "Store",
  render(root, app, opts = {}) {
    const results = el("section.section");
    const search = async (initial) => {
      const q = initial ?? await openKeyboard({ title: "Rechercher dans le Store", suggest: null });
      if (!q) return;
      clear(results).append(el("h2", { text: `Résultats pour « ${q} »` }));
      const grid = el("div.grid", { "data-zone": "results" });
      results.append(grid);
      loadInto(grid, () => api.call("store.search", { query: q }), (r) => {
        const list = asList(r, "results", "apps");
        return list.length ? list.map((a) => appTile(app, a)) : empty(`Aucun résultat pour « ${q} »`, "Vérifiez l'orthographe ou essayez un autre mot.");
      }, { module: "store" });
    };
    root.append(el("div.screen-head", {}, el("div", {}, el("h1", { text: "Store" }),
      el("p.muted", { text: "Logiciels libres vérifiés sur Flathub et jeux gratuits légaux." })),
      el("div.btn-row", { "data-zone": "store-actions" },
        btn("Rechercher", () => search(), { primary: true, attrs: { "data-act": "search", "data-autofocus": true } }),
        btn("Mises à jour", () => openUpdates(app)),
        btn("Installées", () => openInstalled(app)))), results);

    for (const [title, action, zone] of [["À la une", "store.featured", "featured"], ["Jeux gratuits", "store.free_games", "free"]]) {
      const rail = el("div.grid", { "data-zone": zone });
      root.append(el("section.section", {}, el("h2", { text: title }), rail));
      loadInto(rail, () => api.call(action), (r) => {
        const list = asList(r, "apps", "games", "featured");
        return list.length ? list.map((a) => appTile(app, a)) : empty("Rien à afficher pour le moment");
      }, { module: "store" });
    }
    if (opts.query) search(opts.query);
    return { onIntent: (i) => { if (i === "search") { search(); return true; } return false; } };
  },
};

function openInstalled(app) {
  app.push({ id: "installed", title: "Installées", render(root) {
    root.append(el("h1", { style: { marginBottom: "1.2rem" }, text: "Applications installées" }));
    const list = el("div.grid", { "data-zone": "installed" });
    root.append(list);
    loadInto(list, () => api.call("store.installed"), (r) => {
      const l = asList(r, "apps", "installed");
      return l.length ? l.map((a) => appTile(app, { ...a, installed: true })) : empty("Aucune application installée via le Store");
    }, { module: "store" });
  } });
}

function openUpdates(app) {
  app.push({ id: "updates", title: "Mises à jour", render(root) {
    root.append(el("div.screen-head", {}, el("h1", { text: "Mises à jour" }),
      el("div.btn-row", {}, btn("Tout mettre à jour", () => run(() => api.call("store.update_all"), "Mise à jour des applications"), { primary: true }))));
    const list = el("div.list", { "data-zone": "updates" });
    root.append(list);
    loadInto(list, () => api.call("store.updates"), (r) => {
      const l = asList(r, "updates", "apps");
      return l.length ? l.map((a) => el("div.row-item", {}, el("div.grow", {}, el("div.t", { text: nameOf(a) }), el("div.s", { text: String(appId(a) || "") }))))
        : empty("Tout est à jour");
    }, { module: "store" });
  } });
}
