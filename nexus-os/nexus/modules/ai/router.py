"""Deterministic local intent router and complexity classifier (no LLM, always available).

French first, English second. The pipeline is:
  normalize (lowercase, accents removed, punctuation folded)
  -> strip politeness ("peux-tu", "stp", "please"...)
  -> every rule proposes (intent, confidence, entities)
  -> the best score wins.

`classify()` decides whether a request is *simple* (always handled locally,
never sent to an API) or *complex* (may go to a remote LLM if the user allowed
it). Both are pure functions: they are unit-tested without any module loaded.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher


# --------------------------------------------------------------------------- normalisation

def normalize(text: str) -> str:
    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.replace("œ", "oe").replace("æ", "ae").replace("’", " ").replace("'", " ")
    t = re.sub(r"(?<![\w])\.|\.(?![\w])", " ", t)   # keep dots only inside words (youtube.com)
    t = re.sub(r"[^\w\s.+%]", " ", t)
    t = t.replace("_", " ")
    return re.sub(r"\s+", " ", t).strip()


_POLITE_HEAD = re.compile(
    r"^(?:(?:hey|ok|salut|bonjour|dis)\s+)?(?:nexus\s+)?"
    r"(?:(?:s il te plait|s il vous plait|stp|svp|please|est ce que tu peux|est ce que tu pourrais|"
    r"tu peux|tu pourrais|peux tu|pourrais tu|pouvez vous|je voudrais que tu|j aimerais que tu|"
    r"je veux que tu|can you|could you|would you|i want you to)\s+)*")
_POLITE_TAIL = re.compile(r"\s+(?:s il te plait|s il vous plait|stp|svp|please|merci)$")


def clean(text: str) -> str:
    n = normalize(text)
    n = _POLITE_HEAD.sub("", n, count=1)
    n = _POLITE_TAIL.sub("", n)
    return n.strip()


def similarity(a: str, b: str) -> float:
    """Fuzzy title similarity in [0, 1] on normalized strings."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ca, cb = na.replace(" ", ""), nb.replace(" ", "")
    if ca == cb:
        return 0.98
    score = SequenceMatcher(None, na, nb).ratio()
    if len(na) >= 3 and len(nb) >= 3 and (na in nb or nb in na):
        short, long_ = sorted((len(na), len(nb)))
        score = max(score, 0.75 + 0.2 * short / long_)
    ta, tb = set(na.split()), set(nb.split())
    if ta and tb:
        score = max(score, 0.9 * len(ta & tb) / len(ta | tb))
    return score


# --------------------------------------------------------------------------- vocabularies

# canonical ids follow the emulation module catalog (nexus/modules/emulation/catalog.py)
SYSTEM_ALIASES: list[tuple[str, str]] = sorted([
    ("playstation 1", "ps1"), ("playstation one", "ps1"), ("ps1", "ps1"), ("psx", "ps1"), ("ps one", "ps1"),
    ("playstation 2", "ps2"), ("ps2", "ps2"),
    ("playstation 3", "ps3"), ("ps3", "ps3"),
    ("playstation 4", "ps4"), ("ps4", "ps4"),
    ("psp", "psp"), ("playstation portable", "psp"),
    ("ps vita", "psvita"), ("psvita", "psvita"), ("vita", "psvita"),
    ("nintendo switch", "switch"), ("switch", "switch"),
    ("wii u", "wiiu"), ("wiiu", "wiiu"), ("wii", "wii"),
    ("gamecube", "gamecube"), ("game cube", "gamecube"), ("ngc", "gamecube"),
    ("nintendo 64", "n64"), ("n64", "n64"),
    ("super nintendo", "snes"), ("super nes", "snes"), ("snes", "snes"),
    ("nes", "nes"), ("famicom", "nes"),
    ("game boy advance", "gba"), ("gameboy advance", "gba"), ("gba", "gba"),
    ("game boy color", "gbc"), ("gameboy color", "gbc"), ("gbc", "gbc"),
    ("game boy", "gb"), ("gameboy", "gb"),
    ("nintendo 3ds", "3ds"), ("3ds", "3ds"),
    ("nintendo ds", "nds"), ("nds", "nds"), ("ds", "nds"),
    ("mega drive", "megadrive"), ("megadrive", "megadrive"), ("genesis", "megadrive"),
    ("dreamcast", "dreamcast"),
    ("xbox 360", "xbox360"), ("xbox360", "xbox360"), ("xbox", "xbox"),
], key=lambda kv: -len(kv[0]))

SYSTEM_LABELS = {
    "ps1": "PlayStation", "ps2": "PlayStation 2", "ps3": "PlayStation 3", "ps4": "PlayStation 4",
    "psp": "PSP", "psvita": "PS Vita", "switch": "Nintendo Switch", "wiiu": "Wii U", "wii": "Wii",
    "gamecube": "GameCube", "n64": "Nintendo 64", "snes": "Super Nintendo", "nes": "NES",
    "gba": "Game Boy Advance", "gbc": "Game Boy Color", "gb": "Game Boy", "3ds": "Nintendo 3DS",
    "nds": "Nintendo DS", "megadrive": "Mega Drive", "dreamcast": "Dreamcast", "xbox360": "Xbox 360",
    "xbox": "Xbox",
}

# official service URLs (same as nexus/modules/browser/services.py)
SERVICES = {
    "youtube music": "https://music.youtube.com", "youtube": "https://www.youtube.com",
    "twitch": "https://www.twitch.tv", "netflix": "https://www.netflix.com",
    "disney+": "https://www.disneyplus.com", "disney plus": "https://www.disneyplus.com",
    "disney": "https://www.disneyplus.com", "prime video": "https://www.primevideo.com",
    "amazon prime": "https://www.primevideo.com", "crunchyroll": "https://www.crunchyroll.com",
    "spotify": "https://open.spotify.com",
}

_ARTICLES = r"(?:le|la|les|l|un|une|des|du|de|d|mon|ma|mes|ton|ta|the|a|an|my|some)"
_MUSIC_WORDS = re.compile(r"\b(?:musique|musiques|music|chanson|chansons|song|songs|playlist|playlists|"
                          r"morceau|morceaux|album|albums|titre|titres|tracks?|radio)\b")
_CONTEXT_GAME = {"", "ce jeu", "le jeu", "mon jeu", "ce", "ca", "il", "jeu", "this game", "the game",
                 "my game", "it", "game", "celui ci", "ca marche", "ce truc"}

# complexity markers (normalized text)
_MULTI = re.compile(r"\b(?:puis|ensuite|et apres|apres ca|then|and then|after that)\b")
_COMPLEX_MARKERS: list[tuple[str, re.Pattern]] = [
    ("diagnostic", re.compile(r"\b(?:pourquoi|why|plante|plantent|crash\w*|ne (?:se )?(?:marche|fonctionne|lance|demarre)\w* (?:pas|plus)|"
                              r"(?:doesn t|does not|won t|will not|isn t) (?:work|start|launch|run)|diagnosti\w*|bug\w*|freez\w*|"
                              r"rame|lag\w*|erreur|error)\b")),
    ("optimize", re.compile(r"\b(?:optimi\w*|ameliore\w* (?:les )?perf\w*|boost\w*|improve)\b")),
    ("compare", re.compile(r"\b(?:compar\w*|versus|vs|difference\w*|mieux que|better than)\b")),
    ("research", re.compile(r"\b(?:recherch\w*|search for|find out|renseigne\w*|informe\w*|quels? (?:sont|est) les? meilleur\w*|"
                            r"what (?:is|are) the best|recommand\w*|recommend\w*|conseill\w*|suggere\w*|suggest\w*)\b")),
    ("planning", re.compile(r"\b(?:planifi\w*|plan|programme moi|organise\w*|schedule)\b")),
    ("open_question", re.compile(r"\b(?:explique\w*|explain|comment (?:faire|je|on)|how (?:do|can|to|does)|qu est ce que|"
                                 r"c est quoi|what is|what s|dois je|devrais je|should i|est ce que je)\b")),
]
# intents a user expects to be instantaneous: never sent to an API even if a marker matches
ALWAYS_SIMPLE = {"music_pause", "music_resume", "music_stop", "music_next", "music_previous",
                 "volume_up", "volume_down", "volume_set", "mute", "power", "browser_open",
                 "game_launch", "music_play", "help"}
SIMPLE_INTENTS = ALWAYS_SIMPLE | {"emulator_install", "store_install", "free_games", "controller_setup",
                                  "updates_install", "updates_check", "hardware_info", "games_library",
                                  "game_install", "play_ambiguous"}


@dataclass
class Intent:
    name: str
    confidence: float
    entities: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "confidence": round(self.confidence, 2), "entities": self.entities}


@dataclass
class Complexity:
    level: str          # simple | complex
    category: str       # simple | diagnostic | optimize | compare | research | planning | multi_step | open_question
    short: bool

    def to_dict(self) -> dict:
        return {"level": self.level, "category": self.category, "short": self.short}


# --------------------------------------------------------------------------- entity helpers

def find_system(n: str) -> str | None:
    for alias, sid in SYSTEM_ALIASES:
        if re.search(rf"(?<![\w]){re.escape(alias)}(?![\w])", n):
            return sid
    return None


def strip_articles(s: str) -> str:
    s = s.strip()
    prev = None
    while prev != s:
        prev = s
        s = re.sub(rf"^{_ARTICLES}\s+", "", s).strip()
    return s


def game_ref(s: str) -> str | None:
    """Clean a game title captured by a rule; None = 'the game we were talking about'."""
    s = strip_articles(s)
    s = re.sub(r"^(?:jeu|game)\s+", "", s).strip()
    s = re.sub(r"\s+(?:sur steam|on steam|stp|svp|please)$", "", s).strip()
    if s in _CONTEXT_GAME or strip_articles(s) in _CONTEXT_GAME:
        return None
    return s or None


def find_service(target: str) -> str | None:
    t = strip_articles(target)
    for name, url in SERVICES.items():
        if t == name or t == name.replace(" ", "") or t in (f"{name} com", f"{name}.com"):
            return url
    return None


def find_url(raw: str) -> str | None:
    m = re.search(r"https?://[^\s<>\"']+", raw)
    if m:
        return m.group(0).rstrip(".,;)")
    m = re.search(r"(?<![@\w.])((?:[a-z0-9-]+\.)+(?:com|fr|org|net|io|tv|gg|eu|be|ch|ca|de|uk|app|dev))(?:/[^\s]*)?(?![\w])",
                  raw.lower())
    return m.group(0) if m else None


# --------------------------------------------------------------------------- rules

_INSTALL = r"(?:instal+\w*|telecharg\w*|ajout\w*|download|get|il me faut)"


def _rules(n: str, raw: str) -> list[Intent]:
    out: list[Intent] = []
    add = lambda name, c, **e: out.append(Intent(name, c, e))  # noqa: E731
    has_music = bool(_MUSIC_WORDS.search(n))

    # help
    if re.match(r"^(?:aide|help|a l aide|que (?:peux|sais) tu faire|qu est ce que tu (?:peux|sais) faire|"
                r"what can you do|tu sais faire quoi)$", n):
        add("help", 0.95)

    # updates
    upd = r"(?:mises? a jour|maj|updates?|upgrades?)"
    if re.search(rf"\b(?:instal+\w*|appliqu\w*|lanc\w*|fai[st]?|faire|effectu\w*|run|apply|do)\b.*\b{upd}\b", n) \
            or re.match(r"^(?:mets?|mettre|mettez) (?:tout |le systeme |le pc |les jeux )?a jour", n) \
            or re.match(r"^(?:update|upgrade)(?: (?:everything|all|the system|system|my pc))?$", n):
        add("updates_install", 0.95)
    elif re.search(rf"\b(?:verifi\w*|regard\w*|cherch\w*|y a t il|il y a|est ce qu il y a|check\w*|any)\b.*\b{upd}\b", n):
        add("updates_check", 0.9)
    elif re.search(rf"\b{upd}\b", n):
        add("updates_check", 0.6)

    # emulators
    if re.search(r"\bemul\w*\b", n):
        sysid = find_system(n)
        verb = re.search(rf"\b(?:{_INSTALL}|mets|configur\w*|je veux|j ai besoin|need|want)\b", n)
        add("emulator_install", 0.95 if verb else 0.75, system=sysid)

    # free games
    if re.search(r"\b(?:jeux?|games?)\b.*\b(?:gratuit\w*|free)\b", n) or re.search(r"\bfree\b.*\bgames?\b", n) \
            or "free to play" in n or "f2p" in n:
        add("free_games", 0.95)

    # compatibility
    if re.search(r"\b(?:jeux|games)\b.*\bcompatibl\w*", n) or re.search(r"\bcompatibl\w*.*\b(?:jeux|games)\b", n) \
            or re.search(r"\b(?:quels?|which|what) (?:jeux|games) .*\b(?:tourn\w*|run|marche\w*|fonctionn\w*)\b", n):
        add("compat_all", 0.95)
    else:
        m = re.match(r"^(?:est ce que |is |does )?(?P<g>.+?) (?:est|sera|serait|is|will be) compatible\b", n)
        if m:
            add("game_compat", 0.85, game=game_ref(m.group("g")))

    # controller
    if re.search(r"\b(?:manettes?|controllers?|gamepads?|dualsense|dualshock|joycons?|joy con)\b", n) and \
            re.search(r"\b(?:configur\w*|parametr\w*|regl\w*|connect\w*|appair\w*|associ\w*|set ?up|pair\w*|"
                      r"install\w*|branch\w*|synchronis\w*)\b", n):
        model = None
        if re.search(r"\b(?:ps5|dualsense|playstation 5)\b", n):
            model = "dualsense"
        elif re.search(r"\b(?:ps4|dualshock)\b", n):
            model = "dualshock4"
        elif "xbox" in n:
            model = "xbox"
        add("controller_setup", 0.95, controller=model)

    # diagnose
    fail = (r"(?:ne |n )?(?:se )?(?:fonctionne|marche|demarre|lance|tourne|plante|crash\w*|freez\w*|rame|lag\w*|"
            r"bug\w*|ferme|quitte)\b")
    m = re.match(rf"^pourquoi (?:est ce que )?(?P<g>.+?) {fail}", n)
    if m:
        add("diagnose", 0.92, game=game_ref(m.group("g")))
    m = re.match(r"^why (?:does|is|won t|doesn t|did|isn t|will) (?P<g>.+?) (?:not )?(?:keep )?"
                 r"(?:crash\w*|work\w*|launch\w*|start\w*|run\w*|freez\w*|lag\w*)", n)
    if m:
        add("diagnose", 0.92, game=game_ref(m.group("g")))
    m = re.match(r"^(?P<g>.+?) (?:ne |n )(?:se )?(?:fonctionne|marche|demarre|lance)\w* (?:pas|plus)", n) \
        or re.match(r"^(?P<g>.+?) (?:plante|crash\w*|freez\w*)\b", n) \
        or re.match(r"^(?P<g>.+?) (?:won t|doesn t|does not|will not) (?:start|launch|work|run)", n)
    if m and not n.startswith(("pourquoi", "why")):
        add("diagnose", 0.85, game=game_ref(m.group("g")))
    m = re.match(r"^(?:diagnosti\w*|diagnose|debug\w*|repare\w*|fix)\b(?: (?:de|du|le|la|les|l|pour|for|the|my))?\s*(?P<g>.*)$", n)
    if m:
        add("diagnose", 0.9, game=game_ref(m.group("g")))

    # optimize
    m = re.match(r"^(?:optimi\w*|ameliore\w*|boost\w*|improve|optimize)\b(?: (?:les|la|le|the))?"
                 r"(?: (?:performances?|perfs?|fps|framerate|fluidite|performance))?"
                 r"(?: (?:de|du|des|d|pour|sur|dans|of|for|in))?\s*(?P<g>.*)$", n)
    if m:
        add("optimize", 0.92, game=game_ref(m.group("g")))

    # power
    if not has_music:
        if re.search(r"\b(?:eteins|eteindre|eteint|eteignez|shut ?down|power ?off|turn off)\b", n) or \
                re.match(r"^(?:arrete|arreter) (?:le pc|l ordinateur|la console|le systeme|la machine|nexus)$", n):
            add("power", 0.95, action="shutdown")
        if re.search(r"\b(?:redemarr\w*|reboot\w*)\b", n) or re.match(r"^restart(?: (?:the )?(?:pc|computer|console|system))?$", n):
            add("power", 0.95, action="reboot")
        if re.search(r"\b(?:en veille|mise en veille|veille|suspend\w*|sleep)\b", n):
            add("power", 0.93, action="suspend")
        if re.search(r"\b(?:deconnecte moi|ferme la session|log ?out|deconnexion)\b", n):
            add("power", 0.9, action="logout")

    # music transport / volume
    if re.match(r"^(?:mets?|mettre|fais|fait) (?:la musique |la chanson |la lecture )?(?:en )?pause\b", n) or \
            re.match(r"^pause\b", n) or re.search(r"\bpause (?:la )?(?:musique|music|chanson|lecture)\b", n):
        add("music_pause", 0.95)
    if re.match(r"^(?:reprend\w*|resume|continue\w*|unpause)\b", n) or re.search(r"\brelance la (?:musique|lecture)\b", n):
        add("music_resume", 0.88)
    if re.search(r"\b(?:stop\w*|arret\w*|coupe\w*|eteins|eteint|ferme)\b.*\b(?:musique|music|chanson|lecture)\b", n) or \
            re.match(r"^(?:stop|arrete|stoppe)$", n):
        add("music_stop", 0.92)
    if re.search(r"\b(?:suivant\w*|next|skip)\b", n) or re.search(r"\b(?:passe|change)\w* (?:de |la |cette )?(?:chanson|musique|piste|morceau)\b", n):
        add("music_next", 0.9)
    if re.search(r"\b(?:precedent\w*|previous|chanson d avant|reviens en arriere)\b", n):
        add("music_previous", 0.9)
    if re.search(r"\b(?:coupe (?:le )?son|mute|silence)\b", n):
        add("mute", 0.92)
    vol_word = r"(?:son|volume|fort|sound)"
    if re.search(rf"\b(?:monte|augmente|hausse|remonte)\w* (?:\w+ )?{vol_word}\b", n) or \
            re.search(r"\b(?:plus fort|turn (?:it )?up|volume up|louder|raise the volume)\b", n):
        if re.search(r"\b(?:a fond|au max\w*|maximum)\b", n):
            add("volume_set", 0.95, level=100)
        else:
            add("volume_up", 0.92, delta=10)
    if re.search(rf"\b(?:baisse|diminue|reduis|reduit)\w* (?:\w+ )?{vol_word}\b", n) or \
            re.search(r"\b(?:moins fort|turn (?:it )?down|volume down|quieter|lower the volume)\b", n):
        add("volume_down", 0.92, delta=-10)
    m = re.search(r"\b(?:volume|son|sound)\b\D{0,12}?(?P<v>\d{1,3})\s*(?:%|pour ?cent|percent)?", n)
    if m:
        add("volume_set", 0.95, level=max(0, min(100, int(m.group("v")))))

    # browser
    m = re.match(r"^(?:ouvr\w*|va sur|vas sur|aller sur|allons sur|affiche|lanc\w*|mets?|regarde\w*|open|go to|browse|"
                 r"visit\w*|watch)\b(?: moi)?(?: (?:le site(?: web)?|la page|site|sur))?\s+(?P<t>.+)$", n)
    if m:
        target = m.group("t").strip()
        url = find_service(target)
        if url:
            add("browser_open", 0.93, url=url)
        else:
            u = find_url(raw)
            if u and re.match(r"^(?:ouvr\w*|va sur|vas sur|aller sur|allons sur|affiche|open|go to|browse|visit\w*)\b", n):
                add("browser_open", 0.93, url=u if u.startswith("http") else "https://" + u)

    # music play / game launch
    m = re.match(r"^(?P<v>lanc\w*|mets?|mettre|mettez|jou\w*|ecout\w*|play|passe|start|demarr\w*|launch|run|ouvr\w*)"
                 r"(?: moi| nous)?\b\s*(?P<rest>.*)$", n)
    if m:
        verb, rest = m.group("v"), m.group("rest").strip()
        if has_music:
            q = _MUSIC_WORDS.sub(" ", rest)
            q = re.sub(rf"\b{_ARTICLES}\b", " ", q)
            q = re.sub(r"\b(?:de|d|un peu|some|of|playing|en|a)\b", " ", q)
            q = re.sub(r"\s+", " ", q).strip()
            add("music_play", 0.9, query=q or None)
        elif verb.startswith("jou") and re.match(r"^(?:a|au|aux|avec)\s+", rest):
            add("game_launch", 0.9, game=game_ref(re.sub(r"^(?:a|au|aux|avec)\s+", "", rest)))
        elif rest and (verb.startswith(("met", "ecout", "jou", "passe"))) and not \
                re.match(r"^(?:a jour|en veille|en pause|pause|le son|du son|le volume|plus fort|moins fort)\b", rest):
            q = re.sub(r"^(?:moi |nous )?(?:du|de la|de l|des|de|un peu de|un peu d)\s+", "", rest).strip()
            add("music_play", 0.8, query=q or None)
        elif verb == "play" and rest:
            add("play_ambiguous", 0.75, query=strip_articles(re.sub(r"^some\s+", "", rest)))
        elif rest and verb.startswith(("lanc", "start", "demarr", "launch", "run", "ouvr")):
            g = game_ref(rest)
            add("game_launch", 0.8 if not verb.startswith("ouvr") else 0.55, game=g)

    # install from the store / owned game
    m = re.match(rf"^{_INSTALL}(?: moi| nous)?\b\s*(?P<q>.+)$", n)
    if m:
        q = strip_articles(m.group("q"))
        gm = re.match(r"^(?:jeu|game)\s+(?P<g>.+)$", q)
        if gm:
            add("game_install", 0.82, game=game_ref(gm.group("g")))
        else:
            q = re.sub(r"^(?:logiciel|application|appli|app|programme|software)\s+(?:de |d )?", "", q).strip()
            q = strip_articles(q)
            if q:
                add("store_install", 0.8, query=q)

    # hardware / library
    if re.search(r"\b(?:ma config\w*|mon materiel|mes specs|specs|caracteristiques|hardware|"
                 r"(?:quelle|quel) (?:est )?(?:ma |mon )?(?:carte graphique|gpu|cpu|processeur))\b", n):
        add("hardware_info", 0.85)
    if re.search(r"\b(?:mes jeux|ma bibliotheque|ma ludotheque|my games|my library|quels jeux (?:j ai|ai je))\b", n):
        add("games_library", 0.85)
    return out


def route(text: str) -> Intent | None:
    n = clean(text)
    if not n:
        return None
    cands = _rules(n, text)
    if not cands:
        return None
    return max(cands, key=lambda i: i.confidence)


def classify(text: str, intent: Intent | None = None) -> Complexity:
    """Deterministic complexity classifier (see docs/NEXUS_AI.md §Décision)."""
    n = clean(text)
    words = len(n.split())
    short = words <= 12
    if intent is None:
        intent = route(text)
    multi = bool(_MULTI.search(n)) and words >= 4
    if intent and intent.name in ALWAYS_SIMPLE and intent.confidence >= 0.8 and not multi:
        return Complexity("simple", "simple", short)
    if multi:
        return Complexity("complex", "multi_step", short)
    for cat, rx in _COMPLEX_MARKERS:
        if rx.search(n):
            return Complexity("complex", cat, short)
    if intent and intent.confidence >= 0.5:
        if intent.name in ("diagnose",):
            return Complexity("complex", "diagnostic", short)
        if intent.name in ("optimize",):
            return Complexity("complex", "optimize", short)
        if intent.name in ("compat_all", "game_compat"):
            return Complexity("complex", "research", short)
        return Complexity("simple", "simple", short)
    if words >= 4 or "?" in (text or ""):
        return Complexity("complex", "open_question", short)
    return Complexity("simple", "simple", short)
