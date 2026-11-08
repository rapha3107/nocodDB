"""NEXUS Browser — lance un navigateur externe (Firefox Flatpak) en plein écran.

NEXUS Home tourne lui-même dans un navigateur kiosque ; la navigation web générale
se fait dans un navigateur séparé lancé en mode kiosque. La navigation à la manette
dans ce navigateur externe dépend de Steam Input / du mappage manette→souris
(module `hardware`) : Firefox n'interprète pas nativement la manette pour naviguer.
"""

from .module import BrowserModule

MODULE = BrowserModule

__all__ = ["BrowserModule", "MODULE"]
