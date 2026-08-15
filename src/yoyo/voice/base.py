"""Engine-neutral voice types and the adapter contract.

Same shape as `mail/base.py` and for the same reason: two engines with two vocabularies
would push engine branching up into the CLI and the agent. Consumers ask for "the STT
engine" and get a `Transcript` back regardless of what produced it.

**Voice runs entirely on this laptop.** No audio leaves the machine — not to MyAIServer,
not anywhere. That is a deliberate boundary, not an implementation accident: audio is the
most sensitive corpus Yoyo will ever touch (it captures people who never consented to being
recorded by an assistant), and the egress audit that ADR-009 promised does not exist on
Windows (OQ5). Sending audio over the tailnet would be adding an unaudited flow of the most
sensitive data type available. Any future engine that calls out MUST be a separate provider
with its own explicit opt-in.

**Timestamps are kept.** A transcript without them is a wall of text you cannot navigate.
With them, a 47-minute meeting becomes a searchable corpus where a citation points at a
moment you can actually go and listen to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


class VoiceError(RuntimeError):
    pass


class EngineUnavailable(VoiceError):
    """The engine's package or model is not installed. Recoverable with a pip install."""


class AudioDeviceError(VoiceError):
    """No usable microphone or speaker. Recoverable by picking a different device."""


def format_timestamp(seconds: float) -> str:
    """`HH:MM:SS`, always. Fixed width sorts and greps predictably, which a bare
    `M:SS` does not once a recording passes an hour."""
    seconds = max(0.0, seconds)
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


@dataclass(slots=True)
class Segment:
    """One contiguous run of speech."""

    start: float
    end: float
    text: str

    @property
    def stamp(self) -> str:
        return format_timestamp(self.start)

    def as_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "stamp": self.stamp, "text": self.text}


@dataclass(slots=True)
class Transcript:
    text: str
    segments: list[Segment] = field(default_factory=list)
    language: str | None = None
    duration_s: float = 0.0
    engine: str = ""
    model: str = ""
    latency_ms: int = 0

    @property
    def realtime_factor(self) -> float:
        """Audio seconds per wall-clock second. Below 1.0 means transcription is slower
        than listening to the recording, which rules the engine out for live use."""
        elapsed = self.latency_ms / 1000
        return self.duration_s / elapsed if elapsed > 0 else 0.0

    def with_timestamps(self) -> str:
        """Markdown-ish, one segment per line. This is what gets ingested — the stamps
        survive chunking, so a retrieved passage still says where in the audio it came
        from."""
        if not self.segments:
            return self.text
        return "\n".join(f"[{s.stamp}] {s.text.strip()}" for s in self.segments if s.text.strip())

    def as_dict(self, include_segments: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "text": self.text,
            "language": self.language,
            "duration_s": round(self.duration_s, 2),
            "engine": self.engine,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "realtime_factor": round(self.realtime_factor, 2),
        }
        if include_segments:
            out["segments"] = [s.as_dict() for s in self.segments]
        return out


class Transcriber(Protocol):
    """What every STT adapter must implement."""

    name: str

    def is_available(self) -> bool: ...
    def transcribe(self, audio_path: str, language: str | None = None) -> Transcript: ...
    def transcribe_pcm(
        self, pcm: bytes, sample_rate: int, language: str | None = None
    ) -> Transcript: ...


class Speaker(Protocol):
    """What every TTS adapter must implement.

    `synthesise` writes a file; `speak` plays immediately. Separated because writing a wav
    is testable offline and playing one is not — a test suite that needs a sound card is a
    test suite that does not run in CI.
    """

    name: str

    def is_available(self) -> bool: ...
    def synthesise(self, text: str, out_path: str) -> str: ...
    def speak(self, text: str) -> None: ...


# ------------------------------------------------------------------ helpers ---

#: Extensions worth trying. Not a guarantee — the engine decides — but enough to reject an
#: obvious mistake (a .pdf passed to `yoyo transcribe`) before loading a multi-GB model.
AUDIO_SUFFIXES = frozenset(
    {".wav", ".mp3", ".m4a", ".mp4", ".flac", ".ogg", ".opus", ".webm", ".aac", ".wma"}
)


def looks_like_audio(path: str) -> bool:
    from pathlib import Path

    return Path(path).suffix.lower() in AUDIO_SUFFIXES


_SPEAKABLE_STRIP = (
    ("```", " "),      # code fences read as literal backticks
    ("**", ""),
    ("__", ""),
    ("`", ""),
    ("#", ""),
    ("|", " "),        # table pipes become a stutter of "pipe pipe pipe"
)


def speakable(text: str, max_chars: int = 2000) -> str:
    """Strip markdown that a TTS engine would read aloud as punctuation noise.

    Yoyo's answers carry citations like `[7]` and `[MyAIServer.md]`. Spoken, those become
    "bracket seven bracket" in the middle of a sentence. They are removed from speech and
    kept in the printed text — the same answer, rendered for two different channels.
    """
    import re

    out = text or ""
    out = re.sub(r"\[[^\]\n]{1,80}\]", " ", out)      # citations
    for old, new in _SPEAKABLE_STRIP:
        out = out.replace(old, new)
    out = re.sub(r"^\s*[-*+]\s+", "", out, flags=re.M)  # bullet markers
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{2,}", "\n", out).strip()
    if len(out) > max_chars:
        # Cut at a sentence end so speech does not stop mid-word.
        cut = out.rfind(". ", 0, max_chars)
        out = out[: cut + 1] if cut > max_chars // 2 else out[:max_chars]
        out += " … truncated."
    return out
