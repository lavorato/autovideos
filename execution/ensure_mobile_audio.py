"""
One-off: re-mux a deliverable MP4 in place so the audio is AAC-LC 48kHz stereo
(video copied, +faststart). Use on existing `*_final*.mp4` that still have ALAC
or other codecs that fail on Android / in-app players.

  python execution/ensure_mobile_audio.py output/YourClip_final.mp4
"""
from __future__ import annotations

import os
import sys

EXEC = os.path.dirname(os.path.abspath(__file__))
if EXEC not in sys.path:
    sys.path.insert(0, EXEC)

from video_encoding import ensure_mp4_aac_stereo_48k, probe_first_audio_codec_name


def main() -> int:
    if len(sys.argv) < 2:
        print("Usage: python execution/ensure_mobile_audio.py <file.mp4> [file2.mp4 ...]")
        return 1
    for path in sys.argv[1:]:
        before = probe_first_audio_codec_name(path)
        if ensure_mp4_aac_stereo_48k(path):
            print(f"Remuxed audio to AAC: {path}  (was {before})")
        else:
            after = probe_first_audio_codec_name(path)
            if before is None and after is None:
                print(f"No audio stream: {path}")
            elif before == "aac":
                print(f"Already AAC, skipped: {path}")
            else:
                print(f"Unchanged: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
