"""STT via faster-whisper (CTranslate2). Runs on this laptop, CPU or CUDA.

Why faster-whisper rather than openai-whisper: same weights, CTranslate2 runtime, roughly
4x faster on CPU with int8 quantisation and a fraction of the memory. On a laptop without a
usable GPU that is the difference between "transcribes a meeting while you make coffee" and
"transcribes a meeting while you have lunch".

Why not the MyAIServer box: audio would have to cross the tailnet, and ADR-021 voided the
box-side voice plan anyway. See `base.py` for the full argument — briefly, audio is the most
sensitive input Yoyo handles and egress is unaudited (OQ5).

**Model size is a real trade-off, not a default to accept blindly.** `base` is fast and
mangles proper nouns; `small` is the usual sweet spot; `large-v3` is accurate and slow.
Yoyo's corpus is full of proper nouns an assistant must get right — "Qdrant", "LiteLLM",
"Tailscale" — so the config default is `small` and the honest answer is to measure it on
your own audio with `yoyo transcribe --model`.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from .base import EngineUnavailable, Segment, Transcript, VoiceError

log = logging.getLogger(__name__)

#: int8 on CPU, float16 on CUDA. Anything else is slower for no accuracy that survives
#: a listening test on dictation-quality audio.
_DEFAULT_COMPUTE = {"cpu": "int8", "cuda": "float16"}


class WhisperTranscriber:
    name = "faster-whisper"

    def __init__(
        self,
        model: str = "small",
        device: str = "auto",
        compute_type: str | None = None,
        cache_dir: Path | None = None,
        beam_size: int = 5,
        vad_filter: bool = True,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.beam_size = beam_size
        # Whisper hallucinates fluent sentences during silence — a known failure mode, and
        # exactly the kind this project treats as unacceptable. VAD trims non-speech before
        # the model sees it, which is the cheapest mitigation available.
        self.vad_filter = vad_filter
        self.cache_dir = cache_dir
        self._model = None

    # -- lifecycle ------------------------------------------------------------

    def is_available(self) -> bool:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return False
        return True

    def _resolve_device(self) -> tuple[str, str]:
        device = self.device
        if device == "auto":
            device = "cuda" if self._cuda_works() else "cpu"
        compute = self.compute_type or _DEFAULT_COMPUTE.get(device, "int8")
        return device, compute

    @staticmethod
    def _cuda_works() -> bool:
        """Presence of CUDA is not the same as a working CTranslate2 CUDA build — the
        common Windows failure is a torch install that sees the GPU while CTranslate2
        cannot use it. Falling back to CPU is always correct; crashing is not."""
        try:
            import ctranslate2

            return ctranslate2.get_cuda_device_count() > 0
        except Exception:  # noqa: BLE001
            return False

    def load(self):  # noqa: ANN201
        """Loaded lazily and cached. The model is hundreds of MB and most Yoyo commands
        never touch voice — paying that on every `yoyo ask` would be indefensible."""
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise EngineUnavailable(
                "faster-whisper is not installed. Run: "
                'uv pip install -e ".[voice]"'
            ) from exc

        device, compute = self._resolve_device()
        log.info(
            "loading whisper %s on %s (%s)%s",
            self.model_name,
            device,
            compute,
            f", cache={self.cache_dir}" if self.cache_dir else "",
        )
        started = time.monotonic()
        self._model = WhisperModel(
            self.model_name,
            device=device,
            compute_type=compute,
            # Same lesson as fastembed: %TEMP% is cleaned by Windows, so a cached model
            # silently re-downloads. Keep it under data/.
            download_root=str(self.cache_dir) if self.cache_dir else None,
        )
        log.info("whisper loaded in %.1fs", time.monotonic() - started)
        return self._model

    # -- transcription --------------------------------------------------------

    def transcribe(self, audio_path: str, language: str | None = None) -> Transcript:
        path = Path(audio_path)
        if not path.exists():
            raise VoiceError(f"no such audio file: {path}")
        if path.stat().st_size == 0:
            raise VoiceError(f"audio file is empty: {path}")
        return self._run(str(path), language)

    def transcribe_pcm(
        self, pcm: bytes, sample_rate: int, language: str | None = None
    ) -> Transcript:
        """Transcribe raw 16-bit mono PCM straight from the microphone.

        Written to a temp wav rather than passed as an array so the engine's own decoding
        path is used — one code path for files and mics means a bug found in one is fixed
        for both.
        """
        import tempfile
        import wave

        if not pcm:
            raise VoiceError("no audio captured — the microphone returned nothing")

        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "capture.wav"
            with wave.open(str(wav_path), "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(sample_rate)
                wav.writeframes(pcm)
            return self._run(str(wav_path), language)

    def _run(self, path: str, language: str | None) -> Transcript:
        model = self.load()
        started = time.monotonic()
        segments_iter, info = model.transcribe(
            path,
            language=language,
            beam_size=self.beam_size,
            vad_filter=self.vad_filter,
        )
        # faster-whisper yields lazily; nothing runs until this is consumed, so the timing
        # below would be meaningless without materialising the list first.
        segments = [
            Segment(start=float(s.start), end=float(s.end), text=s.text) for s in segments_iter
        ]
        elapsed_ms = int((time.monotonic() - started) * 1000)

        device, compute = self._resolve_device()
        return Transcript(
            text=" ".join(s.text.strip() for s in segments).strip(),
            segments=segments,
            language=getattr(info, "language", None),
            duration_s=float(getattr(info, "duration", 0.0) or 0.0),
            engine=f"{self.name} ({device}/{compute})",
            model=self.model_name,
            latency_ms=elapsed_ms,
        )
