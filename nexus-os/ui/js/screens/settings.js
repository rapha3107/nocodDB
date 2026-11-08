import { el, btn, clear, asList, pick, nameOf, kv, fmtBytes } from "../dom.js";
import * as api from "../api.js";
import { loadInto, empty, run, toast, toastError, unavailable, errorState, loading, openSheet, reportResult } from "../ui.js";
import { openKeyboard, textField } from "../keyboard.js";
import { resultView } from "../view.js";
import { nav } from "../nav.js";

const AVATARS = ["🎮", "🚀", "🐉", "⚡", "🌙", "🦊", "👾", "🎧", "🐱", "🌊", "🔥", "🍀"];
const COLORS = ["#4cc9f0", "#ff6b6b", "#ffb703", "#8e7dff", "#2ec4b6", "#f72585", "#90be6d", "#f9844a"];

/** A row with an on/off switch. get() -> bool, set(bool) -> Promise. */
function toggleRow(label, help, value, set, attrs = {}) {
  let on = !!value;
  const sw = el("span.toggle", { "aria-hidden": "true" });
  const b = btn(null, async () => {
    try { await set(!on); on = !on; sw.classList.toggle("on", on); b.setAttribute("aria-checked", String(on)); }
    catch (e) { toastError(e); }
  }, { cls: "row-item", attrs: { role: "switch", "aria-checked": String(on), ...attrs } });
  sw.classList.toggle("on", on);
  b.append(el("div.grow", {}, el("div.t", { text: label }), help ? el("div.s", { text: help }) : null), sw);
  return b;
}

async function setSetting(app, key, value) {
  const r = await api.call("profiles.settings_set", { key, value });
  app.state.settings[key] = r.value;
  app.applySettings(app.state.settings);
  return r;
}

function choiceRow(app, title, key, choices) {
  const box = el("div.chips", { "data-zone": `set-${key}` });
  const draw = () => {
    clear(box);
    for (const [v, label] of choices) {
      box.append(btn(label, async () => { try { await setSetting(app, key, v); draw(); nav.ensure(); toast(`${title} : ${label}`); } catch (e) { toastError(e); } },
        { cls: "chip", attrs: { "aria-pressed": String(app.state.settings[key] === v), "data-value": v } }));
    }
  };
  draw();
  return el("div.section", {}, el("h3", { style: { marginBottom: ".6rem" }, text: title }), box);
}

// ---- pages ----------------------------------------------------------------------
const PAGES = [
  { id: "profiles", icon: "👤", title: "Profils", sub: "Joueurs, avatars, couleurs", render: profilesPage },
  { id: "display", icon: "🖥️", title: "Langue et affichage", sub: "Langue, thème, clavier", render: displayPage },
  { id: "security", icon: "🛡️", title: "Sécurité", sub: "Installations automatiques", render: securityPage },
  { id: "ai", icon: "✨", title: "NEXUS AI", sub: "Local, API distantes, coûts", render: aiPage },
  { id: "hardware", icon: "🎮", title: "Manettes et matériel", sub: "DualSense, GPU, overlay", render: hardwarePage },
  { id: "system", icon: "🔧", title: "Système", sub: "Mises à jour, retour arrière, diagnostic", render: systemPage },
  { id: "power", icon: "⏻", title: "Alimentation et bureau", sub: "Veille, redémarrage, mode bureau", render: powerPage },
];

function profilesPage(root, app) {
  const list = el("div.list", { "data-zone": "profile-list" });
  root.append(el("div.screen-head", {}, el("h1", { text: "Profils" }),
    el("div.btn-row", {}, btn("Créer un profil", () => createProfile(app, load), { primary: true, attrs: { "data-act": "create-profile", "data-autofocus": true } }))), list);
  const load = () => loadInto(list, () => api.call("profiles.list"), (r) => asList(r, "profiles").map((p) => {
    const row = el("div.row-item", { "data-profile-row": p.id },
      el("span.avatar", { style: { background: p.color }, text: p.avatar }),
      el("div.grow", {}, el("div.t", { text: p.name }), el("div.s", { text: p.active ? "Profil actif" : "" })),
      p.active ? null : btn("Utiliser", async () => { try { app.applyProfile(await api.call("profiles.switch", { id: p.id })); load(); } catch (e) { toastError(e); } }),
      btn("Renommer", async () => { const n = await openKeyboard({ title: "Nouveau nom", value: p.name, maxLength: 24 }); if (n) { await run(() => api.call("profiles.update", { id: p.id, name: n }), "Profil renommé"); load(); } }),
      btn("Supprimer", () => run(async () => { const x = await api.call("profiles.delete", { id: p.id }); load(); api.call("profiles.current").then(app.applyProfile); return x; }, "Profil supprimé"), { danger: true, attrs: { "data-act": "delete-profile" } }));
    return row;
  }), { module: "profiles" });
  load();
  return { refresh: load };
}

async function createProfile(app, done) {
  const name = await openKeyboard({ title: "Nom du nouveau profil", maxLength: 24 });
  if (!name) return;
  let avatar = AVATARS[0], color = COLORS[0];
  openSheet((s, close) => {
    const av = el("div.chips", { "data-zone": "avatars" });
    const co = el("div.chips", { "data-zone": "colors" });
    const draw = () => {
      clear(av); clear(co);
      for (const a of AVATARS) av.append(btn(a, () => { avatar = a; draw(); }, { cls: "chip", attrs: { "aria-pressed": String(a === avatar), "aria-label": `Avatar ${a}` } }));
      for (const c of COLORS) co.append(btn("", () => { color = c; draw(); }, { cls: "chip", attrs: { "aria-pressed": String(c === color), "aria-label": `Couleur ${c}`, style: `background:${c};width:3rem;height:2.4rem` } }));
      nav.ensure();
    };
    draw();
    s.append(el("h2", { text: `Profil « ${name} »` }), el("h3", { text: "Avatar" }), av, el("h3", { text: "Couleur" }), co,
      el("div.btn-row", {}, btn("Annuler", () => close()),
        btn("Créer le profil", async () => {
          try { await api.call("profiles.create", { name, avatar, color }); toast("Profil créé", name); close(); done?.(); }
          catch (e) { toastError(e, "Création du profil"); }
        }, { primary: true, attrs: { "data-act": "confirm-create" } })));
  }, { label: "Nouveau profil" });
}

function displayPage(root, app) {
  root.append(el("h1", { style: { marginBottom: "1.4rem" }, text: "Langue et affichage" }),
    choiceRow(app, "Langue", "language", [["fr", "Français"], ["en", "English"]]),
    el("p.small.muted", { style: { margin: "-1rem 0 1.6rem" }, text: "L'interface est actuellement disponible en français ; la langue choisie est transmise à NEXUS AI." }),
    choiceRow(app, "Thème", "ui.theme", [["dark", "Sombre"], ["oled", "Noir OLED"]]),
    choiceRow(app, "Clavier à l'écran", "ui.keyboard_layout", [["azerty", "AZERTY"], ["qwerty", "QWERTY"]]));
}

function securityPage(root, app) {
  const s = app.state.settings;
  root.append(el("h1", { style: { marginBottom: ".6rem" }, text: "Sécurité" }),
    el("p.muted", { style: { maxWidth: "60ch", marginBottom: "1.4rem" }, text: "Ces réglages décident de ce que NEXUS AI peut installer sans vous demander. Les suppressions, achats, mises à jour système et sources non vérifiées demandent toujours votre confirmation. NEXUS AI ne peut pas modifier ces réglages sans votre accord explicite." }),
    el("div.list", { "data-zone": "security" },
      toggleRow("Installer automatiquement les logiciels libres vérifiés", "Applications Flathub vérifiées demandées à NEXUS AI", s["security.auto_install_verified"],
        (v) => setSetting(app, "security.auto_install_verified", v), { "data-setting": "security.auto_install_verified" }),
      toggleRow("Installer automatiquement les jeux gratuits légaux", "Jeux gratuits depuis des sources autorisées", s["security.auto_install_free_games"],
        (v) => setSetting(app, "security.auto_install_free_games", v), { "data-setting": "security.auto_install_free_games" })));
}

// ---- NEXUS AI (hybrid: local first, remote APIs opt-in) ----------------------------
function aiPage(root, app) {
  const s = app.state.settings;
  root.append(el("h1", { style: { marginBottom: ".6rem" }, text: "NEXUS AI" }),
    el("p.muted", { style: { maxWidth: "62ch", marginBottom: "1.4rem" }, text: "NEXUS AI fonctionne d'abord en local. Les API distantes sont facultatives : elles ne sont utilisées que si vous les autorisez et configurez une clé." }));
  if (!api.hasAction("ai.configure") && !api.hasAction("ai.providers")) {
    root.append(unavailable(api.moduleProblem("ai") || "configuration de l'IA absente"));
    return;
  }
  const general = el("div.list", { "data-zone": "ai-general" });
  general.append(
    toggleRow("Autoriser les API distantes", "Désactivé par défaut. Quand c'est désactivé, aucune donnée ne quitte cette machine.", s["ai.remote_enabled"] === true,
      async (v) => { const r = await api.call("ai.configure", { remote_enabled: v }); s["ai.remote_enabled"] = v; toast(v ? "API distantes autorisées" : "API distantes désactivées"); loadProviders(); return r; },
      { "data-setting": "ai.remote_enabled", "data-autofocus": true }),
    toggleRow("Partager la liste de mes jeux avec les API distantes", "Permet des recommandations plus précises. Jamais vos fichiers.", s["ai.share_library_with_remote"] === true,
      (v) => setSetting(app, "ai.share_library_with_remote", v), { "data-setting": "ai.share_library_with_remote" }),
    toggleRow("Commande vocale", "Micro activé par maintien de R3 ou du bouton PS", s["ai.voice_enabled"] !== false,
      (v) => setSetting(app, "ai.voice_enabled", v), { "data-setting": "ai.voice_enabled" }));
  const providers = el("div.list", { "data-zone": "ai-providers" });
  const usage = el("div.panel", { "data-zone": "ai-usage" });
  const budgetField = textField({ label: "Budget mensuel (USD)", value: s["ai.monthly_budget"] != null ? String(s["ai.monthly_budget"]) : "", placeholder: "Aucun budget fixé",
    onChange: async (v) => {
      const n = Number(String(v).replace(",", "."));
      if (!isFinite(n) || n < 0) { toast("Budget invalide", "Saisissez un nombre, par exemple 5", "error"); return; }
      try { await setSetting(app, "ai.monthly_budget", n); toast("Budget enregistré", `${n} $ par mois`); loadUsage(); } catch (e) { toastError(e); }
    }, attrs: { "data-setting": "ai.monthly_budget" } });
  root.append(el("section.section", {}, general),
    el("section.section", {}, el("h2", { text: "Fournisseurs" }), providers,
      el("p.small.muted", { style: { marginTop: ".8rem", maxWidth: "70ch" }, text: "Un abonnement Claude Pro (ou ChatGPT Plus) ne donne pas accès à l'API : une clé API se crée séparément dans la console du fournisseur et est facturée à l'usage." })),
    el("section.section", {}, el("h2", { text: "Coûts" }), usage, el("div.list", { style: { marginTop: "1rem" }, "data-zone": "ai-budget" }, budgetField)));

  const loadProviders = () => loadInto(providers, () => api.call("ai.providers"), (r) => {
    const l = asList(r, "providers");
    if (!l.length) return empty("Aucun fournisseur");
    return l.map((p) => providerRow(p, loadProviders));
  }, { module: "ai" });
  const loadUsage = () => loadInto(usage, () => api.call("ai.usage"), (r) => usageView(r, s), { module: "ai" });
  loadProviders();
  loadUsage();
  return { refresh: () => { loadProviders(); loadUsage(); } };
}

function providerRow(p, reload) {
  const id = pick(p, "provider", "id", "name");
  const local = ["local", "ollama"].includes(id);
  const enabled = pick(p, "enabled") === true;
  const set = (params, ok) => run(async () => { const r = await api.call("ai.provider_set", { provider: id, ...params }); reload(); return r; }, ok);
  const facts = [pick(p, "model") ? `Modèle : ${p.model}` : null, pick(p, "endpoint") ? `Adresse : ${p.endpoint}` : null,
    pick(p, "state", "status") ? String(pick(p, "state", "status")) : null].filter(Boolean).join(" — ");
  const box = el("div.panel", { "data-provider": id });
  const keyLine = el("div.btn-row");
  if (!local) {
    if (p.key_configured) {
      keyLine.append(el("span.badge.ok", { "data-key-state": "saved", text: "Clé enregistrée ✓" }),
        btn("Remplacer la clé", () => askKey(), { attrs: { "data-act": "key-replace" } }),
        btn("Supprimer la clé et la configuration", () => run(async () => { const r = await api.call("ai.provider_remove", { provider: id }); reload(); return r; }, "Configuration supprimée"), { danger: true }));
    } else {
      keyLine.append(el("span.badge.warn", { "data-key-state": "missing", text: "Aucune clé" }),
        btn("Saisir la clé API", () => askKey(), { primary: true, attrs: { "data-act": "key-set" } }));
    }
  }
  async function askKey() {
    const k = await openKeyboard({ title: `Clé API ${nameOf(p)}`, masked: true });
    if (k && k.trim()) set({ api_key: k.trim() }, "Clé enregistrée");
  }
  box.append(
    el("div.section-head", {}, el("h3", { text: String(pick(p, "title", "label") || id) }),
      el(`span.badge.${local ? "ok" : "warn"}`, { text: local ? "Local" : "Distant" })),
    facts ? el("p.small.muted", { text: facts }) : null,
    el("div.btn-row", { "data-zone": `prov-${id}` },
      btn(enabled ? "Désactiver" : "Activer", () => set({ enabled: !enabled }, enabled ? "Fournisseur désactivé" : "Fournisseur activé")),
      btn("Modèle", async () => { const m = await openKeyboard({ title: `Modèle ${id}`, value: pick(p, "model") || "" }); if (m) set({ model: m }, "Modèle enregistré"); }),
      id !== "local" ? btn("Adresse", async () => { const e = await openKeyboard({ title: `Adresse du serveur ${id}`, value: pick(p, "endpoint") || "" }); if (e) set({ endpoint: e }, "Adresse enregistrée"); }) : null,
      btn("Tester", async () => {
        try {
          const r = await api.call("ai.provider_test", { provider: id });
          const ok = r?.ok !== false;
          toast(ok ? "Test réussi" : "Test échoué", String(pick(r, "message", "error") ?? (r?.latency_ms != null ? `${r.latency_ms} ms` : "")), ok ? "info" : "error");
        } catch (e) { toastError(e, "Test échoué"); }
      }, { attrs: { "data-act": "test" } })),
    local ? null : keyLine);
  return box;
}

function usageView(r, s) {
  const money = (x) => (typeof x === "number" ? `${x.toFixed(x < 1 ? 4 : 2)} $` : "—");
  const out = [el("p.small.muted", { style: { marginBottom: ".8rem" }, text: "Montants estimés à partir des tarifs publics et du nombre de jetons : estimation, pas une facture." })];
  const rows = asList(r, "providers", "by_provider");
  if (rows.length) {
    for (const p of rows) out.push(el("div.row-item", {}, el("div.grow", {}, el("div.t", { text: nameOf(p) || pick(p, "provider") }),
      el("div.s", { text: `Aujourd'hui ${money(pick(p, "day", "today", "cost_day"))} — ce mois ${money(pick(p, "month", "cost_month"))}${pick(p, "calls") != null ? ` — ${p.calls} appels` : ""}` }))));
  } else if (r && typeof r === "object" && !Array.isArray(r)) {
    const byP = r.by_provider || r.providers;
    if (byP && typeof byP === "object") for (const [k, v] of Object.entries(byP)) out.push(el("div.row-item", {}, el("div.grow", {}, el("div.t", { text: k }),
      el("div.s", { text: `Aujourd'hui ${money(pick(v, "day", "today"))} — ce mois ${money(pick(v, "month"))}` }))));
  }
  const total = pick(r, "month_total", "total_month", "month");
  const budget = pick(r, "budget", "monthly_budget") ?? s["ai.monthly_budget"];
  out.push(el("p", { style: { marginTop: ".8rem" }, text: `Total du mois (estimation) : ${money(typeof total === "number" ? total : undefined)}${typeof budget === "number" ? ` sur un budget de ${money(budget)}` : ""}` }));
  if (typeof total === "number" && typeof budget === "number" && budget > 0) {
    out.push(el("div.meter", { style: { marginTop: ".5rem" } }, el("i", { style: { transform: `scaleX(${Math.min(1, total / budget)})` } })));
  }
  return out;
}

// ---- hardware -----------------------------------------------------------------------
function hardwarePage(root, app) {
  const info = el("div.panel");
  const pads = el("div.list", { "data-zone": "pads" });
  const setup = el("div.panel");
  const lightbar = el("div.chips", { "data-zone": "lightbar" });
  const accent = app.state.profile?.color || "#4cc9f0";
  for (const [label, hex] of [["Couleur du profil", accent], ["Bleu", "#0050ff"], ["Rouge", "#ff1030"], ["Vert", "#00d060"], ["Violet", "#8e3dff"], ["Éteinte", "#000000"]]) {
    const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
    lightbar.append(btn(label, () => run(() => api.call("hardware.controller_lightbar", { r, g, b }), `Barre lumineuse : ${label}`), { cls: "chip" }));
  }
  root.append(el("h1", { style: { marginBottom: "1.4rem" }, text: "Manettes et matériel" }),
    el("section.section", {}, el("h2", { text: "Manettes" }), pads),
    inputSection(app),
    el("section.section", {}, el("h2", { text: "Barre lumineuse DualSense" }), api.hasAction("hardware.controller_lightbar") ? lightbar : unavailable(api.moduleProblem("hardware") || "réglage absent")),
    el("section.section", {}, el("h2", { text: "Associer une manette" }), setup),
    el("section.section", {}, el("h2", { text: "Overlay de performances" }), el("div.list", { "data-zone": "overlay" },
      toggleRow("Afficher MangoHud dans les jeux", "S'applique aux prochains lancements", api.moduleStatus("hardware")?.overlay === true || api.moduleStatus("hardware")?.overlay?.enabled === true,
        (v) => api.call("hardware.overlay", { enabled: v }).then((r) => { toast(v ? "Overlay activé" : "Overlay désactivé"); return r; })))),
    el("section.section", {}, el("h2", { text: "Configuration" }), info));
  loadInto(pads, () => api.call("hardware.controllers"), (r) => {
    const l = asList(r, "controllers");
    return l.length ? l.map((c) => el("div.row-item", {}, el("div.grow", {}, el("div.t", { text: nameOf(c) || pick(c, "model") }),
      el("div.s", { text: [pick(c, "connection", "bus"), pick(c, "battery")?.capacity != null ? `batterie ${c.battery.capacity} %` : null].filter(Boolean).join(" — ") }))))
      : empty("Aucune manette détectée", "Branchez la manette en USB ou associez-la en Bluetooth.");
  }, { module: "hardware" });
  loadInto(setup, () => api.call("hardware.controller_setup"), (r) => resultView(r), { module: "hardware" });
  loadInto(info, () => api.call("hardware.info"), (r) => {
    const out = [];
    if (r?.cpu) out.push(el("h3", { text: "Processeur" }), kv(r.cpu));
    if (r?.memory) out.push(el("h3", { style: { marginTop: "1rem" }, text: "Mémoire" }), kv({ totale: fmtBytes(r.memory.total_bytes), disponible: fmtBytes(r.memory.available_bytes) }));
    for (const g of asList(r?.gpus)) out.push(el("h3", { style: { marginTop: "1rem" }, text: "Carte graphique" }), kv(g));
    if (r?.vulkan) out.push(el("h3", { style: { marginTop: "1rem" }, text: "Vulkan" }), el("p", { text: r.vulkan.available ? "disponible" : `indisponible : ${r.vulkan.reason || ""}` }));
    if (r?.disk) out.push(el("h3", { style: { marginTop: "1rem" }, text: "Disque" }), kv({ chemin: r.disk.path, libre: fmtBytes(r.disk.free_bytes), total: fmtBytes(r.disk.total_bytes) }));
    return out.length ? out : resultView(r);
  }, { module: "hardware" });
}

// Gamepad outside NEXUS (Firefox, desktop apps): module `input` (uinput mouse/keyboard).
function inputSection(app) {
  const sec = el("section.section", { "data-input-section": true }, el("h2", { text: "Manette hors de NEXUS (navigateur, applications)" }));
  if (api.moduleProblem("input") || !api.hasAction("input.status")) {
    sec.append(unavailable(api.moduleProblem("input") || "pilotage manette → souris/clavier absent"));
    return sec;
  }
  const status = el("div.panel");
  const mapping = el("div.list", { "data-zone": "input-mapping" });
  const loadStatus = () => loadInto(status, () => api.call("input.status"), (r) => resultView(r), { module: "input" });
  sec.append(el("p.muted", { style: { maxWidth: "64ch", marginBottom: "1rem" }, text: "Quand Firefox ou une application de bureau est au premier plan, la manette pilote un pointeur et un clavier virtuels." }),
    el("div.btn-row", { "data-zone": "input-actions", style: { marginBottom: "1rem" } },
      btn("Activer", () => run(async () => { const r = await api.call("input.enable"); loadStatus(); return r; }, "Pilotage manette activé"), { primary: true }),
      btn("Désactiver", () => run(async () => { const r = await api.call("input.disable"); loadStatus(); return r; }, "Pilotage manette désactivé"))),
    status,
    choiceRow(app, "Vitesse du pointeur", "input.pointer_speed", [1, 3, 5, 7, 10].map((v) => [v, String(v)])),
    choiceRow(app, "Disposition du clavier virtuel", "input.keyboard_layout", [["azerty", "AZERTY"], ["qwerty", "QWERTY"]]),
    el("h3", { style: { margin: "1rem 0 .6rem" }, text: "Schéma des boutons" }), mapping);
  loadStatus();
  loadInto(mapping, () => api.call("input.mapping"), (r) => {
    const rows = asList(r, "mapping", "buttons");
    const entries = rows.length ? rows.map((m) => [pick(m, "button", "input", "name"), pick(m, "action", "label", "description")])
      : Object.entries(r?.mapping || r || {}).filter(([, v]) => typeof v !== "object");
    return entries.length ? entries.map(([b, a]) => el("div.row-item", {}, el("span.glyph", { text: String(b) }), el("div.grow", { text: String(a ?? "") })))
      : empty("Schéma non disponible");
  }, { module: "input" });
  return sec;
}

// ---- system ------------------------------------------------------------------------
function systemPage(root) {
  const status = el("div.panel");
  const out = el("div");
  const show = (action, label) => { clear(out).append(loading(label)); api.call(action).then((r) => { if (r?.task_id) reportResult(r, label); clear(out).append(resultView(r)); nav.ensure(); }).catch((e) => clear(out).append(errorState(e))); };
  root.append(el("div.screen-head", {}, el("h1", { text: "Système" })),
    el("div.btn-row", { "data-zone": "system-actions", style: { marginBottom: "1.6rem" } },
      btn("Rechercher des mises à jour", () => show("system.update_check", "Recherche de mises à jour"), { primary: true, attrs: { "data-autofocus": true } }),
      btn("Installer la mise à jour", () => run(() => api.call("system.update_apply"), "Mise à jour du système"), { attrs: { "data-act": "update-apply" } }),
      btn("Revenir à la version précédente", () => run(() => api.call("system.rollback"), "Retour arrière"), { danger: true }),
      btn("Diagnostic complet", () => show("system.diagnostics", "Diagnostic"))),
    el("p.small.muted", { style: { marginBottom: "1.4rem", maxWidth: "70ch" }, text: "Les mises à jour du système sont atomiques : elles s'appliquent au prochain redémarrage et l'ancienne version reste disponible au démarrage (GRUB) en cas de problème." }),
    out, el("section.section", { style: { marginTop: "1.6rem" } }, el("h2", { text: "État" }), status));
  loadInto(status, () => api.call("system.status"), (r) => resultView(r), { module: "system" });
}

function powerPage(root) {
  root.append(el("h1", { style: { marginBottom: "1.4rem" }, text: "Alimentation et bureau" }));
  if (api.hasAction("system.power")) {
    const choices = api.actionSpec("system.power")?.params?.action?.choices || ["suspend", "reboot", "poweroff"];
    const L = { suspend: "Mettre en veille", reboot: "Redémarrer", poweroff: "Éteindre", logout: "Quitter la session", desktop: "Mode bureau" };
    root.append(el("div.btn-row", { "data-zone": "power" }, ...choices.map((c) => btn(L[c] || c, () => run(() => api.call("system.power", { action: c }), L[c] || c), { danger: c !== "suspend" }))));
  } else root.append(unavailable(api.moduleProblem("system") || "alimentation absente"));
  root.append(el("section.section", { style: { marginTop: "2rem" } }, el("h2", { text: "Mode bureau" }),
    el("p.muted", { style: { maxWidth: "62ch" }, text: "NEXUS OS est basé sur Bazzite : un bureau KDE Plasma complet est disponible. Pour y accéder, choisissez « Bureau » dans le menu de session au démarrage (ou « Quitter la session » ci-dessus si proposé). Revenez à NEXUS en choisissant la session NEXUS." })));
}

export default {
  id: "settings",
  title: "Paramètres",
  render(root, app) {
    root.append(el("div.screen-head", {}, el("div", {}, el("h1", { text: "Paramètres" }), el("p.muted", { text: "Profils, sécurité, NEXUS AI et système." }))));
    const grid = el("div.grid.wide", { "data-zone": "settings-pages" });
    PAGES.forEach((p, i) => {
      const t = btn(null, () => app.push({ id: `settings-${p.id}`, title: p.title, render: (r) => p.render(r, app) }),
        { cls: "tile", attrs: { "data-settings-page": p.id, ...(i === 0 ? { "data-autofocus": true } : {}) } });
      t.append(el("span.tile-icon", { "aria-hidden": "true", text: p.icon }), el("span.tile-title", { text: p.title }), el("span.tile-sub", { text: p.sub }));
      grid.append(t);
    });
    root.append(grid);
  },
};
