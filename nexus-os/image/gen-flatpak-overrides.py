#!/usr/bin/python3 -I
"""Build-time generator: read-only access to the NEXUS ROM folder for catalogue emulators.

Run inside the image build (see Containerfile). For every emulator of
nexus/modules/emulation/catalog.py that has a Flathub id and emulates at least one
system (RetroDECK manages its own library and is skipped), it writes:

  /usr/share/nexus/flatpak-overrides/<app-id>      keyfile, Flatpak override syntax
  /usr/lib/tmpfiles.d/nexus-flatpak-overrides.conf copies each file to
      /var/lib/flatpak/overrides/<app-id> (system-installation overrides) at boot,
      only if no override exists yet for that app (tmpfiles `C`: never overwrites
      an administrator's own override).

Only `~/Games/ROMs:ro` is granted: BIOS/firmware/saves live in each emulator's own
~/.var/app/<id> sandbox (see catalog `bios_dir`), so no extra write access is needed.

    gen-flatpak-overrides.py <root>   (root = / in the build, a temp dir in tests)
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, "/usr/lib/nexus")
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+){2,}$")
ROM_FS = "~/Games/ROMs:ro"


def main(root: str) -> int:
    from nexus.modules.emulation.catalog import EMULATORS

    r = Path(root)
    src = r / "usr/share/nexus/flatpak-overrides"
    src.mkdir(parents=True, exist_ok=True)
    lines = ["# Généré à la construction de l'image NEXUS OS (image/gen-flatpak-overrides.py).",
             "# Accès en lecture seule au dossier ROMs NEXUS pour les émulateurs Flatpak du catalogue.",
             "d /var/lib/flatpak/overrides 0755 root root -"]
    ids = sorted({e["flatpak_id"] for e in EMULATORS.values() if e.get("flatpak_id") and e.get("systems")})
    for app_id in ids:
        if not ID_RE.match(app_id):
            raise SystemExit(f"identifiant Flatpak invalide dans le catalogue: {app_id!r}")
        (src / app_id).write_text(f"[Context]\nfilesystems={ROM_FS};\n")
        lines.append(f"C /var/lib/flatpak/overrides/{app_id} 0644 root root - /usr/share/nexus/flatpak-overrides/{app_id}")
    conf = r / "usr/lib/tmpfiles.d/nexus-flatpak-overrides.conf"
    conf.parent.mkdir(parents=True, exist_ok=True)
    conf.write_text("\n".join(lines) + "\n")
    print(f"{len(ids)} overrides: {', '.join(ids)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/"))
