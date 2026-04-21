"""
Step 7: Color correction - auto white balance, exposure, saturation, contrast.
Applies a subtle cinematic look.
"""
import sys
import os
import subprocess


def color_correct(video_path: str, tmp_dir: str = ".tmp") -> str:
    base = os.path.splitext(os.path.basename(video_path))[0]
    input_video = os.path.join(tmp_dir, f"{base}_fixed_audio.mp4")
    if not os.path.exists(input_video):
        input_video = video_path
    output_path = os.path.join(tmp_dir, f"{base}_color.mp4")

    print(f"[07] Color correction disabled (preserving source colors): {input_video}")

    cmd = [
        "ffmpeg", "-y", "-i", input_video,
        "-c:v", "copy",
        "-c:a", "copy",
        output_path,
    ]
    print("[07] Copying streams without color changes...")
    subprocess.run(cmd, check=True, capture_output=True)
    print(f"[07] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 07_color_correction.py <video_path>")
        sys.exit(1)
    color_correct(sys.argv[1])
