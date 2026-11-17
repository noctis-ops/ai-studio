"""Deterministic sandbox audio library (PHASE3 §H14).

`seed_demo` calls :func:`ensure_demo_library` so a fresh sandbox home has a
small but *real* library under ``<home>/library/{music,ambience,sfx}``:
tiny sine WAVs synthesized with the stdlib (no FFmpeg needed) plus
``index.yaml`` sidecars. Rows are inserted directly — the scan is not run,
because the sandbox must work without FFmpeg on PATH; the fixture loudness
values are fixed metadata, exactly what a scan would record.

Regeneration is idempotent: files are byte-identical on every seed, so the
upsert-by-sha256 path is exercised and repeated seeds change nothing.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import wave
from pathlib import Path
from typing import Any

from studio.config.loader import AppConfig
from studio.db.repos import StudioRepos

_SAMPLE_RATE = 48000

# Fixture loudness is *declared metadata* (a scan would record the same); the
# sine amplitudes below are chosen so the actual integrated loudness matches
# the declaration within ~1 dB (K-weighting deviation at these frequencies is
# well under 1 dB): a sine's RMS is amplitude / sqrt(2), so
# amplitude = sqrt(2) * 10 ** (lufs / 20).
_DECLARED_LUFS: dict[str, float] = {"music": -24.0, "ambience": -34.0, "sfx": -22.0}


def _amplitude_for(kind: str) -> float:
    return float(math.sqrt(2.0) * 10.0 ** (_DECLARED_LUFS[kind] / 20.0))

# rel_path, kind, moods/tags, intensity, loopable, duration_s, freq_hz, bpm,
# license, attribution_required, attribution_text, source
_FIXTURES: list[dict[str, Any]] = [
    {
        "rel_path": "music/hopeful_echo.wav", "kind": "music",
        "title": "Hopeful Echo", "tags": ["hopeful"], "intensity": 2,
        "loopable": True, "duration_s": 2.0, "freq_hz": 392.0, "bpm": 92,
        "license": "CC-BY 4.0", "attribution_required": True,
        "attribution_text": "Demo Composer — Hopeful Echo",
        "source": "studio-demo-generator",
    },
    {
        "rel_path": "music/tense_pulse.wav", "kind": "music",
        "title": "Tense Pulse", "tags": ["tense"], "intensity": 4,
        "loopable": True, "duration_s": 2.0, "freq_hz": 220.0, "bpm": 148,
        "license": "CC0", "attribution_required": False, "source": "studio-demo-generator",
    },
    {
        "rel_path": "music/determined_march.wav", "kind": "music",
        "title": "Determined March", "tags": ["determined"], "intensity": 4,
        "loopable": False, "duration_s": 3.0, "freq_hz": 330.0, "bpm": 132,
        "license": "CC0", "attribution_required": False, "source": "studio-demo-generator",
    },
    {
        "rel_path": "sfx/transition_whoosh.wav", "kind": "sfx",
        "title": "Transition Whoosh", "tags": ["transition", "whoosh"],
        "intensity": None, "loopable": False, "duration_s": 0.75, "freq_hz": 660.0,
        "bpm": None, "license": "CC0", "attribution_required": False,
        "source": "studio-demo-generator",
    },
    {
        "rel_path": "sfx/paper_click.wav", "kind": "sfx",
        "title": "Paper Click", "tags": ["click"],
        "intensity": None, "loopable": False, "duration_s": 0.25, "freq_hz": 880.0,
        "bpm": None, "license": "CC0", "attribution_required": False,
        "source": "studio-demo-generator",
    },
    {
        "rel_path": "ambience/room_tone.wav", "kind": "ambience",
        "title": "Room Tone", "tags": ["room_tone"], "intensity": None,
        "loopable": True, "duration_s": 2.0, "freq_hz": 110.0,
        "bpm": None, "license": "CC0", "attribution_required": False,
        "source": "studio-demo-generator",
    },
    {
        "rel_path": "ambience/wind.wav", "kind": "ambience",
        "title": "Wind", "tags": ["wind"], "intensity": None,
        "loopable": True, "duration_s": 2.0, "freq_hz": 165.0,
        "bpm": None, "license": "CC0", "attribution_required": False,
        "source": "studio-demo-generator",
    },
]


def _sine_bytes(freq_hz: float, duration_s: float, amplitude: float) -> bytes:
    n = int(_SAMPLE_RATE * duration_s)
    frames = bytearray()
    for i in range(n):
        value = int(amplitude * 32767 * math.sin(2.0 * math.pi * freq_hz * i / _SAMPLE_RATE))
        frames += struct.pack("<h", value)
    return bytes(frames)


def _write_wav(path: Path, freq_hz: float, duration_s: float, amplitude: float) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = _sine_bytes(freq_hz, duration_s, amplitude)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(_SAMPLE_RATE)
        handle.writeframes(pcm)
    return int(_SAMPLE_RATE * duration_s)


def _loudness_for(kind: str) -> float:
    return {"music": -24.0, "ambience": -34.0, "sfx": -22.0}[kind]


def ensure_demo_library(config: AppConfig, repos: StudioRepos) -> int:
    """Create the fixture library under ``<home>/library`` and upsert rows.

    Returns the number of files written (or already present and identical).
    """
    library_root = Path(config.settings.paths.home) / config.settings.audio.library_root
    written = 0
    sidecars: dict[str, list[dict[str, Any]]] = {"music": [], "ambience": [], "sfx": []}

    for fixture in _FIXTURES:
        rel_path: str = fixture["rel_path"]
        target = library_root / rel_path
        n_samples = _write_wav(
            target, fixture["freq_hz"], fixture["duration_s"],
            _amplitude_for(fixture["kind"]),
        )
        written += 1
        payload = target.read_bytes()
        sha256 = hashlib.sha256(payload).hexdigest()
        duration_ms = int(fixture["duration_s"] * 1000)
        assert n_samples == int(_SAMPLE_RATE * fixture["duration_s"])
        repos.audio.upsert_library_entry(
            sha256=sha256,
            rel_path=rel_path,
            kind=fixture["kind"],
            title=fixture["title"],
            moods_json=json.dumps(fixture["tags"], sort_keys=True),
            intensity=fixture["intensity"],
            bpm=fixture["bpm"],
            loopable=1 if fixture["loopable"] else 0,
            duration_ms=duration_ms,
            loudness_lufs=_loudness_for(fixture["kind"]),
            license=fixture["license"],
            license_url=None,
            source=fixture["source"],
            attribution_required=1 if fixture["attribution_required"] else 0,
            attribution_text=fixture.get("attribution_text"),
            active=1,
            scan_error=None,
        )
        sidecar_entry: dict[str, Any] = {
            "file": Path(rel_path).name,
            "title": fixture["title"],
            "license": fixture["license"],
            "source": fixture["source"],
            "loopable": fixture["loopable"],
        }
        if fixture["kind"] == "music":
            sidecar_entry["moods"] = fixture["tags"]
            sidecar_entry["intensity"] = fixture["intensity"]
            sidecar_entry["bpm"] = fixture["bpm"]
        else:
            sidecar_entry["tags"] = fixture["tags"]
        if fixture["attribution_required"]:
            sidecar_entry["attribution_required"] = True
            sidecar_entry["attribution_text"] = fixture["attribution_text"]
        sidecars[fixture["kind"]].append(sidecar_entry)

    import yaml

    for kind, entries in sidecars.items():
        kind_dir = library_root / kind
        kind_dir.mkdir(parents=True, exist_ok=True)
        (kind_dir / "index.yaml").write_text(
            yaml.safe_dump(
                {
                    "defaults": {"license": "CC0", "source": "studio-demo-generator"},
                    "tracks": entries,
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
    return written
