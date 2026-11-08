"""Offline voice: microphone capture (arecord / pw-record), whisper.cpp STT, Piper TTS.

All commands go through ctx.runner (allow-listed argv, no shell). Models are
provided by the user (official Hugging Face repositories, see docs/NEXUS_AI.md);
NEXUS does not download them automatically because it cannot pin their SHA256.
"""

from __future__ import annotations

import re
import secrets
import struct
from pathlib import Path

from ...core.errors import CommandFailed, InvalidParams, Unavailable

WHISPER_HELP = ("Modèle Whisper absent. Téléchargez ggml-base.bin (rapide) ou ggml-small.bin (plus précis) "
                "depuis le dépôt officiel https://huggingface.co/ggerganov/whisper.cpp (onglet Files, puis "
                "« download ») et placez-le dans {models}/ ou indiquez son chemin dans Paramètres > NEXUS AI.")
PIPER_HELP = ("Voix Piper absente. Téléchargez une voix française depuis le dépôt officiel "
              "https://huggingface.co/rhasspy/piper-voices (dossier fr/fr_FR/, par ex. siwis/medium) : le fichier "
              "fr_FR-*.onnx ET son fichier .onnx.json, à placer dans {models}/.")
WHISPER_BIN_HELP = ("whisper.cpp (whisper-cli) est absent. Installez-le (paquet whisper-cpp ou compilation depuis "
                    "https://github.com/ggml-org/whisper.cpp) puis relancez NEXUS.")
PIPER_BIN_HELP = "Piper (piper / piper-tts) est absent. Installez-le depuis https://github.com/rhasspy/piper."
RECORDER_HELP = "Aucun outil d'enregistrement (arecord ou pw-record) n'est disponible."
PLAYER_HELP = "Aucun lecteur audio (pw-play, paplay ou aplay) n'est disponible."

LANG_VOICE = {"fr": "fr_FR", "en": "en_US"}


class Voice:
    def __init__(self, runner, models_dir: Path, runtime_dir: Path, settings):
        self.runner = runner
        self.models_dir = models_dir
        self.runtime_dir = runtime_dir
        self.settings = settings

    # ----------------------------------------------------------------- discovery
    def lang(self) -> str:
        lang = str(self.settings.get("language") or "fr")[:2]
        return lang if lang in LANG_VOICE else "fr"

    def recorder(self) -> str | None:
        # arecord first: it has an exact duration option (-d). pw-record has none, so it is
        # bounded by the runner timeout and the WAV header is repaired afterwards.
        for b in ("arecord", "pw-record"):
            if self.runner.available(b):
                return b
        return None

    def player(self) -> str | None:
        return next((b for b in ("pw-play", "paplay", "aplay") if self.runner.available(b)), None)

    def piper_bin(self) -> str | None:
        return next((b for b in ("piper", "piper-tts") if self.runner.available(b)), None)

    def whisper_model(self) -> Path | None:
        custom = self.settings.get("ai.whisper_model")
        if custom:
            p = Path(custom).expanduser()
            return p if p.is_file() else None
        for n in ("ggml-small.bin", "ggml-base.bin", "ggml-small-q5_1.bin", "ggml-base-q5_1.bin"):
            p = self.models_dir / n
            if p.is_file():
                return p
        return None

    def piper_voice(self) -> Path | None:
        custom = self.settings.get("ai.piper_voice")
        if custom:
            p = Path(custom).expanduser()
            return p if p.is_file() else None
        prefix = LANG_VOICE[self.lang()]
        found = sorted(self.models_dir.glob(f"{prefix}-*.onnx")) if self.models_dir.is_dir() else []
        return found[0] if found else None

    def status(self) -> dict:
        wm, pv = self.whisper_model(), self.piper_voice()
        return {
            "enabled": bool(self.settings.get("ai.voice_enabled", True)),
            "recorder": self.recorder(), "player": self.player(),
            "whisper": self.runner.available("whisper-cli"), "whisper_model": wm.name if wm else None,
            "piper": self.piper_bin(), "piper_voice": pv.name if pv else None,
            "models_dir": str(self.models_dir),
            "listen_ready": bool(self.recorder() and self.runner.available("whisper-cli") and wm),
            "speak_ready": bool(self.piper_bin() and pv and self.player()),
        }

    # ----------------------------------------------------------------- STT
    def _require_enabled(self):
        if not self.settings.get("ai.voice_enabled", True):
            raise Unavailable("La voix est désactivée (Paramètres > NEXUS AI).")

    def transcribe_microphone(self, seconds: int) -> str:
        self._require_enabled()
        if not isinstance(seconds, int) or not 1 <= seconds <= 15:
            raise InvalidParams("La durée d'écoute doit être comprise entre 1 et 15 secondes.")
        rec = self.recorder()
        if not rec:
            raise Unavailable(RECORDER_HELP, details={"binary": "arecord|pw-record"})
        if not self.runner.available("whisper-cli"):
            raise Unavailable(WHISPER_BIN_HELP, details={"binary": "whisper-cli"})
        model = self.whisper_model()
        if not model:
            raise Unavailable(WHISPER_HELP.format(models=self.models_dir), details={"models_dir": str(self.models_dir)})
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        wav = self.runtime_dir / f"ai-listen-{secrets.token_hex(6)}.wav"
        try:
            if rec == "arecord":
                self.runner.run(["arecord", "-q", "-f", "S16_LE", "-r", "16000", "-c", "1", "-d", str(seconds),
                                 "-t", "wav", str(wav)], timeout=seconds + 10, check=True)
            else:
                try:
                    self.runner.run(["pw-record", "--rate", "16000", "--channels", "1", "--format", "s16", str(wav)],
                                    timeout=seconds)
                except CommandFailed:
                    pass  # stopped by the timeout: expected for pw-record (no duration option)
                repair_wav(wav)
            if not wav.exists() or wav.stat().st_size <= 44:
                raise CommandFailed("Aucun son n'a été enregistré (micro absent ou coupé ?).")
            r = self.runner.run(["whisper-cli", "-m", str(model), "-f", str(wav), "-l", self.lang(), "-nt"],
                                timeout=120, check=True)
            return clean_transcript(r.stdout)
        finally:
            wav.unlink(missing_ok=True)

    # ----------------------------------------------------------------- TTS
    def speak(self, text: str) -> dict:
        self._require_enabled()
        text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text or "").strip()
        if not text:
            raise InvalidParams("Texte vide.")
        if len(text) > 2000:
            text = text[:2000]
        piper = self.piper_bin()
        if not piper:
            raise Unavailable(PIPER_BIN_HELP, details={"binary": "piper"})
        voice = self.piper_voice()
        if not voice:
            raise Unavailable(PIPER_HELP.format(models=self.models_dir), details={"models_dir": str(self.models_dir)})
        player = self.player()
        if not player:
            raise Unavailable(PLAYER_HELP, details={"binary": "pw-play"})
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        wav = self.runtime_dir / f"ai-speak-{secrets.token_hex(6)}.wav"
        try:
            self.runner.run([piper, "--model", str(voice), "--output_file", str(wav)], input=text, timeout=120,
                            check=True)
            self.runner.run([player, str(wav)], timeout=300, check=True)
        finally:
            wav.unlink(missing_ok=True)
        return {"spoken": True, "chars": len(text), "voice": voice.name}


def clean_transcript(stdout: str) -> str:
    lines = []
    for line in (stdout or "").splitlines():
        line = re.sub(r"^\[[^\]]*-->[^\]]*\]\s*", "", line.strip())  # timestamps, if any
        line = re.sub(r"\[(?:BLANK_AUDIO|MUSIC|Musique|silence|SOUND)[^\]]*\]|\((?:musique|music)[^)]*\)", "", line,
                      flags=re.I).strip()
        if line:
            lines.append(line)
    return " ".join(lines).strip()


def repair_wav(path: Path) -> None:
    """Fix RIFF/data sizes of a WAV whose writer was killed before finalising its header."""
    try:
        data = bytearray(path.read_bytes())
    except OSError:
        return
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return
    i = 12
    while i + 8 <= len(data):
        cid, size = data[i:i + 4], struct.unpack_from("<I", data, i + 4)[0]
        if cid == b"data":
            real = len(data) - (i + 8)
            if size != real:
                struct.pack_into("<I", data, i + 4, real)
                struct.pack_into("<I", data, 4, len(data) - 8)
                path.write_bytes(bytes(data))
            return
        if size <= 0 or i + 8 + size > len(data):
            return
        i += 8 + size + (size & 1)
