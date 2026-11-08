# NEXUS OS — Architecture technique

> Document de référence de l'Architecte système (Agent 1, piloté par le Head of Agents).
> Toute modification d'interface entre modules doit être reflétée ici **avant** d'être codée.

## 1. Choix de la base Linux

| Critère | Bazzite (Universal Blue) | SteamOS 3 | ChimeraOS | Nobara |
|---|---|---|---|---|
| Image publique dérivable (OCI `FROM`) | **Oui** (`ghcr.io/ublue-os/bazzite*`) | Non (pas d'image dérivable officielle pour PC) | Partiel (images frzr) | Non (distro classique) |
| Mises à jour atomiques + rollback | **Oui** (bootc / rpm-ostree, 2 déploiements) | Oui (A/B) | Oui (frzr) | Non |
| Steam + Proton + gamescope préinstallés | **Oui** | Oui | Oui | Oui |
| Support matériel PC large (NVIDIA, AMD, Intel) | **Oui** (images `-nvidia`) | AMD surtout | Oui | Oui |
| DualSense (hid-playstation, Steam Input, HHD) | **Oui** | Oui | Oui | Oui |
| Génération d'ISO depuis une image dérivée | **Oui** (bootc-image-builder / Titanoboa) | Non | Non | Non |

**Décision : Bazzite (variante `bazzite` desktop, tag `stable`)**, image dérivée par `Containerfile`.
Justification : c'est la seule base qui combine (1) une image OCI dérivable — NEXUS OS n'est
qu'une couche au-dessus, sans maintenir de distribution ; (2) des mises à jour atomiques avec
retour arrière (exigence « récupération en cas d'échec de mise à jour ») ; (3) la pile gaming
complète déjà intégrée et maintenue ; (4) un chemin documenté vers une ISO installable.
Le noyau, Mesa, les pilotes et Steam restent ceux de Bazzite.

## 2. Vue d'ensemble

```
┌──────────────────────────── gamescope (session « NEXUS ») ───────────────────────────┐
│  NEXUS Home (HTML/JS, plein écran, Gamepad API)  ⇄  nexusd (127.0.0.1:7777, jeton)   │
│                                                     │                                 │
│                    Registry ── PermissionManager ── AuditLog                          │
│   ┌───────┬─────────┬───────┬───────┬────────┬────┬──────────┬────────┬──────────┐   │
│   │  ai   │ games   │ emul. │ store │ music  │ vr │ browser  │hardware│ system   │   │
│   └───┬───┴────┬────┴───┬───┴───┬───┴───┬────┴─┬──┴────┬─────┴───┬────┴────┬─────┘   │
│       │  Runner (argv allow-list, pas de shell)  ·  Net (HTTPS allow-list)  ·  Flatpak│
└───────┴────────────────────────────────────────────────────────────────────────────┘
   Steam/Proton · Heroic · Lutris · émulateurs Flatpak · mpv · Firefox · Monado · rpm-ostree/bootc
```

* **nexusd** (Python ≥ 3.11, *stdlib uniquement*) : service utilisateur systemd, jamais root.
* **NEXUS Home** : application web statique servie par nexusd, affichée dans un navigateur
  en mode kiosque dans gamescope. Pas d'étape de build (vanilla JS, ES modules).
* **Modules** : `nexus/modules/<nom>/__init__.py` expose `MODULE = <classe NexusModule>`.
  Découverte automatique ; un module qui plante au chargement est isolé (`load_errors`).

## 3. Contrat interne (API des modules)

Toute fonctionnalité est une **action** `module.nom` déclarée avec `@action(nom, Risk, description, params)`.

* L'UI appelle `POST /api/call {action, params, confirmation_id?}` (origine `USER`).
* NEXUS AI appelle `ctx.call(action, params, origin=Origin.AI)`.
* Un module appelle un autre module **uniquement** via `self.ctx.call(...)` (jamais d'import croisé).
* Les opérations longues renvoient `{"task_id": ...}` via `ctx.tasks.submit` ; la progression
  arrive en événements SSE `task.update`.
* Les erreurs sont des `NexusError` avec un `code` stable (`not_found`, `invalid_params`,
  `permission_denied`, `confirmation_required`, `command_failed`, `unavailable`, `network_error`).
* Les commandes système passent **exclusivement** par `ctx.runner` ; le réseau par `ctx.net`.
* Les fichiers servis à l'UI (jaquettes) passent par `serve_file(subpath) -> Path|None`
  exposé en `GET /api/m/<module>/<subpath>` (images/audio uniquement).

### 3.1 Niveaux de risque → décision (déterministe, `security/permissions.py`)

| Risk | Origine USER | Origine AI |
|---|---|---|
| READ, CONTROL | autorisé | autorisé |
| SETTINGS | autorisé | confirmation |
| INSTALL_VERIFIED (FOSS vérifié Flathub) | autorisé | autorisé si `security.auto_install_verified` sinon confirmation |
| INSTALL_FREE_GAME (jeu gratuit légal) | autorisé | autorisé si `security.auto_install_free_games` |
| INSTALL_OWNED (client officiel) | autorisé | autorisé |
| PURCHASE | confirmation | confirmation |
| DELETE_DATA, SYSTEM | confirmation | confirmation |
| UNKNOWN_SOURCE | confirmation | **refus** |

Une confirmation est liée à l'action **et** aux paramètres exacts, à usage unique, expire en
5 min, et ne peut être approuvée que via `POST /api/confirmations/<id>/approve` (jeton UI).

### 3.2 Actions minimales par module (contrat)

Les noms ci-dessous sont **le contrat** utilisé par NEXUS Home et NEXUS AI. Un module peut en
ajouter ; il ne doit pas en renommer sans mise à jour de ce document.

| Module | Actions requises |
|---|---|
| `games` | `library`, `refresh`, `launch{id}`, `install{id}`, `uninstall{id}`, `details{id}`, `compatibility{id}`, `diagnose{id}`, `optimize{id}` |
| `emulation` | `systems`, `emulators`, `install{emulator}`, `uninstall{emulator}`, `roms`, `scan`, `launch{rom_id}`, `recommend{system}` |
| `store` | `featured`, `search{query}`, `details{app_id}`, `install{app_id}`, `install_unverified{app_id,remote}`, `uninstall{app_id}`, `installed`, `updates`, `update_all`, `free_games` |
| `music` | `status`, `library`, `scan`, `play{query?,track_id?,playlist_id?}`, `pause`, `resume`, `stop`, `next`, `previous`, `volume{level}`, `volume_step{delta}`, `playlists`, `playlist_create{name}`, `playlist_add{playlist_id,track_id}`, `radio_search{query}`, `radio_play{station_id}` |
| `browser` | `open{url}`, `close`, `bookmarks`, `bookmark_add{title,url}`, `bookmark_remove{id}`, `downloads`, `services` |
| `vr` | `status`, `apps`, `start_runtime`, `stop_runtime`, `set_performance{level}` |
| `hardware` | `info`, `controllers`, `controller_lightbar{r,g,b}`, `controller_setup`, `performance_profiles`, `set_performance_profile{profile}`, `overlay{enabled}` |
| `system` | `status`, `update_check`, `update_apply`, `rollback`, `power{action}`, `logs`, `diagnostics` |
| `ai` | `ask{text}`, `listen{seconds?}`, `speak{text}`, `status`, `configure{...}`, `history` |
| `profiles` | `list`, `create{name,avatar?}`, `switch{id}`, `delete{id}`, `current` ; `settings` : `get`, `set{key,value}` |

## 4. Propriété des fichiers (anti-conflit entre agents)

| Agent | Répertoires possédés (écriture exclusive) |
|---|---|
| Head of Agents / Architecte | `nexus/core/`, `nexus/security/`, `nexus/api/`, `docs/ARCHITECTURE.md`, `docs/PROJECT_STATUS.md` |
| 2 — Interface | `ui/`, `nexus/modules/profiles/`, `tests/ui/`, `tests/test_profiles*.py` |
| 3 — NEXUS AI | `nexus/modules/ai/`, `tests/test_ai*.py` |
| 4 — Gaming | `nexus/modules/games/`, `tests/test_games*.py` |
| 5 — Émulation | `nexus/modules/emulation/`, `tests/test_emulation*.py` |
| 6 — Store | `nexus/modules/store/`, `tests/test_store*.py` |
| 7 — VR (+ matériel) | `nexus/modules/vr/`, `nexus/modules/hardware/`, `tests/test_vr*.py`, `tests/test_hardware*.py` |
| 8 — Music | `nexus/modules/music/`, `tests/test_music*.py` |
| 9 — Browser | `nexus/modules/browser/`, `tests/test_browser*.py` |
| 10 — Sécurité | rapport `docs/audits/SECURITY_AUDIT.md` ; correctifs remontés au Head of Agents |
| 11 — Qualité | `tests/integration/`, `docs/audits/QA_REPORT.md` |
| 12 — Build | `image/`, `nexus/modules/system/`, `tests/test_system*.py`, `.github/workflows/nexus-*.yml`, `docs/INSTALL_USB.md` |

Un agent qui a besoin d'un changement dans le cœur le demande au Head of Agents.

## 5. Démarrage

1. GRUB → noyau Bazzite → systemd → autologin SDDM sur la session `nexus` (gamescope-session).
2. `nexusd.service` (unité **utilisateur**) démarre ; puis le client kiosque ouvre
   `http://127.0.0.1:7777/` en plein écran dans gamescope.
3. Steam est démarré en arrière-plan (`-silent`) pour que `steam://rungameid/<id>` fonctionne.
4. Le menu « Bureau » permet de basculer vers la session KDE de Bazzite (mode bureau facultatif).

## 6. Mises à jour et récupération

* Système : `bootc upgrade` / `rpm-ostree upgrade` → nouveau déploiement, l'ancien est conservé.
  En cas d'échec au démarrage, GRUB propose le déploiement précédent ; `system.rollback` le
  réactive. Les mises à jour ne s'appliquent qu'au redémarrage (atomicité).
* Applications : `flatpak update` (Flathub uniquement).
* NEXUS lui-même est dans l'image : il est mis à jour et restauré avec le système.
