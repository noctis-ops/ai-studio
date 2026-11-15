"""Demo pipeline (Phase 1): deterministic end-to-end slice used by tests and
`studio run --demo`."""

from studio.demo.seed import SeedResult, seed_demo
from studio.demo.stages import demo_specs

__all__ = ["SeedResult", "demo_specs", "seed_demo"]
