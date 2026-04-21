"""
Step 8: Zoom and PAN effects every ~6 seconds, centering on the face subject.
Uses MediaPipe for face detection and MoviePy for smooth animated effects.
"""
import sys
import os
import json
import numpy as np
import cv2
from moviepy import VideoFileClip
import subprocess
from video_encoding import build_moviepy_lossless_params


def detect_face_positions(video_path: str, sample_fps: float = 5.0) -> list:
    """Sample frames and detect face center positions using Haar cascade."""
    print("[08] Detecting face positions (Haar cascade)...")
    face_cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )

    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    sample_interval = max(1, int(fps / sample_fps))
    positions = []

    frame_idx = 0
    while cap.isOpened():
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % sample_interval == 0:
            t = frame_idx / fps
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            scale = min(1.0, 640.0 / max(width, height))
            small = cv2.resize(gray, None, fx=scale, fy=scale)
            faces = face_cascade.detectMultiScale(small, 1.1, 5, minSize=(30, 30))

            cx, cy = 0.5, 0.5
            if len(faces) > 0:
                largest = max(faces, key=lambda f: f[2] * f[3])
                fx, fy, fw, fh = largest
                cx = (fx + fw / 2) / small.shape[1]
                cy = (fy + fh / 2) / small.shape[0]

            positions.append({"time": t, "cx": cx, "cy": cy})

        frame_idx += 1

    cap.release()
    print(f"[08] Sampled {len(positions)} face positions")
    return positions, width, height, fps


def smooth_positions(positions: list, window: int = 5) -> list:
    """Smooth face positions to avoid jitter."""
    if len(positions) < window:
        return positions
    smoothed = []
    for i in range(len(positions)):
        start = max(0, i - window // 2)
        end = min(len(positions), i + window // 2 + 1)
        avg_cx = np.mean([p["cx"] for p in positions[start:end]])
        avg_cy = np.mean([p["cy"] for p in positions[start:end]])
        smoothed.append({"time": positions[i]["time"], "cx": avg_cx, "cy": avg_cy})
    return smoothed


def ease_in_out(t: float) -> float:
    """Smooth easing function (cubic)."""
    if t < 0.5:
        return 4 * t * t * t
    return 1 - pow(-2 * t + 2, 3) / 2


def generate_zoom_pan_filter(positions: list, width: int, height: int,
                              fps: float, duration: float,
                              effect_interval: float = 6.0,
                              zoom_amount: float = 1.15,
                              effect_duration: float = 2.5) -> str:
    """Generate FFmpeg zoompan filter with face-centered effects."""
    # We'll use the zoompan filter with expressions
    # Build keyframes for zoom effects every N seconds
    total_frames = int(duration * fps)

    # For zoompan, we need frame-based expressions
    # z: zoom level, x/y: pan position
    # Effect: zoom from 1.0 to zoom_amount and back, centered on face

    # Build a simpler approach: use multiple trim+scale+crop segments
    # Actually, let's use the crop+scale approach for more control

    effects = []
    t = 0
    effect_idx = 0
    while t < duration:
        # Find face position at this time
        pos = min(positions, key=lambda p: abs(p["time"] - t))
        effects.append({
            "start": t,
            "end": min(t + effect_duration, duration),
            "cx": pos["cx"],
            "cy": pos["cy"],
            "type": "zoom" if effect_idx % 2 == 0 else "pan",
        })
        t += effect_interval
        effect_idx += 1

    return effects


def apply_zoom_pan(video_path: str, tmp_dir: str = ".tmp") -> str:
    base = os.path.splitext(os.path.basename(video_path))[0]
    input_video = os.path.join(tmp_dir, f"{base}_color.mp4")
    if not os.path.exists(input_video):
        input_video = video_path
    output_path = os.path.join(tmp_dir, f"{base}_effects.mp4")

    positions, width, height, fps = detect_face_positions(input_video)
    positions = smooth_positions(positions)

    # Get duration
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", input_video],
        capture_output=True, text=True, check=True,
    )
    duration = float(probe.stdout.strip())

    effects = generate_zoom_pan_filter(positions, width, height, fps, duration)

    print(f"[08] Applying {len(effects)} zoom/pan effects...")

    # Use MoviePy for frame-accurate zoom/pan with face tracking
    clip = VideoFileClip(input_video)

    def get_face_at_time(t):
        if not positions:
            return 0.5, 0.5
        closest = min(positions, key=lambda p: abs(p["time"] - t))
        return closest["cx"], closest["cy"]

    def make_frame(get_frame, t):
        frame = get_frame(t)
        h, w = frame.shape[:2]

        # Determine if we're in an effect
        zoom = 1.0
        pan_x, pan_y = 0.0, 0.0

        for eff in effects:
            if eff["start"] <= t < eff["end"]:
                eff_dur = eff["end"] - eff["start"]
                progress = (t - eff["start"]) / eff_dur

                # Ease in first half, ease out second half
                if progress < 0.5:
                    p = ease_in_out(progress * 2)
                else:
                    p = ease_in_out((1.0 - progress) * 2)

                if eff["type"] == "zoom":
                    zoom = 1.0 + (0.15 * p)
                else:  # pan
                    cx, cy = eff["cx"], eff["cy"]
                    pan_x = (cx - 0.5) * 0.1 * p
                    pan_y = (cy - 0.5) * 0.1 * p
                break

        if zoom == 1.0 and pan_x == 0.0 and pan_y == 0.0:
            return frame

        # Apply zoom centered on face
        cx, cy = get_face_at_time(t)

        # Calculate crop region
        new_w = int(w / zoom)
        new_h = int(h / zoom)

        # Center crop on face position with pan offset
        crop_cx = int((cx + pan_x) * w)
        crop_cy = int((cy + pan_y) * h)

        x1 = max(0, crop_cx - new_w // 2)
        y1 = max(0, crop_cy - new_h // 2)
        x2 = x1 + new_w
        y2 = y1 + new_h

        # Clamp
        if x2 > w:
            x1 = w - new_w
            x2 = w
        if y2 > h:
            y1 = h - new_h
            y2 = h
        x1 = max(0, x1)
        y1 = max(0, y1)

        cropped = frame[y1:y2, x1:x2]
        # Resize back to original dimensions
        resized = cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LANCZOS4)
        return resized

    processed = clip.transform(lambda gf, t: make_frame(gf, t))
    processed.write_videofile(
        output_path,
        codec="libx264",
        preset="veryslow",
        ffmpeg_params=build_moviepy_lossless_params(input_video),
        audio_codec="alac",
        logger=None,
    )
    clip.close()
    processed.close()

    print(f"[08] Output: {output_path}")
    return output_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python 08_zoom_pan.py <video_path>")
        sys.exit(1)
    apply_zoom_pan(sys.argv[1])
