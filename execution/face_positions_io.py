"""
Face-position sidecar: Haar samples from detect_face_positions (steps 08 / 08b) as JSON
for reuse (captions, overlays, future steps). Written next to other .tmp sidecars, e.g.
  {editor_stem}_face_positions.json
"""
from __future__ import annotations

import json
import os
from typing import Any

FORMAT_VERSION = 1
JSON_SUFFIX = "_face_positions.json"


def face_positions_json_path(tmp_dir: str, editor_stem: str) -> str:
    """Path: tmp_dir + editor_stem + _face_positions.json (use stem_for_editor_gate for stem)."""
    return os.path.join(tmp_dir, f"{editor_stem}{JSON_SUFFIX}")


def _json_default(obj: Any) -> Any:
    if hasattr(obj, "item") and callable(getattr(obj, "item", None)):
        try:
            return obj.item()
        except (ValueError, TypeError):
            pass
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def write_face_positions_json(path: str, document: dict[str, Any]) -> None:
    out = {**document, "format_version": FORMAT_VERSION}
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=_json_default)


def read_face_positions_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
