"""Premaster mix renderer (PHASE3 §H9).

Single FFmpeg run. The narration stem (stdlib-wave assembled, sample-exact)
is the only sample-exact input; music/ambience/SFX cues are placed with
millisecond-precision ``adelay`` (speech is sample-exact before this stage).

Buses: narration (pan mono->stereo) + music + ambience + sfx. Music is
ducked against the narration stem — envelope mode by default
(``sidechaincompress`` in mode ``sidechain``). Ambience always gets the
envelope duck scaled by ``ambience_fraction`` (the spec defines sidechain
only for music). ``amix=normalize=0`` sums everything (no level
normalization), and the output is forced to exactly ``total_samples`` with
``apad=whole_len`` + ``atrim=end_sample``.

The filtergraph is always written to a UTF-8 file and passed with the
version-appropriate flag (``-/filter_complex`` on FFmpeg >= 7.0, otherwise
``-filter_complex_script``) — Windows command lines cap near 32k.

Output: a ``pcm_s24le`` premaster WAV in ``work_dir`` (never directly into
the CAS; the caller moves it through ContentStore). When ``emit_music_bus_stems``
is set, additional ``music_pre_duck.wav`` / ``music_post_duck.wav`` taps are
written alongside (used only by the duck-depth QC; stay in ``work_dir``).
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studio.audio.ducking import duck_envelope_points, speech_spans
from studio.audio.envelope import build_gain_expr
from studio.video.ffmpeg import ffmpeg_version, filtergraph_script_args, run_ffmpeg

MIX_LABEL = "mixout"


@dataclass(frozen=True)
class CueRender:
    """One cue ready to be rendered into the premaster."""

    path: str
    bus: str                      # "music" | "ambience" | "sfx"
    loop: bool
    start_frame: int
    end_frame: int
    source_offset_ms: int
    gain_db: float
    envelope: tuple[tuple[float, float], ...] | None   # (t_s, dB offset), cue-relative
    fade_in_ms: int
    fade_out_ms: int


@dataclass(frozen=True)
class DuckSpec:
    mode: str                     # "envelope" | "sidechain"
    depth_db: float
    pre_ms: float
    attack_ms: float
    release_ms: float
    merge_gap_ms: float
    ambience_fraction: float
    sidechain_threshold: float
    sidechain_ratio: float
    sidechain_attack_ms: float
    sidechain_release_ms: float
    sidechain_makeup: float


@dataclass(frozen=True)
class MixResult:
    premaster_path: Path
    music_pre_duck: Path | None
    music_post_duck: Path | None
    total_samples: int
    graph_path: Path


def _channels_of(path: str) -> int | None:
    from studio.video.probe import probe

    try:
        channels = probe(Path(path)).audio_channels
    except Exception:
        return None
    return int(channels) if channels is not None else None


def _fmt(value: float) -> str:
    if value == 0:
        return "0"
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _envelope_chain(points: list[tuple[float, float]] | tuple[tuple[float, float], ...],
                    sample_rate: int) -> str:
    expr = build_gain_expr(list(points), sample_rate=sample_rate)
    return (f"asetnsamples=n={max(1, sample_rate // 100)}:p=0,"
            f"volume='pow(10,({expr})/20)':eval=frame")


def build_premaster_graph(
    *,
    stem_index: int,
    cues: list[CueRender],
    total_samples: int,
    fps: int,
    sample_rate: int,
    duck: DuckSpec,
    spans: list[tuple[int, int]],
    emit_music_bus_stems: bool = False,
    cue_channels: list[int | None] | None = None,
) -> str:
    """Filtergraph text (UTF-8). Input 0 is the narration stem.

    ``cue_channels`` carries the probed channel count per cue (same order);
    mono cues get a unity-gain duplicating ``pan`` (the auto-inserted
    converter would attenuate by 3 dB, and coefficients vary by build).
    """
    lines: list[str] = []
    # mono/stereo narration -> stereo float, duplicated channel (spec: pan)
    lines.append(
        f"[{stem_index}:a]aresample={sample_rate},aformat=sample_fmts=fltp,"
        f"pan=stereo|c0=c0|c1=c0[narr]"
    )

    music_labels: list[str] = []
    ambience_labels: list[str] = []
    sfx_labels: list[str] = []

    for i, cue in enumerate(cues):
        idx = i + 1
        channels = cue_channels[i] if cue_channels and i < len(cue_channels) else None
        if channels == 1:
            pan = "pan=stereo|c0=c0|c1=c0"
        else:
            pan = "pan=stereo|c0=c0|c1=c1"
        parts = [f"[{idx}:a]aresample={sample_rate},aformat=sample_fmts=fltp,{pan}"]
        if cue.source_offset_ms > 0:
            parts.append(f"atrim=start={_fmt(cue.source_offset_ms / 1000.0)},asetpts=PTS-STARTPTS")
        if cue.gain_db != 0.0:
            parts.append(f"volume={_fmt(cue.gain_db)}dB")
        if cue.envelope:
            parts.append(_envelope_chain(cue.envelope, sample_rate))
        span_frames = max(1, cue.end_frame - cue.start_frame)
        dur_s = span_frames / fps
        fade_in_s = cue.fade_in_ms / 1000.0
        fade_out_s = cue.fade_out_ms / 1000.0
        if fade_in_s > 0:
            parts.append(f"afade=t=in:st=0:d={_fmt(fade_in_s)}:curve=qsin")
        if fade_out_s > 0:
            start = max(0.0, dur_s - fade_out_s)
            parts.append(f"afade=t=out:st={_fmt(start)}:d={_fmt(fade_out_s)}:curve=qsin")
        # cue span defines how long the cue plays
        parts.append(f"atrim=end={_fmt(dur_s)},asetpts=PTS-STARTPTS")
        delay_ms = math.floor(cue.start_frame * 1000.0 / fps + 0.5)
        if delay_ms > 0:
            parts.append(f"adelay={delay_ms}|{delay_ms}")
        label = f"cue{i}"
        lines.append(",".join(parts) + f"[{label}]")
        {"music": music_labels, "ambience": ambience_labels,
         "sfx": sfx_labels}.get(cue.bus, sfx_labels).append(f"[{label}]")

    def sum_bus(labels: list[str], out: str) -> None:
        if len(labels) == 1:
            lines.append(f"{labels[0]}anull[{out}]")
        else:
            lines.append("".join(labels) +
                         f"amix=inputs={len(labels)}:normalize=0:duration=longest[{out}]")

    if music_labels:
        sum_bus(music_labels, "music_bus")
    if ambience_labels:
        sum_bus(ambience_labels, "ambience_bus")
    if sfx_labels:
        sum_bus(sfx_labels, "sfx_bus")

    # ---- ducking
    if music_labels:
        duck_src = "music_bus"
        if emit_music_bus_stems:
            # fan out: one branch into the ducker, one into the pre-duck QC tap
            lines.append("[music_bus]asplit=2[music_duck_in][music_pre_qc_src]")
            duck_src = "music_duck_in"
        if duck.mode == "sidechain":
            lines.append("[narr]asplit=2[narr_out][narr_key]")
            lines.append(
                f"[{duck_src}][narr_key]sidechaincompress="
                f"threshold={_fmt(duck.sidechain_threshold)}:ratio={_fmt(duck.sidechain_ratio)}:"
                f"attack={_fmt(duck.sidechain_attack_ms)}:release={_fmt(duck.sidechain_release_ms)}:"
                f"makeup={_fmt(duck.sidechain_makeup)}[music_duck]"
            )
        else:
            lines.append("[narr]anull[narr_out]")
            pts = duck_envelope_points(
                spans, depth_db=duck.depth_db, pre_ms=duck.pre_ms,
                attack_ms=duck.attack_ms, release_ms=duck.release_ms,
                ambience_fraction=1.0, sample_rate=sample_rate,
            )
            lines.append(f"[{duck_src}]{_envelope_chain(pts, sample_rate)}[music_duck]")
    else:
        lines.append("[narr]anull[narr_out]")

    if ambience_labels:
        frac_pts = duck_envelope_points(
            spans, depth_db=duck.depth_db, pre_ms=duck.pre_ms,
            attack_ms=duck.attack_ms, release_ms=duck.release_ms,
            ambience_fraction=duck.ambience_fraction, sample_rate=sample_rate,
        )
        lines.append(f"[ambience_bus]{_envelope_chain(frac_pts, sample_rate)}[ambience_duck]")

    music_duck_label = "music_duck"
    if music_labels and emit_music_bus_stems:
        lines.append("[music_duck]asplit=2[music_duck_amix][music_post_qc_src]")
        music_duck_label = "music_duck_amix"

    mix_inputs = ["[narr_out]"]
    if music_labels:
        mix_inputs.append(f"[{music_duck_label}]")
    if ambience_labels:
        mix_inputs.append("[ambience_duck]")
    if sfx_labels:
        mix_inputs.append("[sfx_bus]")
    if len(mix_inputs) == 1:
        lines.append(f"[narr_out]apad=whole_len={total_samples},"
                     f"atrim=end_sample={total_samples}[{MIX_LABEL}]")
    else:
        lines.append("".join(mix_inputs) +
                     f"amix=inputs={len(mix_inputs)}:normalize=0:duration=longest,"
                     f"apad=whole_len={total_samples},atrim=end_sample={total_samples}[{MIX_LABEL}]")

    if emit_music_bus_stems and music_labels:
        lines.append("[music_pre_qc_src]anull[music_pre_qc]")
        lines.append("[music_post_qc_src]anull[music_post_qc]")
    return ";\n".join(lines) + ";\n"


def cues_from_rows(cues: list[Any], library_paths: dict[str, str]) -> list[CueRender]:
    """Convert audio_cues rows into render items (frames stay frames)."""
    out: list[CueRender] = []
    for cue in cues:
        params = json.loads(cue.params_json or "{}")
        envelope = tuple(
            (float(t), float(db)) for t, db in params.get("envelope", [])
        ) or None
        out.append(
            CueRender(
                path=library_paths[cue.library_sha256],
                bus=cue.kind,
                loop=bool(params.get("loop", False)),
                start_frame=int(cue.start_frame),
                end_frame=int(cue.end_frame),
                source_offset_ms=int(cue.source_offset_ms),
                gain_db=float(cue.gain_db),
                envelope=envelope,
                fade_in_ms=int(cue.fade_in_ms),
                fade_out_ms=int(cue.fade_out_ms),
            )
        )
    return out


async def render_premaster(
    *,
    stem_path: Path,
    cues: list[CueRender],
    duck: DuckSpec,
    placements: list[tuple[int, int]],
    total_samples: int,
    fps: int,
    sample_rate: int,
    work_dir: Path,
    emit_music_bus_stems: bool = False,
) -> MixResult:
    await asyncio.to_thread(work_dir.mkdir, parents=True, exist_ok=True)
    premaster = work_dir / "premaster.wav"
    graph_path = work_dir / "premaster.filtergraph"
    music_pre = work_dir / "music_pre_duck.wav"
    music_post = work_dir / "music_post_duck.wav"

    cue_channels = [await asyncio.to_thread(_channels_of, cue.path) for cue in cues]
    spans = speech_spans(placements, merge_gap_ms=duck.merge_gap_ms, sample_rate=sample_rate)
    graph = build_premaster_graph(
        stem_index=0,
        cues=cues,
        total_samples=total_samples,
        fps=fps,
        sample_rate=sample_rate,
        duck=duck,
        spans=spans,
        emit_music_bus_stems=emit_music_bus_stems,
        cue_channels=cue_channels,
    )
    await asyncio.to_thread(graph_path.write_text, graph + "\n", "utf-8")

    args: list[str] = ["-hide_banner", "-y"]
    args += ["-f", "wav", "-i", str(stem_path)]
    for cue in cues:
        if cue.loop:
            args += ["-stream_loop", "-1"]
        ext = cue.path.rsplit(".", 1)[-1].lower() if "." in cue.path else ""
        if ext in ("wav", "flac", "mp3", "ogg", "aac", "m4a"):
            args += ["-f", ext]
        args += ["-i", str(cue.path)]
    version = await ffmpeg_version()
    args += filtergraph_script_args(str(graph_path), version)

    has_music = any(cue.bus == "music" for cue in cues)
    args += ["-map", f"[{MIX_LABEL}]",
             "-bitexact", "-fflags", "+bitexact", "-map_metadata", "-1",
             "-c:a", "pcm_s24le", str(premaster)]
    outputs = [music_pre, music_post] if emit_music_bus_stems and has_music else []
    labels = ("music_pre_qc", "music_post_qc")
    for label, target in zip(labels, outputs, strict=False):
        args += ["-map", f"[{label}]", "-c:a", "pcm_s24le", str(target)]
    await run_ffmpeg(args, timeout_s=600.0)
    return MixResult(
        premaster_path=premaster,
        music_pre_duck=outputs[0] if outputs else None,
        music_post_duck=outputs[1] if outputs else None,
        total_samples=total_samples,
        graph_path=graph_path,
    )
