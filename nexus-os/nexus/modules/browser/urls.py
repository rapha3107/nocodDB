"""Validation et normalisation des URL ouvertes par NEXUS Browser.

Règles :
  * seuls `http` et `https` sont acceptés (file:, javascript:, data:, chrome:,
    about:, view-source:, blob:… sont refusés) ;
  * une saisie sans schéma qui ressemble à un nom d'hôte reçoit `https://` ;
  * une saisie qui n'est pas une URL devient une recherche ;
  * les hôtes locaux / privés (localhost, 127.0.0.0/8, 10/8, 192.168/16, ::1,
    link-local, CGNAT, formes IPv4 « legacy » comme 0x7f000001 ou 2130706433…)
    sont refusés pour ne pas exposer nexusd ni le réseau local à une page web,
    sauf s'ils figurent dans une liste blanche explicite (vide par défaut).

Limite connue : aucune résolution DNS n'est faite ici. Un nom public qui
résoudrait vers une adresse privée (DNS rebinding) n'est pas détecté ; nexusd
se protège de son côté par son jeton d'API.
"""

from __future__ import annotations

import ipaddress
import re
import urllib.parse

from ...core.errors import InvalidParams, PermissionDenied

DEFAULT_SEARCH_ENGINE = "https://duckduckgo.com/?q="
MAX_URL_LEN = 4096

ALLOWED_SCHEMES = {"http", "https"}
_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_HOST_LABEL_RE = re.compile(r"^(?!-)[a-z0-9\-¡-￿]{1,63}(?<!-)$", re.IGNORECASE)
_LOCAL_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".lan", ".home.arpa")
_LOCAL_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback", "broadcasthost"}


def _parse_legacy_ipv4(host: str) -> ipaddress.IPv4Address | None:
    """Interprète un hôte comme inet_aton le ferait (127.1, 0x7f000001, 0177.0.0.1, 2130706433).

    Renvoie None si l'hôte n'est pas une forme numérique IPv4.
    Lève InvalidParams pour une forme numérique invalide (ex. 0x7g).
    """
    parts = host.split(".")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if not parts or len(parts) > 4:
        return None
    nums: list[int] = []
    for p in parts:
        if not p:
            return None
        low = p.lower()
        try:
            if low.startswith("0x"):
                n = int(low[2:] or "0", 16)
            elif len(low) > 1 and low.startswith("0") and low.isdigit():
                n = int(low, 8)
            elif low.isdigit():
                n = int(low, 10)
            else:
                return None
        except ValueError:
            # Ressemble à un nombre (0x… / 0…) mais invalide : on refuse plutôt que deviner.
            raise InvalidParams(f"Adresse numérique invalide: {host}")
        nums.append(n)
    *head, last = nums
    if any(n > 255 for n in head) or last >= 256 ** (4 - len(head)):
        raise InvalidParams(f"Adresse IPv4 invalide: {host}")
    value = 0
    for n in head:
        value = value * 256 + n
    value = value * (256 ** (4 - len(head))) + last
    return ipaddress.IPv4Address(value)


def host_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    h = host.strip("[]")
    if ":" in h:
        try:
            ip = ipaddress.IPv6Address(h.split("%", 1)[0])
        except ValueError:
            raise InvalidParams(f"Adresse IPv6 invalide: {host}")
        return ip.ipv4_mapped or ip
    return _parse_legacy_ipv4(h)


def is_local_host(host: str) -> bool:
    """True si l'hôte désigne la machine locale ou un réseau non public."""
    h = host.lower().rstrip(".")
    if not h:
        return True
    if h in _LOCAL_NAMES or h.endswith(_LOCAL_SUFFIXES):
        return True
    ip = host_ip(h)
    if ip is not None:
        return not ip.is_global
    if "." not in h:
        # Nom court (« nas », « router ») : résolu sur le réseau local.
        return True
    return False


def canonical_host(host: str) -> str:
    h = host.lower().rstrip(".")
    try:
        ip = host_ip(h)
    except InvalidParams:
        return h
    return str(ip) if ip is not None else h


def looks_like_host(text: str) -> bool:
    """« youtube.com », « fr.wikipedia.org/wiki/X », « localhost:8080 », « 10.0.0.1 »."""
    if any(c.isspace() for c in text):
        return False
    hostport = re.split(r"[/?#]", text, maxsplit=1)[0]
    if hostport.startswith("["):
        return "]" in hostport
    host, _, port = hostport.rpartition(":") if hostport.count(":") == 1 else (hostport, "", "")
    if port and not port.isdigit():
        return False
    host = host.lower().rstrip(".")
    if not host:
        return False
    if host in _LOCAL_NAMES:
        return True
    try:
        if _parse_legacy_ipv4(host) is not None:
            return True
    except InvalidParams:
        return True  # forme numérique invalide : traité comme URL, puis refusé
    labels = host.split(".")
    if len(labels) < 2:
        return False
    if not all(_HOST_LABEL_RE.match(l) for l in labels):
        return False
    tld = labels[-1]
    return len(tld) >= 2 and not tld.isdigit()


def check_url(url: str, allowed_local: list[str] | tuple[str, ...] = ()) -> str:
    """Valide une URL absolue http(s). Renvoie l'URL normalisée, ou lève."""
    if not isinstance(url, str):
        raise InvalidParams("URL invalide")
    url = url.strip()
    if not url or len(url) > MAX_URL_LEN:
        raise InvalidParams("URL vide ou trop longue")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in url) or " " in url:
        raise InvalidParams("URL contenant des caractères interdits")
    m = _SCHEME_RE.match(url)
    scheme = m.group(1).lower() if m else ""
    if scheme not in ALLOWED_SCHEMES:
        raise PermissionDenied(f"Schéma d'URL refusé: {scheme or '(aucun)'}:",
                               details={"scheme": scheme, "allowed": sorted(ALLOWED_SCHEMES)})
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port  # lève ValueError si port invalide
    except ValueError:
        raise InvalidParams("URL mal formée")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise PermissionDenied("Les identifiants dans l'URL (user@hôte) sont refusés")
    if "\\" in parts.netloc:
        raise InvalidParams("URL mal formée")
    host = parts.hostname or ""
    if not host:
        raise InvalidParams("URL sans nom d'hôte")
    canon = canonical_host(host)
    allowed = {canonical_host(a) for a in allowed_local if isinstance(a, str) and a}
    if canon not in allowed and is_local_host(host):
        raise PermissionDenied(
            "Adresse locale ou privée refusée : NEXUS Browser n'ouvre pas les pages du réseau local "
            "ni de cette machine.", details={"host": host})
    netloc = parts.netloc.lower() if parts.netloc.isascii() else parts.netloc
    path = parts.path
    return urllib.parse.urlunsplit((scheme, netloc, path, parts.query, parts.fragment))


def search_url(text: str, engine: str = DEFAULT_SEARCH_ENGINE) -> str:
    if not isinstance(engine, str) or not engine.startswith("https://"):
        raise InvalidParams("Moteur de recherche invalide (doit commencer par https://)")
    return engine + urllib.parse.quote_plus(text.strip())


def resolve_input(text: str, *, engine: str = DEFAULT_SEARCH_ENGINE,
                  allowed_local: list[str] | tuple[str, ...] = ()) -> tuple[str, str]:
    """Saisie de la barre d'adresse → (url, kind) où kind ∈ {"url", "search"}."""
    if not isinstance(text, str):
        raise InvalidParams("Saisie invalide")
    t = text.strip()
    if not t:
        raise InvalidParams("Saisie vide")
    if len(t) > MAX_URL_LEN:
        raise InvalidParams("Saisie trop longue")
    m = _SCHEME_RE.match(t)
    if m and not any(c.isspace() for c in t):
        scheme = m.group(1).lower()
        rest = t[m.end():]
        if scheme in ALLOWED_SCHEMES:
            return check_url(t, allowed_local), "url"
        # « hote:port/… » sans schéma (localhost:8080, example.com:8443)
        port = re.split(r"[/?#]", rest, maxsplit=1)[0]
        if not (port.isdigit() and looks_like_host(t)):
            raise PermissionDenied(f"Schéma d'URL refusé: {scheme}:",
                                   details={"scheme": scheme, "allowed": sorted(ALLOWED_SCHEMES)})
    elif m and m.group(1).lower() not in ALLOWED_SCHEMES and t[m.end():m.end() + 2] != "//" \
            and m.group(1).lower() in {"javascript", "data", "file", "vbscript", "about", "chrome",
                                       "view-source", "blob", "resource", "moz-extension",
                                       "chrome-extension"}:
        raise PermissionDenied(f"Schéma d'URL refusé: {m.group(1).lower()}:")
    if t.startswith("//"):
        t = t[2:]
    if looks_like_host(t):
        return check_url("https://" + t, allowed_local), "url"
    return search_url(t, engine), "search"
