"""
Master pipeline runner: processes all videos in input/ through the full editing pipeline.

Usage:
  python execution/run_pipeline.py                                # all videos, full pipeline
  python execution/run_pipeline.py input/video.mp4                # one video, full pipeline
  python execution/run_pipeline.py input/video.mp4 --step 08b    # one video, one step
  python execution/run_pipeline.py --step 09                     # all videos, one step
  python execution/run_pipeline.py --skip 08b,09                 # skip specific steps
  # --skip 00: if .tmp/STEM.mp4 exists (from 00b), it is used as source — not raw input/STEM.mp4
  python execution/run_pipeline.py --only 01,08b,09              # run only these steps
  python execution/run_pipeline.py --enable 07                   # permanently enable step(s)
  python execution/run_pipeline.py --disable 08_zoom_pan         # permanently disable step(s)
  python execution/run_pipeline.py --list                        # list all steps + status
  python execution/run_pipeline.py input/video.mp4 --verify      # verify output after each step
  python execution/run_pipeline.py input/video.mp4 --verify --fail-fast
                                                              # stop at first failure and exit non-zero
  python execution/run_pipeline.py --dry-run                   # import step modules only (no video)
  python execution/run_pipeline.py --watch                     # always-on: auto-run 00 (convert) on new input/ files
  python execution/run_pipeline.py --watch --interval 5        # watcher with custom poll interval
  python execution/run_pipeline.py --skip-editor-gate ...     # skip trim + transcript review gates
  python execution/test_pipeline_steps.py list|smoke|run ...   # developer harness for single steps
"""
import sys
import os
import json
import time
import traceback
from contextlib import contextmanager

# Add execution dir to path
sys.path.insert(0, os.path.dirname(__file__))

# Load environment variables from the repo-root .env as early as possible,
# so every step module (imported below) sees OPENROUTER_API_KEY, WHISPERX_*,
# BROLL_*, etc. regardless of the current working directory.
try:
    from dotenv import load_dotenv
    _ENV_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    load_dotenv(_ENV_PATH, override=False)
except ImportError:
    pass

from importlib import import_module

import editor_gate
import env_paths

# All steps in pipeline order. "enabled" is the default state.
ALL_STEPS = [
    {"id": "00", "module": "00_convert_source",   "func": "convert_source",      "label": "Convert Source",     "enabled": True},
    {"id": "01", "module": "01_transcribe",       "func": "transcribe",          "label": "Transcription",      "enabled": True},
    {"id": "03", "module": "03_remove_fillers",   "func": "remove_fillers",      "label": "Remove Fillers",     "enabled": True},
    {"id": "02", "module": "02_remove_retakes",   "func": "remove_retakes",      "label": "Remove Retakes",     "enabled": True},
    {"id": "03b","module": "03b_isolate_voice",   "func": "isolate_voice",       "label": "Isolate Voice",      "enabled": True},
    {"id": "04", "module": "04_studio_sound",     "func": "apply_studio_sound",  "label": "Studio Sound",       "enabled": True},
    {"id": "05", "module": "05_fix_mute",         "func": "fix_mute",            "label": "Fix Mute Gaps",      "enabled": True},
    {"id": "06", "module": "06_split_scenes",     "func": "split_scenes",        "label": "Scene Detection",    "enabled": True},
    {"id": "07", "module": "07_color_correction", "func": "color_correct",       "label": "Color Correction",   "enabled": False},
    {"id": "08", "module": "08_zoom_pan",         "func": "apply_zoom_pan",      "label": "Zoom & PAN Effects", "enabled": False},
    {"id": "08a","module": "08a_multicam",        "func": "apply_multicam",      "label": "Multi-Cam Intercut", "enabled": True},
    {"id": "08b","module": "08b_hard_cut_zoom",   "func": "apply_hard_cut_zoom", "label": "Hard Cut Zoom",      "enabled": True},
    {"id": "08c","module": "08c_broll",           "func": "apply_broll",         "label": "B-Roll Overlay",     "enabled": True},
    {"id": "08d","module": "08d_fx_sounds",       "func": "add_fx_sounds",       "label": "FX Sounds",          "enabled": True},
    {"id": "08e","module": "08e_data_viz",        "func": "apply_data_viz",      "label": "Data Viz Overlay",   "enabled": True},
    {"id": "09", "module": "09_captions",         "func": "add_captions",        "label": "Captions",           "enabled": True},
    {"id": "10", "module": "10_background_music", "func": "add_background_music","label": "Background Music",   "enabled": True},
    {"id": "11", "module": "11_video_ending",     "func": "append_video_ending", "label": "Video Ending (outro)", "enabled": True},
]

REVIEW_GATED_STEP_IDS = editor_gate.editor_gate_step_ids(ALL_STEPS)
TRANSCRIBE_GATED_STEP_IDS = editor_gate.TRANSCRIBE_GATE_STEP_IDS

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "pipeline_config.json")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
LOGS_DIR = env_paths.logs_pipeline_dir()


def step_output_patterns() -> dict:
    """Expected artifact paths per step id (``{base}`` = video stem)."""
    t = env_paths.tmp_dir()
    o = env_paths.output_dir()
    return {
        "00": [os.path.join(t, "{base}.mp4")],
        "01": [os.path.join(t, "{base}_transcript.json")],
        "02": [os.path.join(t, "{base}_no_retakes.mp4")],
        "03": [os.path.join(t, "{base}_no_fillers.mp4")],
        "03b": [os.path.join(t, "{base}_voice.mp4")],
        "04": [os.path.join(t, "{base}_studio.mp4")],
        "05": [os.path.join(t, "{base}_fixed_audio.mp4")],
        "06": [os.path.join(t, "{base}_scenes.json")],
        "07": [os.path.join(t, "{base}_color.mp4")],
        "08": [os.path.join(t, "{base}_effects.mp4")],
        "08a": [os.path.join(t, "{base}_multicam.mp4")],
        "08b": [os.path.join(t, "{base}_hardcut.mp4")],
        "08c": [os.path.join(t, "{base}_broll.mp4")],
        "08d": [os.path.join(t, "{base}_fx.mp4")],
        "08e": [os.path.join(t, "{base}_dataviz.mp4")],
        "09": [os.path.join(o, "{base}_final.mp4")],
        "10": [os.path.join(o, "{base}_final.mp4")],
        "11": [os.path.join(o, "{base}_final.mp4")],
    }


class TeeStream:
    """Write stream output to multiple targets (terminal + log file)."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for stream in self.streams:
            stream.write(data)
        return len(data)

    def flush(self):
        for stream in self.streams:
            stream.flush()

    def isatty(self):
        primary = self.streams[0]
        return getattr(primary, "isatty", lambda: False)()

    def __getattr__(self, attr):
        # Keep compatibility with normal text streams.
        return getattr(self.streams[0], attr)


def build_log_file_path() -> str:
    """Create a timestamped pipeline log file path."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return os.path.abspath(os.path.join(LOGS_DIR, f"run-{timestamp}.log"))


@contextmanager
def pipeline_run_log():
    """Tee stdout/stderr to ``logs/pipeline/run-*.log`` for the block body.

    Use this when invoking ``process_video`` from another module (e.g. ``00b_editor``)
    so file logging matches ``python execution/run_pipeline.py``."""
    log_path = build_log_file_path()
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    log_file = open(log_path, "w", encoding="utf-8")
    try:
        sys.stdout = TeeStream(original_stdout, log_file)
        sys.stderr = TeeStream(original_stderr, log_file)
        print(f"Logging this run to: {log_path}")
        try:
            yield log_path
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
    finally:
        log_file.close()
        print(f"Run log saved to: {log_path}", file=original_stdout, flush=True)


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

def clean_tmp(video_path: str, tmp_dir: str | None = None):
    """Remove all intermediate files for a specific video from the tmp folder."""
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
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

def find_videos(input_dir: str | None = None) -> list:
    if input_dir is None:
        input_dir = env_paths.input_dir()
    videos = []
    for f in sorted(os.listdir(input_dir)):
        if os.path.splitext(f)[1].lower() in VIDEO_EXTENSIONS:
            videos.append(os.path.join(input_dir, f))
    return videos


def run_step(step: dict, video_path: str):
    """Run one pipeline step. Returns (ok, result) where result is whatever
    the step callable returned (usually an output file path, sometimes None)."""
    module_name, func_name, label = step["module"], step["func"], step["label"]
    print(f"\n--- [{step['id']}] {label} ---")
    start = time.time()
    try:
        mod = import_module(module_name)
        func = getattr(mod, func_name)
        result = func(video_path)
        elapsed = time.time() - start
        print(f"  Completed in {elapsed:.1f}s")
        return True, result
    except Exception as e:
        print(f"  ERROR: {e}")
        traceback.print_exc()
        return False, None


def expected_outputs_for_step(step: dict, video_path: str) -> list:
    """Resolve expected output file paths for a step."""
    base = os.path.splitext(os.path.basename(video_path))[0]
    patterns = step_output_patterns().get(step["id"], [])
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


def _resolve_prepared_working_path(
    video_path: str,
    active_step_ids: set,
    tmp_dir: str,
) -> str:
    """If step 00 is not in this run, use ``.tmp/{base}.mp4`` when it exists (trim/convert from
    00b) instead of a path under ``input/``. Without this, ``--skip 00,01`` on ``input/…`` still
    fed the full raw file into 02+ while the transcript and gates referred to the trimmed version."""
    if "00" in active_step_ids:
        return video_path
    base = editor_gate.resolve_base_from_cli_arg(video_path)
    canonical = os.path.abspath(os.path.join(tmp_dir, f"{base}.mp4"))
    current = os.path.abspath(video_path)
    if current == canonical:
        return video_path
    if editor_gate.tmp_base_has_video(tmp_dir, base):
        print(
            f"  Step 00 skipped: using prepared source from .tmp/ (trim + convert as in 00b):"
            f"\n    {canonical}"
        )
        return canonical
    print(
        f"  WARNING: Step 00 skipped and no non-empty {canonical!r}.\n"
        f"  Will use {current!r} for video; it may not match the transcript if that file is the"
        f" unprocessed source. Re-run 00 or save the trim in 00b to create {os.path.basename(canonical)}."
    )
    return video_path


def process_video(
    video_path: str,
    steps: list,
    do_clean: bool = False,
    verify_outputs: bool = False,
    fail_fast: bool = False,
    skip_editor_gate: bool = False,
    tmp_dir: str | None = None,
) -> bool:
    if tmp_dir is None:
        tmp_dir = env_paths.tmp_dir()
    print(f"\n{'='*60}")
    print(f"  Processing: {video_path}")
    print(f"  Steps: {len(steps)} active")
    print(f"{'='*60}")

    if do_clean:
        clean_tmp(video_path, tmp_dir=tmp_dir)

    active_ids = {s["id"] for s in steps}
    video_path = _resolve_prepared_working_path(video_path, active_ids, tmp_dir)
    if "01" not in active_ids and active_ids - {"00", "01"}:
        b = editor_gate.stem_for_editor_gate(editor_gate.resolve_base_from_cli_arg(video_path))
        if not editor_gate.tmp_base_has_transcript(tmp_dir, b):
            print(
                f"  ERROR: Step 01 skipped but no transcript at "
                f"{os.path.join(tmp_dir, f'{b}_transcript.json')!r}.\n"
                f"  Run 01 or finish transcribe in 00b before post-01 steps."
            )
            return False

    start_total = time.time()
    all_ok = True
    for step in steps:
        if (
            not skip_editor_gate
            and step["id"] in TRANSCRIBE_GATED_STEP_IDS
            and not editor_gate.is_trim_complete(video_path, tmp_dir)
        ):
            marker = os.path.abspath(editor_gate.editor_review_path(video_path, tmp_dir))
            print(
                f"\n  BLOCKED: Trim must be finished in 00b before transcribe (step 01).\n"
                f"  Open `python execution/00b_editor.py`, save your trim, then “Mark trim done (ready to transcribe)”.\n"
                f"  Marker file: {marker}\n"
                f"  Or bypass (automation only): --skip-editor-gate or PIPELINE_SKIP_EDITOR_GATE=1\n"
            )
            return False
        if (
            not skip_editor_gate
            and step["id"] in REVIEW_GATED_STEP_IDS
            and not editor_gate.is_editor_review_complete(video_path, tmp_dir)
        ):
            marker = os.path.abspath(editor_gate.editor_review_path(video_path, tmp_dir))
            print(
                f"\n  BLOCKED: Transcript review is not confirmed for this video (post-step-01).\n"
                f"  Expected marker: {marker}\n"
                f"  Open `python execution/00b_editor.py`, fix the transcript if needed, then “Mark transcript review done”.\n"
                f"  Or bypass (automation only): --skip-editor-gate or PIPELINE_SKIP_EDITOR_GATE=1\n"
            )
            return False
        step_ok, step_result = run_step(step, video_path)
        # Step 00 transcodes the source to a smaller .mp4; swap the active
        # video_path so every downstream step operates on the lighter file.
        # The output keeps the original basename (e.g. .tmp/IMG_1792.mp4),
        # so .tmp/{base}_*.* intermediates remain addressable.
        # Any step that returns a non-empty .mp4 becomes the active source for
        # the rest of the run (transcribe returns .json — ignored).
        if (
            step_ok
            and isinstance(step_result, str)
            and step_result.lower().endswith(".mp4")
            and os.path.isfile(step_result)
            and os.path.getsize(step_result) > 0
            and step_result != video_path
        ):
            print(f"  Pipeline source swapped to: {step_result}")
            video_path = step_result
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
  --skip-editor-gate       Skip trim gate (before 01) and review gate (before 02+)
  --dry-run                Import each selected step's module/callable only (exits 0 if OK)
  --watch                  Always-on: monitor input/ and auto-run 00 (convert) on new files
                           (accepts --interval, --once, --force, --input-dir, --tmp-dir)

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

    # --watch: delegate to the always-on folder watcher and exit.
    if "--watch" in sys.argv:
        from importlib import import_module
        watch_mod = import_module("watch_input")
        # Forward supported flags through unchanged; watch_input.py does its
        # own argparse, so we pass everything except --watch itself.
        forwarded = [a for a in sys.argv[1:] if a != "--watch"]
        sys.exit(watch_mod.main(forwarded))

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

    # --clean
    do_clean = "--clean" in sys.argv
    verify_outputs = "--verify" in sys.argv
    fail_fast = "--fail-fast" in sys.argv
    skip_editor_gate = "--skip-editor-gate" in sys.argv or os.environ.get(
        "PIPELINE_SKIP_EDITOR_GATE", ""
    ).strip().lower() in ("1", "true", "yes")

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
        ok = process_video(
            video_path,
            active_steps,
            do_clean,
            verify_outputs,
            fail_fast,
            skip_editor_gate=skip_editor_gate,
        )
        if not ok:
            sys.exit(1)
    else:
        videos = find_videos()
        if not videos:
            print(f"No videos found in {env_paths.input_dir()}/")
            sys.exit(1)

        print(f"Found {len(videos)} video(s):")
        for v in videos:
            print(f"  - {v}")

        failed_videos = []
        for video in videos:
            ok = process_video(
                video,
                active_steps,
                do_clean,
                verify_outputs,
                fail_fast,
                skip_editor_gate=skip_editor_gate,
            )
            if not ok:
                failed_videos.append(video)
                if fail_fast:
                    break

        if failed_videos:
            print("\nRun completed with failures:")
            for video in failed_videos:
                print(f"  - {video}")
            sys.exit(1)

        print(f"\nAll done! Check {env_paths.output_dir()}/ for final videos.")


def run_with_file_logging():
    """Run pipeline while duplicating all stdout/stderr into a log file."""
    with pipeline_run_log():
        main()


if __name__ == "__main__":
    run_with_file_logging()
