# Part 1: Audit of the Phase 4 report

**Verdict: accepted with conditions.** The core contract held:
- All paid stages sit behind the H0 barrier, which covers the engine, the gateway and crash recovery.
- The crash matrix passed for fal.
- `--languages ar,en` leaves image and video call counts unchanged (T-ENG-16).
- Conform is frame-exact against real ffmpeg 7.1.1.
- Nothing live was run, and the report says so repeatedly.

**What the real tools found.** This validates R6 from the Phase 3 audit.
- Real ffprobe exposed a PNG probe bug that the stand-in had hidden.
- Real ffmpeg exposed the x264 chroma-SSIM problem (Deviation 2).
- Real ffmpeg exposed the `avg_frame_rate = 24.2` container artifact (Deviation 3).

I accept Deviations 2, 3, 4, 5, 6 and 7–14 as written. Deviation 3 matters for Phase 5: **container durations cannot be trusted for frame accounting**.

## Findings

| # | Sev | Finding | Required action |
|---|---|---|---|
| F1 | **High** | **Per-module coverage target silently missed.** H3 required each new module ≥ 85% and the report says "no per-module gate is applied". It is not in the Deviations list. Seven modules are under 85%: `media/sanitizer.py` 78, `providers/fal/adapter.py` 79, `video/ffmpeg.py` 77, `media/stages.py` 82, `qc/visual.py` 83, `media/registry.py` 83, `video/probe.py` 82. TOTAL is 90.15% against a 90% gate, a 0.15-point margin. Any Phase 5 code with less than 90% coverage fails CI. | Lift all seven to ≥ 85%. Add per-module floors enforced in CI. Target TOTAL ≥ 91%. Split `media/stages.py` first (F8). |
| F2 | Med | **`circuit_breaker_consecutive_failures` is parsed and validated but read by no code.** The fail-fast behavior is acceptable and I accept it. The report cites an operator confirmation on 2026-11-17 that I was not party to. Treat it as your decision. If it was not, tell me before Phase 5 starts. A dead config knob is a lie in the config file. | Deprecate and remove the knob (§D H5.1). |
| F3 | Med | **Policy-blocked recovery is unproven under fail-fast.** After the sanitizer cap the shot is `failed_policy` and, per the report, the run fails. The prompt said the operator then edits with `studio shot regenerate --prompt-file` and resumes. T-SAN-01 proves the parking, not the recovery. Sibling shots must not be re-paid. | T-P4F-01 (§P). |
| F4 | Med | **Clip-regeneration invariant is asserted by a test that never regenerates.** T-CLP-01 cites T-ENG-14 for "regeneration never changes `clip_duration_ms`/`clip_frames`/`speed_factor`/`hold_frames`". T-ENG-14 runs S10 once. T-KF-05 regenerates a keyframe, not a clip. | T-P4F-02. |
| F5 | Med | **The R4 gate problem is not visibly resolved.** `metadata_safety` carries a `G3_FINAL_CUT` gate and is upstream of the paid stages. T-INT-01 mentions only the G1 approval, and the §7 runbook mentions no G1 or metadata approval at all. The operator may be asked to approve a "final cut" before any media exists. | Discovery C1, then H5.7. |
| F6 | Low-Med | **The Discovery Report is cited, not embedded.** Item 7 of the report format required every §C item with paths. Deviation 7 says only T-LANG-01 changed among Phase 1–3 assertions. H4 should have changed Phase 2 tests that assumed `gpt-4o-mini`, and T-ENG-08, the mock size and the demo stage replacement were also touched. These may all be consistent, but the report does not show it. | Step 0 of the prompt: diff-based assertion audit. |
| F7 | Low | **§7 runbook is wrong in three places.** It says "auto-approve on draft" and then lists manual `gates approve` steps. It mentions an "assembler" that does not exist yet. It omits the G1 approval, so a user following it would see an idle worker. | H5.2. |
| F8 | Med | **`media/stages.py` is 982 statements** (s4p, S8a, S8, s10p, S10 and the retry policy in one file). Adding Phase 5 on top is unreviewable. | Pure-move split before any feature work (§D H5.8). |
| F9 | Low | **Migration 006 was a table rebuild that fixed a CHECK constraint bug in 005.** It is exercised only by one sheet test. Phase 5 needs new enum values and columns, so the same trap applies. | T-MIG-01: full chain 001→007 over seeded data, and enumerate every CHECK that new values touch. |
| F10 | Low ⚠ | **Possible filter deprecation, from my memory and unverified.** The report says S8's first-frame SSIM uses `scale2ref`. I believe `scale2ref` is deprecated or being merged into `scale` in newer FFmpeg lines. It works on 7.1.1, but clip dimensions are already known from ffprobe. | H5.3: replace with explicit `scale=W:H`. |
| F11 | Info | The register's #19 and #21 are odd items, and it omits behaviors the report itself discovered (the `tpad` freeze-pad, the SSIM luma choice, the `-r` pin). | Phase 5 register must include ffmpeg-behavior rows. |
| F12 | Info | The `PHASE3_AUDIT.md` probe fix landed in this phase's branch, not Phase 3. The report intro says "fixed in Phase 3". Cosmetic. | None. |
| F13 | Info | Still unobserved: Windows CI, any live fal call, T-EL-06, the live LLM smoke test, and ElevenLabs audition. | Operator list below. |

## Scope note on your request

You asked me to fold S13–S15 into Phase 5. My roadmap had Phase 7 owning S13–S15 and Phase 5 owning S9/S11/S12/NVENC. Merging them is feasible. It is also the largest prompt so far and the one most likely to exhaust a coding session.

I wrote the prompt with an explicit **Stage A / Stage B split** and a mandatory checkpoint commit between them. If the session runs short, it stops cleanly at the boundary and writes a partial report.
- **Stage A:** S9, G2 move, S11, S12, G3, encoders, hardening.
- **Stage B:** S13, S14, S15.

Phase 6 (Streamlit) still owns the UI. Until then the G3 audience checklist is CLI-driven.

## Decisions I made that you can overrule

1. **S13 makes no LLM call.** The title, description and hashtags were generated at `metadata_safety` and human-reviewed. A second LLM pass after approval would create unreviewed text. The blueprint called S13 "LLM-light", and I am deliberately removing that.
2. **S14 uses `llm.creative`** (Claude), following your allocation of Claude to "continuous Series Bible updates". The blueprint said LLM-light. Cost is about one call per episode.
3. **S15 is a service + CLI, not an engine DAG node.** It is operator-triggered and would otherwise leave every run waiting forever.
4. **`j_cut_ms` stays unsupported.** It is a hard refusal at S9 and S11, plus a documented v1.1 deferral. Implementing it would change S6's locked durations, which violates the standing invariant.
5. **Draft/sandbox exports go to `exports/_draft/…`.** Only final-profile exports land in `exports/<channel_key>/…`.

## What you need to do

**Not blocking Phase 5 coding** (it is entirely offline and zero-spend). Carried over and still needed before any real end-to-end run:
1. Send the Phase 3 and Phase 4 CI run links for ubuntu and windows-latest (R6, F13).
2. Run the live LLM smoke test (`STUDIO_LIVE_LLM=1`, ≤ $1) if not already done.
3. Fill the fal registry and run the Phase 4 audition and smoke tests, per the Phase 4 operator list.
4. Run the ElevenLabs audition and T-EL-06, then compare billed characters with the dashboard.

**After Phase 5 merges:**
- Run `studio encode probe` on your NVENC machine. Then run `STUDIO_LIVE_NVENC=1 pytest -k nvenc_live`, which is free and local.
- Watch one sandbox demo animatic and one demo final render, and check A/V sync by eye.
- Return the Phase 5 report.

---

# Part 2: Phase 5 Implementation Prompt

=== BEGIN PHASE 5 PROMPT ===

# PHASE 5 — Assembly, Final Cut, Export, Bible Proposals, Retention
Stages: **S9 animatic (+ Gate G2 moved here), S11 assemble, S12 render QC (+ Gate G3 on a render), S13 export package, S14 bible proposals, S15 publish/purge.**
Plus the Phase 4 carry-over hardening (H5.0–H5.8).

## A. Role, mission, staging

You are a senior Python engineer extending the `ai-studio` repo (baseline: Phase 4 merged; 536 collected tests; ruff clean; `mypy --strict` clean on 110 files; TOTAL coverage 90.15%). You have real ffmpeg/ffprobe. Use them. Do not stub what you can run.

**Mission:** after Phase 5, a production-shaped run goes `… s8 → s9(G2) → s10p → s10 → s11 → s12(G3) → s13 → s14`, produces a verified, frame-exact, A/V-synced, loudness-checked MP4 per deliverable, exports a package with a sidecar, proposes (never applies) Bible updates, and can be purged after "mark published", without ever paying twice, exceeding a cap, or losing recoverability.

**Staging (mandatory):**
- **Stage A** = Steps 0–6 of §R (hardening, G2 move, S9, encoders, S11, S12, G3).
- **Stage B** = Steps 7–9 (S13, S14, S15, integration).
- At the end of Stage A, commit, run the full suite, and write `PHASE5A_CHECKPOINT.md` (test results, coverage table, open issues).
- If your session is running low on context or budget, **stop at a stage boundary** rather than rushing Stage B. Write a partial report stating exactly what is done and what is not.
- Never leave a half-wired stage in the DAG.

**Non-goals (do not build):**
- The Streamlit UI (Phase 6).
- Direct uploads.
- C2PA signing.
- Auto-chapters.
- Thumbnails with text.
- `j_cut_ms`/L-cuts.
- Crossfades or transitions (hard cuts only in v1).
- `chain_mode: last_frame`.
- Visual-LLM QC.
- Any new paid media call.

## B. Hard rules

1. **No new runtime dependencies.** No PIL, numpy, pydub, SDKs, lxml or ffmpeg-python. Use stdlib, `httpx` (already present) and ffmpeg/ffprobe via subprocess. Dev dependencies stay as they are. Coverage JSON output is part of coverage.py.
2. Never modify S1–S3 prompts or schemas. New prompt files are allowed (S14 only).
3. **Never mutate S6's locked values** (`clip_duration_ms`, `clip_frames`, `speed_factor`, `hold_frames`, total samples). S9/S11/S12/S13 read them and assert equality. They never recompute them with different inputs. `build_timeline` is reused, not reimplemented.
4. **No paid provider call from S9, S11, S12, S13 or S15.** Tests assert zero provider calls (spy providers). S14 is the only new paid path, and it is marked `spends_money`.
5. Never trust container-level duration or `avg_frame_rate` for frame accounting. Phase 4 showed ffmpeg 7.1.1 writes `(n-1)/fps` into mdhd for filter-graph CFR output. Use `nb_read_frames` (`-count_frames`) or an equivalent decode count, and S6's frame numbers.
6. Never use `-shortest`. Never let ffmpeg silently pad, trim, drop or duplicate frames. Every produced video is verified by frame count against the expected value, and a mismatch is an error.
7. Never crop or stretch beyond the declared tolerance (§E4). Never auto-switch encoders when `encode.encoder` is explicitly `nvenc` (fail closed). Fallback happens only under `auto` and is logged in the render report.
8. Never overwrite an existing export directory or its contents. Never delete a protected asset (§M).
9. Never put secrets, signed URLs or absolute home paths in sidecars, audit rows or logs.
10. Never invent platform facts. Every platform limit, bitrate, loudness or policy number must be configurable and marked `⚠ unverified` in config comments and in your register.
11. T-LANG-01: the lint scan must cover every new package. No literal `"ar"`/`"en"` outside the allowed locations. Channel keys come from config.
12. Anything you change in Phases 1–4 beyond §O must be reported in Deviations, including every changed assertion.
13. Every behavior you could not verify (Windows, NVENC, AAC overshoot, concat copy-join, `silencedetect` accuracy) goes in the Unverified register with the test that pins your handling of it.

## C. Discovery Protocol (before any code)

Write `PHASE5_DISCOVERY.md` and **embed it verbatim in the final report**. For every item, give an answer plus the file paths and symbols you inspected. Where you disagree with this prompt, say so and resolve fail-closed.

- **C1 Gates and paid ancestry.**
  - List the production and demo DAGs with `gate_after`, `spends_money` and dependencies.
  - Where does `metadata_safety` (its `G3_FINAL_CUT` gate) sit relative to `s4p`, S5 and S8a?
  - What exactly must an operator approve today, in order, before S5 can run?
  - Is any gate whose kind is `G3_FINAL_CUT` an ancestor of any paid stage?
- **C2 Gate machinery.**
  - `request_changes`/`reject` semantics, rounds, `snapshot_hash` derivation, and how `gate_after` is consumed.
  - Which stage re-runs on reject today, and per-subject rejection (G0 per character, shots).
  - `auto_approve` rules.
  - Where approval metadata could be stored (existing JSON column?) for a checklist.
- **C3 S6/timeline.**
  - The signature and return of `build_timeline`.
  - Where fps, total frames, sample rate and `total_samples` live.
  - The exact relation between total video frames and `total_samples` for a deliverable.
  - Which S6 outputs are per track and which are per deliverable.
- **C4 S7b/audio QC.**
  - The master asset format (sample rate, channels, sample format).
  - The mastering targets (LUFS, true peak).
  - Which `qc/audio.py` functions can be pointed at an arbitrary decoded WAV (loudness, true peak, clipping, duck depth).
  - Where S7b QC results are stored.
- **C5 S10 outputs.**
  - How to select, per shot, the accepted attempt and its conformed asset.
  - What is stored on `shot_attempts` (`clip_frames`? geometry?).
  - What happens to a shot with no accepted attempt.
  - The raw clip geometry/codec variability you observe in mock providers.
- **C6 Assets/CAS/retention.**
  - The CAS API (put, put_file, move-in, path, delete).
  - Retention classes.
  - What `studio purge` does today.
  - Is there `assets.purged_at` or equivalent?
  - What the gateway does on a cache hit whose asset file or row is missing.
  - How "references" to an asset are determined (columns on `shot_attempts`, `provider_calls`, `character_sheets`, `narration_lines`, stage outputs).
  - Can one CAS file back multiple asset rows?
- **C7 Episodes/deliverables/channels.**
  - Status columns and CHECK constraints for episodes, deliverables, runs and `shot_attempts`.
  - `channel_for(language, aspect)` and the contents of `channel_profiles.yaml`.
  - The slug source for an episode.
  - Any existing `published_at` or export columns.
- **C8 Metadata and licenses.**
  - The `metadata_safety` output schema (per language? fields?).
  - Where adaptation `source_ref`/`source_license` are stored.
  - Library license records for music/SFX and how the S7 audio plan references them per cue.
  - Which models appear in `provider_calls` for an episode.
- **C9 Bible.**
  - Storage and versioning of the Series Bible (migration 003).
  - The existing read path (context pack).
  - Whether any write path exists.
  - The structured-output helper S1–S3 use through the gateway, and how a new prompt file is registered and sha'd.
- **C10 ffmpeg wrappers.**
  - The runner abstraction in `video/ffmpeg.py` (injectable? timeout? stderr capture?).
  - The filter-script option and version probe.
  - Every use of `scale2ref` in `src/`.
  - How Windows paths are escaped for filter graphs and concat lists today.
- **C11 CLI inventory.** Existing `purge`, `export`, `publish`, `render`, `bible`, `episode` and `encode` commands.
- **C12 Phase 4 carry-over audit.**
  - Run `git diff 952c74c --stat -- tests/` and list every pre-existing test file touched.
  - For each, list every changed or removed assertion line, grouped by reason (H4, T-ENG-08, mock size, demo replacement, lint-only).
  - Include the per-module coverage table from the Phase 4 report, re-measured.
- **C13 Demo DAG.** What `demo_finalize` does today, and what the demo path would need to run real S9–S13.

## D. Phase 4 carry-over hardening

- **H5.0 Assertion audit.** Produce the C12 output. Any assertion that was weakened without being listed in the Phase 4 report is a Deviation you must report and justify, or restore.
- **H5.1 Circuit-breaker honesty.**
  - Remove `media.circuit_breaker_consecutive_failures` from the settings model and `settings.yaml`.
  - To avoid breaking existing files, a present key is accepted once with a `DeprecationWarning` logged via the audit path and a `studio doctor` INFO row: "ignored: media stage is fail-fast".
  - Update README and the Phase 4 section accordingly.
  - Fail-fast stays.
- **H5.2 Runbook fix.** README and `--help` text list the true gate order and approval commands. No mention of an assembler that does not exist. "Auto-approve" is described only as a sandbox/draft setting.
- **H5.3 `scale2ref` removal ⚠.** Replace any `scale2ref` use with an explicit `scale=W:H` computed from ffprobe dimensions of the clip. This applies to the first-frame SSIM comparison. Re-run the Phase 4 SSIM tests unchanged. They must pass with identical expected values (±0.01). If they cannot, report why.
- **H5.4 Policy-blocked recovery.** Make T-P4F-01 pass. This covers a shot that reaches `failed_policy`, the human editing via `studio shot regenerate --prompt-file`, and `studio resume`. Fix whatever blocks it, and report fixes as Deviations.
- **H5.5 Clip regeneration invariants.** Make T-P4F-02 pass.
- **H5.6 Migration chain.** T-MIG-01 (§P).
- **H5.7 Final-cut gate hygiene (R4).**
  - Required outcome, whatever Discovery C1 finds: no paid stage may have an ancestor gate whose *kind* is `G3_FINAL_CUT` (the final cut), and the final cut is reviewed only on a render.
  - The `metadata_safety` text review stays mandatory before export. It is a text approval, not a cut approval.
  - Preferred mechanism: re-kind the `metadata_safety` gate as `G1_SCRIPT` (it is text approval), in the gate spec, with a data migration in 007 for any existing rows (pre-release, so expected to be zero).
  - If Discovery shows a different minimal fix, take it and justify it.
  - Add a static DAG test (T-GATE-02): for every `spends_money` stage, assert no ancestor gate kind equals `G3_FINAL_CUT`.
  - Add the carry-forward test (T-G3-01): export requires a G3 round whose gated stage is `s12` (a render), and approving any other G3-kind round does not satisfy it.
- **H5.8 Split `media/stages.py` — pure move first.**
  - Split into cohesive modules, e.g. `media/preflight.py` (s4p, s10p), `media/sheets.py` (S8a), `media/keyframes.py` (S8), `media/clips.py` (S10), `media/retry.py` (shared attempt policy).
  - Keep a thin `media/stages.py` re-export shim only if needed to avoid editing >10 call sites.
  - This commit must be behavior-neutral: the same tests pass with no assertion edits, and `git diff -M --stat` shows renames/moves.
  - Only after that commit, lift coverage (§Q).

## E. Architecture decisions

### E1. DAG
- Production: `s4p → s5a → s5 → s6 → s7 → s7b → s8a(G0) → s8 → s9(G2) → s10p → s10 → s11 → s12(G3) → s13 → s14`.
- S8 loses its `gate_after`. S9 gains `G2_KEYFRAMES`. S12 gains `G3_FINAL_CUT`.
- Scopes: S9 deliverable, S11 deliverable, S12 deliverable (render), S13 deliverable, S14 episode.
- The demo DAG gets the same handlers over mock providers. Replace `demo_finalize` with real S9, S11, S12, S13 and S14 stages (§O).

### E2. New StageSpec flags (§O-authorized)
- `requires_cleared_gates: bool`. The H0 barrier applies when `spends_money or requires_cleared_gates`. It is set on S13 (free but export-gated). S14 is `spends_money`.
- `gate_reject_stage: str | None`. The stage re-run when a gate row is rejected. Default is the gated stage. G2 on S9 sets it to `s8_keyframes`. G3 on S12 sets it to `s11_assemble`.
- The barrier additionally refuses paid stages when the episode status is `published` or `archived` (error `EpisodeClosed`, with an audit row). Free stages remain runnable so a re-export is possible.

### E3. G2 and G3 snapshot rules
- **G2 snapshot = S9's deterministic input-content hash.** It covers the ordered accepted keyframe shas, the master sha for each deliverable, the S6 timeline hash and the animatic settings. It does **not** depend on the animatic MP4's bytes, because x264 output is not guaranteed byte-stable across thread counts or versions.
- **G3 snapshot = the sha256 of each deliverable's render asset plus the QC report hashes.** The human approves the exact file they watched, and S13 exports exactly that asset.
- Stage outputs contain no timestamps and no mutable ledger numbers (§G4).

### E4. Geometry (`video/geometry.py`, pure and table-tested)
`fit_plan(src_w, src_h, dst_w, dst_h, tol_pct)`:
- If the source aspect differs from the destination aspect by more than `tol_pct` (default `assemble.geometry_tolerance_pct: 2.0`), raise `GeometryError`.
- Otherwise plan "scale to cover, then center-crop to exact destination size". Report `crop_loss_pct` per axis. Uniform scale only, never non-uniform stretch.
- Always emit `setsar=1`.
- Scaling uses lanczos.
- Render-geometry adjustments are recorded in the render report and the sidecar.
- Real fal clips will not be 1080p. The same-aspect upscale is expected and is not a violation.

### E5. Segmented render primitive (`assemble/segments.py`)
A 20-minute episode can have about 240 clips, so a single ffmpeg graph with N inputs is forbidden past `assemble.max_inputs_per_graph` (24). Recommended design D1, which you may replace if evidence requires, with the decision recorded:
1. Render each shot to its own **segment** with one ffmpeg process: geometry plan applied, `-an`, explicit `-frames:v clip_frames`, and the final encoder profile.
2. Use a container without MP4 edit-list quirks for segments (Matroska or MPEG-TS), recorded as `assemble.segment_container`.
3. Verify each segment's frame count by decode count. Write a segment sidecar `.ok.json` containing the frame count, input shas and encoder profile hash.
4. Join segments by the concat demuxer with `-c copy`, then **mux** with the AAC audio into MP4 (`-c:v copy`, `+faststart`).
5. Verify total frames equal Σ `clip_frames`.

- **Required evidence:** run the frame-run-length test (T-ASM-01) against the real ffmpeg in your sandbox.
- If copy-join cannot be made frame-exact, fall back to a documented mezzanine (crf ≤ 12 intermediate) plus one final encode. Report this and its quality implications.
- Segments live under `data/tmp/render/<run>/<deliverable>/<input_hash>/`. Verified segments are reused on crash-resume and deleted on success.
- Paths are passed in list files and filter scripts with a single escaping helper, tested on Windows-style paths (spaces, apostrophes, non-ASCII, drive letters, as string-level tests that run on Linux too).

### E6. Encoder profile (`video/encoders.py`)
- `EncoderProfile` is a frozen dataclass: name, args list, `profile_hash`.
- Settings `encode.encoder: auto|nvenc|x264`.
- NVENC defaults from the blueprint: `h264_nvenc -preset p5 -rc vbr -cq 19 -b:v 0 -pix_fmt yuv420p`, with per-preset `maxrate`/`bufsize` caps (⚠ operator-tunable, not platform-mandated).
- x264 fallback: `libx264 -crf 18 -preset medium`.
- Tag the output as BT.709 limited range (`-colorspace bt709 -color_primaries bt709 -color_trc bt709 -color_range tv`) ⚠. For still-image inputs (animatic only), use `scale=…:out_color_matrix=bt709` because swscale's RGB→YUV default is not BT.709 ⚠.
- Use `-fflags +bitexact -flags:a +bitexact` where it keeps muxing deterministic ⚠.

## F. Config and migration 007

Add to `settings.yaml` with strict validation (`extra=forbid`). Numeric defaults below are uncalibrated placeholders and are marked ⚠ in comments.

- `encode: {encoder: auto, nvenc: {preset: p5, rc: vbr, cq: 19}, x264: {crf: 18, preset: medium}, probe: {timeout_s: 30, cache_file: data/cache/encoder_probe.json}}`
- `assemble: {segment_container: mkv, max_inputs_per_graph: 24, geometry_tolerance_pct: 2.0, min_free_disk_factor: 2.5, av_sync: {warn_ms: 40, fail_ms: 100}}`
- `animatic: {short_side_px: 540, crf: 28, preset: veryfast, audio_bitrate_k: 96}`
- `qc.render: {video_scan: true, duration_tolerance_frames: 1, true_peak_warn_db: -1.0, true_peak_fail_db: -0.3}`. Reuse the Phase 3 loudness thresholds from the existing config and do not duplicate them.
- `export: {root: exports, draft_root: exports/_draft, thumbnails: 3}`
- `bible: {proposals: {model_role: creative, max_facts: 40}}`
- `retention: {rejected_attempt_days: 7}`. The existing `keep_raw_clips_days` (30) and `keep_narration_audio_days` (30) are unchanged.

New file `configs/export_presets.yaml`, keyed by channel key (`yt_ar_wide`, `yt_en_wide`, `short_ar_tall`, `short_en_tall`):
- `aspect`, `width`, `height` (1920×1080 or 1080×1920), `fps: 24`.
- `video`: `maxrate_k`, `bufsize_k` (⚠).
- `audio`: AAC, 192k, 48000 Hz, stereo.
- `limits` (all ⚠ from memory, WARN-level only): `max_duration_s` (Shorts), `title_max_chars`, `description_max_bytes`, `hashtags_max`.
- The key set must equal the key set of `channel_profiles.yaml`. Validate at load (T-EXP-04).

**Migration `007_assembly.sql`.** First enumerate every CHECK constraint touched by new enum values (episode/run statuses, gate kinds, asset kinds/retention). Use the table-rebuild pattern where needed. Lesson from 006: a CHECK that rejects a new value is a crash.
- `assets.purged_at`, `assets.purge_reason` (if absent).
- `exports` table: `id, deliverable_id, render_asset_id, dir_path, profile, status, created_at`, with `UNIQUE(deliverable_id, render_asset_id)`.
- `bible_proposals`: `id, episode_id, base_bible_version, status (pending|accepted|rejected|stale), proposal_json, provider_call_id, created_at, decided_at`.
- `deliverables.published_at`, `deliverables.published_url`.
- `episodes.status` gains `published` and `archived` if such a column exists. Discovery C7 decides the exact column.
- A gate checklist column (e.g. `approval_gates.checklist_json`) if no suitable column exists.
- Any `metadata_safety` gate re-kind data migration (H5.7).

## G. S9 — Animatic + Gate G2 relocation

**G1. Purpose.** A free, local review artifact per deliverable. The operator sees the stills at the *real* timing with the *real* mastered audio *before* any clip money is spent. Hard cuts only.

**G2. Build.**
- Inputs: accepted keyframes per shot (S8), the S6 timeline via `build_timeline`, the S7b master, the animatic settings.
- Render each shot as a still held for exactly `clip_frames` frames at `timing.fps`, using the segment primitive (E5). Geometry is via `fit_plan` to a `short_side_px` canvas of the track's aspect, so tall is 540×960 and wide is 960×540.
- Join the segments, then mux the master as AAC at `audio_bitrate_k`.
- Verify the frame count equals Σ `clip_frames` and the audio duration equals the video within the A/V tolerance.
- No `drawtext`. It is fragile on Windows because of fonts and path escaping.
- Outputs: the animatic asset (retention `intermediate`), plus `animatic_shots.json` containing, per shot, `index, shot_id, attempt_id, keyframe_sha, start_frame, end_frame, start_ms, clip_duration_ms, narration lines` (text escaped at render time).
- `j_cut_ms != 0` raises `ConfigError` at S9. S9 never mutates S6.

**G3. Gate relocation.**
- S8 has no gate. S9 carries G2.
- Reject on G2 takes explicit subjects: `studio gates reject <gate-id> --shots 3,7 --reason "…" --yes`, or `--all`, which shows the cost and requires `--yes`.
- Reject delegates to the same code path as `studio shot regenerate --stage keyframe` for each listed shot. That means a fresh nonce, a cost display and a cost check, only those shots re-paid, and S8 re-run for them (via `gate_reject_stage = s8_keyframes`).
- That re-run stales S9, closes the old gate round and creates a new round on the rebuilt animatic. The barrier allows this because G2 is no longer an ancestor of S8.
- A legacy approval recorded on the old S8 gate does **not** satisfy S9's gate. The operator re-approves, which is cheap (T-G2-04).
- `auto_approve` behaves as before: allowed in sandbox/draft, always refused in final.

**G4. Gate panel (not part of stage output).**
- `pipeline/panel.py::gate_panel(run_id, gate_id)` computes, at presentation time:
  - the S4 media estimate by bucket (tts/sheets/keyframes/video, low/expected/high);
  - spent to date by capability and model, and TTS estimate vs actual;
  - exact clip projection, first-pass and with retries (reuse S8's projection helpers and `_estimate_drift`);
  - the remaining episode and monthly cap;
  - per-bucket drift percentage with warn flags;
  - a prominent verdict: `ok` or `over_remaining_cap (s10p would halt)`.
- A frozen copy is written to the gate row's presented-state JSON at gate creation, for audit.
- The panel is rendered by `studio gates show <gate-id>` and `studio media preview <run> --stage animatic`.
- Mutable numbers never enter S9's stage output or the snapshot hash.

**G5. Preview.** `studio media preview <run> --stage animatic` writes a static HTML file. It contains an HTML5 `<video>` for each deliverable, a shot table, and a small inline script that highlights the current shot on `timeupdate`. All text is HTML-escaped, `dir="auto"` applies to narration text, and the gate panel is included.

## H. Encoders and NVENC probing

- `video/encoders.py::resolve_encoder(settings, runner)`:
  - Under `x264`, return x264.
  - Under `nvenc` or `auto`, perform an **actual probe encode**: a 24-frame `lavfi` `color` source at each preset geometry (1080×1920 and 1920×1080), using the exact production NVENC arguments, with a timeout.
  - Success requires exit 0, a non-empty output and the correct frame count. Presence in `-encoders` is never sufficient.
  - Under `nvenc`, a failed probe raises `EncoderUnavailable` (fail closed). Under `auto`, it falls back to x264 and records `fallback_reason` (stderr tail, scrubbed).
- Cache the result in `encode.probe.cache_file`, keyed by `(ffmpeg version, platform, nvidia-smi driver string if available, geometry)`. `nvidia-smi` is optional and its absence is not an error.
- `studio encode probe` prints the resolved encoder, the reason and the cache state. `studio doctor` gets an encoder row.
- Record the resolved encoder name, args and `profile_hash` in the S11 cache key and render report (encoders produce different bytes).
- Extend `scripts/check_ffmpeg.py` and doctor with the filters `concat`, `loudnorm`, `ebur128`, `silencedetect`, `astats`, `aresample`, `anullsrc`, the encoders `aac` and `libx264`, and the muxers `mp4`, `matroska` and `mpegts`.
- Tests use an injected fake runner for all branches (T-ENC-01). A live local test is opt-in with `STUDIO_LIVE_NVENC=1` (free, no network): a 2-second encode with frame-count verification (T-ENC-02).

## I. S11 — Assemble (per deliverable)

**I1. Preflight (free, fail-closed).**
- Every shot has an accepted conformed clip.
- The master exists, and its sample count equals S6's `total_samples` exactly.
- The geometry plan passes for every clip.
- The encoder resolves.
- Free disk is at least `min_free_disk_factor` × (Σ clip bytes + master bytes), otherwise error `InsufficientDisk` (T-ASM-09).
- `j_cut_ms == 0`.
- Each problem is listed in one error.

**I2. Render.**
- Follow E5. Re-probe every conformed clip. Never assume it has no audio stream or that it matches the preset. Map video only.
- The audio is the S7b master encoded once to AAC per the preset (48 kHz, stereo, 192k). Resample only if the master's rate differs.
- No filters on audio beyond resampling.

**I3. Verify (inside S11; S12 re-verifies independently).**
- Video frame count equals Σ `clip_frames`, exactly.
- Expected video duration is `frames / fps`.
- Muxed audio duration is within the A/V tolerance of the video.

**I4. Outputs and caching.**
- The render asset has retention `permanent`.
- A render report records the encoder, args hash, geometry adjustments (`crop_loss_pct`), segment count, timings and ffmpeg version.
- The cache key is a hash of: the ordered conformed-clip shas, the master sha, the preset, the encoder `profile_hash`, and an `ASSEMBLER_VERSION` constant. The same key means skip, free but time-saving.
- Crash-resume reuses `.ok.json`-verified segments and re-does only the rest (T-CRASH-05).

## J. S12 — Render QC and Gate G3

Run these checks against the final MP4 only, never against intermediates. The **muxed AAC** is measured. Do not copy numbers from S7b.

1. The file is non-zero and ffprobe-parseable. The container is MP4.
2. **Faststart:** a stdlib top-level box scan shows `moov` before `mdat`. Handle 64-bit sizes and size-0 boxes.
3. Streams: exactly one video (h264, yuv420p, `24/1` fps, preset width and height, SAR 1:1, BT.709 tags) and exactly one audio (aac, 48000, 2 ch).
4. Decoded frame count equals Σ `clip_frames`, exactly.
5. Video and audio durations are within tolerance, and A/V start-time delta is within `av_sync` (warn at 40 ms, fail at 100 ms, ⚠ uncalibrated).
6. Loudness of the muxed audio is measured by decoding to WAV and reusing the Phase 3 analyzers (integrated LUFS vs target, sample peak, clipping). **True peak is re-measured on the muxed AAC** (R10: AAC can overshoot about 0.5 dB). Warn and fail ceilings are `qc.render.true_peak_*` (⚠). The fix advice on failure is "lower the mastering true-peak target and re-run S7b", with no silent gain change in S11.
7. Video scan (`qc.render.video_scan`): `blackdetect` and `freezedetect` over the whole render, plus first- and last-frame black checks, reusing `video/analysis.py`. These are warn-level at render scope.
8. An aggregate of upstream QC results (S3b tashkeel integrity, S7b mix QC, S10 clip QC) is read, not recomputed.
9. Preset `limits` produce WARNs (duration for shorts, bitrate over `maxrate`, title/description/hashtag limits where the metadata is known). These are WARN-level only because the numbers are ⚠.

**Result:** a QC report asset plus a summary `pass|warn|fail`.
- `fail` fails the stage (fail-fast, consistent with the rest of the engine).
- Remediation is by changing settings or inputs, which changes `input_hash`, which stales S11 and S12 naturally.
- Add `studio render rebuild <run> [--deliverable KEY]`, which invalidates S11 and S12 only (free).

**Gate G3 on S12.**
- The snapshot is E3's render shas plus QC report hashes.
- Approval requires a **checklist** passed as `--checklist-file checklist.json` or flags: `reviewed_full_render`, `audience_made_for_kids` (true/false, with no default in final), `ai_disclosure` (default true), `third_party_ip_confirmed`, `licenses_confirmed`.
- The checklist is stored on the gate row and copied to the sidecar.
- In final, approval fails if any item is missing. In sandbox/draft with `auto_approve`, the checklist is filled with `auto` markers and the sidecar says so.
- `auto_approve` is always refused in final.
- Reject takes `--deliverable KEY --reason …`. It re-runs S11 for that deliverable, which is free, and creates a new G3 round.
- **A G3-kind round on any other stage, including `metadata_safety` if it was not re-kinded, never satisfies S13.**

## K. S13 — Export package (per deliverable, free, no LLM)

`requires_cleared_gates=True`, so the barrier demands G1, G0, G2 and G3 (on S12) all approved against current snapshots. It makes no provider call and no network call (T-EXP-05).

**Blocks (final profile), all collected and reported at once:**
1. Any model in the episode's settled `provider_calls` with `commercial_use_verified: false`. This is deliberately a conservative superset. Override via `studio export … --override-commercial-use --reason "…"`, which writes an audit row and appears in the sidecar's `overrides`.
2. Any music or SFX cue in the deliverable's audio plan lacking a license record.
3. An adaptation missing `source_ref` or `source_license`.
4. An incomplete G3 checklist.
5. S12 `fail`.

In draft/sandbox these become `export_flags` in the sidecar, with `profile: draft`, and the output goes under `export.draft_root`. Final goes under `export.root`.

**Layout:** `<root>/<channel_key>/<YYYYMMDD>_<slug>/` containing `<slug>_<lang>_<aspect>.mp4`, `metadata.json`, `credits_licenses.txt` and `thumbnail_candidates/`.
- The date is the first-export date, persisted in the `exports` row. Re-running never creates a new date directory.
- The slug is ASCII `[a-z0-9-]`, at most 48 chars, from the episode slug source (C7). Guard Windows reserved names (CON, NUL, COM1 …). Warn when the full path exceeds 240 characters.
- The export is **atomic**: write to `<dir>.tmp-<uuid>`, fsync, rename. Leftover `.tmp-*` directories are cleaned on the next run and flagged by doctor.
- The MP4 is copied (or hard-linked where safe) from the approved render asset, and its sha256 is verified after copy.
- A new approved render (different sha) creates `<dir>_r2`. Prior exports are never overwritten (T-EXP-07).

**`metadata.json`** (UTF-8, `ensure_ascii=False`, sorted keys, indent 2, `schema_version: 1`) contains:
- `channel_key, language, aspect, preset, profile`.
- `title, description, hashtags` copied from the approved `metadata_safety` output. **No text is regenerated.**
- `audience: {made_for_kids, checklist, gate_id, round}` and `ai_disclosure`.
- `source: {source_ref, source_license}`.
- `render: {file, sha256, bytes, duration_s, frames, fps, width, height, video_codec, encoder, audio_codec, sample_rate, integrated_lufs, true_peak_dbtp, av_offset_ms, geometry_adjust}`.
- `qc: {status, warnings}`.
- `credits` (license ids) and `models` (role, model_key, provider, `commercial_use_verified`).
- `cost: {total_musd, by_stage}`.
- `gates` (ids, rounds and approval times) and `overrides`.
- `files` with sha256 for each exported file.

Platform AI-disclosure toggles are set on the platform at upload time, which this tool cannot do. `credits_licenses.txt` therefore carries a plain reminder line, and README says so. Do not assert any specific platform policy text.

**Thumbnails:** `thumbnail_candidates/` holds N frames (`export.thumbnails`, default 3) extracted from the render at deterministic frame indexes (~15%, ~45%, ~75%) with an exact-frame `select`. No text overlays.

## L. S14 — Bible proposals (episode, paid, `llm.creative`)

- The new prompt file `configs/prompts/bible_update.txt` is sha'd as the prompt version. It is called through the gateway with the same structured-output helper S1–S3 use. It is a single-call provider and follows the existing `submit_started_at` / `potential_orphan` rules.
- It is `spends_money`, so the H0 barrier applies (blocked before G3 is cleared).
- Input: S1 scenes (pivot language), the S3 plan summary (characters, locations) and the current bible version.
- **Output schema** (strict validator):
  - `new_characters[]`, `character_changes[]`, `locations[]`, `plot_events[]`, `unresolved_threads[]`, `continuity_notes[]`.
  - Every fact carries `evidence: [{scene_id}]` with at least one entry.
  - Every `scene_id` must exist in this episode's S1 output.
  - Character references must match a known bible name or an entry in `new_characters`.
  - At most `bible.proposals.max_facts` facts, with length caps.
  - Any violation is rejected, and the call is not re-paid in a loop. Allow at most one paid regeneration with a new nonce, then fail to the operator.
- Result is stored as a `bible_proposals` row (status `pending`, `base_bible_version`). **Nothing is auto-applied**, in any profile. The run completes regardless of pending proposals.
- CLI: `studio bible proposals list|show|accept|reject <id>`.
  - `accept` applies through the bible repository with a version bump and an audit row.
  - If the bible version moved since the proposal's `base_bible_version`, accept refuses with status `stale`. The operator regenerates with `studio bible propose <episode> --regenerate --yes`, which is paid and shows the cost.
- Resume never double-pays. The cache key is stable across crash-resume. The nonce changes only on `--regenerate`.

## M. S15 — Publish marker and purge (service + CLI, not an engine node)

- `studio publish mark <deliverable-id> --url URL [--published-at ISO] --yes`:
  - requires an `exports` row for the deliverable;
  - sets `published_at` and `published_url` and writes an audit row;
  - when all of the episode's deliverables are published, sets the episode status to `published`.
  - From `published` on, the barrier refuses paid stages (`EpisodeClosed`) while free stages stay runnable.
- `studio purge plan --episode E` prints per class: count, bytes and the rule that selected each item. It deletes nothing. The default for `purge run` is a dry run.
- `studio purge run --episode E --yes`:
  - refuses unless the episode is `published`;
  - deletes files, then sets `assets.purged_at` and `purge_reason`;
  - writes one batch audit row with counts and bytes (action names prefixed `s15_purge_*`);
  - re-checks protection at deletion time (not only at plan time);
  - retries a locked file three times with 0.5 s backoff (Windows), then skips it, reports it and exits non-zero;
  - is idempotent;
  - resumes safely after a crash mid-purge.
- `studio purge gc` (dry run default) removes CAS files with no asset row, leftover `data/tmp/**`, and stale `.tmp-*` export directories.

**Protected, never deleted:**
- Assets with retention `permanent`: final renders, sidecars, export directories and the Series Bible.
- Assets referenced by an approved `character_sheets` row (any series).
- Gate-approved keyframes.
- Embeddings and cached character data.
- **Any asset still referenced by any non-published episode or run.** The CAS can share files across episodes, so check references across **all** episodes, not just this one.

**Deletable:**
- Intermediates: conformed clips, segments, animatics, stems, mixes, conformed audio and QC scratch.
- Raw clips older than `keep_raw_clips_days`.
- Raw TTS older than `keep_narration_audio_days`.
- Assets of non-accepted attempts older than `retention.rejected_attempt_days`.

**Never-pay-twice after purge (priority 1).**
- A gateway cache hit whose asset is purged must **fail closed** with `AssetPurged` and make zero provider calls.
- It must never silently re-pay. A deliberate re-pay requires an explicit `--allow-repay-purged` flag on an explicit CLI action, with a cost display and an audit row.
- A purged intermediate can be rebuilt for free from retained sources if they exist. If the raw clips are gone, only the final render and the export remain, and this is documented.

## N. CLI additions (all destructive or paid actions require `--yes` and show cost)

- `studio gates show <gate-id>`
- `studio gates reject <gate-id> --shots … | --all | --deliverable KEY --reason …`
- `studio gates approve <gate-id> [--checklist-file f | flags]`
- `studio render rebuild <run> [--deliverable KEY]`
- `studio encode probe`
- `studio export <run> [--deliverable KEY] [--override-commercial-use --reason …]`
- `studio publish mark …`
- `studio purge plan|run|gc …`
- `studio bible proposals list|show|accept|reject`, `studio bible propose … --regenerate`
- `studio media preview <run> --stage animatic|render`
- `studio doctor` additions: encoder probe, export root writable, free disk, presets vs channels, retention settings sanity, leftover `.tmp-*` and the deprecated-knob INFO.

## O. Explicitly authorized changes to earlier phases

1. Production DAG extension and demo DAG replacement of `demo_finalize` with real S9–S14 over mock providers.
2. Moving `gate_after` from S8 to S9, and the new `gate_reject_stage`, `requires_cleared_gates` and `EpisodeClosed` barrier behavior.
3. Removing `media.circuit_breaker_consecutive_failures` (H5.1).
4. `scale2ref` replacement (H5.3).
5. The `media/stages.py` split (H5.8) and import-path updates.
6. The `metadata_safety` gate re-kind (H5.7) and its test updates.
7. Migration 007.
8. Coverage and CI changes (§Q).

Everything else is a reportable Deviation. For every Phase 1–4 assertion you change, give the old line, the new line and the reason.

## P. Mandatory tests

ffmpeg-marked tests run real ffmpeg. Keep clips tiny: mostly ≤ 3 s and small geometry via a test preset override. Mark anything over 20 s as `slow`.

**Hardening**
- **T-P4F-01:** a shot hits `failed_policy`. The human edits via `shot regenerate --prompt-file`, then `resume`. The shot completes, and sibling shots are **not** re-paid (provider call count asserted).
- **T-P4F-02:** regenerate a clip. S6's `clip_duration_ms`, `clip_frames`, `speed_factor` and `hold_frames` are byte-identical before and after. The new conformed clip has exactly `clip_frames` frames.
- **T-MIG-01:** apply migrations 001→007 over a database seeded at 004 with episodes, shots, sheets (including `rejected`) and gates. All data survives, every new enum value inserts successfully, and `PRAGMA foreign_key_check` is clean.
- **T-CFG-06:** the deprecated circuit-breaker key is accepted with a warning, and is absent from the model and defaults.
- **T-GATE-02:** static DAG: no `spends_money` stage has an ancestor gate of kind `G3_FINAL_CUT`.
- **T-COV-01:** the coverage-floor script fails when a module drops below its floor (synthetic coverage JSON).

**Geometry and encoders**
- **T-GEO-01:** table-driven `fit_plan`: exact, within-tolerance crop with reported loss, beyond tolerance error, upscale, and the tall and wide cases.
- **T-ENC-01:** fake-runner probe. NVENC OK; encoder listed but the probe fails; timeout; wrong frame count. `nvenc` fails closed, `auto` falls back with a reason, and the cache hit and invalidation (key changes) behave.
- **T-ENC-02:** live NVENC (`STUDIO_LIVE_NVENC=1`), skipped by default.

**S9 and G2**
- **T-ANI-01:** exact total frames; audio present and within tolerance; the shots map matches S6.
- **T-ANI-02:** replace one keyframe. S9 goes stale and a new G2 round appears. Unchanged inputs hit the cache.
- **T-ANI-03:** the panel contents and the `over_remaining_cap` verdict. The mutable numbers are absent from S9's stage output and the snapshot hash.
- **T-ANI-04:** preview HTML escapes Arabic and HTML, and has `dir="auto"`.
- **T-G2-01:** S8 has no gate; S9 has G2.
- **T-G2-02:** S10 makes zero provider calls and zero reservations until S9's gate is approved.
- **T-G2-03:** reject shots 3 and 7. Only those two keyframes are re-paid (nonce changes, count asserted), S9 rebuilds and a new round is created.
- **T-G2-04:** a legacy G2 approval recorded on S8 does not satisfy S9.
- **T-G2-05:** auto-approve works in sandbox/draft and is refused in final.

**S11**
- **T-ASM-01:** synthetic clips with distinct constant luma per clip and known frame counts. After S11, per-frame `signalstats` run-lengths equal each `clip_frames` in order (no dup/drop at boundaries) and the total equals Σ `clip_frames`.
- **T-ASM-02:** about 120 tiny clips complete within the input and graph bounds (`max_inputs_per_graph` honored). File paths with spaces, apostrophes and non-ASCII characters work. Windows-style path escaping is tested at string level.
- **T-ASM-03:** A/V sync probe. A flash at frame K and a beep at K/fps. Detect the flash by `signalstats` and the beep onset by `silencedetect` on the *decoded muxed audio*. The delta is ≤ 1 frame plus AAC priming tolerance.
- **T-ASM-04:** a clip beyond the aspect tolerance fails S11. A clip within it records `crop_loss_pct`.
- **T-ASM-05:** a cache hit skips the render. A different encoder `profile_hash` produces a different key.
- **T-ASM-06:** `j_cut_ms != 0` is refused at S9 and S11.
- **T-ASM-07:** S6 columns are unchanged after S9 and S11.
- **T-ASM-08:** zero provider calls from S9, S11 and S12 (spy providers).
- **T-ASM-09:** `InsufficientDisk` preflight (injected free-space function).
- **T-CRASH-05:** a crash mid-segment and a crash during mux. Resume reuses verified segments, produces a correct render and makes zero paid calls.

**S12 and G3**
- **T-QCR-01:** each check against a purpose-built bad render: wrong fps, wrong size, no audio, two audio streams, no faststart, frame count off by one, loudness off, true peak over, a black section.
- **T-QCR-02:** muxed true peak is re-measured and not copied from the master.
- **T-QCR-03:** the upstream QC aggregate.
- **T-G3-01:** approving a `metadata_safety` round (or any non-render G3-kind round) does not unlock S13. Approving S12's G3 does. A re-render with a different sha blocks S13 until re-approved.
- **T-G3-02:** the checklist is required in final, stored on the gate and copied to the sidecar, including `made_for_kids`.
- **T-G3-03:** auto-approve is refused in final. In draft the checklist is `auto`-marked.

**S13**
- **T-EXP-01:** layout, atomic write (a crash leaves only `.tmp-*`), idempotent re-run, sidecar schema, sha matches.
- **T-EXP-02:** the commercial-use block, and the override with its audit row and sidecar entry.
- **T-EXP-03:** a missing license blocks in final and flags in draft.
- **T-EXP-04:** presets vs channels, and the four keys map to the four configs.
- **T-EXP-05:** S13 makes zero provider calls and no network access.
- **T-EXP-06:** the limit warnings.
- **T-EXP-07:** a re-render creates `_r2` and preserves the old directory.
- **T-EXP-08:** slug sanitation and Windows reserved names.

**S14**
- **T-BIB-01:** schema and evidence validation (unknown `scene_id` rejected, over-cap rejected).
- **T-BIB-02:** never auto-applied. `accept` bumps the version with audit. A stale base is refused.
- **T-BIB-03:** the crash points never double-pay beyond the declared single-call windows, with exactly one `potential_orphan` where applicable.
- **T-BIB-04:** blocked before G3 clears.

**S15**
- **T-PUR-01:** plan and dry-run by default. Protected classes are never listed.
- **T-PUR-02:** retention windows are respected (raw clips under 30 days are kept).
- **T-PUR-03:** a cache hit on a purged asset fails closed with zero provider calls.
- **T-PUR-04:** purge refuses for unpublished episodes.
- **T-PUR-05:** a shared CAS file referenced by a second unpublished episode is kept. A locked-file retry path exists, with injected failures.
- **T-PUR-06:** idempotent, and a crash mid-purge resumes with audit rows.
- **T-PUR-07:** a published episode refuses paid stages with `EpisodeClosed`, while a free re-export still works.

**Integration**
- **T-ENG-17:** the sandbox demo through S13 with real handlers and real ffmpeg. Paid-call counts are unchanged from Phase 4 (T-ENG-14 formula), S14 makes exactly one mock LLM call, and S6 values are unchanged.
- **T-ENG-18:** a production-shaped fake-LLM chain through S13 with the gates approved in order G1 → G0 → G2 → G3. No paid call happens before its ancestors are approved.
- **T-ENG-19:** `--languages ar,en`: image and video call counts unchanged; TTS, mix, S9, S11, S12 and S13 double; two export directories.
- **T-CLI-05:** all new commands, including refusal without `--yes`.
- **T-LANG-01:** the lint covers `studio.assemble`, `studio.export`, `studio.retention`, `studio.bible` and the new `video` modules.

## Q. Coverage and CI

- Add `--cov=studio.assemble --cov=studio.export --cov=studio.retention --cov=studio.bible` (and any new package) to the CI invocation. Keep all existing paths.
- **TOTAL ≥ 91%.** Each Phase 5 module ≥ 88%. Engine not below 88%.
- Lift all seven Phase 4 modules to **≥ 85%** (`media/sanitizer.py`, `providers/fal/adapter.py`, `video/ffmpeg.py`, `qc/visual.py`, `media/registry.py`, `video/probe.py`, and the split `media/*` modules, replacing `media/stages.py`).
- Run `--cov-report=term-missing` per module and write tests for the real uncovered branches. Use `tests/media_env.py` builders for crash-resume and refusal branches. Priorities:
  - fal response-body edge branches.
  - sanitizer fall-through.
  - registry error paths.
  - ffmpeg wrapper error paths.
  - visual QC branches.
- Do not add `# pragma: no cover` to hit a number. Any use must be justified individually in the report.
- Add `scripts/check_coverage_floors.py` (stdlib only), reading `coverage.json` and `coverage_floors.json` (glob → floor). CI runs pytest with `--cov-report=json` and then this script. Add it to `ci.yml`.
- Windows CI must run the new ffmpeg-marked tests. Keep total CI runtime reasonable and mark heavy tests `slow` (the weekly workflow).

## R. Implementation order (one commit per step; full test suite green at each)

0. Discovery plus H5.0 audit (commit the discovery and audit documents).
1. H5.8 pure-move split; then H5.1–H5.6 hardening and coverage lift of the seven modules; coverage floors script.
2. Config, migration 007, StageSpec flags, barrier extension, and H5.7 gate re-kind.
3. `video/geometry.py`, `video/encoders.py`, probe and `studio encode probe`, segment primitive.
4. S9 animatic, G2 move, panel and the G2 CLI (reject with shots).
5. S11 assemble.
6. S12 render QC, G3, checklist and `render rebuild`. Then write `PHASE5A_CHECKPOINT.md`.
7. S13 export, presets and publish-less export flow.
8. S14 bible proposals.
9. S15 publish/purge, DAG wiring for production and demo, integration tests, doctor, README, report.

## S. Report format: `PHASE5_REPORT.md`

1. **Discovery Report (§C), embedded verbatim**, with file paths for every item.
2. **Test results by ID**, with real pytest, ruff, mypy and `studio doctor` output.
3. **Coverage:** the exact CI command output, the per-module table (including the seven lifted modules and every new module), and the floors script output.
4. **Deviations**, including every changed Phase 1–4 assertion (old line, new line, reason).
5. **Decisions on ambiguities.** This must include the E5 segment-join evidence (what you tried and what the frame run-length test showed).
6. **Known limitations.**
7. **Unverified register:** encoder probe behavior and NVENC flags, BT.709 tagging, `bitexact` flags, concat copy-join behavior, AAC priming and overshoot, `silencedetect` accuracy, A/V sync thresholds, preset bitrates and platform limits, true-peak ceilings, faststart scan, Windows path handling, `nvidia-smi` parsing. Each entry says where it is tested.
8. **Commands:** the full operator flow (probe → sandbox demo through S13 → production-shaped run with gate approvals in order → export → publish mark → purge plan/run), for Linux and Windows.
9. **Files:** new and modified.

## T. Definition of done

- All §P tests pass on Ubuntu and Windows CI, with the coverage and floor gates met, ruff and `mypy --strict` clean.
- `studio run --demo --profile sandbox --languages ar` completes through S13 with real handlers and mock providers, producing an export directory whose MP4 has exactly Σ `clip_frames` frames and passes S12.
- No paid call occurs before every ancestor gate is approved. S9, S11, S12, S13 and S15 never call a provider.
- G2 is on S9, and G3 is on a render. Approving a non-render G3-kind round never unlocks export.
- S6's locked values are never mutated.
- Purged assets never trigger a silent re-pay.
- No new runtime dependencies.
- The report contains no claim that was not observed. Anything live, Windows or NVENC that was not run is listed as unobserved.

=== END PHASE 5 PROMPT ===
