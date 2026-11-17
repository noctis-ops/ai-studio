Part 1: Audit of the Phase 3 report
Verdict: accept Phase 3 as merged, with conditions. The arithmetic checks out (412 = 410 + 1 + 1; 4995 − 351 = 4644, which is 92.97%). The report is honest about scope changes and does not fabricate verification. It also surfaced two things that must be fixed before any paid image or video call exists.

#	Sev	Finding	Action
R1	High	Contradiction on gates and spend. §C1 says the engine never enumerates past a pending gate. §C7 says jobs enqueued while a gate is pending can still execute, and the engine invalidates them on approval. With TTS that is a nuisance. With fal it means a $0.50 clip could be paid before Gate 2 and then thrown away.	Phase 4 H0: a paid-stage gate barrier, enforced at execution time and independent of scheduler behavior. Test T-GATE-01.
R2	High	Audio stages exist only in the demo DAG. S5a–S7b have never run on lines materialized by the real S3/S3b. text_final is NULL until a post-gate edit, so which column S5 reads in production is unproven. If it reads the undiacritized column, the Arabic pipeline silently sends bare text to ElevenLabs.	Phase 4 H1: wire audio into the production DAG. Test T-INT-01 asserts that the text sent to TTS equals the diacritized text approved at Gate 1.
R3	Med	Gate 1 shows no media estimate. S4 counts LLM tokens only, so the operator approves a script without seeing TTS or video cost. T-PRICE-02 was dropped, and the S6 drift check is a no-op.	Phase 4 H2: add an additive media estimate to S4, revive T-PRICE-02, and activate S6 drift. Unconfirmed models show "unavailable", never $0.
R4	Med	The G3 metadata_safety round is the terminal gate of the production DAG. Appending media stages after it makes it an upstream prerequisite, so the operator approves "Final Cut" before TTS.	Accepted for Phase 4 (more human approval, never less, and it blocks media when flagged). It is UX debt for Phase 5/6, and the Phase 5 render-subject G3 test is still required.
R5	Med	The coverage gate omits studio.qc and studio.video, though QC is safety-critical. T-MIX-05 (20 min) is excluded from CI with no scheduled run, so scale regressions are invisible.	Phase 4 H3: widen the coverage paths and add a weekly and manual slow workflow.
R6	Med	Linux numbers ran through an ffprobe stand-in that parses ffmpeg -i stderr. Windows was never executed. Phase 4 QC leans heavily on ffprobe.	Operator: confirm the Phase 3 CI run is green on both OSes with real ffprobe before running Phase 4. Phase 4 parses only ffprobe -print_format json.
R7	Low	The Finding #6 answer is a reconstruction, not evidence. The report says it "cannot reconstruct 16".	Not blocking. The Phase 4 Discovery lists the collected crash test IDs, and T-REC-03 extends the matrix to the fal adapters.
R8	Info	Finding #8 approved, but not yet implemented, since the decision arrived after Phase 3.	Phase 4 H4 (config plus tests).
R9	Info	The "locked duration" invariant (A5) is documentation only.	Phase 4 enforces it (S10 requests the locked duration and conforms to exact frames) and tests it.
R10	Info	Finding #7 answer accepted (approval is per stage row).	Carry-forward to Phase 5: the render-subject G3 test, tooling that filters by gate kind alone, and moving G2 from S8 to S9.
Accepted deviations: D1/D12 (no fabricated S4 baseline), D2 (demo-only wiring, now closed by H1), D3, D5, D6, D11 (now R6), D13 and D14.

Phase 4 decisions I made and you should know about:

Character consistency. No single reference-image endpoint guarantees "strict" consistency. The design is approved reference sheets, reference-conditioned keyframes, an audition tool so you can judge endpoints by eye, and Gate 2 with per-shot regeneration as the safety net. Character LoRA training is deferred.
Deferred: location references, chain_mode: last_frame (it conflicts with the S8 → Gate 2 → S10 ordering), upload-URL caching, and any visual-LLM QC.
Transport: fal is called over raw httpx with an injectable transport, like ElevenLabs. There is no fal_client dependency. Every fal wire detail is from my memory and marked ⚠.
Part 2: Operator prerequisites for Phase 4
Confirm Phase 3 CI is green on ubuntu and windows-latest (send the run links).
Confirm whether you ran the live LLM smoke test (STUDIO_LIVE_LLM=1, ≤ $1). Phase 4 prompts consume S1–S4 output (shot descriptions, character visual specs), and their real quality is still unmeasured. If you skipped it, run it now.
ElevenLabs audition and T-EL-06 are still pending. They don't block Phase 4 code, but they block a real-audio benchmark.
Create a fal.ai account and put FAL_KEY in .env. Load only $10–20 of prepaid balance and treat it as the hard ceiling, since I don't know of a spend limit.
Verify candidates yourself on each model's fal page. My shortlist, all ⚠ unverified from memory:
Reference-capable image: a Flux Kontext variant, a Nano Banana (Gemini image) edit endpoint, a Seedream edit endpoint, or Qwen Image Edit.
Image-to-video: a Kling 2.x variant, a MiniMax Hailuo variant, a Seedance variant, a Wan variant, or LTX Video.
Your original names ("Seedream 5.0", "Seedance 2.5", "MiniMax H3 Max") map to these families, but I can't confirm those versions exist.
For each candidate, record:
exact endpoint ID
input field names (prompt, image, refs, aspect, duration, seed, negative)
accepted value formats, such as "5" vs 5 vs "5s"
supported aspects and durations
output JSON shape
price, unit and resolution
whether it accepts data URIs
commercial-use terms
After Phase 4 merges, you will fill the registry, run studio doctor, run studio media audition image and clip, pick endpoints, set pricing_verified_on, and run studio media smoke. Then return the Phase 4 report plus your audition choices.
Part 3: PHASE 4 IMPLEMENTATION PROMPT
BEGIN PROMPT

A. Role, ground rules, definition of done
You are a senior Python engineer implementing Phase 4 (Media: fal adapters, character sheets, keyframes, clips, prompt sanitizer, visual QC) of the ai-studio repository. Phases 1–3 are merged (412 tests, 92.97% coverage on the gated packages, ruff clean, mypy --strict clean). Read PHASE3_REPORT.md first.

Priorities, in order:

Never pay twice for the same result.
Never exceed a budget cap.
Any crash is recoverable.
Clean, typed, documented code.
Audio and video stay in sync by construction.
Hard rules:

No new runtime dependencies. No fal_client, PIL, numpy or pydub. Use httpx (already used by the ElevenLabs adapter), stdlib, and ffmpeg/ffprobe via subprocess.
No SDK imports in src/ beyond Phase 2's LLM adapters.
Never modify S1–S3 prompts or schemas, and never reinterpret Phase 1–3 contracts, except where this prompt explicitly authorizes it (listed in §O).
Never invent fal endpoint IDs, prices or API facts. Everything marked ⚠ is unverified memory. Implement it as written, test it only against httpx.MockTransport, and list it in the report's "Unverified register".
Fail closed and spend less when ambiguous.
Never disable a provider's safety checker. Never auto-switch providers on refusal. Never substitute a pan/zoom of a still for a failed clip (the product forbids Ken Burns), because a failed shot stays failed.
Language neutrality: visuals are per track (aspect) and shared across languages. --languages ar,en must not change image or video call counts (T-LANG-01 lint still applies).
Output complete file contents. Mark any test you did not execute as "unverified".
Definition of done:

All §P tests pass on Ubuntu and Windows CI, coverage targets are met, and ruff and mypy --strict are clean.
studio run --demo --profile sandbox --languages ar completes S1 → S10 with mock providers through the real stage handlers.
A production-shaped test (fake LLM, mock TTS/image/video) completes through S10, and no paid call occurs before its ancestor gates are approved.
Every conformed clip has exactly clip_frames frames at timing.fps. S10 never mutates S6's locked values.
Only after_submit_before_persist and during_single_call can double-invoke a provider, and each logs exactly one potential_orphan.
The live fal tests exist, are skipped by default, and carry no verified claims.
B. Scope
In scope:

H0–H4 hardening (§D)
registry, config and migration (§E)
the fal job adapter (§F)
prompt composition and the PromptSanitizer (§G)
S8a character sheets with Gate G0 (§H)
S8 keyframes (§I)
S10 clips with locked-duration conform (§J)
visual tech QC (§K)
preflights, budget and the media estimate (§L)
DAG wiring (§M)
CLI (§N)
tests (§P) and live smoke (§Q)
Out of scope:

S9 animatic, S11–S15, the Streamlit UI and NVENC probing
location reference images
chain_mode: last_frame (config value raises ConfigError("last_frame chaining not supported before Phase 5"))
character LoRA training
any visual-LLM validation
changes to the TTS/audio engine other than §D, §J's use of build_timeline, and the S6 input-hash fix in §J
C. Discovery Protocol (do this first; write the Discovery Report before coding)
Inspect the code and answer each item with file paths. If an answer contradicts this prompt, choose the fail-closed option and report it.

DAG table for both the demo and production DAGs: stage id, depends_on, gate, scope, spends_money. Identify which stage carries G1_SCRIPT and which carries the metadata_safety G3 round (R4). State exactly which stages are enqueued and which run while a gate is pending (R1).
Production text → audio contract (R2). Which narration_lines column or columns hold the diacritized Arabic after S3/S3b? What does S5 read (text_final, else what)? Does S3b write back into narration_lines?
Shot schema. List every field available per shot (visual description, camera, action, characters, location, mood, and any image_prompt or motion_prompt). Do not modify S2. If there is no motion field, §G composes one deterministically.
Characters and style. Where are character visual specs, the style lock text and the series bible stored? Is there any existing sheet or reference-asset table or column? Which Phase 1 tables or columns can be reused for shot_attempts (kind, nonce, prompt, QC, asset, cost)?
Registry and gateway. List the existing image/video entries (fal-ai/flux/dev, fal-ai/minimax, fal-ai/kling), pricing units supported, needs_confirmation semantics, param_map support, profile role names for image/video, the JobProvider protocol, how input assets reach adapters, and how mock image/video providers are wired.
Demo DAG media stages. What do the demo image/video stages do today? Phase 4 replaces them with the real handlers plus mock providers (§M).
S6. Where do supported durations come from (settings or registry)? Does S6's input_hash include the video model key and its durations?
Gate semantics. How are approval_gates snapshots computed? Does request_changes operate per run or per shot? Does a studio estimate command exist? Does studio budget exist, and how are caps raised?
tts audition mechanics. Which budget scope and gateway path does it use? Reuse it for media audition.
Collected crash-test IDs: pytest --collect-only -q -k crash.
ffmpeg filters already probed by scripts/check_ffmpeg.py.
Exact key names S6 reads as the Gate 1 video-estimate baseline (projected_video_cost_musd / gate1_video_estimate_musd) and which stage output holds them.
D. Hardening (do first, one commit)
H0. Paid-stage gate barrier (R1).

Add StageSpec.spends_money: bool (true for S5, S8a, S8, S10 and the LLM sanitizer path).
Compute each stage's ancestor stages from the DAG object.
At handler entry, before any gateway call, call assert_gates_cleared(run_id, stage_id):
Every gate attached to any ancestor stage must have its latest round approved.
That round's snapshot_hash must equal the current hash of that ancestor stage's outputs.
If not, raise GateNotCleared. The engine defers the job without consuming an attempt or budget, and writes an audit row paid_stage_deferred.
Do not change scheduler behavior. This is defense in depth.
Also add a second guard inside ProviderGateway: refuse non-cached calls with capability tts/image/video if the calling context carries gate_cleared=False. Contexts built outside stage handlers (audition and smoke CLIs) set an explicit gate_exempt="audition" with an audit row.
H1. Wire audio into the production DAG (R2). Append s5a → s5 → s6 → s7 → s7b after the current terminal text stage (see §M). Add a production-shaped integration test (T-INT-01).

H2. Media estimate in S4 (R3). Implement pipeline/estimate_media.py and extend S4's output with additive keys only (use exactly the key names S6 already reads; gate1_video_estimate_musd is the expected value):

media_estimate with tts, sheets, keyframes and video, each as {low_musd, expected_musd, high_musd} plus an available: bool and reason.
TTS is exact, from len(text_to_send) of the S3b output under tts.keep_diacritics.
Sheets cost the number of characters without an approved sheet × roles × price.
Keyframes cost shots × (1 + media.assumed_retry_rate.image) × price.
Video durations come from narration words ÷ timing.words_per_second (+ lead-in/tail), passed through the same pure function S6 uses to pick a supported duration. Low is plain quantization. High is the next supported duration or 1.0 + assumed_retry_rate.video.
If any needed model is unconfirmed (needs_confirmation) or its price is unverified, set available: false with a reason. Never emit 0.
Include the registry prices and the relevant settings in S4's input_hash.
Revive T-PRICE-02: S4 estimate vs S6 projection, drift event past estimate_drift_warn_pct.
H3. CI and coverage (R5).

Add studio.qc, studio.video, studio.media (new) and studio.providers (all) to --cov, keeping ≥ 90% overall.
Each new module must reach ≥ 85%, and pipeline/engine.py must not drop below its current 88%.
Add .github/workflows/slow.yml (workflow_dispatch plus weekly cron) that runs -m slow.
Extend the ffmpeg capability list in scripts/check_ffmpeg.py, doctor, and tests/unit/test_ffmpeg_capabilities.py with: signalstats blackdetect freezedetect ssim fps trim setpts tpad scale setsar format.
Make ffprobe wrappers use JSON output only.
H4. Finding #8. Set the draft profile's creative role to llm.creative. Update the Phase 2 tests that assumed gpt-4o-mini in draft, and report each changed assertion. Add T-CFG-05.

E. Registry, config, migration
E1. Registry (merge, never rename or delete existing keys).

Keep the existing placeholder entries. Mark them supports_refs: false where they are flux-dev-like, and do not rely on them for keyframes.
Add the following entries. Every endpoint is "<CONFIRM_FAL_ENDPOINT_ID>", needs_confirmation: true, commercial_use_verified: false, pricing_verified_on: null:
image.t2i.draft
image.ref.draft
image.ref.final
video.i2v.draft
video.i2v.final
Wire them into profile roles using the existing role names, or add image_t2i, image_ref and video_i2v roles.
Price-affecting parameters (resolution, tier) are pinned in fixed_params. One model key equals one price. Requests may not override them.
Schema additions under each entry's media: block:
YAML

media:
  supported_aspects: ["9:16", "16:9"]        # fail closed outside this list
  aspect_values: {"9:16": "<CONFIRM>", "16:9": "<CONFIRM>"}   # value for param_map.aspect
  aspect_extra_params: {}                    # e.g. width/height for models needing them
  supported_durations_s: [5, 10]             # video only
  max_refs: 0                                # image refs accepted per call
  ref_style: list                            # list | single
  param_map: {prompt: prompt, refs: image_urls, image: image_url,
              aspect: aspect_ratio, duration: duration, seed: seed, negative: negative_prompt}
  param_formats: {duration: int}             # int | str | str_s ("5s")
  fixed_params: {}
  result_map: {asset_url: "images.0.url", seed: "seed", nsfw_flags: "has_nsfw_concepts"}
  input_transport: data_uri                  # data_uri | upload
  prompt_templates: {ref_clause: "The character {name} must look exactly like the character in reference image {n}."}
pricing: {unit: per_image|per_megapixel|per_second|per_video, usd: 0.0}
limits: {max_concurrency: 1, min_interval_s: 1.0}
Add the pricing units per_megapixel, per_second and per_video only if Phase 1 lacks them (additive). The Phase 1 guard stays: zero or unverified prices are blocked in draft and final.
result_map is a dotted path with integer indices.
E2. Config (merge into settings.yaml; do not duplicate existing keys such as qc_auto_retries).

YAML

fal: {queue_base_url: "https://queue.fal.run", key_env: FAL_KEY,
      request_timeout_s: 60, download_timeout_s: 120, download_retries: 3}
media:
  poll: {initial_s: 3, max_s: 15, backoff: 1.5, max_wait_s: 900}
  max_paid_attempts_per_shot: 4
  circuit_breaker_consecutive_failures: 3
  failed_job_billing: charge            # charge | free   ⚠ unverified how fal bills failed/flagged jobs
  assumed_retry_rate: {image: 0.25, video: 0.30}
  ref_max_side_px: 1024
  ref_jpeg_quality: 90
  max_inline_bytes: 8000000
  refs_overflow: primary_only           # primary_only | fail
  chain_mode: off
  clip: {duration_tolerance_ms: 500, conform_crf: 14, strip_audio: true}
  qc:
    image: {min_short_side_px: 512, aspect_tolerance_pct: 2.0, min_bytes: 5000,
            min_luma_mean: 8, min_luma_stddev: 3.0, auto_retries: 2}
    video: {aspect_tolerance_pct: 2.0, min_bytes: 20000, black_fail_ratio: 0.15,
            black_pic_th: 0.98, black_min_d_s: 0.1, freeze_warn_ratio: 0.5,
            freeze_fail_ratio: 0.85, first_frame_black_luma: 8,
            first_frame_ssim_warn: 0.6, first_frame_ssim_fail: 0.0, auto_retries: 1}
character_sheets: {roles: [ref_full], aspect: "9:16"}
sanitizer: {max_levels: 2, model_key: llm.light, max_scene_chars: 600}
Rules:

Frame rate is timing.fps. Do not duplicate it.
Validate at load: chain_mode == off, 0 < ratios ≤ 1, and black_fail_ratio in (0, 1].
ref_face, if listed in roles, is conditioned on ref_full's sha.
Store the thresholds as config, not constants. All are unverified starting points, calibrated by the audition and smoke runs.
E3. Migration 005_media.sql (additive; reuse existing columns where Discovery finds them):

provider_calls.remote_handle_json (request_id, status_url, response_url, cancel_url), persisted in the same statement as remote_request_id.
shot_attempts, only if missing:
kind (keyframe|clip), nonce
prompt_spec_json, sanitize_level, refusal_log_json
qc_status, qc_json
asset_id, conformed_asset_id
cost_musd, provider_call_id
policy_blocked (bool)
character_sheets: (id, series_id, character_id, role, version, asset_id, status candidate|approved|superseded, spec_hash, nonce, created_at, approved_at), with a unique partial index on one approved row per (character_id, role).
Keyframe and clip stage input_hash includes the approved sheet shas, so a changed sheet stales the keyframes.
F. The fal adapter (providers/fal/)
Raw httpx.AsyncClient with an injectable transport. Auth header Authorization: Key <FAL_KEY>, with the key read from the env var named in fal.key_env. Implement JobProvider.submit/poll/fetch for both image and video. The expected wire behavior below is ⚠ unverified:

Submit: POST {queue_base_url}/{endpoint} with a JSON body built from param_map, param_formats, aspect_values, aspect_extra_params and fixed_params. The response carries request_id, status_url, response_url, cancel_url. Persist all four URLs and the id before returning. Never reconstruct URLs from the endpoint, because subpath endpoints are suspected to differ. Fall back to construction only if the response omits them, and log that.
Poll: GET status_url, expecting IN_QUEUE | IN_PROGRESS | COMPLETED. Handle an error field on COMPLETED. Use the media.poll backoff. When max_wait_s is exceeded, do not fail or cancel: leave the call submitted and requeue the engine job. Resume polls the same request id and never resubmits.
Fetch: GET response_url, extract fields via result_map, and download the asset immediately (stream to temp, atomic move into the CAS). Reject a missing or empty body (ZeroByteAssetError) and a content-type that is not image/* or video/*. Retry the download up to fal.download_retries times, re-GETting response_url for a fresh URL once.
Inputs:
Refs and images come from CAS assets prepared deterministically by media/refs.py (ffmpeg → JPEG, long side ≤ ref_max_side_px). The prepared sha, not the original, goes into the cache key.
data_uri transport enforces max_inline_bytes (MediaConfigError if exceeded).
upload transport does ⚠ initiate → PUT → file URL, re-uploading on each submit with no caching.
Refs beyond max_refs: follow refs_overflow (primary_only keeps the first N by shot prominence order and records a refs_truncated warning on the attempt).
Seed: seed = int(sha256(f"{episode_id}:{shot_id}:{nonce}")[:8], 16) % 2**31, included in the cache key. Remember that many video models ignore seeds, so the cache is the only reproducibility guarantee.
Error taxonomy (⚠ unverified):
timeouts, connection errors, 408, 429 (honor Retry-After) and 5xx → TransientProviderError
403 whose detail mentions balance, locked or exhausted → ProviderQuotaExceeded (halt halted_budget, reason provider_quota)
other 401/403 → PermanentProviderError (auth)
422 whose detail[].type is content_policy_violation → ContentPolicyError(billed=False)
a result whose nsfw_flags has any true value → ContentPolicyError(billed=True). Do not store the asset. This is a silent-black-output risk.
a failed job → permanent, classified by detail
any other 4xx → PermanentProviderError
Cost: compute from the pricing unit. For per_second use the requested locked duration. For per_megapixel use the requested pixel dimensions. Reserve the worst case. Settle as follows:
success: settle the full computed cost
ContentPolicyError(billed=False): release the reservation
billed=True: settle in full
provider failure after acceptance: follow media.failed_job_billing (default charge, conservative)
Secrets: the key and any signed URL query strings never appear in request_json, audit rows, logs or exception messages (T-FAL-04).
Use one adapter implementation with a kind of image or video, so the mock providers share the same registry-driven validation (supported_aspects, supported_durations_s).
Update the mock image/video providers to honor requested duration and aspect, with an optional test-only variant fps and duration offset to exercise conform.
G. Prompt composition and PromptSanitizer (media/prompts.py, media/sanitizer.py)
Templates. Create prompts/media/keyframe.txt, motion.txt, negative.txt and sanitizer.txt. Their sha256 is the "prompt version sha" in every media cache key.

PromptSpec (deterministic, no LLM):

locked_prefix: series style lock text, then for each character in the shot Name: <visual spec>, then the model's ref_clause per ref.
scene_text (the only sanitizable part): built from the S2 shot fields. If there is no motion field, compose a motion sentence from the action, camera movement and mood.
locked_suffix and negative: from negative.txt, passed via param_map.negative when available, else appended as "Avoid: …".
Visual prompts are English only.
Sanitizer flow (on ContentPolicyError, for any attempt kind):

Level 1: llm.light rewrites scene_text toward implied rather than graphic action. Never add content, never change who or where, never touch locked parts.
Level 2: apply the deterministic lexicon (prompts/sanitizer_lexicon.yaml, a map of graphic term → implied phrasing), then a stronger rewrite.
The LLM call goes through the gateway as a single-call provider, with nonce san{level} so the cache never returns the same rewrite. Rewrites are validated: non-empty, ≤ max_scene_chars, and no new proper nouns versus the original. A failure falls to the next level.
After max_levels refusals, the attempt is failed_policy: the shot parks for the human, who can edit the prompt via studio shot regenerate --prompt-file. The sanitizer never resubmits an identical prompt after a refusal.
Persist prompt_spec_json, sanitize_level and refusal_log_json (original, rewritten, timestamp) before each submit. A crash then resumes at the sanitized prompt without re-calling the LLM.
The sanitizer is invoked only for policy refusals, never for transient errors or QC failures.
Sanitization counts toward max_paid_attempts_per_shot.
H. S8a Character sheets (scope: per character × role; spends; Gate G0)
Runs after S7b. A free precheck skips every character that already has an approved sheet for each configured role, with zero calls and no gate.
For missing sheets, generate one image per (character, role). The first role (ref_full) is a single-view, full-body, neutral-pose image on a plain backdrop, in the series style, from the character's visual spec. It uses image.t2i.*, or the ref-capable model with no refs. Later roles use the ref-capable model, conditioned on the ref_full sha.
Gate G0 is created only if something was generated. Approval flips the chosen rows to approved and supersedes older ones. Rejecting one character regenerates only that character's sheets, with a new nonce and a cost check.
QC runs on each image (§K). Retry policy is §K.
The G0 snapshot includes asset shas and prompts.
I. S8 Keyframes (scope: per shot per track; spends; Gate G2)
Refs per shot are the approved ref_full sheet (plus ref_face for close-up shots, if enabled) of each character in the shot, up to max_refs. Shots without characters get no refs and rely on the style lock.
Native aspect: the request aspect equals the track aspect via aspect_values. Unsupported aspect → MediaConfigError at preflight. There is never cropping or stretching.
Outputs go to shot_attempts (kind keyframe). The stage output (which feeds the G2 snapshot) includes:
the accepted attempt id and sha per shot
warnings (refs_truncated, QC warnings)
projected_clip_cost_musd_first_pass, computed exactly from S6's locked durations × price
projected_clip_cost_musd_with_retries, using assumed_retry_rate.video
estimate_drift against S4 (reuse the S6 helper)
Gate G2 is attached to this stage (temporary; Phase 5 moves it to S9). S10 depends on this stage and the barrier enforces the approval.
Attempt policy:
Trigger	Action	Counts toward retry budget
429/5xx/timeout	backoff + jitter, max 5	No
Policy refusal	sanitizer (§G)	Yes
Retryable QC failure	new nonce base.qN, max qc.image.auto_retries	Yes
Non-retryable QC failure	fail the shot immediately	n/a
Human reject	new attempt, optional edited prompt, cost-checked	resets per-round counters
Budget	halt	n/a
max_paid_attempts_per_shot hard-caps the total paid submissions per shot per round.
Circuit breaker: circuit_breaker_consecutive_failures consecutive shots failing after retries (or one non-retryable failure, such as an aspect mismatch) fails the stage with MediaCircuitOpen and an audit row, so a broken endpoint cannot burn money.
Resume after a crash repays nothing. A regeneration changes the nonce. A resume keeps it.
Regenerating a keyframe stales that shot's clip via input_hash but never purges the paid clip (retention rules apply).
J. S10 Clips (scope: per shot per track; spends)
Inputs: the accepted keyframe sha, the motion prompt, the locked duration, and the track aspect. The i2v call is first-frame-conditioned.
Duration lock (enforced here):
The requested duration is S6's locked clip_duration_ms / 1000, which must be in the video model's supported_durations_s, formatted per param_formats.
s10p preflight fails closed if any locked duration is unsupported, with the message "re-run S6".
Fix S6 so its input_hash includes the resolved video model key and supported_durations_s, and so it reads durations from the registry if it does not already. Changing the video model then stales S6.
Conform (derived, free, deterministic from the raw clip; the raw paid asset is stored first and never regenerated):
clip_frames = round(clip_duration_ms × timing.fps / 1000).
Normalize to timing.fps (fps filter), then trim to exactly clip_frames. If the clip is shorter by ≤ clip.duration_tolerance_ms, freeze-pad the last frame (tpad). Beyond tolerance in either direction, fail the QC check duration_mismatch.
Strip audio when strip_audio is set. Keep the native resolution and set SAR 1.
Encode with libx264 at conform_crf, yuv420p, -an. If the raw clip is already exactly compliant (fps, frame count, no audio), reference the raw asset with no re-encode.
Store the conformed asset as intermediate. Never downgrade CAS retention.
The conformed frame count is verified with ffprobe and must equal clip_frames. A mismatch raises.
Invariant: regenerating or retrying a clip must not change clip_duration_ms, clip_frames, speed_factor or hold_frames. Add a test that snapshots the S6 columns before and after.
Attempt policy, circuit breaker and cost rules are the same as §I, with qc.video.auto_retries.
Output: conformed asset id and sha per shot plus QC data. Phase 5's assembler consumes these via build_timeline.
Retention: raw clips are keep_n_days (keep_raw_clips_days).
K. Visual tech QC (qc/visual.py, ffmpeg and ffprobe only)
Each check returns {name, status pass|warn|fail, retryable, value, threshold} and persists to qc_json.

Image:
readable, with min_bytes met
short side ≥ min_short_side_px
aspect within aspect_tolerance_pct of the target (non-retryable failure)
not black or blank, using signalstats mean luma and stddev against thresholds (retryable)
Video:
full decode with zero errors (-v error -f null)
min_bytes met
aspect within tolerance (non-retryable)
duration within tolerance (via the conform step)
frame-count and fps consistency, and a first-frame luma check (not black)
blackdetect total black ratio ≤ black_fail_ratio
freezedetect ratio: warn at freeze_warn_ratio, fail at freeze_fail_ratio. Cartoons have legitimate holds, so these are lenient.
first-frame SSIM against the keyframe (ssim filter, with a frame-scaled copy). It is recorded always, warns below first_frame_ssim_warn, and fails only if first_frame_ssim_fail > 0 (disabled by default until calibrated).
audio-stream presence is reported, since conform strips it
Retry classes: retryable = zero-byte, decode error, black/blank, extreme freeze, first-frame black. Non-retryable = aspect mismatch, below-minimum resolution, unsupported container, duration_mismatch beyond tolerance, MediaConfigError.
No visual-LLM validation anywhere. Aesthetic and character matching are Gate 0/2/3 human judgments.
Parsing helpers live in video/analysis.py and are unit-tested.
L. Preflights and budget
s4p_media_preflight (free; runs after the last text stage and before S5a):

Collect all problems at once and then fail. The checks:
FAL_KEY is set for fal-backed models
the models for sheets, keyframes and clips resolve and none has needs_confirmation
in draft/final, prices are verified (the Phase 1 guard) and commercial_use_verified is true (final)
the track aspects are supported
max_refs ≥ 1 for the ref model (if characters exist)
the video model's supported durations are non-empty
ref-capable models are marked
chain_mode == off
Whole-episode affordability: halt with BudgetExceeded before any spend if the expected total (H2) exceeds the remaining episode or monthly cap. Warn if high exceeds it. Use the existing cap-raising path, audited.
s10p_clip_preflight (free, after G2): recheck the model checks and durations, compute the exact first-pass worst case (Σ locked seconds × price), and halt if it exceeds the remaining budget. Retries are protected per call by the gateway reservation.

Add studio budget status --by-model (if missing) so you can compare the ledger to the fal dashboard.

M. DAG wiring
Production DAG, appended after the current terminal text stage(s), even though the metadata_safety G3 round currently sits there (R4): s4p → s5a → s5 → s6 → s7 → s7b → s8a(G0) → s8(G2) → s10p → s10.
Demo DAG: the same handlers with mock providers, replacing the old demo image/video stages.
Update T-ENG-08 deliberately. Expected image calls = shots + characters-without-approved-sheets × roles, video calls = shots, and TTS calls are unchanged. State the formula and any changed assertion in the report.
G0 appears only when sheets were generated. final never auto-approves, and draft/sandbox auto-approve works only with auto_approve=True.
Mark spends_money=True on S5, S8a, S8, S10.
N. CLI
studio shot list <run>: per shot status, attempts, cost, QC warnings, sanitize level.
studio shot regenerate <run> <shot_id> --stage keyframe|clip [--prompt-file f] [--reason ...] --yes:
touches only that shot, creates a new nonce, shows the reserve cost first, and refuses without --yes
resets per-round counters and audit-logs the reason and any prompt edit with old and new sha256
studio media preview <run> [--stage sheets|keyframes|clips]:
writes data/previews/<run_id>/<stage>.html, a stdlib-only static page
shows each asset with its prompt, QC results, warnings, attempt history and cost
uses html.escape, UTF-8 and dir="auto" (Arabic safe), with relative links via Path.as_posix()
studio media qc <asset-or-attempt>: run and print the visual QC.
studio media audition image --model-keys k1,k2 --prompts-file p.txt [--ref PATH ...] --aspect 9:16 --cap-usd 1.00 --yes:
no refs means pure T2I (use it to create candidate character refs); with refs it tests reference-guided consistency
prints worst-case cost first and requires --yes
writes data/previews/audition_media/<ts>/ with a README of model, prompt, cost and QC per output
studio media audition clip --model-keys ... --image PATH --prompt-text "..." --duration 5 --aspect 9:16 --cap-usd 3.00 --yes: the same, plus the conform report (frame counts, fps, SSIM).
Audition commands reuse tts audition's budget-scope mechanism and use the explicit gate-exempt context from H0.
studio media smoke --cap-usd 3.00 --yes (§Q).
studio doctor gains:
FAL_KEY present
media registry entries confirmed, prices verified and commercial flags
max_refs, supported aspects and durations
failed_job_billing notice
the new ffmpeg filters
chain_mode
a warning for each placeholder endpoint
O. Explicitly authorized changes to earlier phases
S4 output gains additive keys, and its input_hash gains the registry prices and settings (H2).
The draft profile's creative role is llm.creative (H4), with the listed Phase 2 test updates.
S6's input_hash includes the video model key and durations (§J).
T-ENG-08's expected image count follows the §M formula.
The production DAG is extended (§M), and the demo image/video stages are replaced by the real handlers.
Nothing else from Phases 1–3 may change. Report any other change you were forced to make.

P. Mandatory tests
Offline only: httpx.MockTransport and mock providers. Table-drive where noted. The ffmpeg-backed QC tests use lavfi-generated bad inputs (black clip, solid image, truncated file, wrong aspect, clip with audio, frozen clip).

Hardening

T-GATE-01: paid stage with a pending ancestor gate makes zero provider calls and zero reservations. Parametrize over S5, S8a, S8, S10. Include "approved, then ancestor output changed (snapshot mismatch)" and the gateway-level guard.
T-INT-01: fake-LLM S1–S4 → approvals → S5a–S7b. The text sent to TTS equals the diacritized text approved at G1, lines are keyed correctly, and the master exists.
T-CFG-05: draft creative role resolves to llm.creative.
T-PRICE-02: S4 media estimate (exact TTS chars, video low/expected/high, unconfirmed → unavailable, never 0) plus the S6 drift event.
T-CI-02: the capability list includes the new filters, and coverage paths and the slow workflow are present (parse the YAML).
T-REC-03: crash matrix, 7 points × {fal image, fal video} via MockTransport. Only after_submit_before_persist re-pays and logs exactly one potential_orphan; resume reuses remote_handle_json.
fal adapter

T-FAL-01: request building (URL, auth header, param_map/formats/aspect_values/fixed_params, a safety checker is never disabled).
T-FAL-02: poll lifecycle, result_map extraction, immediate download into the CAS, handle persisted in one statement, and poll timeout → requeue → same request id with a single submit.
T-FAL-03: error table (429 with Retry-After, 5xx, timeout, 403 balance → quota halt, 401, 422 policy billed=False, nsfw flag billed=True with no stored asset, failed job, empty download, wrong content-type).
T-FAL-04: key and signed query strings never appear in request_json, audit, caplog or exceptions.
T-FAL-05: data_uri size guard, upload path, and deterministic ref preparation (same bytes → same sha).
T-FAL-06: cost per unit, and failed_job_billing charge vs free.
T-FAL-LIVE-01/02: opt-in via STUDIO_LIVE_FAL=1 (one image under $0.25, one shortest clip under $1.50). Skipped by default. Never executed here, so the report says so.
Prompts and sanitizer

T-PRM-01: deterministic composition, locked parts are byte-identical across sanitize levels, prompt-version sha is in the key.
T-SAN-01: refusal → level 1 → success. The original and rewritten prompts are logged, the cap yields failed_policy, an identical prompt is never resubmitted, and a crash after a refusal resumes at the sanitized prompt with zero extra LLM calls.
T-SAN-02: rewrite validation (empty, too long, new proper nouns) falls through to the lexicon.
T-SAN-03: the sanitizer is not invoked for transient or QC failures.
Sheets, keyframes

T-SHT-01: sheets are generated only for missing (character, role). If all are approved there are zero calls and no gate. Rejecting one character regenerates only it, with a cost check.
T-SHT-02: ref_face is conditioned on ref_full's sha.
T-KF-01: ref selection, max_refs truncation warning, and a changed sheet stales keyframes.
T-KF-02: native aspect request, unsupported aspect → preflight error, and the aspect QC.
T-KF-03: black image → retries q1, q2, then fail. Non-retryable failures do not retry. Circuit breaker.
T-KF-04: crash resume repays nothing, and chain_mode: last_frame raises ConfigError.
T-KF-05: studio shot regenerate touches only that shot, uses a new nonce, shows cost, needs --yes, and stales that shot's clip without purging it.
Clips and QC

T-CLP-01 (table-driven conform): exact; +1 frame; 3 frames short (pad); long (trim); 30 fps source; beyond tolerance → duration_mismatch. Plus: S6 columns are unchanged after regeneration, and the requested duration equals the locked duration.
T-CLP-02: an unsupported locked duration fails s10p closed, and changing the video model stales S6.
T-CLP-03: only the accepted keyframe is used, and a clip regeneration does not touch the keyframe.
T-CLP-04: black, freeze (warn vs fail), decode error, audio stripped, and SSIM recorded.
T-CLP-05: raw-clip retention and the never-downgrade rule.
T-QCV-01: each visual check against a purpose-built bad input.
T-PRE-01: preflight lists every problem at once (unconfirmed models, missing key, unsupported aspect or duration, ref capability, unverified price in final, over-budget).
Integration

T-ENG-14: sandbox demo DAG through S10 with mocks (real handlers), including counts and the T-ENG-08 formula.
T-ENG-15: production-shaped fake-LLM chain through S10, asserting no paid call before each gate.
T-ENG-16: --languages ar,en leaves image and video counts unchanged and doubles only TTS and mix.
T-CLI-04: shot list/regenerate, media preview (HTML escaping with Arabic text), media qc, media audition (mock transport), and doctor.
T-LANG-01 lint covers the new packages.
Coverage: ≥ 90% across studio.core, studio.db, studio.pipeline, studio.audio, studio.providers, studio.media, studio.qc, studio.video.

Q. Live smoke (studio media smoke --cap-usd 3.00 --yes)
Opt-in. Requires FAL_KEY. Refuses if any needed registry entry is needs_confirmation or has an unverified price.
Build a scratch series with 1 character and 2 shots (5 s each, fixed prompts, 9:16) in a scratch STUDIO_HOME-scoped run.
Run S8a, S8 and S10 on the draft profile with auto-approve, through the real handlers, against live fal under the cap.
Write data/previews/media_smoke/<ts>/ with the HTML preview, per-call costs, QC values (including SSIM and freeze ratios for calibration), the conform report, and a ledger-vs-expected summary.
Print the request ids so you can reconcile against the dashboard.
Never run it in CI.
R. Implementation order (commit per step)
Discovery Report plus H0–H4.
Registry/config/migration plus the fal adapter and its tests.
Prompts and sanitizer.
Visual QC and the ffmpeg helpers.
S4 media estimate, s4p and s10p.
S8a (G0) and S8 (G2) plus shot CLI.
S10, conform and the S6 hash fix.
DAG wiring, integration tests, media CLI (preview, audition, smoke), doctor, README, report.
S. Report format (PHASE4_REPORT.md)
Mirror the Phase 3 report:

Discovery Report (§C, every item, with file paths).
Test results by ID, with real pytest, ruff, mypy and studio doctor output, not a bare "pass". Mark anything not executed as unverified.
Coverage, per module for the new packages and engine.py.
Deviations, including every changed Phase 1–3 assertion (§O).
Decisions on ambiguities.
Known limitations.
Unverified register: every fal wire detail, error mapping, pricing assumption, failed_job_billing behavior and QC threshold, each with where it is tested.
Commands: audition → smoke → draft run, on Linux and Windows.
Files (new and modified).
END PROMPT





