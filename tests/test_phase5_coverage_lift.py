"""Phase 5 §Q coverage lift for the seven sub-85% Phase 4 modules.

Every test here exercises a real branch that the Phase 4 suite left uncovered —
no ``# pragma: no cover`` and no assertion weakened. Grouped by module:

* ``video/ffmpeg.py``  — find/start/timeout/exit error paths, version parse,
  duration/sample-rate probe failures, filtergraph argument spelling.
* ``video/probe.py``   — fraction parsing and ffprobe failure paths.
* ``media/sanitizer.py``— lexicon loading (explicit home, env home, missing
  file), substitution, and the rewrite validators.
* ``media/registry.py``— role/profile resolution errors, aspect/duration
  validation, ref capability.
* ``media/sheets.py``  — ``_pixel_dims_for`` and the per-image/per-second cost
  helpers across all pricing units.
* ``qc/visual.py``     — unreadable image, probe failure, no-audio and
  first-frame branches.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from studio.config.registry import (
    MediaSpec,
    ModelEntry,
    PerImage,
    PerMegapixel,
    PerSecond,
    PerVideo,
    ResolvedProfile,
)
from studio.domain.enums import Aspect
from studio.domain.errors import MediaConfigError, MediaToolError
from studio.media import registry as media_registry
from studio.media import sanitizer
from studio.media.sheets import _estimate_clip_cost, _estimate_image_cost, _pixel_dims_for
from studio.video import ffmpeg as ffmpeg_mod
from studio.video import probe as probe_mod


def _entry(pricing, **kwargs) -> ModelEntry:  # type: ignore[no-untyped-def]
    data = {
        "provider": "mock",
        "capability": "image_reference_generate",
        "model_id": "mock-image",
        "simulated": True,
        "pricing": pricing,
    }
    data.update(kwargs)
    return ModelEntry.model_validate(data)


# ======================================================================
# video/ffmpeg.py
# ======================================================================


def test_ffmpeg_find_binary_rejects_unknown_name():
    with pytest.raises(MediaToolError, match="unknown media binary"):
        ffmpeg_mod.find_binary("ffplay")


def test_ffmpeg_run_start_failure_is_media_tool_error(monkeypatch):
    async def boom(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError("no such file")

    monkeypatch.setattr(ffmpeg_mod.asyncio, "create_subprocess_exec", boom)
    with pytest.raises(MediaToolError, match="failed to start"):
        asyncio.run(ffmpeg_mod.run_ffmpeg(["-version"]))


def test_ffmpeg_run_timeout_kills_the_process(monkeypatch):
    class SlowProc:
        returncode = None

        def __init__(self) -> None:
            self.killed = False
            self.waited = False

        async def communicate(self) -> tuple[bytes, bytes]:
            await asyncio.sleep(5)
            return b"", b""

        def kill(self) -> None:
            self.killed = True

        async def wait(self) -> None:
            self.waited = True
            self.returncode = -9

    proc = SlowProc()

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        return proc

    monkeypatch.setattr(ffmpeg_mod.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(MediaToolError, match="timed out"):
        asyncio.run(ffmpeg_mod.run_ffmpeg(["-version"], timeout_s=0.01))
    assert proc.killed and proc.waited


def test_ffmpeg_run_nonzero_exit_carries_stderr_tail(monkeypatch):
    class FailingProc:
        returncode = 3

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b"x" * 900 + b"boom"

    async def fake_exec(*args, **kwargs):  # type: ignore[no-untyped-def]
        return FailingProc()

    monkeypatch.setattr(ffmpeg_mod.asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(MediaToolError) as excinfo:
        asyncio.run(ffmpeg_mod.run_ffprobe(["-version"]))
    message = str(excinfo.value)
    assert message.startswith("ffprobe exited with 3")
    assert message.endswith("boom")
    assert len(message) < 600, "stderr must be truncated"


def test_ffmpeg_parse_version_rejects_garbage():
    with pytest.raises(MediaToolError, match="could not parse ffmpeg version"):
        ffmpeg_mod.parse_version("this is not ffmpeg")


def test_ffmpeg_filtergraph_args_switch_on_major_version():
    assert ffmpeg_mod.filtergraph_script_args("f.txt", (7, 0, 0))[0] == "-/filter_complex"
    assert ffmpeg_mod.filtergraph_script_args("f.txt", (6, 1, 1))[0] == (
        "-filter_complex_script"
    )
    assert ffmpeg_mod.filtergraph_script_args("f.txt", (6, 0, 0)) == [
        "-filter_complex_script",
        "f.txt",
    ]


def test_ffmpeg_probe_duration_and_stream_errors(monkeypatch):
    async def bad_probe(args, *, timeout_s=60.0):  # type: ignore[no-untyped-def]
        return b"not json", b""

    monkeypatch.setattr(ffmpeg_mod, "run_ffprobe", bad_probe)
    with pytest.raises(MediaToolError, match="could not read duration"):
        asyncio.run(ffmpeg_mod.probe_duration_seconds("x.mp4"))
    with pytest.raises(MediaToolError, match="could not read stream info"):
        asyncio.run(ffmpeg_mod.probe_sample_rate_channels("x.mp4"))

    async def no_streams(args, *, timeout_s=60.0):  # type: ignore[no-untyped-def]
        return json.dumps({"streams": []}).encode(), b""

    monkeypatch.setattr(ffmpeg_mod, "run_ffprobe", no_streams)
    with pytest.raises(MediaToolError, match="could not read stream info"):
        asyncio.run(ffmpeg_mod.probe_sample_rate_channels("x.mp4"))


def test_ffmpeg_probe_duration_and_stream_success(monkeypatch):
    async def ok_duration(args, *, timeout_s=60.0):  # type: ignore[no-untyped-def]
        return json.dumps({"format": {"duration": "1.5"}}).encode(), b""

    monkeypatch.setattr(ffmpeg_mod, "run_ffprobe", ok_duration)
    assert asyncio.run(ffmpeg_mod.probe_duration_seconds("x.mp4")) == 1.5

    async def ok_stream(args, *, timeout_s=60.0):  # type: ignore[no-untyped-def]
        return json.dumps({"streams": [{"sample_rate": "48000", "channels": 2}]}).encode(), b""

    monkeypatch.setattr(ffmpeg_mod, "run_ffprobe", ok_stream)
    assert asyncio.run(ffmpeg_mod.probe_sample_rate_channels("x.mp4")) == (48000, 2)


def test_ffmpeg_version_caches_and_parses(monkeypatch):
    calls = {"n": 0}

    async def version(args, *, timeout_s=120.0):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return b"ffmpeg version 9.0.2 Copyright", b""

    monkeypatch.setattr(ffmpeg_mod, "run_ffmpeg", version)
    monkeypatch.setattr(ffmpeg_mod, "_version_cache", None)
    assert asyncio.run(ffmpeg_mod.ffmpeg_version()) == (9, 0, 2)
    assert asyncio.run(ffmpeg_mod.ffmpeg_version()) == (9, 0, 2)
    assert calls["n"] == 1
    monkeypatch.setattr(ffmpeg_mod, "_version_cache", None)


# ======================================================================
# video/probe.py
# ======================================================================


def test_probe_fraction_rejects_garbage_and_zero_denominator():
    assert probe_mod._parse_fraction("abc") is None
    assert probe_mod._parse_fraction("24") == 24.0
    assert probe_mod._parse_fraction("24000/1001") == pytest.approx(23.976, rel=1e-3)
    assert probe_mod._parse_fraction("24/0") is None


def test_probe_invalid_json_and_nonzero_exit(monkeypatch):
    class Proc:
        def __init__(self, returncode: int, stdout: bytes) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = b"broken"

    monkeypatch.setattr(
        probe_mod.subprocess, "run", lambda *a, **k: Proc(0, b"not json")
    )
    with pytest.raises(MediaToolError, match="invalid JSON"):
        probe_mod.probe(Path("whatever.mp4"))

    monkeypatch.setattr(probe_mod.subprocess, "run", lambda *a, **k: Proc(1, b""))
    with pytest.raises(MediaToolError, match="exited with 1"):
        probe_mod.probe(Path("whatever.mp4"))


def test_probe_image_classification_from_extension(monkeypatch):
    payload = {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "png",
                "width": 540,
                "height": 960,
                "nb_frames": "1",
                "avg_frame_rate": "25/1",
            }
        ],
        "format": {"duration": "0.040000"},
    }

    class Proc:
        returncode = 0
        stdout = json.dumps(payload).encode()
        stderr = b""

    monkeypatch.setattr(probe_mod.subprocess, "run", lambda *a, **k: Proc())
    result = probe_mod.probe(Path("still.png"))
    assert result.kind == "image"
    assert (result.width, result.height) == (540, 960)


# ======================================================================
# media/sanitizer.py
# ======================================================================


def test_sanitizer_lexicon_loading_paths(tmp_path, monkeypatch):
    explicit = tmp_path / "home"
    config_dir = explicit / "configs" / "prompts" / "media"
    config_dir.mkdir(parents=True)
    (config_dir / "sanitizer_lexicon.yaml").write_text(
        "lexicon:\n  Blood: crimson mist\n", encoding="utf-8"
    )
    assert sanitizer.load_lexicon(explicit) == {"blood": "crimson mist"}

    # Missing file is not an error: an empty lexicon means "no substitutions".
    assert sanitizer.load_lexicon(tmp_path / "nothing-here") == {}

    monkeypatch.setenv("STUDIO_HOME", str(explicit))
    assert sanitizer.load_lexicon(None) == {"blood": "crimson mist"}

    # Non-mapping YAML falls back to an empty lexicon.
    (config_dir / "sanitizer_lexicon.yaml").write_text("- just a list\n", encoding="utf-8")
    assert sanitizer.load_lexicon(explicit) == {}


def test_sanitizer_apply_lexicon_is_whole_word_and_case_insensitive():
    text = "Blood on the bloody wall, blood."
    out = sanitizer.apply_lexicon(text, {"blood": "crimson mist"})
    assert out.startswith("crimson mist on the bloody wall")


def test_sanitizer_validate_rewrite_rejects_bad_candidates():
    valid = sanitizer.validate_rewrite
    assert valid("a man stabs a guard", "a figure in shadow", max_chars=100)
    assert not valid("x", "", max_chars=100)
    assert not valid("x", "   ", max_chars=100)
    assert not valid("x", "y" * 101, max_chars=100)
    # A rewrite may not introduce a new proper noun.
    assert not valid("Bob appears", "then Alice appears", max_chars=100)
    # ... and it may not keep one the deterministic step should have removed? No:
    # keeping an existing proper noun is allowed, inventing one is not.
    assert valid("Bob appears", "Bob appears in shadow", max_chars=100)


# ======================================================================
# media/registry.py
# ======================================================================


def _config_with(models: dict[str, ModelEntry]):  # type: ignore[no-untyped-def]
    class Registry:
        def __init__(self) -> None:
            self.models = models

    class Config:
        def __init__(self) -> None:
            self.registry = Registry()

    return Config()


def _resolved(name: str, model_keys: dict[str, str] | None = None) -> ResolvedProfile:
    return ResolvedProfile(
        name=name, registry_version=1, model_keys=model_keys or {}, entries={}
    )


def test_registry_media_model_key_errors():
    config = _config_with({})
    with pytest.raises(MediaConfigError, match="no media model table"):
        media_registry.media_model_key(config, _resolved("unknown-profile"), "clips")
    with pytest.raises(MediaConfigError, match="unknown media model"):
        media_registry.media_model_key(config, _resolved("sandbox"), "clips")


def test_registry_optional_roles_win():
    entry = _entry(PerImage(unit="per_image", usd=Decimal("0.01")))
    config = _config_with({"operator.image": entry})
    resolved = _resolved("sandbox", {"image_t2i": "operator.image"})
    assert media_registry.media_model_key(config, resolved, "sheets") == "operator.image"


def test_registry_entry_capability_mismatch():
    entry = _entry(PerImage(unit="per_image", usd=Decimal("0.01")))
    config = _config_with({"mock.video": entry})
    with pytest.raises(MediaConfigError, match="capability"):
        media_registry.media_model_entry(config, _resolved("sandbox"), "clips")


def test_registry_media_spec_missing_and_aspect_errors():
    bare = _entry(PerImage(unit="per_image", usd=Decimal("0.01")))
    with pytest.raises(MediaConfigError, match="no media wire contract"):
        media_registry.media_spec(bare)

    no_aspects = _entry(
        PerImage(unit="per_image", usd=Decimal("0.01")),
        capability="image_to_video",
        media=MediaSpec(),
    )
    with pytest.raises(MediaConfigError, match="declares no supported aspects"):
        media_registry.supported_aspects(no_aspects)

    bad_aspect = _entry(
        PerImage(unit="per_image", usd=Decimal("0.01")),
        media=MediaSpec(supported_aspects=["4:3"]),
    )
    with pytest.raises(MediaConfigError, match="unsupported aspect"):
        media_registry.supported_aspects(bad_aspect)

    unsupported = _entry(
        PerImage(unit="per_image", usd=Decimal("0.01")),
        media=MediaSpec(supported_aspects=["16:9"]),
    )
    with pytest.raises(MediaConfigError, match="does not support aspect"):
        media_registry.aspect_value(unsupported, Aspect.TALL)

    unconfirmed = _entry(
        PerImage(unit="per_image", usd=Decimal("0.01")),
        media=MediaSpec(supported_aspects=["16:9"], aspect_values={"16:9": "<CONFIRM>"}),
    )
    with pytest.raises(MediaConfigError, match="unconfirmed"):
        media_registry.aspect_value(unconfirmed, Aspect.WIDE)


def test_registry_durations_and_ref_capability():
    no_durations = _entry(
        PerImage(unit="per_image", usd=Decimal("0.01")),
        capability="image_to_video",
        media=MediaSpec(supported_aspects=["16:9"]),
    )
    with pytest.raises(MediaConfigError, match="no supported durations"):
        media_registry.supported_durations_s(no_durations)

    durations = _entry(
        PerImage(unit="per_image", usd=Decimal("0.01")),
        capability="image_to_video",
        media=MediaSpec(supported_durations_s=[5, 5, 10]),
        supported_durations_s=[5, 10],
    )
    assert media_registry.supported_durations_s(durations) == (5, 10)

    refs = _entry(
        PerImage(unit="per_image", usd=Decimal("0.01")),
        media=MediaSpec(supported_aspects=["16:9"], max_refs=0),
        supports_refs=False,
    )
    media_registry.validate_ref_capable(refs, required=0)  # no-op
    with pytest.raises(MediaConfigError, match="not verified reference-capable"):
        media_registry.validate_ref_capable(refs, required=1)


# ======================================================================
# media/sheets.py cost helpers
# ======================================================================


def test_pixel_dims_for_all_shapes():
    fixed = _entry(
        PerMegapixel(unit="per_megapixel", usd=Decimal("0.03")),
        media=MediaSpec(fixed_params={"width": 1024, "height": 1024}),
    )
    assert _pixel_dims_for(fixed, Aspect.WIDE) == (1024, 1024)

    per_aspect = _entry(
        PerMegapixel(unit="per_megapixel", usd=Decimal("0.03")),
        media=MediaSpec(
            fixed_params={"width": 1024},
            aspect_extra_params={"9:16": {"height": 1792}, "16:9": {"height": 576}},
        ),
    )
    assert _pixel_dims_for(per_aspect, Aspect.TALL) == (1024, 1792)
    assert _pixel_dims_for(per_aspect, Aspect.WIDE) == (1024, 576)

    missing = _entry(
        PerMegapixel(unit="per_megapixel", usd=Decimal("0.03")),
        media=MediaSpec(fixed_params={"width": 1024}),
    )
    assert _pixel_dims_for(missing, Aspect.WIDE) is None

    unconfirmed = _entry(
        PerMegapixel(unit="per_megapixel", usd=Decimal("0.03")),
        media=MediaSpec(fixed_params={"width": "<CONFIRM>", "height": 1024}),
    )
    assert _pixel_dims_for(unconfirmed, Aspect.WIDE) is None

    not_int = _entry(
        PerMegapixel(unit="per_megapixel", usd=Decimal("0.03")),
        media=MediaSpec(fixed_params={"width": "wide", "height": 1024}),
    )
    assert _pixel_dims_for(not_int, Aspect.WIDE) is None


def test_estimate_costs_across_pricing_units():
    image = _entry(PerImage(unit="per_image", usd=Decimal("0.10")))
    assert _estimate_image_cost(image, aspect=Aspect.WIDE) == 100_000

    video_priced = _entry(
        PerVideo(unit="per_video", usd=Decimal("0.50")),
        capability="image_to_video",
    )
    assert _estimate_image_cost(video_priced, aspect=Aspect.WIDE) == 500_000
    assert _estimate_clip_cost(video_priced, clip_duration_ms=5_000) == 500_000

    per_second = _entry(PerSecond(unit="per_second", usd=Decimal("0.10")))
    assert _estimate_clip_cost(per_second, clip_duration_ms=5_000) == 500_000

    megapixel = _entry(
        PerMegapixel(unit="per_megapixel", usd=Decimal("1.00")),
        media=MediaSpec(fixed_params={"width": 1000, "height": 1000}),
    )
    assert _estimate_image_cost(megapixel, aspect=Aspect.WIDE) == 1_000_000

    uncomputable = _entry(
        PerMegapixel(unit="per_megapixel", usd=Decimal("1.00")),
        media=MediaSpec(fixed_params={"width": 1000}),
    )
    assert _estimate_image_cost(uncomputable, aspect=Aspect.WIDE) is None

    token_priced = _entry(PerSecond(unit="per_second", usd=Decimal("0.10")))
    assert _estimate_image_cost(token_priced, aspect=Aspect.WIDE) is None


# ======================================================================
# qc/visual.py
# ======================================================================


def _make_video(path: Path, *, seconds: float = 1.0, audio: bool = False) -> Path:
    import subprocess

    args = [
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate=24:duration={seconds}",
    ]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"]
    args += ["-pix_fmt", "yuv420p", "-c:v", "libx264"]
    if audio:
        args += ["-c:a", "aac"]
    args += [str(path)]
    subprocess.run(args, check=True, capture_output=True)
    return path


@pytest.mark.ffmpeg
@pytest.mark.asyncio
async def test_visual_qc_unreadable_inputs(tmp_path, monkeypatch):
    from conftest import make_home

    from studio.config.settings import load_settings_file

    home = make_home(
        tmp_path, settings_overrides={}, registry_overrides={}, filename="vqc"
    )
    monkeypatch.setenv("STUDIO_HOME", str(home))
    settings = load_settings_file(home / "configs" / "settings.yaml")

    from studio.qc.visual import qc_image, qc_video

    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    checks = await qc_image(broken, aspect=Aspect.TALL, settings=settings)
    assert any(c.status == "fail" for c in checks)

    missing = tmp_path / "missing.png"
    checks = await qc_image(missing, aspect=Aspect.TALL, settings=settings)
    assert any(c.status == "fail" for c in checks)

    silent = _make_video(tmp_path / "silent.mp4", audio=False)
    checks = await qc_video(silent, aspect=Aspect.WIDE, settings=settings)
    # Phase 4 semantics: clips must have their audio stripped, so a *present*
    # audio stream is the warn case and an absent one passes.
    assert any(c.name == "has_audio" and c.value is False for c in checks)

    noisy = _make_video(tmp_path / "noisy.mp4", audio=True)
    checks = await qc_video(noisy, aspect=Aspect.WIDE, settings=settings)
    assert any(c.name == "has_audio" and c.status == "warn" for c in checks)

    unprobeable = tmp_path / "junk.mp4"
    unprobeable.write_bytes(b"\x00" * 64)
    checks = await qc_video(unprobeable, aspect=Aspect.WIDE, settings=settings)
    assert any(c.status == "fail" for c in checks)
