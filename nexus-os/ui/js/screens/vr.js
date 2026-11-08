import { el, btn, asList, pick, nameOf, kv } from "../dom.js";
import * as api from "../api.js";
import { loadInto, empty, run } from "../ui.js";
import { levelBadge } from "../view.js";

export default {
  id: "vr",
  title: "VR",
  render(root, app) {
    const status = el("div.panel");
    const apps = el("div.grid", { "data-zone": "vr-apps" });
    const perfSpec = api.actionSpec("vr.set_performance");
    const levels = perfSpec?.params?.level?.choices || [];
    root.append(el("div.screen-head", {}, el("div", {}, el("h1", { text: "Réalité virtuelle" }),
      el("p.muted", { text: "La compatibilité des casques sous Linux varie : vérifiez l'état ci-dessous avant de jouer." })),
      el("div.btn-row", { "data-zone": "vr-actions" },
        btn("Démarrer le runtime", () => run(async () => { const r = await api.call("vr.start_runtime"); load(); return r; }, "Runtime VR démarré"), { primary: true }),
        btn("Arrêter le runtime", () => run(async () => { const r = await api.call("vr.stop_runtime"); load(); return r; }, "Runtime VR arrêté")))),
      el("section.section", {}, el("h2", { text: "État" }), status));
    if (levels.length) {
      root.append(el("section.section", {}, el("h2", { text: "Performances VR" }),
        el("div.chips", { "data-zone": "vr-perf" }, ...levels.map((l) => btn(String(l), () => run(() => api.call("vr.set_performance", { level: l }), `Niveau ${l}`), { cls: "chip" })))));
    }
    root.append(el("section.section", {}, el("h2", { text: "Applications VR" }), apps));
    const load = () => loadInto(status, () => api.call("vr.status"), (r) => {
      const hs = asList(r, "headsets", "devices");
      return [kv(Object.fromEntries(Object.entries(r || {}).filter(([, v]) => typeof v !== "object"))),
        ...hs.map((h) => el("div.row-item", { style: { marginTop: ".6rem" } }, el("div.grow", {}, el("div.t", { text: nameOf(h) }), el("div.s", { text: String(pick(h, "note", "notes", "runtime") || "") })), levelBadge(pick(h, "support", "level", "compatibility"))))];
    }, { module: "vr" });
    load();
    loadInto(apps, () => api.call("vr.apps"), (r) => {
      const l = asList(r, "apps");
      return l.length ? l.map((a) => {
        // Launching goes through the game library (same launcher as flat games).
        const t = btn(null, () => app.go("library", { query: nameOf(a) }), { cls: "tile", attrs: { "data-vr-app": pick(a, "id") ?? "" } });
        t.append(el("span.tile-title", { text: nameOf(a) }), el("span.tile-sub", { text: String(pick(a, "source", "note") || "") }), levelBadge(pick(a, "compatibility", "level")));
        return t;
      }) : empty("Aucune application VR détectée", "Installez des jeux VR depuis Steam ou le Store.");
    }, { module: "vr" });
    return { refresh: load };
  },
};
