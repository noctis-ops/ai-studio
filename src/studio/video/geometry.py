"""Render geometry: scale-to-cover + centre-crop (Phase 5 §E4).

Pure functions only, so the whole module is table-testable without ffmpeg.

A real fal clip is rarely 1080p and its aspect can be slightly off (the model
returns "1080x1920-ish"). The policy is therefore:

* the *aspect* difference must be within ``tol_pct`` (else :class:`GeometryError`),
* the frame is scaled up/down uniformly to cover the destination,
* the overflow is centre-cropped to the exact destination size, and
* the horizontal/vertical loss is reported as ``crop_loss_pct``.

Never stretch: a non-uniform scale would distort faces, which is worse than a
few pixels of crop.
"""

from __future__ import annotations

from dataclasses import dataclass

from studio.domain.errors import GeometryError


@dataclass(frozen=True)
class FitPlan:
    """The exact ffmpeg plan for one source frame -> destination frame."""

    src_w: int
    src_h: int
    dst_w: int
    dst_h: int
    scale_w: int
    scale_h: int
    crop_x: int
    crop_y: int
    aspect_delta_pct: float
    crop_loss_pct_x: float
    crop_loss_pct_y: float

    @property
    def crop_loss_pct(self) -> float:
        """Worst-axis loss, the number the render report records."""
        return max(self.crop_loss_pct_x, self.crop_loss_pct_y)

    @property
    def untouched(self) -> bool:
        return (
            (self.scale_w, self.scale_h) == (self.dst_w, self.dst_h)
            and self.crop_loss_pct == 0.0
        )

    def filter_chain(self, *, out_color_matrix: str | None = None) -> str:
        """scale -> crop -> setsar. ``out_color_matrix`` is for still inputs.

        swscale's RGB->YUV default is *not* BT.709, so the animatic path passes
        ``out_color_matrix=bt709`` on the scale filter itself (§E6 ⚠).
        """
        scale = f"scale={self.scale_w}:{self.scale_h}:flags=lanczos"
        if out_color_matrix is not None:
            scale += f":out_color_matrix={out_color_matrix}"
        return (
            f"{scale},crop={self.dst_w}:{self.dst_h}:{self.crop_x}:{self.crop_y},setsar=1"
        )

    def report(self) -> dict[str, object]:
        return {
            "src_w": self.src_w,
            "src_h": self.src_h,
            "dst_w": self.dst_w,
            "dst_h": self.dst_h,
            "scale_w": self.scale_w,
            "scale_h": self.scale_h,
            "crop_x": self.crop_x,
            "crop_y": self.crop_y,
            "aspect_delta_pct": round(self.aspect_delta_pct, 4),
            "crop_loss_pct": round(self.crop_loss_pct, 4),
        }


def _check_positive(**dims: int) -> None:
    for name, value in dims.items():
        if value <= 0:
            raise GeometryError(f"{name} must be positive, got {value}")


def _even_up(value: float) -> int:
    """yuv420p needs even dimensions; always round *up* so we still cover."""
    rounded = int(value + 0.999999)
    return rounded + (rounded % 2)


def aspect_delta_pct(src_w: int, src_h: int, dst_w: int, dst_h: int) -> float:
    _check_positive(src_w=src_w, src_h=src_h, dst_w=dst_w, dst_h=dst_h)
    src_ar = src_w / src_h
    dst_ar = dst_w / dst_h
    return abs(src_ar - dst_ar) / dst_ar * 100.0


def fit_plan(
    src_w: int,
    src_h: int,
    dst_w: int,
    dst_h: int,
    tol_pct: float = 2.0,
) -> FitPlan:
    """Plan "scale to cover, then centre-crop" or raise :class:`GeometryError`."""
    _check_positive(src_w=src_w, src_h=src_h, dst_w=dst_w, dst_h=dst_h)
    if tol_pct < 0:
        raise GeometryError(f"tol_pct must be >= 0, got {tol_pct}")
    delta = aspect_delta_pct(src_w, src_h, dst_w, dst_h)
    if delta > tol_pct:
        raise GeometryError(
            f"aspect mismatch: source {src_w}x{src_h} differs from destination "
            f"{dst_w}x{dst_h} by {delta:.3f}% (tolerance {tol_pct:.3f}%)"
        )
    factor = max(dst_w / src_w, dst_h / src_h)
    scale_w = _even_up(src_w * factor)
    scale_h = _even_up(src_h * factor)
    # Coverage is guaranteed by construction, but even-rounding must never
    # leave a row/column short on the *other* axis either.
    scale_w = max(scale_w, dst_w)
    scale_h = max(scale_h, dst_h)
    crop_x = (scale_w - dst_w) // 2
    crop_y = (scale_h - dst_h) // 2
    # Keep crop offsets even for yuv420p chroma alignment.
    crop_x -= crop_x % 2
    crop_y -= crop_y % 2
    return FitPlan(
        src_w=src_w,
        src_h=src_h,
        dst_w=dst_w,
        dst_h=dst_h,
        scale_w=scale_w,
        scale_h=scale_h,
        crop_x=crop_x,
        crop_y=crop_y,
        aspect_delta_pct=delta,
        crop_loss_pct_x=(scale_w - dst_w) / scale_w * 100.0,
        crop_loss_pct_y=(scale_h - dst_h) / scale_h * 100.0,
    )


def destination_for(aspect: str, short_side_px: int) -> tuple[int, int]:
    """(width, height) for ``short_side_px`` on a ``9:16`` / ``16:9`` canvas."""
    if aspect == "9:16":
        return short_side_px, int(short_side_px * 16 / 9)
    if aspect == "16:9":
        return int(short_side_px * 16 / 9), short_side_px
    raise GeometryError(f"unsupported aspect {aspect!r}; expected 9:16 or 16:9")
