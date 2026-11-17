"""Audio engine (Phase 3): local, deterministic, FFmpeg-only media math.

Nothing in this package imports subprocess, httpx, or a TTS/LLM SDK.
FFmpeg/ffprobe are invoked only through ``studio.video.ffmpeg``.
"""
