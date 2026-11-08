import { el, btn, clear, asList, pick, nameOf } from "../dom.js";
import * as api from "../api.js";
import { loadInto, empty, run, errorState, loading } from "../ui.js";
import { gameTile, levelBadge } from "../view.js";
import { openGame } from "./library.js";
import { nav } from "../nav.js";

const sysId = (s) => (typeof s === "string" ? s : pick(s, "id", "system", "key", "name"));

function emulatorRow(e, system) {
  const id = typeof e === "string" ? e : pick(e, "id", "emulator", "app_id", "name");
  const installed = typeof e === "object" && pick(e, "installed") === true;
  const row = el("div.row-item", { "data-emulator": id },
    el("div.grow", {}, el("div.t", { text: typeof e === "string" ? e : nameOf(e) }),
      el("div.s", { text: [pick(e, "app_id", "flatpak"), pick(e, "license"), pick(e, "note", "notes")].filter(Boolean).join(" — ") })),
    levelBadge(pick(e, "level", "compatibility", "support")),
    installed
      ? btn("Désinstaller", () => run(() => api.call("emulation.uninstall", { emulator: id }), `Désinstallation de ${id}`), { danger: true })
      : btn("Installer", () => run(() => api.call("emulation.install", { emulator: id }), `Installation de ${id}`), { primary: true, attrs: { "data-act": "install-emulator" } }));
  return row;
}

function openSystem(app, s) {
  const id = sysId(s);
  app.push({
    id: "system",
    title: nameOf(s),
    render(root) {
      root.append(el("div.screen-head", {}, el("div", {}, el("h1", { text: typeof s === "string" ? s : nameOf(s) }),
        el("p.muted", { text: pick(s, "rom_dir", "roms_dir", "path") ? `Placez vos ROMs dans ${pick(s, "rom_dir", "roms_dir", "path")}` : "Vos propres sauvegardes de jeux uniquement. NEXUS ne télécharge ni ROM ni BIOS." }))));
      const rec = el("div.panel");
      const emus = el("div.list", { "data-zone": "emulators" });
      const roms = el("div.rail", { "data-zone": "roms" });
      root.append(el("section.section", {}, el("h2", { text: "Émulateur recommandé" }), rec),
        el("section.section", {}, el("h2", { text: "Émulateurs" }), emus),
        el("section.section", {}, el("h2", { text: "ROMs" }), roms));
      loadInto(rec, () => api.call("emulation.recommend", { system: id }), (r) => {
        const e = r?.emulator || r?.recommended || r;
        if (!e) return empty("Aucune recommandation pour ce système");
        const bios = pick(r, "bios", "bios_note");
        return [emulatorRow(e, id), bios ? el("p.small.muted", { style: { marginTop: ".6rem" }, text: typeof bios === "string" ? bios : "Un BIOS fourni par vous peut être requis." }) : null];
      }, { module: "emulation" });
      loadInto(emus, () => api.call("emulation.emulators"), (r) => {
        const list = asList(r, "emulators").filter((e) => {
          const sys = pick(e, "systems", "system");
          return !sys || (Array.isArray(sys) ? sys.includes(id) : sys === id);
        });
        return list.length ? list.map((e) => emulatorRow(e, id)) : empty("Aucun émulateur référencé pour ce système");
      }, { module: "emulation" });
      loadInto(roms, () => api.call("emulation.roms", { system: id }), (r) => {
        const list = asList(r, "roms").filter((x) => !pick(x, "system") || pick(x, "system") === id);
        return list.length ? list.map((x) => gameTile(x, () => openGame(app, { ...x, __rom: true, id: pick(x, "rom_id", "id") })))
          : empty("Aucune ROM trouvée", "Copiez vos fichiers puis lancez une analyse.", btn("Analyser les ROMs", () => run(() => api.call("emulation.scan"), "Analyse des ROMs")));
      }, { module: "emulation" });
    },
  });
}

export default {
  id: "emulation",
  title: "Émulation",
  render(root, app) {
    root.append(el("div.screen-head", {}, el("div", {}, el("h1", { text: "Émulation" }),
      el("p.muted", { text: "Choisissez un système. La compatibilité est indicative, jamais garantie." })),
      el("div.btn-row", {}, btn("Analyser les ROMs", () => run(() => api.call("emulation.scan"), "Analyse des ROMs"), { attrs: { "data-act": "scan" } }))));
    const grid = el("div.grid", { "data-zone": "systems" });
    root.append(grid);
    loadInto(grid, () => api.call("emulation.systems"), (r) => {
      const list = asList(r, "systems");
      if (!list.length) return empty("Aucun système référencé");
      return list.map((s) => {
        const t = btn(null, () => openSystem(app, s), { cls: "tile", attrs: { "data-system": sysId(s) } });
        const n = pick(s, "rom_count", "roms");
        t.append(el("span.tile-icon", { "aria-hidden": "true", text: "🕹️" }), el("span.tile-title", { text: typeof s === "string" ? s : nameOf(s) }),
          el("span.tile-sub", { text: [pick(s, "maker", "manufacturer"), typeof n === "number" ? `${n} ROM${n > 1 ? "s" : ""}` : null].filter(Boolean).join(" — ") }),
          levelBadge(pick(s, "level", "support")));
        return t;
      });
    }, { module: "emulation" });
  },
};
