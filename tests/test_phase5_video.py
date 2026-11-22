"""Phase 5 step 3: geometry (T-GEO-01), encoders (T-ENC-01/02), segment primitive.

The geometry and escaping tests are pure string/table tests (they run
everywhere). The segment tests drive the **real** ffmpeg on tiny synthetic
clips — they are the frame-run-length evidence required by §E5/T-ASM-01: no
duplicated or dropped frame at a segment boundary.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import make_home

from studio.assemble.paths import (
    concat_line,
    escape_concat_path,
    escape_filter_path,
    write_concat_list,
)
from studio.assemble.segments import (
    SegmentSpec,
    join_segments,
    load_sidecar,
    render_segment,
    sha256_file,
)
from studio.config.loader import load_all
from studio.domain.errors import (
    EncoderUnavailable,
    GeometryError,
    MediaToolError,
)
from studio.video.encoders import (
    ASSEMBLER_VERSION,
    ProbeOutput,
    cache_key,
    decoded_frame_count,
    driver_string,
    load_probe_cache,
    nvenc_profile,
    profile_hash_for,
    resolve_encoder,
    scrub_text,
    x264_profile,
)
from studio.video.ffmpeg import find_binary
from studio.video.geometry import (
    aspect_delta_pct,
    destination_for,
    fit_plan,
)

FFMPEG = find_binary("ffmpeg")


def _settings(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    monkeypatch.setenv("STUDIO_HOME", str(home))
    return load_all(home).settings


# ---------------------------------------------------------------------------
# T-GEO-01
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("src", "dst", "expect_scale", "tol"),
    [
        ((1080, 1920), (1080, 1920), (1080, 1920), 2.0),
        ((1920, 1080), (1920, 1080), (1920, 1080), 2.0),
        ((480, 270), (1920, 1080), (1920, 1080), 2.0),
        ((270, 480), (540, 960), (540, 960), 2.0),
        # Same 9:16 aspect family, 0.48% off: allowed, cropped horizontally.
        ((1085, 1920), (1080, 1920), None, 2.0),
    ],
)
def test_geo_01_table(src, dst, expect_scale, tol):
    plan = fit_plan(*src, *dst, tol_pct=tol)
    assert (plan.dst_w, plan.dst_h) == dst
    assert plan.scale_w >= dst[0] and plan.scale_h >= dst[1]
    assert plan.scale_w % 2 == 0 and plan.scale_h % 2 == 0
    assert plan.crop_x % 2 == 0 and plan.crop_y % 2 == 0
    assert plan.crop_loss_pct >= 0.0
    if expect_scale is not None:
        assert (plan.scale_w, plan.scale_h) == expect_scale


def test_geo_cover_and_crop_reporting():
    plan = fit_plan(1085, 1920, 1080, 1920, tol_pct=2.0)
    assert plan.scale_w == 1086
    assert plan.crop_loss_pct > 0.0
    report = plan.report()
    assert report["crop_loss_pct"] == round(plan.crop_loss_pct, 4)
    assert "lanczos" in plan.filter_chain()
    assert plan.filter_chain().endswith("setsar=1")
    assert "crop=1080:1920" in plan.filter_chain()


def test_geo_exact_is_untouched_and_no_crop():
    plan = fit_plan(1920, 1080, 1920, 1080)
    assert plan.untouched is True
    assert plan.crop_loss_pct == 0.0
    assert "crop=1920:1080:0:0" in plan.filter_chain()


def test_geo_upscale_is_expected_not_a_violation():
    plan = fit_plan(270, 480, 1080, 1920, tol_pct=2.0)
    assert (plan.scale_w, plan.scale_h) == (1080, 1920)
    assert plan.crop_loss_pct == 0.0
    assert plan.aspect_delta_pct < 0.01


def test_geo_beyond_tolerance_raises():
    with pytest.raises(GeometryError) as excinfo:
        fit_plan(480, 270, 1080, 1920, tol_pct=2.0)
    assert "aspect mismatch" in str(excinfo.value)
    # The tolerance is inclusive: exactly at it is allowed.
    delta = aspect_delta_pct(1085, 1920, 1080, 1920)
    assert fit_plan(1085, 1920, 1080, 1920, tol_pct=delta + 0.001).crop_loss_pct > 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"src_w": 0, "src_h": 10, "dst_w": 10, "dst_h": 10},
        {"src_w": 10, "src_h": -2, "dst_w": 10, "dst_h": 10},
        {"src_w": 10, "src_h": 10, "dst_w": 10, "dst_h": 0},
    ],
)
def test_geo_invalid_dimensions(kwargs):
    with pytest.raises(GeometryError):
        fit_plan(**kwargs)


def test_geo_color_matrix_for_stills():
    plan = fit_plan(540, 960, 540, 960)
    chain = plan.filter_chain(out_color_matrix="bt709")
    assert "out_color_matrix=bt709" in chain


def test_geo_destination_for():
    assert destination_for("9:16", 540) == (540, 960)
    assert destination_for("16:9", 540) == (960, 540)
    with pytest.raises(GeometryError):
        destination_for("4:3", 540)


# ---------------------------------------------------------------------------
# T-ENC-01 — fake runner probe
# ---------------------------------------------------------------------------


class FakeRunner:
    """Records calls; writes a fake payload file, like ffmpeg would."""

    def __init__(self, *, returncode: int = 0, stderr: str = "", write_bytes: int = 4096):
        self.returncode = returncode
        self.stderr = stderr
        self.write_bytes = write_bytes
        self.calls: list[list[str]] = []

    async def run(self, args: list[str], *, timeout_s: float) -> ProbeOutput:
        self.calls.append(list(args))
        out = Path(args[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        if self.returncode == 0:
            out.write_bytes(b"x" * self.write_bytes)
        return ProbeOutput(returncode=self.returncode, stderr_tail=self.stderr)


def _counter(value: int | None):
    async def count(_path: Path) -> int | None:
        return value

    return count


GEOMETRIES = [(1080, 1920), (1920, 1080)]


def _resolve(tmp_path, settings, runner, **kwargs):
    return asyncio.run(
        resolve_encoder(
            settings,
            runner,
            geometries=kwargs.pop("geometries", GEOMETRIES),
            cache_path=kwargs.pop("cache_path", tmp_path / "cache" / "probe.json"),
            ffmpeg_version=kwargs.pop("ffmpeg_version", "7.1.1"),
            driver=kwargs.pop("driver", None),
            frame_counter=kwargs.pop("frame_counter", _counter(24)),
            **kwargs,
        )
    )


def test_enc_01_nvenc_ok(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    runner = FakeRunner()
    resolution = _resolve(tmp_path, settings, runner)
    assert resolution.profile.name == "nvenc"
    assert resolution.fallback_reason is None
    assert resolution.cache_hit is False
    assert len(runner.calls) == 2
    args = runner.calls[0]
    assert "-c:v" in args and "h264_nvenc" in args
    assert "p5" in args and "vbr" in args
    assert "-colorspace" in args and "bt709" in args
    cache = load_probe_cache(tmp_path / "cache" / "probe.json")
    assert len(cache) == 2 and all(entry["ok"] for entry in cache.values())


def test_enc_01_cache_hit_skips_probe(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    first = _resolve(tmp_path, settings, FakeRunner())
    again = FakeRunner()
    second = _resolve(tmp_path, settings, again)
    assert second.cache_hit is True
    assert second.profile.name == "nvenc"
    assert again.calls == []
    assert second.profile.profile_hash == first.profile.profile_hash


def test_enc_01_cache_key_changes_with_geometry_and_driver(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    _resolve(tmp_path, settings, FakeRunner())
    other = FakeRunner()
    resolution = _resolve(
        tmp_path, settings, other, geometries=[(1080, 1920), (720, 1280)]
    )
    assert resolution.cache_hit is True  # first geometry cached
    assert len(other.calls) == 1  # only the new geometry probed
    assert cache_key(ffmpeg_version="7.1.1", geometry=(1, 2), driver="550.1") != cache_key(
        ffmpeg_version="7.1.1", geometry=(1, 2), driver="560.2"
    )
    assert cache_key(ffmpeg_version="7.1.1", geometry=(1, 2), driver=None) != cache_key(
        ffmpeg_version="7.1.1", geometry=(2, 1), driver=None
    )


def test_enc_01_nvenc_fails_closed(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    settings = settings.model_copy(
        update={"encode": settings.encode.model_copy(update={"encoder": "nvenc"})}
    )
    runner = FakeRunner(returncode=1, stderr="Cannot load libnvidia-encode.so.1")
    with pytest.raises(EncoderUnavailable) as excinfo:
        _resolve(tmp_path, settings, runner)
    assert "libnvidia-encode" in str(excinfo.value)


def test_enc_01_auto_falls_back_with_scrubbed_reason(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    secret_url = "https://queue.fal.run/secret-endpoint?key=falkey-0123456789abcdef0123456789abcdef"
    runner = FakeRunner(returncode=255, stderr=f"OpenEncodeSessionEx failed {secret_url}")
    resolution = _resolve(tmp_path, settings, runner)
    assert resolution.profile.name == "x264"
    assert resolution.fallback_reason
    assert "https://" not in resolution.fallback_reason
    assert "falkey-0123456789abcdef0123456789abcdef" not in resolution.fallback_reason


def test_enc_01_timeout_and_wrong_frame_count(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    timeout_runner = FakeRunner(returncode=-1, stderr="probe timed out after 30.0s")
    resolution = _resolve(tmp_path, settings, timeout_runner)
    assert resolution.profile.name == "x264"
    assert "timed out" in (resolution.fallback_reason or "")

    wrong = _resolve(
        tmp_path / "wrong", settings, FakeRunner(), frame_counter=_counter(23)
    )
    assert wrong.profile.name == "x264"
    assert "expected 24" in (wrong.fallback_reason or "")


def test_enc_01_empty_and_missing_output(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    empty = _resolve(tmp_path, settings, FakeRunner(write_bytes=0))
    assert "empty" in (empty.fallback_reason or "")

    broken = asyncio.run(
        resolve_encoder(
            settings,
            FakeRunner(returncode=99),
            geometries=[(4, 4)],
            cache_path=None,
            ffmpeg_version="x",
            frame_counter=_counter(24),
        )
    )
    assert broken.profile.name == "x264"


def test_enc_01_x264_mode_never_probes(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    settings = settings.model_copy(
        update={"encode": settings.encode.model_copy(update={"encoder": "x264"})}
    )
    runner = FakeRunner()
    resolution = _resolve(tmp_path, settings, runner)
    assert resolution.profile.name == "x264"
    assert resolution.reason == "x264 configured"
    assert runner.calls == []


def test_enc_01_profiles_and_hashes(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    x264 = x264_profile(settings)
    assert x264.args[:2] == ("-c:v", "libx264")
    assert "medium" in x264.args and "18" in x264.args
    assert x264.profile_hash == profile_hash_for("x264", x264.args)
    capped = x264.with_bitrate(12000, 24000)
    assert capped.args[-4:] == ("-maxrate", "12000k", "-bufsize", "24000k")
    assert capped.profile_hash != x264.profile_hash
    nvenc = nvenc_profile(settings, maxrate_k=8000, bufsize_k=16000)
    assert nvenc.args[1] == "h264_nvenc"
    assert "8000k" in nvenc.args and "16000k" in nvenc.args
    assert nvenc.report()["assembler_version"] == ASSEMBLER_VERSION
    assert scrub_text("") == ""
    assert scrub_text("a" * 900) == "<redacted>"
    assert len(scrub_text("word " * 300)) <= 500
    assert driver_string() is None or isinstance(driver_string(), str)


def test_enc_01_unreadable_cache_is_ignored(tmp_path, monkeypatch):
    path = tmp_path / "cache.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_probe_cache(path) == {}
    path.write_text('{"entries": [1, 2]}', encoding="utf-8")
    assert load_probe_cache(path) == {}
    assert load_probe_cache(tmp_path / "missing.json") == {}


def test_h_ffmpeg_assembly_capabilities():
    """§H: concat/loudnorm/... filters, aac/libx264 encoders, mp4/matroska/mpegts."""
    from studio.video.ffmpeg import (
        REQUIRED_ENCODERS,
        REQUIRED_MUXERS,
        _parse_flag_table,
        assembly_capability_check,
    )

    assert _parse_flag_table(
        " V....D libx264    H.264\n  E  mp4  MP4\n D.. = Demuxing\n"
    ) == {"libx264", "mp4"}
    filters, encoders, muxers = asyncio.run(assembly_capability_check())
    assert filters == []
    assert encoders == []
    assert muxers == []
    assert set(REQUIRED_ENCODERS) == {"aac", "libx264"}
    assert set(REQUIRED_MUXERS) == {"mp4", "matroska", "mpegts"}


def test_encode_probe_cli(tmp_path, monkeypatch):
    """`studio encode probe` prints the resolved encoder (x264: no GPU probing)."""
    from typer.testing import CliRunner

    from studio.cli import app

    home = make_home(
        tmp_path, settings_overrides={"encode": {"encoder": "x264"}}, filename="enc"
    )
    monkeypatch.setenv("STUDIO_HOME", str(home))
    result = CliRunner().invoke(app, ["encode", "probe", "--no-cache"])
    assert result.exit_code == 0, result.output
    assert "encoder:" in result.output and "x264" in result.output
    assert "profile_hash:" in result.output


def test_encode_probe_cli_fails_closed_without_nvenc(tmp_path, monkeypatch):
    """`encoder: nvenc` on a box without a GPU is an error, never a silent x264."""
    from typer.testing import CliRunner

    from studio.cli import app

    home = make_home(
        tmp_path, settings_overrides={"encode": {"encoder": "nvenc"}}, filename="enc2"
    )
    monkeypatch.setenv("STUDIO_HOME", str(home))
    result = CliRunner().invoke(app, ["encode", "probe", "--no-cache"])
    if driver_available():
        pytest.skip("this host really has NVENC; the fail-closed path is unreachable")
    assert result.exit_code == 1
    assert "nvenc" in result.output


def driver_available() -> bool:
    return driver_string() is not None


def test_enc_02_live_nvenc_opt_in(tmp_path, monkeypatch):
    """Opt-in local probe (free, no network): verifies a real NVENC encode."""
    import os

    if os.environ.get("STUDIO_LIVE_NVENC") != "1":
        pytest.skip("set STUDIO_LIVE_NVENC=1 to probe the real GPU")
    settings = _settings(tmp_path, monkeypatch)
    settings = settings.model_copy(
        update={"encode": settings.encode.model_copy(update={"encoder": "nvenc"})}
    )

    from studio.video.encoders import SubprocessProbeRunner

    resolution = asyncio.run(
        resolve_encoder(
            settings,
            SubprocessProbeRunner(),
            geometries=[(1080, 1920)],
            cache_path=None,
            frame_counter=decoded_frame_count,
        )
    )
    assert resolution.profile.name == "nvenc"


# ---------------------------------------------------------------------------
# Path escaping (string-level, incl. Windows-style paths)
# ---------------------------------------------------------------------------


def test_paths_concat_escaping():
    assert escape_concat_path("/tmp/a b/c'd.mkv") == "/tmp/a b/c'\\''d.mkv"
    assert escape_concat_path("C:\\Users\\أحمد\\clip 1.mkv") == "C:/Users/أحمد/clip 1.mkv"
    assert concat_line(Path("/x/it's.mkv")) == "file '/x/it'\\''s.mkv'"
    with pytest.raises(MediaToolError):
        escape_concat_path("/x/bad\nname.mkv")


def test_paths_filter_escaping():
    assert escape_filter_path("/tmp/a:b") == "/tmp/a\\:b"
    assert escape_filter_path("C:\\clips\\it's [1],2.mkv") == (
        "C\\:/clips/it\\'s \\[1\\]\\,2.mkv"
    )


def test_paths_concat_list_file(tmp_path, monkeypatch):
    parts = [tmp_path / "seg one.mkv", tmp_path / "seg'2.mkv"]
    list_path = write_concat_list(tmp_path / "list.txt", parts)
    lines = list_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == f"file '{parts[0]}'"
    assert lines[1] == f"file '{str(parts[1]).replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"


# ---------------------------------------------------------------------------
# Segment primitive against real ffmpeg (T-ASM-01 evidence)
# ---------------------------------------------------------------------------


def _make_clip(path: Path, *, color: str, frames: int, size: str = "270x480", fps: int = 24) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi",
            "-i", f"color=c={color}:s={size}:r={fps}:d={frames / fps:.4f}",
            "-frames:v", str(frames),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "10",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _per_frame_luma(path: Path) -> list[int]:
    completed = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(path),
            "-vf", "signalstats,metadata=print:file=-",
            "-f", "null", "-",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    values: list[int] = []
    for line in completed.stdout.splitlines():
        marker = "lavfi.signalstats.YAVG="
        if marker in line:
            values.append(round(float(line.split(marker, 1)[1])))
    return values


def _runs(values: list[int]) -> list[int]:
    runs: list[int] = []
    previous: int | None = None
    for value in values:
        if previous is not None and value == previous:
            runs[-1] += 1
        else:
            runs.append(1)
        previous = value
    return runs


def test_asm_01_frame_run_lengths_survive_segment_boundaries(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    profile = x264_profile(settings)
    clips = [
        (_make_clip(tmp_path / "in" / "black.mp4", color="black", frames=12), 12),
        (_make_clip(tmp_path / "in" / "white.mp4", color="white", frames=24), 24),
        (_make_clip(tmp_path / "in" / "gray.mp4", color="gray", frames=12), 12),
    ]
    root = tmp_path / "segments"
    plan = fit_plan(270, 480, 270, 480)
    parts = []
    for index, (clip, frames) in enumerate(clips):
        spec = SegmentSpec(
            index=index,
            shot_id=f"shot-{index}",
            src=clip,
            clip_frames=frames,
            dst_w=270,
            dst_h=480,
            input_sha=sha256_file(clip),
        )
        result = asyncio.run(
            render_segment(
                spec, plan=plan, profile=profile, fps=24, root=root, container="mkv"
            )
        )
        assert result.frames == frames
        assert result.reused is False
        sidecar = load_sidecar(result.path)
        assert sidecar is not None
        assert sidecar["input_sha"] == spec.input_sha
        assert sidecar["profile_hash"] == profile.profile_hash
        parts.append(result.path)

    joined = asyncio.run(
        join_segments(parts, tmp_path / "joined.mkv", max_inputs=24, work_dir=tmp_path / "join")
    )
    luma = _per_frame_luma(joined)
    assert len(luma) == 48
    assert _runs(luma) == [12, 24, 12]
    # No duplicate/dropped frame at the boundaries: three distinct plateaus.
    assert len(set(luma)) == 3


def test_asm_01_segment_reuse_and_profile_change(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    profile = x264_profile(settings)
    clip = _make_clip(tmp_path / "in" / "clip.mp4", color="black", frames=6)
    spec = SegmentSpec(
        index=0, shot_id="s", src=clip, clip_frames=6, dst_w=270, dst_h=480,
        input_sha=sha256_file(clip),
    )
    plan = fit_plan(270, 480, 270, 480)
    first = asyncio.run(
        render_segment(spec, plan=plan, profile=profile, fps=24, root=tmp_path / "seg")
    )
    mtime = first.path.stat().st_mtime_ns
    rebuilt = asyncio.run(
        render_segment(spec, plan=plan, profile=profile, fps=24, root=tmp_path / "seg")
    )
    assert rebuilt.reused is True
    assert rebuilt.path.stat().st_mtime_ns == mtime
    # A different encoder profile hash is a different sidecar: re-render.
    other = x264_profile(settings, maxrate_k=1000, bufsize_k=2000)
    changed = asyncio.run(
        render_segment(spec, plan=plan, profile=other, fps=24, root=tmp_path / "seg")
    )
    assert changed.reused is False


def test_asm_02_chunked_join_and_awkward_paths(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    profile = x264_profile(settings)
    awkward = tmp_path / "in dir" / "it's كليب"
    parts = []
    plan = fit_plan(96, 96, 96, 96)
    for index in range(7):
        clip = _make_clip(
            awkward / f"c {index}.mp4", color="black", frames=1, size="96x96"
        )
        spec = SegmentSpec(
            index=index, shot_id=f"s{index}", src=clip, clip_frames=1,
            dst_w=96, dst_h=96, input_sha=sha256_file(clip),
        )
        parts.append(
            asyncio.run(
                render_segment(
                    spec, plan=plan, profile=profile, fps=24,
                    root=tmp_path / "segments with space", container="mkv",
                )
            ).path
        )
    joined = asyncio.run(
        join_segments(parts, tmp_path / "out" / "joined.mkv", max_inputs=3, work_dir=tmp_path / "join")
    )
    assert joined.exists()
    assert len(_per_frame_luma(joined)) == 7
    with pytest.raises(MediaToolError):
        asyncio.run(join_segments([], tmp_path / "x.mkv", max_inputs=3, work_dir=tmp_path))
    with pytest.raises(MediaToolError):
        asyncio.run(join_segments(parts, tmp_path / "y.mkv", max_inputs=1, work_dir=tmp_path))


def test_segment_sidecar_mismatch_forces_render(tmp_path, monkeypatch):
    settings = _settings(tmp_path, monkeypatch)
    profile = x264_profile(settings)
    clip = _make_clip(tmp_path / "in" / "c.mp4", color="black", frames=4)
    spec = SegmentSpec(
        index=0, shot_id="s", src=clip, clip_frames=4, dst_w=270, dst_h=480,
        input_sha=sha256_file(clip),
    )
    plan = fit_plan(270, 480, 270, 480)
    result = asyncio.run(
        render_segment(spec, plan=plan, profile=profile, fps=24, root=tmp_path / "seg")
    )
    sidecar = load_sidecar(result.path)
    assert sidecar is not None
    sidecar["frames"] = 999
    result.path.with_name(result.path.name + ".ok.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )
    rebuilt = asyncio.run(
        render_segment(spec, plan=plan, profile=profile, fps=24, root=tmp_path / "seg")
    )
    assert rebuilt.reused is False
    assert shutil.which("ffmpeg")
