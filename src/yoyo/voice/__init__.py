"""Voice: speech in, speech out, entirely on this laptop.

Config lives in `yoyo-voice.yaml`. Engines are chosen there and resolved here, so nothing
above this package names an engine — the same indirection `yoyo-models.yaml` gives roles.

ADR-021 voided the box-side voice plan (CPU-pinned containers on the GB10's efficiency
cores). This is the replacement: local engines on the laptop, no audio over the wire.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from ..config import REPO_ROOT, get_settings
from .base import (
    AudioDeviceError,
    EngineUnavailable,
    Segment,
    Speaker,
    Transcriber,
    Transcript,
    VoiceError,
    format_timestamp,
    looks_like_audio,
    speakable,
)

log = logging.getLogger(__name__)

CONFIG_FILE = REPO_ROOT / "yoyo-voice.yaml"

__all__ = [
    "AudioDeviceError",
    "EngineUnavailable",
    "Segment",
    "Speaker",
    "Transcriber",
    "Transcript",
    "VoiceError",
    "VoiceConfig",
    "format_timestamp",
    "looks_like_audio",
    "speakable",
    "load_config",
    "get_transcriber",
    "get_speaker",
    "model_cache_dir",
    "status",
]


@dataclass
class VoiceConfig:
    stt_engine: str = "whisper"
    stt_model: str = "small"
    stt_device: str = "auto"
    stt_compute_type: str | None = None
    stt_language: str | None = None
    stt_beam_size: int = 5
    stt_vad_filter: bool = True

    tts_engine: str = "sapi"
    tts_model_path: str | None = None
    tts_binary: str | None = None

    mic_device: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def model_cache_dir() -> Path:
    """Under data/, never %TEMP%.

    Learned the hard way with fastembed: Windows cleans %TEMP%, so a cached model silently
    re-downloads and a fast command becomes a slow one for no visible reason.
    """
    path = get_settings().data_dir / "voice-models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_config(path: Path | None = None) -> VoiceConfig:
    path = path or CONFIG_FILE
    if not path.exists():
        return VoiceConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    stt = raw.get("stt") or {}
    tts = raw.get("tts") or {}
    mic = raw.get("mic") or {}

    engine = stt.get("engine", "whisper")
    if engine != "whisper":
        raise ValueError(
            f"unknown stt.engine {engine!r}. Only 'whisper' is implemented — adding one "
            f"means a new adapter in voice/, not a config value."
        )
    tts_engine = tts.get("engine", "sapi")
    if tts_engine not in {"piper", "sapi"}:
        raise ValueError(f"unknown tts.engine {tts_engine!r}. Expected 'piper' or 'sapi'.")

    return VoiceConfig(
        stt_engine=engine,
        stt_model=stt.get("model", "small"),
        stt_device=stt.get("device", "auto"),
        stt_compute_type=stt.get("compute_type"),
        stt_language=stt.get("language"),
        stt_beam_size=int(stt.get("beam_size", 5)),
        stt_vad_filter=bool(stt.get("vad_filter", True)),
        tts_engine=tts_engine,
        tts_model_path=tts.get("model_path"),
        tts_binary=tts.get("binary"),
        mic_device=mic.get("device"),
        extra={k: v for k, v in raw.items() if k not in {"stt", "tts", "mic"}},
    )


def get_transcriber(config: VoiceConfig | None = None, model: str | None = None):  # noqa: ANN201
    cfg = config or load_config()
    from .whisper import WhisperTranscriber

    return WhisperTranscriber(
        model=model or cfg.stt_model,
        device=cfg.stt_device,
        compute_type=cfg.stt_compute_type,
        cache_dir=model_cache_dir(),
        beam_size=cfg.stt_beam_size,
        vad_filter=cfg.stt_vad_filter,
    )


def get_speaker(config: VoiceConfig | None = None, engine: str | None = None):  # noqa: ANN201
    cfg = config or load_config()
    name = engine or cfg.tts_engine

    if name == "piper":
        from .tts import PiperSpeaker

        model_path = Path(cfg.tts_model_path) if cfg.tts_model_path else None
        if model_path and not model_path.is_absolute():
            model_path = REPO_ROOT / model_path
        return PiperSpeaker(model_path=model_path, binary=cfg.tts_binary)

    from .tts import SapiSpeaker

    return SapiSpeaker()


def status() -> list[dict[str, Any]]:
    """What is configured and what actually works. Backs `yoyo voice status`.

    Deliberately reports availability by *asking the engine*, not by reading config. A
    config file saying `engine: piper` proves nothing about whether Piper is installed.
    """
    cfg = load_config()
    rows: list[dict[str, Any]] = []

    stt = get_transcriber(cfg)
    rows.append(
        {
            "component": "STT",
            "engine": stt.name,
            "detail": f"model={cfg.stt_model} device={cfg.stt_device}",
            "available": stt.is_available(),
            "hint": "" if stt.is_available() else 'uv pip install -e ".[voice]"',
        }
    )

    tts = get_speaker(cfg)
    hint = ""
    if not tts.is_available():
        hint = (
            "set tts.model_path to a Piper .onnx voice"
            if cfg.tts_engine == "piper"
            else "SAPI needs Windows PowerShell"
        )
    rows.append(
        {
            "component": "TTS",
            "engine": tts.name,
            "detail": cfg.tts_model_path or "built-in voice",
            "available": tts.is_available(),
            "hint": hint,
        }
    )

    try:
        from .mic import default_device, list_devices

        devices = list_devices()
        chosen = default_device()
        rows.append(
            {
                "component": "microphone",
                "engine": "sounddevice",
                "detail": (
                    f"{len(devices)} input device(s), default: {chosen['name']}"
                    if chosen
                    else "no input devices found"
                ),
                "available": bool(devices),
                "hint": "" if devices else "no microphone detected",
            }
        )
    except AudioDeviceError as exc:
        rows.append(
            {
                "component": "microphone",
                "engine": "sounddevice",
                "detail": str(exc)[:80],
                "available": False,
                "hint": 'uv pip install -e ".[voice]"',
            }
        )

    return rows
