"""
Step 6: Split video into scenes using PySceneDetect.
Outputs scene markers JSON and optionally splits into separate files.
"""
import sys
import json
import os
from scenedetect import open_video, SceneManager
from scenedetect.detectors import ContentDetector, AdaptiveDetector
import env_paths
from video_encoding import build_lossless_x264_args


def split_scenes(video_path: str, tmp_dir: str | None = None,
                 threshold: float = 27.0, split_files: bool = False) -> str:
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
    base = os.path.splitext(os.path.basename(video_path))[0]
    input_video = os.path.join(tmp_dir, f"{base}_fixed_audio.mp4")
    if not os.path.exists(input_video):
        input_video = video_path
    output_json = os.path.join(tmp_dir, f"{base}_scenes.json")

    print(f"[06] Detecting scenes in: {input_video}")

    video = open_video(input_video)
    scene_manager = SceneManager()
    scene_manager.add_detector(ContentDetector(threshold=threshold))
    scene_manager.detect_scenes(video)
    scene_list = scene_manager.get_scene_list()

    scenes = []
    for i, (start, end) in enumerate(scene_list):
        scenes.append({
            "scene": i + 1,
            "start_frame": start.get_frames(),
            "end_frame": end.get_frames(),
            "start_time": start.get_seconds(),
            "end_time": end.get_seconds(),
            "duration": end.get_seconds() - start.get_seconds(),
        })

    scene_data = {
        "video": input_video,
        "total_scenes": len(scenes),
        "scenes": scenes,
    }

    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(scene_data, f, indent=2)

    print(f"[06] Detected {len(scenes)} scenes")
    for s in scenes:
        print(f"  Scene {s['scene']}: {s['start_time']:.2f}s - {s['end_time']:.2f}s ({s['duration']:.1f}s)")

    # Optionally split into separate files
    if split_files and scenes:
        import subprocess
        scenes_dir = os.path.join(tmp_dir, "scenes")
        os.makedirs(scenes_dir, exist_ok=True)
        for s in scenes:
            out = os.path.join(scenes_dir, f"{base}_scene{s['scene']:03d}.mp4")
            cmd = [
                "ffmpeg", "-y", "-i", input_video,
                "-ss", str(s["start_time"]),
                "-to", str(s["end_time"]),
                *build_lossless_x264_args(input_video),
                "-c:a", "alac",
                out,
            ]
            subprocess.run(cmd, check=True, capture_output=True)
        print(f"[06] Split files saved to: {scenes_dir}/")

    print(f"[06] Scene markers: {output_json}")
    return output_json


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 06_split_scenes.py <video_path> [--split]")
        sys.exit(1)
    do_split = "--split" in sys.argv
    split_scenes(sys.argv[1], split_files=do_split)
