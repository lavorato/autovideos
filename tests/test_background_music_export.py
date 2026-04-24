"""
Step 10 (background music) should re-encode with build_fast_pipeline_encode_args
so color metadata matches other pipeline export steps. Compare ffprobe fields.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from importlib import import_module
from unittest.mock import patch

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
EXEC = os.path.join(ROOT, "execution")
if EXEC not in sys.path:
    sys.path.insert(0, EXEC)

bgm = import_module("10_background_music")


def _ffprobe_color(path: str) -> dict:
    p = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=color_space,color_transfer,color_primaries,color_range,codec_name",
            "-of",
            "json",
            path,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(p.stdout or "{}")
    streams = data.get("streams") or []
    return streams[0] if streams else {}


def _make_test_video_with_color_tags(path: str) -> None:
    # Short clip with explicit bt709 + tagged range so the probe is non-trivial.
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=#336699:s=128x128:d=1:r=30",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-colorspace",
            "bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _make_silent_mp3(path: str) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-t",
            "2",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "128k",
            path,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


class TestBackgroundMusicExportColor(unittest.TestCase):
    def test_output_video_color_tags_match_input(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vpath = os.path.join(tmp, "color_probe.mp4")
            _make_test_video_with_color_tags(vpath)
            music = os.path.join(tmp, "bed.mp3")
            _make_silent_mp3(music)
            out_dir = os.path.join(tmp, "out")
            os.makedirs(out_dir, exist_ok=True)

            with patch.object(bgm, "pick_random_track", return_value=music):
                out = bgm.add_background_music(
                    vpath, tmp_dir=tmp, output_dir=out_dir
                )
            self.assertTrue(out and os.path.isfile(out), f"missing output: {out!r}")

            before = _ffprobe_color(vpath)
            after = _ffprobe_color(out)
            for key in (
                "color_space",
                "color_transfer",
                "color_primaries",
                "color_range",
            ):
                self.assertEqual(
                    before.get(key),
                    after.get(key),
                    f"{key}: input={before.get(key)} output={after.get(key)}",
                )


if __name__ == "__main__":
    unittest.main()
