"""
Master pipeline runner: processes all videos in input/ through the full editing pipeline.

Usage:
  python execution/run_pipeline.py                                # all videos, full pipeline
  python execution/run_pipeline.py input/video.mp4                # one video, full pipeline
  python execution/run_pipeline.py input/video.mp4 --step 08b    # one video, one step
  python execution/run_pipeline.py --step 09                     # all videos, one step
  python execution/run_pipeline.py --skip 08b,09                 # skip specific steps
  python execution/run_pipeline.py --only 01,08b,09              # run only these steps
  python execution/run_pipeline.py --enable 07                   # permanently enable step(s)
  python execution/run_pipeline.py --disable 08_zoom_pan         # permanently disable step(s)
  python execution/run_pipeline.py --list                        # list all steps + status
  python execution/run_pipeline.py input/video.mp4 --verify      # verify output after each step
  python execution/run_pipeline.py input/video.mp4 --verify --fail-fast
                                                              # stop at first failure and exit non-zero
  python execution/run_pipeline.py --dry-run                   # import step modules only (no video)
  python execution/test_pipeline_steps.py list|smoke|run ...   # developer harness for single steps
"""
import sys
import os
import json
import time
import traceback

# Add execution dir to path
sys.path.insert(0, os.path.dirname(__file__))

from importlib import import_module

# All steps in pipeline order. "enabled" is the default state.
ALL_STEPS = [
    {"id": "01", "module": "01_transcribe",       "func": "transcribe",          "label": "Transcription",      "enabled": True},
    {"id": "02", "module": "02_remove_retakes",   "func": "remove_retakes",      "label": "Remove Retakes",     "enabled": True},
    {"id": "03", "module": "03_remove_fillers",   "func": "remove_fillers",      "label": "Remove Fillers",     "enabled": True},
    {"id": "04", "module": "04_studio_sound",     "func": "apply_studio_sound",  "label": "Studio Sound",       "enabled": True},
    {"id": "05", "module": "05_fix_mute",         "func": "fix_mute",            "label": "Fix Mute Gaps",      "enabled": True},
    {"id": "06", "module": "06_split_scenes",     "func": "split_scenes",        "label": "Scene Detection",    "enabled": True},
    {"id": "07", "module": "07_color_correction", "func": "color_correct",       "label": "Color Correction",   "enabled": False},
    {"id": "08", "module": "08_zoom_pan",         "func": "apply_zoom_pan",      "label": "Zoom & PAN Effects", "enabled": False},
    {"id": "08b","module": "08b_hard_cut_zoom",   "func": "apply_hard_cut_zoom", "label": "Hard Cut Zoom",      "enabled": True},
    {"id": "08c","module": "08c_broll",           "func": "apply_broll",         "label": "B-Roll Overlay",     "enabled": True},
    {"id": "09", "module": "09_captions",         "func": "add_captions",        "label": "Captions",           "enabled": True},
    {"id": "10", "module": "10_background_music", "func": "add_background_music","label": "Background Music",   "enabled": True},
]

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "pipeline_config.json")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}


# ── Config persistence ────────────────────────────────────────

def load_config() -> dict:
    """Load disabled steps from config file."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict):
    """Save config to file."""
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def get_steps() -> list:
    """Return ALL_STEPS with enabled/disabled state from config."""
    cfg = load_config()
    overrides = cfg.get("disabled_steps", [])
    enabled_overrides = cfg.get("enabled_steps", [])
    steps = []
    for s in ALL_STEPS:
        step = dict(s)
        if step["id"] in overrides or step["module"] in overrides:
            step["enabled"] = False
        if step["id"] in enabled_overrides or step["module"] in enabled_overrides:
            step["enabled"] = True
        steps.append(step)
    return steps


# ── Step resolution ───────────────────────────────────────────

def match_step(query: str, steps: list) -> dict | None:
    """Find a step by id, module prefix, or label substring."""
    q = query.lower().strip()
    for s in steps:
        if q == s["id"] or s["module"].startswith(q) or s["module"] == q:
            return s
        if q in s["label"].lower():
            return s
    return None


def resolve_step_list(csv: str, steps: list) -> list:
    """Resolve a comma-separated list of step queries."""
    result = []
    for q in csv.split(","):
        q = q.strip()
        if not q:
            continue
        found = match_step(q, steps)
        if found:
            result.append(found)
        else:
            print(f"Warning: step '{q}' not found, skipping")
    return result


# ── Cleanup ───────────────────────────────────────────────────

def clean_tmp(video_path: str, tmp_dir: str = ".tmp"):
    """Remove all intermediate files for a specific video from .tmp/."""
    base = os.path.splitext(os.path.basename(video_path))[0]
    if not os.path.isdir(tmp_dir):
        return
    removed = 0
    for f in os.listdir(tmp_dir):
        if f.startswith(base):
            path = os.path.join(tmp_dir, f)
            if os.path.isfile(path):
                os.remove(path)
                removed += 1
    if removed:
        print(f"  Cleaned {removed} intermediate files for {base}")


# ── Execution ─────────────────────────────────────────────────

def find_videos(input_dir: str = "input") -> list:
    videos = []
    for f in sorted(os.listdir(input_dir)):
        if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
            videos.append(os.path.join(input_dir, f))
    return videos


def run_step(step: dict, video_path: str):
    module_name, func_name, label = step["module"], step["func"], step["label"]
    print(f"\n--- [{step['id']}] {label} ---")
    start = time.time()
    try:
        mod = import_module(module_name)
        func = getattr(mod, func_name)
        func(video_path)
        elapsed = time.time() - start
        print(f"  Completed in {elapsed:.1f}s")
        return True
    except Exception as e:
        print(f"  ERROR: {e}")
        traceback.print_exc()
        return False


def expected_outputs_for_step(step: dict, video_path: str) -> list:
    """Resolve expected output file paths for a step."""
    base = os.path.splitext(os.path.basename(video_path))[0]
    patterns = STEP_OUTPUT_PATTERNS.get(step["id"], [])
    return [os.path.abspath(p.format(base=base)) for p in patterns]


def verify_step_output(step: dict, video_path: str) -> bool:
    """Check if expected output artifact(s) for step exist and are non-empty."""
    expected_files = expected_outputs_for_step(step, video_path)
    if not expected_files:
        return True

    missing = []
    empty = []
    for path in expected_files:
        if not os.path.exists(path):
            missing.append(path)
            continue
        if os.path.isfile(path) and os.path.getsize(path) == 0:
            empty.append(path)

    if missing or empty:
        if missing:
            print("  VERIFY FAILED: missing output file(s):")
            for p in missing:
                print(f"    - {p}")
        if empty:
            print("  VERIFY FAILED: empty output file(s):")
            for p in empty:
                print(f"    - {p}")
        return False

    for path in expected_files:
        print(f"  Verified output: {path}")
    return True


def process_video(video_path: str, steps: list, do_clean: bool = False, verify_outputs: bool = False, fail_fast: bool = False) -> bool:
    print(f"\n{'='*60}")
    print(f"  Processing: {video_path}")
    print(f"  Steps: {len(steps)} active")
    print(f"{'='*60}")

    if do_clean:
        clean_tmp(video_path)

    start_total = time.time()
    all_ok = True
    for step in steps:
        step_ok = run_step(step, video_path)
        if verify_outputs and step_ok:
            step_ok = verify_step_output(step, video_path)
        if not step_ok:
            all_ok = False
            if fail_fast:
                print("  Stopping due to --fail-fast")
                break

    total_elapsed = time.time() - start_total
    print(f"\n{'='*60}")
    status = "SUCCESS" if all_ok else "FAILED"
    print(f"  Done: {video_path} ({total_elapsed:.1f}s) -> {status}")
    print(f"{'='*60}\n")
    return all_ok


# ── CLI ───────────────────────────────────────────────────────

def print_steps_table(steps: list):
    print("\nPipeline steps:")
    for s in steps:
        icon = "  " if s["enabled"] else "# "
        print(f"  {icon}{s['id']:5s} {s['module']:25s} {s['label']}")
    print("\n  # = disabled (won't run in full pipeline)")
    print("""
Commands:
  --list                   Show this table
  --step <id>              Run only one step
  --skip <id,id,...>       Skip steps for this run
  --only <id,id,...>       Run only these steps for this run
  --disable <id,id,...>    Permanently disable steps (saved to pipeline_config.json)
  --enable <id,id,...>     Permanently enable steps (saved to pipeline_config.json)
  --clean                  Clear .tmp/ files for the video before running
  --verify                 Verify expected output file after each step
  --fail-fast              Stop run on first failed step (or failed verify)
  --dry-run                Import each selected step's module/callable only (exits 0 if OK)

Step IDs accept: number (08b), module name (08b_hard_cut_zoom), or label (captions)
""")


def get_flag_value(flag: str) -> str | None:
    """Get the value after a flag like --step 08b."""
    if flag not in sys.argv:
        return None
    idx = sys.argv.index(flag)
    if idx + 1 >= len(sys.argv):
        print(f"Error: {flag} requires a value")
        sys.exit(1)
    return sys.argv[idx + 1]


def main():
    all_steps = get_steps()

    # --list
    if "--list" in sys.argv:
        print_steps_table(all_steps)
        return

    # --enable / --disable (persistent, then exit)
    enable_val = get_flag_value("--enable")
    disable_val = get_flag_value("--disable")
    if enable_val or disable_val:
        cfg = load_config()
        disabled = set(cfg.get("disabled_steps", []))
        enabled = set(cfg.get("enabled_steps", []))
        if enable_val:
            for s in resolve_step_list(enable_val, all_steps):
                enabled.add(s["id"])
                disabled.discard(s["id"])
                print(f"  Enabled: [{s['id']}] {s['label']}")
        if disable_val:
            for s in resolve_step_list(disable_val, all_steps):
                disabled.add(s["id"])
                enabled.discard(s["id"])
                print(f"  Disabled: [{s['id']}] {s['label']}")
        cfg["disabled_steps"] = sorted(disabled)
        cfg["enabled_steps"] = sorted(enabled)
        save_config(cfg)
        print(f"\nSaved to {os.path.abspath(CONFIG_PATH)}")
        # Refresh and show
        print_steps_table(get_steps())
        return

    # Build active steps for this run
    active_steps = [s for s in all_steps if s["enabled"]]

    # --step (single step, overrides everything)
    step_val = get_flag_value("--step")
    if step_val:
        found = match_step(step_val, all_steps)
        if not found:
            print(f"Error: step '{step_val}' not found")
            print_steps_table(all_steps)
            sys.exit(1)
        active_steps = [found]

    # --only (run only these)
    only_val = get_flag_value("--only")
    if only_val:
        active_steps = resolve_step_list(only_val, all_steps)
        if not active_steps:
            print("Error: no valid steps in --only list")
            sys.exit(1)

    # --skip (remove from this run)
    skip_val = get_flag_value("--skip")
    if skip_val:
        skip_ids = {s["id"] for s in resolve_step_list(skip_val, all_steps)}
        active_steps = [s for s in active_steps if s["id"] not in skip_ids]

    if not active_steps:
        print("Error: no steps to run after applying filters")
        sys.exit(1)

    # --dry-run (no video required; validates imports for the active step set)
    if "--dry-run" in sys.argv:
        print("Dry run: validating step modules and callables...\n")
        failed = False
        for step in active_steps:
            try:
                mod = import_module(step["module"])
                fn = getattr(mod, step["func"])
                if not callable(fn):
                    print(f"  FAIL [{step['id']}] {step['module']}.{step['func']} is not callable")
                    failed = True
                else:
                    print(f"  OK   [{step['id']}] {step['module']}.{step['func']}")
            except Exception as e:
                print(f"  FAIL [{step['id']}] {step['module']}: {e}")
                failed = True
        print()
        if failed:
            print("Dry run failed.")
            sys.exit(1)
        print("Dry run passed.")
        return

    # --clean
    do_clean = "--clean" in sys.argv
    verify_outputs = "--verify" in sys.argv
    fail_fast = "--fail-fast" in sys.argv

    # Resolve videos (positional args that aren't flag values)
    flag_values = set()
    for flag in ["--step", "--skip", "--only", "--enable", "--disable"]:
        v = get_flag_value(flag)
        if v:
            flag_values.add(v)
    video_args = [
        a for a in sys.argv[1:]
        if not a.startswith("--") and a not in flag_values
    ]

    if video_args:
        video_path = video_args[0]
        if not os.path.exists(video_path):
            print(f"Error: {video_path} not found")
            sys.exit(1)
        ok = process_video(video_path, active_steps, do_clean, verify_outputs, fail_fast)
        if not ok:
            sys.exit(1)
    else:
        videos = find_videos()
        if not videos:
            print("No videos found in input/")
            sys.exit(1)

        print(f"Found {len(videos)} video(s):")
        for v in videos:
            print(f"  - {v}")

        failed_videos = []
        for video in videos:
            ok = process_video(video, active_steps, do_clean, verify_outputs, fail_fast)
            if not ok:
                failed_videos.append(video)
                if fail_fast:
                    break

        if failed_videos:
            print("\nRun completed with failures:")
            for video in failed_videos:
                print(f"  - {video}")
            sys.exit(1)

        print("\nAll done! Check output/ for final videos.")


def run_with_file_logging():
    """Run pipeline while duplicating all stdout/stderr into a log file."""
    log_path = build_log_file_path()
    original_stdout = sys.stdout
    original_stderr = sys.stderr

    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            sys.stdout = TeeStream(original_stdout, log_file)
            sys.stderr = TeeStream(original_stderr, log_file)
            print(f"Logging this run to: {log_path}")
            main()
    finally:
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        print(f"Run log saved to: {log_path}")


if __name__ == "__main__":
    run_with_file_logging()
