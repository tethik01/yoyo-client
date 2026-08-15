"""TTS: Piper (good, needs a model) and Windows SAPI (mediocre, needs nothing).

Two engines because the alternative is a feature that does not work until you download a
voice model. SAPI is built into Windows, sounds like a satnav, and works the moment you
type `yoyo say`. Piper is a small neural TTS that sounds close to natural and needs one
`.onnx` file. Config picks; SAPI is the fallback so the command is never dead on arrival.

Both run locally. Nothing is sent anywhere — see `base.py`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import EngineUnavailable, VoiceError, speakable

log = logging.getLogger(__name__)


class PiperSpeaker:
    """Neural TTS via the `piper` binary or the `piper-tts` Python package.

    The binary is preferred when present: it is what Piper's own docs distribute, and the
    Python package's API has moved between releases while the CLI has not.
    """

    name = "piper"

    def __init__(self, model_path: Path | None = None, binary: str | None = None) -> None:
        self.model_path = Path(model_path) if model_path else None
        self.binary = binary or shutil.which("piper")

    def is_available(self) -> bool:
        if not self.model_path or not self.model_path.exists():
            return False
        if self.binary:
            return True
        try:
            import piper  # noqa: F401

            return True
        except ImportError:
            return False

    def _require(self) -> None:
        if not self.model_path:
            raise EngineUnavailable(
                "No Piper voice model configured. Set tts.model_path in yoyo-voice.yaml, "
                "or set tts.engine: sapi to use the built-in Windows voice."
            )
        if not self.model_path.exists():
            raise EngineUnavailable(
                f"Piper voice model not found at {self.model_path}. Download a .onnx voice "
                f"from the Piper releases and point tts.model_path at it."
            )
        if not self.is_available():
            raise EngineUnavailable(
                "Piper is not installed. Either put the `piper` binary on PATH or run: "
                'uv pip install -e ".[voice]"'
            )

    def synthesise(self, text: str, out_path: str) -> str:
        self._require()
        clean = speakable(text)
        if not clean:
            raise VoiceError("nothing to speak — the text was empty after cleanup")

        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        if self.binary:
            proc = subprocess.run(  # noqa: S603
                [self.binary, "--model", str(self.model_path), "--output_file", str(out)],
                input=clean.encode("utf-8"),
                capture_output=True,
                timeout=300,
                check=False,
            )
            if proc.returncode != 0:
                raise VoiceError(
                    f"piper failed ({proc.returncode}): "
                    f"{proc.stderr.decode('utf-8', 'replace')[:300]}"
                )
            return str(out)

        import wave

        from piper import PiperVoice  # type: ignore[import-not-found]

        voice = PiperVoice.load(str(self.model_path))
        with wave.open(str(out), "wb") as wav:
            voice.synthesize(clean, wav)
        return str(out)

    def speak(self, text: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "speech.wav"
            self.synthesise(text, str(wav))
            play_wav(str(wav))


class SapiSpeaker:
    """Windows' built-in voice. No install, no model, no download.

    Driven through PowerShell's `System.Speech` rather than a Python COM binding so it has
    no dependency at all — the point of this engine is that it always works.
    """

    name = "sapi"

    def is_available(self) -> bool:
        import sys

        return sys.platform == "win32" and shutil.which("powershell") is not None

    def _script(self, body: str) -> None:
        if not self.is_available():
            raise EngineUnavailable(
                "The SAPI voice is Windows-only. On another platform, configure Piper "
                "(tts.engine: piper) with a voice model."
            )
        proc = subprocess.run(  # noqa: S603
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", body],
            capture_output=True,
            timeout=300,
            check=False,
        )
        if proc.returncode != 0:
            raise VoiceError(
                f"SAPI failed ({proc.returncode}): "
                f"{proc.stderr.decode('utf-8', 'replace')[:300]}"
            )

    @staticmethod
    def _quote(text: str) -> str:
        """PowerShell single-quoted strings escape a quote by doubling it. Nothing else is
        special inside them, which is why this is a single-quoted literal and not an
        interpolating double-quoted one — user text must never become PowerShell."""
        return "'" + (text or "").replace("'", "''") + "'"

    def synthesise(self, text: str, out_path: str) -> str:
        clean = speakable(text)
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        self._script(
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.SetOutputToWaveFile({self._quote(str(out))}); "
            f"$s.Speak({self._quote(clean)}); $s.Dispose()"
        )
        return str(out)

    def speak(self, text: str) -> None:
        clean = speakable(text)
        if not clean:
            return
        self._script(
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Speak({self._quote(clean)}); $s.Dispose()"
        )


def play_wav(path: str) -> None:
    """Play a wav file on whatever this platform offers."""
    import sys

    if sys.platform == "win32":
        subprocess.run(  # noqa: S603
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                f"(New-Object Media.SoundPlayer '{path}').PlaySync()",
            ],
            capture_output=True,
            timeout=600,
            check=False,
        )
        return

    for player in ("aplay", "afplay", "paplay"):
        exe = shutil.which(player)
        if exe:
            subprocess.run([exe, path], capture_output=True, timeout=600, check=False)  # noqa: S603
            return
    raise VoiceError(f"no audio player found; the file is at {path}")
