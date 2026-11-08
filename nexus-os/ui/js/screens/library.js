import { el, btn, clear, asList, pick, nameOf } from "../dom.js";
import * as api from "../api.js";
import { loadInto, empty, run, toastError, loading, errorState, reportResult } from "../ui.js";
import { gameTile, coverStyle, resultView, levelBadge } from "../view.js";
import { openKeyboard } from "../keyboard.js";
import { nav } from "../nav.js";

/** Game detail view: Lancer / Installer / Diagnostiquer / Optimiser / Compatibilité / Désinstaller. */
export function openGame(app, game) {
  app.push({
    id: "game",
    title: nameOf(game),
    render(root) {
      const id = pick(game, "id");
      const isRom = game.__rom === true;
      const cover = el("div.cover", { style: coverStyle(game), "aria-hidden": "true" }, coverStyle(game).backgroundImage ? null : (isRom ? "🕹️" : "🎮"));
      const info = el("div");
      const out = el("div.output", { "aria-live": "polite" });
      const actions = el("div.btn-row", { "data-zone": "game-actions" });
      const showOut = (p) => p.then((r) => { if (r?.task_id) reportResult(r, nameOf(game)); clear(out).append(resultView(r)); nav.ensure(); })
        .catch((e) => { clear(out).append(errorState(e)); });
      const installed = pick(game, "installed") !== false;

      if (isRom) {
        actions.append(btn("Lancer", () => run(() => api.call("emulation.launch", { rom_id: id }), `Lancement de ${nameOf(game)}`), { primary: true, attrs: { "data-autofocus": true, "data-act": "launch" } }));
      } else {
        if (installed) actions.append(btn("Lancer", () => run(() => api.call("games.launch", { id }), `Lancement de ${nameOf(game)}`), { primary: true, attrs: { "data-autofocus": true, "data-act": "launch" } }));
        else actions.append(btn("Installer", () => run(() => api.call("games.install", { id }), `Installation de ${nameOf(game)}`), { primary: true, attrs: { "data-autofocus": true, "data-act": "install" } }));
        actions.append(
          btn("Diagnostiquer", () => { clear(out).append(loading("Diagnostic en cours")); showOut(api.call("games.diagnose", { id })); }, { attrs: { "data-act": "diagnose" } }),
          btn("Optimiser", () => { clear(out).append(loading("Optimisation")); showOut(api.call("games.optimize", { id })); }, { attrs: { "data-act": "optimize" } }),
          btn("Compatibilité", () => { clear(out).append(loading("Analyse")); showOut(api.call("games.compatibility", { id })); }, { attrs: { "data-act": "compat" } }),
        );
        if (installed) actions.append(btn("Désinstaller", () => run(() => api.call("games.uninstall", { id }), `Désinstallation de ${nameOf(game)}`), { danger: true, attrs: { "data-act": "uninstall" } }));
      }

      info.append(el("h1", { text: nameOf(game) }),
        el("p.muted", { text: [pick(game, "source", "launcher"), pick(game, "system"), installed ? "installé" : "non installé"].filter(Boolean).join(" — ") }),
        actions, out);
      root.append(el("div.detail", {}, cover, info));

      if (!isRom && api.hasAction("games.details")) {
        const more = el("div", { style: { marginTop: "1rem" } });
        info.insertBefore(more, actions);
        api.call("games.details", { id }).then((d) => {
          const desc = pick(d, "description", "summary");
          const compat = pick(d, "compatibility", "protondb", "deck");
          clear(more).append(desc ? el("p", { style: { maxWidth: "60ch" }, text: String(desc).slice(0, 600) }) : null,
            compat ? el("p", { style: { marginTop: ".6rem" } }, "Compatibilité : ", levelBadge(compat)) : null);
        }).catch(() => {});
      }
    },
  });
}

export default {
  id: "library",
  title: "Bibliothèque",
  render(root, app, opts = {}) {
    let query = opts.query || "";
    let filter = "all";
    let items = [];
    const chips = el("div.chips", { "data-zone": "filters", role: "toolbar", "aria-label": "Filtrer par source" });
    const grid = el("div.grid", { "data-zone": "games", style: { gridTemplateColumns: "repeat(auto-fill, minmax(12rem, 1fr))" } });
    const queryLine = el("p.muted");
    const status = el("div");

    const search = async () => {
      const q = await openKeyboard({ title: "Rechercher dans la bibliothèque", value: query });
      if (q === null) return;
      query = q;
      draw();
    };

    root.append(el("div.screen-head", {}, el("div", {}, el("h1", { text: "Bibliothèque" }), queryLine),
      el("div.btn-row", {},
        btn("Rechercher", search, { attrs: { "data-act": "search" } }),
        btn("Actualiser", () => run(async () => { const r = await api.call("games.refresh"); load(); return r; }, "Bibliothèque actualisée")))),
      chips, status, grid);

    const draw = () => {
      queryLine.textContent = query ? `Recherche : « ${query} »` : "Tous vos jeux, toutes sources confondues";
      const sources = [...new Set(items.map((g) => g.__rom ? "ROMs" : String(pick(g, "source", "launcher") || "autre")))];
      clear(chips);
      for (const s of ["all", ...sources]) {
        chips.append(btn(s === "all" ? "Tout" : s, () => { filter = s; draw(); }, { cls: "chip", attrs: { "aria-pressed": String(filter === s), "data-filter": s } }));
      }
      clear(grid);
      const q = query.toLowerCase();
      const shown = items.filter((g) => (filter === "all" || (g.__rom ? "ROMs" : String(pick(g, "source", "launcher") || "autre")) === filter)
        && (!q || nameOf(g).toLowerCase().includes(q)));
      if (!shown.length) {
        grid.append(empty(query ? `Aucun résultat pour « ${query} »` : "Aucun jeu", query ? "Essayez le Store pour trouver de nouveaux jeux." : "",
          query ? btn("Chercher dans le Store", () => app.go("store", { query }), { primary: true }) : null));
      }
      for (const g of shown) {
        const t = gameTile(g, () => openGame(app, g));
        t._options = [{ label: "Voir la fiche", fn: () => openGame(app, g) }];
        grid.append(t);
      }
      nav.ensure();
    };

    const load = async () => {
      clear(status).append(loading());
      const parts = await Promise.allSettled([
        api.moduleProblem("games") ? Promise.reject(new api.ApiError("unavailable", api.moduleProblem("games"))) : api.call("games.library"),
        api.moduleProblem("emulation") ? Promise.reject(new api.ApiError("unavailable", api.moduleProblem("emulation"))) : api.call("emulation.roms"),
      ]);
      items = [];
      clear(status);
      const [g, r] = parts;
      if (g.status === "fulfilled") items.push(...asList(g.value, "games"));
      else status.append(errorState(g.reason));
      if (r.status === "fulfilled") items.push(...asList(r.value, "roms").map((x) => ({ ...x, __rom: true, id: pick(x, "rom_id", "id") })));
      else if (g.status === "fulfilled") status.append(el("p.small.muted", { style: { marginBottom: "1rem" }, text: `ROMs non listées : ${r.reason?.message || r.reason}` }));
      draw();
    };
    load();
    return {
      onIntent: (i) => { if (i === "search") { search(); return true; } return false; },
      refresh: load,
    };
  },
};

export { loadInto };
