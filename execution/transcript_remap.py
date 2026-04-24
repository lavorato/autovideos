"""
Remap word/segment timestamps after FFmpeg trim+concat cuts.

``keeps`` is an ordered list of (start, end) intervals in the *source* timeline
that were concatenated in order to build the new video.
"""
from __future__ import annotations

import os
from typing import Any


def _acc_before_keeps(keeps: list[tuple[float, float]], idx: int) -> float:
    return sum(e - s for s, e in keeps[:idx])


def _find_keep_for_word(
    ws: float,
    we: float,
    keeps: list[tuple[float, float]],
    tol: float = 0.02,
) -> tuple[int, float, float] | None:
    for i, (s, e) in enumerate(keeps):
        if ws >= s - tol and we <= e + tol:
            return i, s, e
    return None


def remap_transcript_to_keeps(
    data: dict[str, Any],
    keeps: list[tuple[float, float]],
    *,
    new_video_path: str,
    new_video_size: int | None,
    new_video_duration: float | None,
) -> dict[str, Any]:
    """
    Return a new transcript dict with ``words`` / ``segments`` remapped into the
    concatenated timeline of ``keeps``. Words that fall entirely inside removed
    regions are dropped (e.g. cut filler tokens).
    """
    keeps = [(float(a), float(b)) for a, b in keeps]
    words_in = list(data.get("words") or [])
    new_words: list[dict[str, Any]] = []
    for w in words_in:
        ws = float(w.get("start", 0))
        we = float(w.get("end", 0))
        found = _find_keep_for_word(ws, we, keeps)
        if found is None:
            continue
        ki, s, _e = found
        acc = _acc_before_keeps(keeps, ki)
        nw = dict(w)
        nw["start"] = round(acc + (ws - s), 3)
        nw["end"] = round(acc + (we - s), 3)
        new_words.append(nw)

    segments_in = list(data.get("segments") or [])
    new_segments: list[dict[str, Any]] = []
    for seg in segments_in:
        sub = seg.get("words") or []
        if isinstance(sub, list) and sub:
            mapped_sub: list[dict[str, Any]] = []
            for w in sub:
                ws = float(w.get("start", 0))
                we = float(w.get("end", 0))
                found = _find_keep_for_word(ws, we, keeps)
                if found is None:
                    continue
                ki, s, _e = found
                acc = _acc_before_keeps(keeps, ki)
                nw = dict(w)
                nw["start"] = round(acc + (ws - s), 3)
                nw["end"] = round(acc + (we - s), 3)
                mapped_sub.append(nw)
            if not mapped_sub:
                continue
            ns = dict(seg)
            ns["words"] = mapped_sub
            ns["start"] = mapped_sub[0]["start"]
            ns["end"] = mapped_sub[-1]["end"]
            ns["text"] = "".join(str(x.get("word", "")) for x in mapped_sub).strip()
            new_segments.append(ns)

    full_text = " ".join(
        str(w["word"]).strip() for w in new_words if str(w.get("word", "")).strip()
    )
    out = dict(data)
    out["words"] = new_words
    out["segments"] = new_segments
    out["text"] = full_text
    out["video"] = os.path.abspath(new_video_path)
    if new_video_size is not None:
        out["video_size"] = new_video_size
    if new_video_duration is not None:
        out["video_duration"] = round(float(new_video_duration), 3)
    return out


def write_transcript(path: str, data: dict[str, Any]) -> None:
    import json

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
