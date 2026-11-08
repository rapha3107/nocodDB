# Brief commun à tous les agents NEXUS OS

Projet : `/home/user/nocodDB/nexus-os` (le reste du dépôt est NocoDB : **ne pas y toucher**).

## À lire avant de coder
1. `docs/ARCHITECTURE.md` — architecture, tableau des risques, **contrat d'actions (§3.2)**, propriété des fichiers (§4).
2. `nexus/core/module.py` — `NexusModule`, `@action`, `Param`, `Context` (`ctx.call`, `ctx.runner`, `ctx.net`, `ctx.tasks`, `ctx.events`, `ctx.settings`, `ctx.paths`).
3. `nexus/security/runner.py` (allow-list de binaires + validateurs), `nexus/core/net.py` (allow-list d'hôtes HTTPS), `nexus/core/flatpak.py` (installation Flathub partagée), `nexus/security/verify.py`.
4. `tests/conftest.py` (fixtures `make_ctx`, `runner` = `FakeRunner`, `net` = `FakeNet`, `paths`) et `tests/test_core_security.py` comme modèle de tests.

## Règles non négociables
* N'écris **que** dans les répertoires que tu possèdes (ARCHITECTURE §4). Ne modifie jamais `nexus/core`, `nexus/security`, `nexus/api`, ni les fichiers d'un autre agent. Si tu as besoin d'un changement du cœur (binaire à autoriser, hôte réseau, nouveau type), **ne contourne pas** : écris-le dans la section « Demandes au cœur » de ton rapport.
* **Pas de `git commit` / `git push`** : le Head of Agents intègre et commite.
* Python ≥ 3.11, **bibliothèque standard uniquement** (pas de dépendance pip dans les modules).
* Aucune commande système hors `ctx.runner` (pas de `subprocess`, `os.system`, shell). Aucun réseau hors `ctx.net`.
* Aucune fonctionnalité inventée, aucun bouton sans action, aucune donnée fictive présentée comme réelle. Les catalogues curatés (ex. identifiants Flathub d'émulateurs) doivent contenir des **identifiants réels** que tu connais avec certitude ; sinon, ne les mets pas.
* Ne présente jamais une compatibilité comme garantie (jeu, émulateur, casque VR). Utilise des niveaux honnêtes (`stable`, `bon`, `expérimental`, `limité`, `non supporté`).
* Rien de piraté : aucun téléchargement de ROM, BIOS, firmware, clé. L'utilisateur fournit ses propres fichiers.
* Erreurs : lève des `NexusError` (codes stables) avec messages en français ; si un binaire/matériel manque, `Unavailable` et un `status()` qui l'indique clairement.
* Opérations longues → `ctx.tasks.submit(titre, fn)` qui renvoie `{"task_id"}`.
* Chaque action déclare le bon `Risk` (voir tableau ARCHITECTURE §3.1). Suppression = `DELETE_DATA`, système = `SYSTEM`, source inconnue = `UNKNOWN_SOURCE`.
* Rends les chemins système injectables (paramètres du constructeur ou réglages) pour pouvoir tester avec `tmp_path` au lieu de `/sys`, `~/.steam`, etc.
* Les textes utilisateur sont en français.

## Tests
* Tes tests vont dans `tests/test_<module>*.py` (ou les dossiers que tu possèdes). Utilise `make_ctx(modules=["<ton_module>"])`.
* Lance `cd /home/user/nocodDB/nexus-os && python3 -m pytest -q tests/test_<module>*.py` puis **la suite complète** `python3 -m pytest -q` (d'autres agents travaillent en parallèle : si un test d'un autre module échoue, signale-le sans le modifier).
* Ne déclare jamais un test réussi sans l'avoir exécuté. Ne masque jamais un échec.

## Rapport final (ton dernier message)
1. Fichiers créés/modifiés.
2. Actions exposées (nom, risque, description courte).
3. Tests : commande exécutée et résultat exact (nombre passés/échoués).
4. Ce qui n'a **pas** pu être testé ici (pas de matériel, pas de GPU, etc.) et la procédure de test sur machine réelle.
5. Limitations et bugs connus.
6. Demandes au cœur (binaires, hôtes, changements d'API).
