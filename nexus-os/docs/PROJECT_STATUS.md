# NEXUS OS — Document central de pilotage

Maintenu par le Head of Agents. Chaque agent ajoute ses entrées dans *Journal des modifications*.

## Environnement de développement (inspecté le 2026-11-08)

| Ressource | État | Conséquence |
|---|---|---|
| CPU / RAM / disque | 4 vCPU, 15 Go RAM, ~30 Go libres | build d'image possible, ISO juste |
| Python 3.13, Node 22, Go, Rust | présents | backend Python stdlib |
| Docker 29 (démon lancé manuellement) | fonctionnel | build de l'image OCI Bazzite possible |
| Accès ghcr.io (image Bazzite) | OK | `FROM ghcr.io/ublue-os/bazzite:stable` |
| /dev/kvm, QEMU | **absents** | **impossible de démarrer l'ISO ici** → procédure sur machine réelle |
| GPU, manette, micro, casque VR | **absents** | tests matériels uniquement via doublures + procédure réelle |
| Chromium + Playwright | présents | tests UI de bout en bout (manette simulée via Gamepad API) |
| Sous-agents | disponibles (outil Agent) | équipe parallèle par répertoire |

## Équipe et missions

| # | Agent | Mission | Statut |
|---|---|---|---|
| 1 | Architecte (Head of Agents) | cœur, sécurité, API, contrat | ✅ cœur livré, 45 tests |
| 2 | Interface NEXUS Home | `ui/`, profils | en cours |
| 3 | NEXUS AI | `modules/ai` | en cours |
| 4 | Gaming & launchers | `modules/games` | en cours |
| 5 | Émulation | `modules/emulation` | en cours |
| 6 | NEXUS Store | `modules/store` | en cours |
| 7 | VR + matériel | `modules/vr`, `modules/hardware` | en cours |
| 8 | NEXUS Music | `modules/music` | en cours |
| 9 | NEXUS Browser | `modules/browser` | en cours |
| 10 | Sécurité | audit indépendant | après intégration |
| 11 | Tests & qualité | intégration + rapport | après intégration |
| 12 | Build & distribution | `image/`, `modules/system` | en cours |

## Décisions techniques

* D1 — Base Bazzite (voir ARCHITECTURE §1).
* D2 — Backend stdlib-only : rien à empaqueter en plus dans l'image atomique, surface de dépendances minimale.
* D3 — UI web vanilla servie localement : testable en headless, Gamepad API standard (DualSense supportée par Chromium/Firefox).
* D4 — Politique d'autorisation déterministe ; l'IA ne peut jamais approuver ses propres demandes ni obtenir root.
* D5 — Logiciels : Flathub uniquement par défaut ; toute autre source = UNKNOWN_SOURCE.
* D6 — Aucun BIOS/firmware/clé/ROM n'est téléchargé par NEXUS : l'utilisateur fournit ses propres dumps.

## Journal des modifications
* 2026-11-08 Head of Agents — cœur (`core/`, `security/`, `api/`), contrat d'actions, fixtures de test.
