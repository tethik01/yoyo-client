"""Voice tests — all offline. No microphone, no sound card, no model download.

The engines themselves cannot be tested without hardware and multi-hundred-MB weights, so
the boundary is drawn deliberately: everything that transforms data is pure and tested here,
and the parts that touch a device are isolated in `voice/mic.py` and the engine `_run`
methods. A test suite that needs a sound card is a test suite nobody runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from yoyo import voice
from yoyo.voice.base import Segment, Transcript, format_timestamp, looks_like_audio, speakable
from yoyo.voice.tts import SapiSpeaker

# ------------------------------------------------------------- timestamps ----


def test_timestamps_are_fixed_width_past_an_hour():
    assert format_timestamp(0) == "00:00:00"
    assert format_timestamp(61.4) == "00:01:01"
    assert format_timestamp(3725) == "01:02:05"


def test_negative_time_does_not_produce_nonsense():
    assert format_timestamp(-5) == "00:00:00"


def test_transcript_keeps_stamps_so_a_citation_points_at_a_moment():
    t = Transcript(
        text="one two",
        segments=[Segment(0.0, 2.0, " one "), Segment(122.0, 124.0, "two")],
    )
    assert t.with_timestamps() == "[00:00:00] one\n[00:02:02] two"


def test_empty_segments_fall_back_to_plain_text():
    assert Transcript(text="just text").with_timestamps() == "just text"


def test_blank_segments_are_dropped_not_rendered_as_empty_stamps():
    t = Transcript(text="x", segments=[Segment(0, 1, "  "), Segment(1, 2, "real")])
    assert t.with_timestamps() == "[00:00:01] real"


def test_realtime_factor_reports_faster_than_realtime():
    t = Transcript(text="x", duration_s=60.0, latency_ms=20_000)
    assert t.realtime_factor == pytest.approx(3.0)


def test_realtime_factor_is_zero_not_a_crash_when_nothing_was_timed():
    assert Transcript(text="x", duration_s=60.0, latency_ms=0).realtime_factor == 0.0


# ------------------------------------------------------------ speakable ------


def test_citations_are_not_read_aloud():
    """`[7]` spoken is "bracket seven bracket" mid-sentence. The text keeps them; the
    speech does not."""
    out = speakable("The bake-off concluded [7] that scaling is empirical [MyAIServer.md].")
    assert "[" not in out and "]" not in out
    assert "bake-off concluded" in out
    assert "scaling is empirical" in out


def test_markdown_emphasis_and_fences_are_stripped():
    out = speakable("**bold** and `code` and ```a fence```")
    for junk in ("**", "`", "```"):
        assert junk not in out


def test_table_pipes_do_not_become_a_stutter():
    assert "|" not in speakable("| a | b |\n| 1 | 2 |")


def test_long_text_is_cut_at_a_sentence_boundary():
    text = ("This is a sentence. " * 300).strip()
    out = speakable(text, max_chars=200)
    assert len(out) < 260
    assert out.endswith("truncated.")
    assert "sentenc." not in out  # never mid-word


def test_short_text_is_untouched_and_has_no_truncation_marker():
    assert speakable("Hello there.") == "Hello there."


def test_empty_input_is_safe():
    assert speakable("") == ""
    assert speakable(None) == ""  # type: ignore[arg-type]


# ------------------------------------------------------------ file typing ----


@pytest.mark.parametrize("name", ["a.wav", "b.MP3", "c.m4a", "d.opus"])
def test_audio_extensions_are_recognised(name):
    assert looks_like_audio(name)


@pytest.mark.parametrize("name", ["notes.md", "report.pdf", "data.csv"])
def test_non_audio_is_rejected_before_a_model_loads(name):
    assert not looks_like_audio(name)


# ----------------------------------------------------------------- config ----


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "yoyo-voice.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_missing_config_yields_working_defaults():
    """Voice must work before the user has written any config."""
    cfg = voice.load_config(Path("/nonexistent/yoyo-voice.yaml"))
    assert cfg.stt_engine == "whisper"
    assert cfg.tts_engine == "sapi"      # the engine that needs no download
    assert cfg.stt_vad_filter is True    # hallucination guard on by default


def test_config_values_are_read(tmp_path):
    path = _write(
        tmp_path,
        {
            "stt": {"engine": "whisper", "model": "medium", "device": "cpu", "language": "en"},
            "tts": {"engine": "piper", "model_path": "data/voice-models/v.onnx"},
            "mic": {"device": 3},
        },
    )
    cfg = voice.load_config(path)
    assert (cfg.stt_model, cfg.stt_device, cfg.stt_language) == ("medium", "cpu", "en")
    assert cfg.tts_engine == "piper"
    assert cfg.mic_device == 3


def test_unknown_stt_engine_is_rejected_loudly(tmp_path):
    """A typo must not silently fall back to the default — that hides a config error until
    someone wonders why their chosen engine never ran."""
    path = _write(tmp_path, {"stt": {"engine": "vosk"}})
    with pytest.raises(ValueError, match="vosk"):
        voice.load_config(path)


def test_unknown_tts_engine_is_rejected_loudly(tmp_path):
    path = _write(tmp_path, {"tts": {"engine": "elevenlabs"}})
    with pytest.raises(ValueError, match="elevenlabs"):
        voice.load_config(path)


def test_vad_filter_can_be_turned_off_explicitly(tmp_path):
    path = _write(tmp_path, {"stt": {"vad_filter": False}})
    assert voice.load_config(path).stt_vad_filter is False


# ---------------------------------------------------------------- engines ----


def test_transcriber_is_built_from_config_without_loading_a_model(tmp_path):
    """Constructing must not download or load anything — `yoyo voice status` calls this."""
    cfg = voice.load_config(_write(tmp_path, {"stt": {"model": "base", "device": "cpu"}}))
    engine = voice.get_transcriber(cfg)
    assert engine.model_name == "base"
    assert engine._model is None


def test_model_override_beats_config(tmp_path):
    cfg = voice.load_config(_write(tmp_path, {"stt": {"model": "small"}}))
    assert voice.get_transcriber(cfg, model="large-v3").model_name == "large-v3"


def test_models_are_cached_under_data_never_temp():
    """Learned with fastembed: Windows cleans %TEMP%, so a cached model silently
    re-downloads and a fast command mysteriously becomes a slow one."""
    cache = voice.model_cache_dir()
    assert cache.name == "voice-models"
    assert "temp" not in str(cache).lower()


def test_speaker_engine_selection_is_explicit(tmp_path):
    cfg = voice.load_config(_write(tmp_path, {"tts": {"engine": "sapi"}}))
    assert voice.get_speaker(cfg).name == "sapi"
    assert voice.get_speaker(cfg, engine="piper").name == "piper"


def test_piper_without_a_model_is_unavailable_rather_than_crashing(tmp_path):
    cfg = voice.load_config(_write(tmp_path, {"tts": {"engine": "piper"}}))
    assert voice.get_speaker(cfg).is_available() is False


def test_piper_reports_a_missing_model_with_a_fix(tmp_path):
    from yoyo.voice.tts import PiperSpeaker

    speaker = PiperSpeaker(model_path=tmp_path / "absent.onnx")
    with pytest.raises(voice.EngineUnavailable, match="not found"):
        speaker.synthesise("hello", str(tmp_path / "out.wav"))


def test_status_reports_every_component_without_hardware():
    rows = voice.status()
    assert {r["component"] for r in rows} == {"STT", "TTS", "microphone"}
    for row in rows:
        assert isinstance(row["available"], bool)
        if not row["available"]:
            assert row["hint"] or row["detail"], f"{row['component']} gives no way forward"


# ------------------------------------------------------------- shell safety ---


def test_sapi_quoting_neutralises_an_embedded_quote():
    """User text becomes part of a PowerShell command line. A single quote must terminate
    nothing — the doubling rule is the whole defence."""
    assert SapiSpeaker._quote("it's fine") == "'it''s fine'"


def test_sapi_quoting_does_not_let_text_become_a_command():
    hostile = "'; Remove-Item C:\\ -Recurse; '"
    quoted = SapiSpeaker._quote(hostile)
    assert quoted.startswith("'") and quoted.endswith("'")
    # Every inner quote is doubled, so no odd number of quotes can close the literal.
    assert quoted[1:-1].count("'") % 2 == 0


def test_sapi_quoting_handles_none():
    assert SapiSpeaker._quote(None) == "''"  # type: ignore[arg-type]


# ------------------------------------------------------------------ policy ----


def test_no_voice_module_sends_audio_over_the_network():
    """Structural guard on the boundary in base.py: audio is the most sensitive input Yoyo
    handles and egress is unaudited (OQ5). If an engine ever needs the network it must be a
    separate, explicitly opted-in provider — not a quiet import here."""
    import yoyo.voice as pkg

    root = Path(pkg.__file__).parent
    banned = ("httpx", "requests", "urllib.request", "aiohttp")
    for path in root.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for name in banned:
            assert f"import {name}" not in source, f"{path.name} imports {name}"
