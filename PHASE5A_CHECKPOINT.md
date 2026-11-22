# Phase 5 — Stage A checkpoint (steps 0–6)

Date: 2026-11-21 · branch `arena/eb70200d-ai-studio` · base `b6905e5`

Stage A is the free, non-export half of Phase 5: hardening + the gate
relocation, the animatic (S9 / G2), the encoder resolver, the assembly (S11),
render QC (S12) and gate G3. Export (S13), Bible proposals (S14) and
publish/purge (S15) are Stage B and are **not** in this checkpoint.

## What ships

| Step | Commit | Deliverable |
| ---- | ------ | ----------- |
| 0 | `2648501` | `PHASE5_DISCOVERY.md` — C1–C13, H5.0 audit, D-1…D-9 |
| 1 | `69445be` + `a61b93d` | Phase 5 settings/presets, migration `007_assembly.sql`, six error classes, `StageSpec` flags (`requires_cleared_gates`, `gate_reject_stage`) |
| 2 | `f0f2f88` | `video/geometry.py`, `video/encoders.py`, `assemble/segments.py`, `probe_frames`, `studio encode probe` |
| 3 | `c17b26d` | encoder resolver hardening (`resolve_encoder` cache, NVENC fallback, never probe x264) |
| 4 | `7c0deaf` | S9 animatic, **G2 moved from S8 to S9**, panel freeze + `gates show/reject`, `media preview --stage animatic`, metadata review re-kinded to `G1_SCRIPT` |
| 5 | `95ba904` | **S11 assemble** — segment render per shot, join, mux, frame/A-V verification, render report, cache key |
| 6 | this commit | **S12 render QC + G3**, `studio render rebuild`, `media preview --stage render`, G3 checklist CLI |

### Production DAG after Stage A

`… s8 → s9(G2) → s10p → s10 → s11 → s12(G3)`

- G2 is on the animatic (`s9_animatic`, free, deliverable scope); a rejection
  re-runs `s8_keyframes` so only the named shots are re-paid.
- G3 is on the render QC (`s12_render_qc`, free, deliverable scope); a
  rejection re-runs `s11_assemble` (free) and produces a new render sha.
- `metadata_safety` carries `G1_SCRIPT`, not `G3_FINAL_CUT`: the operator is
  never asked to approve a "final cut" before any media exists (F5/C1).
- No paid stage has a `G3_FINAL_CUT` ancestor (T-GATE-02 basis; the static
  assertion lands with the S13/S14 rewiring step).

## S11 — assemble

- Preflight (`assemble_plan`) is free, fail-closed and **collects every
  problem**: missing master, missing/unconformed clip, `clip_duration_ms`
  /`speed_factor` unset, geometry beyond tolerance, insufficient free disk
  (`min_free_disk_factor ×` estimated render bytes), `j_cut_ms != 0`.
- Geometry: `fit_plan` with `geometry_tolerance_pct 2.0`; `crop_loss_pct` is
  recorded per shot in the render report (T-ASM-04: beyond tolerance fails,
  within tolerance records the loss).
- Retiming: the conformed clip holds `clip_frames`; S6 locked
  `effective_frames = ceil(clip_frames / speed) + hold`. Assembly reproduces
  that exactly — `setpts` + `fps=…:round=up` for the speed change, then a
  **frame-count** hold (`tpad … stop=N`, never a duration, which rounds to the
  wrong frame).
- Join: `max_inputs_per_graph` bounded concat tree; mux maps video-only from the
  joined file and the S7b master once to AAC (`-c:v copy`, `+faststart`,
  `-map_metadata -1`), then rewrites BT.709 into the H.264 VUI with
  `h264_metadata` so the *muxed* file carries the tags §J.3 requires.
- Verify: `probe_frames` (decoded count) equals Σ `effective_frames`; video and
  audio durations within `assemble.av_sync.fail_ms`.
- Outputs: render asset (retention `permanent`), `render-report.json`
  (encoder, args, profile hash, geometry with crop loss, timings, ffmpeg
  version, segments reused), summary keys in the stage output.
- Cache key: ordered conformed shas + master sha + timeline hash + preset +
  encoder mode/profile hash + `ASSEMBLER_VERSION` + sample rate.

## S12 — render QC

Measured on the final MP4 only (`src/studio/pipeline/qc_render.py`):

1. non-empty + ISO-BMFF box scan (32-bit, 64-bit, size-0 boxes);
2. faststart (`moov` before `mdat`);
3. exactly one h264/yuv420p video at the preset geometry, SAR 1:1, 24 fps,
   BT.709 primaries/transfer/matrix; exactly one aac 48 kHz stereo audio;
4. decoded frame count == S11's frame count;
5. video/audio duration + A/V start delta (`av_sync` 40 ms warn / 100 ms fail);
6. integrated LUFS vs the mastering target, **true peak re-measured on the
   muxed AAC** (never copied from S7b), sample peak;
7. `blackdetect` / `freezedetect` / first- and last-frame luma — warn level;
8. upstream QC *aggregate* (S3b tashkeel, S7b mix, S10 clips) — read, not
   recomputed;
9. preset limits (duration, bitrate, title/description/hashtag) — warn only
   (⚠ unverified platform limits).

`fail` fails the stage, but the QC report asset is written to the CAS first so
the operator sees exactly which check failed and its fix advice.

G3 snapshot = S12's input hash = render sha + render-report sha + preset +
QC settings + QC version, so re-rendering or re-measuring invalidates a stale
approval (T-G3-01, minus the S13 half, which needs S13).

### G3 checklist

`reviewed_full_render`, `audience_made_for_kids`, `ai_disclosure`,
`third_party_ip_confirmed`, `licenses_confirmed` — via
`studio gates approve <id> --checklist-file checklist.json` or the individual
flags. Stored on the gate row; verbatim in the audit row; auto-filled items are
marked `"auto"` and are refused by the final profile (and by S13's later
blocker). Sandbox/draft may omit items; final may not.

## CLI added in Stage A

```
studio gates show <gate_id> [--json]
studio gates reject <gate_id> --shots 3,7 --reason X --yes     # G2
studio gates reject <gate_id> --deliverable KEY --reason X     # G3 (free)
studio gates approve <gate_id> --checklist-file checklist.json # G3
studio render rebuild <run> [--deliverable KEY]                # stale S11+S12 only
studio media preview <run> --stage animatic|render
studio encode probe
```

## Test evidence (this branch)

- `tests/test_phase5_assemble.py` — T-ASM-01 (frame count and report on a real
  render), T-ASM-04 (both directions), T-ASM-05 (cache key + encoder profile),
  T-ASM-06 (j-cut refused at the loader and in the stage guard), T-ASM-07 (S6
  columns untouched), T-ASM-08 (zero provider calls from S11), T-ASM-09
  (insufficient disk, all problems in one message).
- `tests/test_phase5_render_qc.py` — T-QCR-01 (good render passes; wrong fps,
  wrong size, no audio, two audio streams, no faststart, off-by-one frame,
  quiet loudness, true-peak over, black section), T-QCR-02 (true peak measured
  on the muxed AAC), T-QCR-03 (upstream aggregate is a read), T-G3-02 (checklist
  stored; final requires every item), T-G3-03 (`auto` markers in sandbox),
  T-G3-01 partial (render sha changes the snapshot), `render rebuild` scoping
  and G3 CLI checklist behaviour.
- `tests/test_phase5_animatic.py` (15), `tests/test_phase5_video.py` (35),
  `tests/test_phase4_flow.py` — updated for the G3 park at the end of the chain.
- `tests/test_phase5_cli_coverage.py`, `tests/test_phase5_floors.py`,
  `tests/test_phase5_coverage_lift3.py` — the CLI surface end to end, the
  panel/assemble/QC error paths, and `media/clips.py`'s fail-closed preflight.
- `ruff check src/ tests/ scripts/` and `mypy --strict src/studio` are clean.

### Full-suite numbers (this branch)

```
pytest -m "not slow" --cov=studio --cov-report=json:coverage.json
  689 passed, 4 skipped, 1 deselected
python scripts/check_coverage_floors.py coverage.json coverage_floors.json
  every per-module floor PASSES (panel.py, stages_render.py, media/clips.py …)
  TOTAL 87.38 % (12063 / 13805) vs the 91 % floor — FAIL, see below
```

`media/clips.py` is now **88 %** (was 84 %) against its 85 % floor, and
`panel.py` / `stages_render.py` are back over their 88 % floors.

## Known deviations / open items

1. **ffmpeg 9.0.2 + ffprobe 5.2.0** (imageio-ffmpeg / `@ffprobe-installer`),
   not the spec's 7.1.1 pair, and only in this sandbox (`.tools/bin`, git
   ignored). Byte-exact expectations from other environments may differ; the
   frame-count and loudness assertions are version-independent.
2. **TOTAL coverage is 87.38 %, 500 statements short of the 91 % floor.**
   All per-module floors pass. The 500 lines are concentrated in areas that
   only a live or semi-live path reaches:
   `cli.py` 714 missing (tables, refusals, `tts audition`, `media smoke`,
   `run --production`), `pipeline/stages_audio.py` 66,
   `pipeline/qc_render.py` 55 (mostly `MediaToolError` branches),
   `providers/fal/adapter.py` 47, `demo/stages.py` 27, `config/channels.py` 24.
   The cheapest next steps are CLI refusal/error paths (each test is ~1 s) and
   the `qc_render` failure branches (synthetic bad files already in
   `tests/test_phase5_render_qc.py`).
   **No floor was lowered**: `total_floor` stays 91.0.
3. `studio doctor` has no encoder row yet (FFmpeg/x264/NVENC visibility).
4. T-G3-01's S13 half (`a non-render G3 round never unlocks export`) needs S13.
   The Stage-B-only floor rules now live in `coverage_floors_stage_b.json`
   (identical values) because `check_coverage_floors.py` refuses a glob that
   matches no file; they must move back with the commit that adds those
   modules.
5. T-ENG-18/T-ENG-19 and the demo DAG rewiring (real S9/S11/S12 handlers on
   mock providers, `--languages ar,en`) are the last Stage A/B bridge step.
6. `timing.j_cut_ms` stays refused (loader + S9/S11 guards); enabling it is a
   future timeline change, not a flag flip.

## Next

1. S13 export package + sidecar + commercial-use/licence blocks (Step 7), and
   with it the rest of T-G3-01.
2. S14 Bible proposals (paid, `spends_money`), S15 publish/purge (Step 8).
3. Production + demo DAG rewiring, T-GATE-02 static assertion, T-ENG-18/19.
4. Coverage: lift `media/clips.py` over 85 %, re-prove TOTAL ≥ 91 %.
5. `studio doctor` encoder row.
