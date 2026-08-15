"""Microphone capture for push-to-talk.

Isolated in its own module because it is the one part of the voice stack that cannot be
tested without hardware. Everything above it takes PCM bytes and is testable offline; the
sound card stops here.

16 kHz mono 16-bit is not a preference — it is what Whisper resamples to internally.
Capturing at 44.1 kHz and letting the engine downsample wastes bandwidth and adds a
resampling step for no accuracy gain.
"""

from __future__ import annotations

import logging
import queue
import time

from .base import AudioDeviceError

log = logging.getLogger(__name__)

SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "int16"

#: Below this, treat the capture as silence rather than sending it to the model. Whisper
#: hallucinates confident sentences from near-silent audio — a documented failure mode, and
#: precisely the kind this project refuses to ship. Cheaper to say "I heard nothing".
MIN_CAPTURE_S = 0.4
#: A hard stop so a stuck key cannot record until the disk fills.
MAX_CAPTURE_S = 300


def _sounddevice():  # noqa: ANN202
    try:
        import sounddevice
    except (ImportError, OSError) as exc:
        # OSError too: on Windows sounddevice imports fine but raises when PortAudio's DLL
        # is missing, which reads as a crash rather than a missing dependency.
        raise AudioDeviceError(
            "Microphone capture needs sounddevice (and PortAudio). Run: "
            'uv pip install -e ".[voice]"'
        ) from exc
    return sounddevice


def list_devices() -> list[dict]:
    """Input devices only. Windows lists every output as a device too, and a user picking
    from that list will eventually pick a speaker and wonder why nothing records."""
    sd = _sounddevice()
    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev.get("max_input_channels", 0) > 0:
            out.append(
                {
                    "index": idx,
                    "name": dev.get("name", "?"),
                    "channels": dev.get("max_input_channels"),
                    "default_samplerate": int(dev.get("default_samplerate") or 0),
                }
            )
    return out


def default_device() -> dict | None:
    devices = list_devices()
    if not devices:
        return None
    sd = _sounddevice()
    try:
        default_index = sd.default.device[0]
    except Exception:  # noqa: BLE001
        default_index = None
    for d in devices:
        if d["index"] == default_index:
            return d
    return devices[0]


class Recorder:
    """Records until `stop()` is called or the hard cap is hit.

    A context manager rather than a record(seconds) call because push-to-talk length is
    decided by the human holding the key, not by us guessing.
    """

    def __init__(self, device: int | None = None, sample_rate: int = SAMPLE_RATE) -> None:
        self.device = device
        self.sample_rate = sample_rate
        self._chunks: list[bytes] = []
        self._queue: queue.Queue[bytes] = queue.Queue()
        self._stream = None
        self._started = 0.0

    def __enter__(self) -> Recorder:
        sd = _sounddevice()

        def callback(indata, frames, time_info, status) -> None:  # noqa: ANN001, ARG001
            if status:
                # Overflows mean dropped audio. Surfacing it matters: a transcript with a
                # silently missing sentence is worse than one you know is incomplete.
                log.warning("audio input status: %s", status)
            self._queue.put(bytes(indata))

        try:
            self._stream = sd.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=0,
                device=self.device,
                channels=CHANNELS,
                dtype=DTYPE,
                callback=callback,
            )
            self._stream.start()
        except Exception as exc:  # noqa: BLE001
            raise AudioDeviceError(
                f"could not open the microphone: {exc}. "
                f"Run `yoyo voice devices` to see what is available."
            ) from exc

        self._started = time.monotonic()
        return self

    def drain(self) -> float:
        """Move queued audio into the buffer. Returns seconds captured so far, so a caller
        can render a live duration without reaching into internals."""
        while True:
            try:
                self._chunks.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return self.seconds

    @property
    def seconds(self) -> float:
        total_bytes = sum(len(c) for c in self._chunks)
        return total_bytes / (self.sample_rate * 2 * CHANNELS)

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self._started) > MAX_CAPTURE_S

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                log.debug("error closing the audio stream", exc_info=True)
        self.drain()

    @property
    def pcm(self) -> bytes:
        return b"".join(self._chunks)

    @property
    def too_short(self) -> bool:
        return self.seconds < MIN_CAPTURE_S
