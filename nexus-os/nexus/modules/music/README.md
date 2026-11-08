# NEXUS Music (`music`)

Lecture de musique locale, playlists, radios Internet et volume, pilotés par **mpv** en
arrière-plan via son IPC JSON.

## Lecteur

* mpv est lancé une seule fois, à la première lecture, par `ctx.runner.spawn` :
  `mpv --input-ipc-server=$XDG_RUNTIME_DIR/nexus/mpv-music.sock --no-video --idle=yes
  --really-quiet --no-terminal --force-window=no --audio-display=no --ytdl=no --volume=<music.volume>`.
* `--ytdl=no` : mpv ne résout jamais une page web via yt-dlp (pas de lecture détournée de YouTube).
* Tous les ordres (`loadfile`, `set_property pause/volume`, `playlist-next/prev`, `stop`,
  `get_property time-pos/duration/media-title/...`) passent par le socket UNIX (`mpv.py`) ;
  les chemins de fichiers ne sont jamais passés en argument de ligne de commande.
* **Musique pendant les jeux** : mpv est un processus détaché (nouvelle session), indépendant de
  NEXUS Home et de nexusd. La musique continue quand un jeu passe au premier plan dans gamescope,
  et si nexusd redémarre il se reconnecte au socket existant. `music.stop` vide la file ; mpv
  reste inactif (`--idle`) pour redémarrer instantanément.
* **Baisse du volume au lancement d'un jeu** : le module écoute le bus d'événements
  (`ctx.events.subscribe()` dans un thread). Sur `games.launched`, si la musique joue et que
  `music.duck_on_game` (défaut `true`) est actif, le volume mpv passe à `music.duck_level`
  (défaut 30). Il est restauré sur `games.exited` **si** le module Gaming publie cet événement
  (sinon : `music.volume` ou `music.volume_step`). Tout réglage manuel du volume annule la restauration.
* Repli Flatpak `io.mpv.Mpv` (**expérimental**) : utilisé seulement si `mpv` natif est absent.
  Le bac à sable Flatpak peut empêcher mpv de créer son socket dans `$XDG_RUNTIME_DIR/nexus`
  ou de lire `~/Music` ; NEXUS n'ajoute aucune permission Flatpak. Si le socket n'apparaît pas
  en 5 s, l'action échoue avec `unavailable` (`reason = no_socket_flatpak`). mpv natif est recommandé.

## Bibliothèque

* Dossiers : réglage `music.dirs` (liste), `~/Music` par défaut. Extensions : mp3, flac, ogg, opus, m4a, wav.
  Dossiers et fichiers cachés ignorés, liens symboliques de dossiers non suivis.
* Tags en pur Python (`tags.py`) : ID3v2.2/2.3/2.4 (TIT2/TPE1/TALB/TCON…), ID3v1, commentaires
  Vorbis FLAC (+ durée STREAMINFO), Ogg Vorbis/Opus. M4A/WAV : nom de fichier
  `Artiste - Titre` (numéro de piste initial retiré).
* Pochettes : `cover.jpg`, `folder.jpg` (et variantes png/jpeg, `front.jpg`) du dossier du morceau,
  servies par `GET /api/m/music/cover/<track_id>`. Les pochettes intégrées aux fichiers ne sont pas extraites.
* Index JSON : `~/.cache/nexus/music/index.json`, rescan incrémental (taille + date).
  Premier scan lancé en tâche au démarrage si l'index n'existe pas.
* Recherche floue (`play{query}`, `library{query}`) : titre/artiste/album/genre, insensible à la
  casse et aux accents, tolère une faute de frappe sur les mots de 4 lettres ou plus ; chaque mot
  doit correspondre. Une playlist dont le nom correspond exactement à la requête est jouée en priorité.

## Radios

Annuaire **Radio Browser** (`de1.api.radio-browser.info`, repli `all.api.radio-browser.info`, via
`ctx.net`). `radio_play` récupère le flux par `/json/stations/byuuid/<uuid>` ; seules les URL
`http(s)` sont jouées (le flux lui-même est ouvert par mpv, pas par `ctx.net`). Favoris persistés
dans `~/.local/share/nexus/music/radio_favorites.json` (URL en cache utilisée si l'annuaire est injoignable).

## Volume

* `volume{level}` / `volume_step{delta}` : volume de mpv (mémorisé dans `music.volume`).
* `system_volume{level}` / `system_volume_step{delta}` / `system_volume_get` : volume général
  PipeWire via `wpctl set-volume @DEFAULT_AUDIO_SINK@ 0.50` (format `0.NN` / `1.00` vérifié strictement).
  Ce sont les commandes vocales « monte le son ».

## Spotify et YouTube Music

NEXUS **n'intègre aucun contournement** (pas d'extraction de flux, de téléchargement, ni de
suppression de publicités). `music.services` décrit :

* **Spotify** : application officielle Flatpak `com.spotify.Client` (Flathub), installable par
  `store.install`. Compte Spotify requis, conditions d'utilisation de Spotify applicables.
  Le contrôle à distance via MPRIS (D-Bus) n'est pas intégré : il nécessiterait un outil comme
  `playerctl` dans l'allow-list du cœur.
* **YouTube Music** : site officiel `https://music.youtube.com` ouvert dans NEXUS Browser
  (`browser.open`). Conditions d'utilisation de YouTube applicables (la lecture en arrière-plan
  écran éteint relève de YouTube Premium).

## Événements publiés

`music.state` (à chaque changement : état, source, morceau, volume), `music.library`,
`music.playlists`, `music.system_volume`.
