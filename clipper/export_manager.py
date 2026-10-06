"""Export manager shim — re-exports clip_renderer for PRD naming."""
from chopster.clipper.clip_renderer import export_clip, export_batch, ExportResult, QUALITY_PRESETS, _verify_output  # noqa: F401

__all__ = ["export_clip", "export_batch", "ExportResult", "QUALITY_PRESETS"]
