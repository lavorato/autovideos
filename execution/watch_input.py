"""
Always-on watcher for the input/ folder.

Purpose:
    Monitor `input/` and, as soon as a new video lands and finishes copying,
    automatically run ingest only:
        - Step 00: convert_source (transcode heavy formats → .tmp/{base}.mp4)

    Then: trim in 00b → mark trim done → transcribe (step 01) → edit transcript
    if needed → mark review done → remaining pipeline:
        - 00b_editor.py (trim, then after 01: transcript fixes + gates)
        - 00b “Mark trim done” triggers transcribe in the editor by default
        - run_pipeline.py --skip 00,01 (steps 02+ after “Mark transcript review done”)

Behavior:
    - Polls `input/` every N seconds (default 3s) with plain os.scandir.
      No external deps (watchdog is not installed in this project).
    - Stable-file detection: a file must have the same (size, mtime) across
      two consecutive polls before it is considered "done copying". This
      avoids picking up half-transferred iPhone .MOVs.
    - Cache-aware: convert_source short-circuits when its output exists and matches.
      We additionally keep an in-memory
      signature set so fully-processed files aren't re-imported every loop.
    - Ignores:
        * dotfiles (.DS_Store, ._AppleDouble, etc.)
        * subdirectories that happen to live inside input/ (treated as
          B-roll asset folders, handled later by step 08c)
        * non-video extensions
    - Sequential: one file at a time. Work is serialized so two big .MOVs
      arriving together don't fight over CPU/.tmp/.
    - Graceful Ctrl+C: finishes the file in flight when possible, then exits.

Usage:
    python execution/watch_input.py                     # default: watch input/, 3s interval
    python execution/watch_input.py --interval 5        # custom poll interval
    python execution/watch_input.py --input-dir drops/  # watch a different folder
    python execution/watch_input.py --once              # scan once and exit (CI-friendly)
    python execution/watch_input.py --force             # ignore cache, reprocess everything

    # Equivalent entry point through the pipeline runner:
    python execution/run_pipeline.py --watch
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import traceback
from dataclasses import dataclass
from typing import Iterable

# Make sibling step modules importable when invoked from repo root or anywhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from dotenv import load_dotenv
    _ENV_PATH = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    load_dotenv(_ENV_PATH, override=False)
except ImportError:
    pass

import editor_gate  # noqa: E402
import env_paths  # noqa: E402

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
DEFAULT_INPUT_DIR = env_paths.input_dir()
DEFAULT_TMP_DIR = env_paths.tmp_dir()
DEFAULT_INTERVAL = 3.0
STABLE_POLLS_REQUIRED = 2  # same (size, mtime) across this many polls = ready

# ANSI color codes. The Cursor terminal honors these, and they degrade
# silently on dumb terminals. No dependency on rich/colorama.
C_RESET = "\033[0m"
C_DIM = "\033[2m"
C_BOLD = "\033[1m"
C_CYAN = "\033[36m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_MAGENTA = "\033[35m"


# ── Pretty logging ─────────────────────────────────────────────

def _ts() -> str:
    return time.strftime("%H:%M:%S")


def log(msg: str, color: str = "") -> None:
    prefix = f"{C_DIM}[{_ts()}]{C_RESET} {C_MAGENTA}watch{C_RESET}"
    if color:
        print(f"{prefix} {color}{msg}{C_RESET}", flush=True)
    else:
        print(f"{prefix} {msg}", flush=True)


def log_info(msg: str) -> None:
    log(msg)


def log_ok(msg: str) -> None:
    log(msg, C_GREEN)


def log_warn(msg: str) -> None:
    log(msg, C_YELLOW)


def log_err(msg: str) -> None:
    log(msg, C_RED)


def log_hl(msg: str) -> None:
    log(msg, C_CYAN + C_BOLD)


# ── File discovery ─────────────────────────────────────────────

@dataclass(frozen=True)
class Candidate:
    path: str
    size: int
    mtime: float

    @property
    def signature(self) -> tuple[str, int, float]:
        return (self.path, self.size, self.mtime)


def _iter_input_videos(input_dir: str) -> Iterable[Candidate]:
    """Yield video files directly inside input_dir (non-recursive).

    Subdirectories are intentionally skipped — in this project a folder like
    `input/IMG_1792/` holds B-roll assets consumed later by step 08c, not a
    video to process on its own.
    """
    try:
        entries = list(os.scandir(input_dir))
    except FileNotFoundError:
        return
    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue
        if not entry.is_file(follow_symlinks=False):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in VIDEO_EXTENSIONS:
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        yield Candidate(path=os.path.abspath(entry.path), size=st.st_size, mtime=st.st_mtime)


# ── Cache check ────────────────────────────────────────────────

def _already_prepared(video_path: str, tmp_dir: str) -> bool:
    """True if step 00 output exists for this basename (ready for manual trim)."""
    base = os.path.splitext(os.path.basename(video_path))[0]
    transcoded = os.path.join(tmp_dir, f"{base}.mp4")
    try:
        return os.path.isfile(transcoded) and os.path.getsize(transcoded) > 0
    except OSError:
        return False


def _post_step00_hint(base: str, tmp_dir: str) -> str:
    """Next-step line for logs; aligned with 00b_editor gates (not always 'trim')."""
    if not editor_gate.is_trim_complete_for_base(base, tmp_dir):
        return (
            "Ready to trim in 00b_editor — then “Mark trim done” "
            "(auto-transcribe unless EDITOR_AUTO_TRANSCRIBE=0). "
            "Or run_pipeline --skip 00,01 manually after gates."
        )
    if not editor_gate.tmp_base_has_transcript(tmp_dir, base):
        return (
            "Trim already marked done — transcribe (step 01) should run from 00b; "
            "if not, run it manually. Then edit transcript in 00b."
        )
    if not editor_gate.is_editor_review_complete_for_base(base, tmp_dir):
        return (
            "Transcript on disk — edit in 00b_editor (Transcript mode), then "
            "“Save and continue” or “Mark transcript review done” to confirm; "
            "plain “Save” only writes the file."
        )
    return (
        "Transcript review already confirmed — run remaining steps with "
        "run_pipeline.py --skip 00,01 if needed."
    )


# ── Processing ─────────────────────────────────────────────────

def _run_step_00(video_path: str, tmp_dir: str) -> str:
    """Run step 00 and return the (possibly transcoded) path downstream steps should use."""
    from importlib import import_module
    mod = import_module("00_convert_source")
    return mod.convert_source(video_path, tmp_dir=tmp_dir)


def process_candidate(cand: Candidate, tmp_dir: str) -> bool:
    """Run step 00 (convert) on a single file. Returns True on full success."""
    base = os.path.splitext(os.path.basename(cand.path))[0]
    log_hl(f"▶ Preparing {base} ({cand.size / (1024*1024):.1f} MB)")

    t0 = time.time()
    try:
        active_path = _run_step_00(cand.path, tmp_dir)
    except Exception as e:
        log_err(f"  step 00 failed for {base}: {e}")
        traceback.print_exc()
        return False
    t1 = time.time()
    if active_path != cand.path:
        log_info(f"  step 00 ok in {t1 - t0:.1f}s — source swapped to {active_path}")
    else:
        log_info(f"  step 00 ok in {t1 - t0:.1f}s — source passed through")

    log_ok(
        f"✓ {base} step 00 complete ({t1 - t0:.1f}s). {_post_step00_hint(base, tmp_dir)}"
    )
    return True


# ── Main loop ──────────────────────────────────────────────────

class Watcher:
    def __init__(
        self,
        input_dir: str,
        tmp_dir: str,
        interval: float,
        force: bool,
    ):
        self.input_dir = input_dir
        self.tmp_dir = tmp_dir
        self.interval = interval
        self.force = force

        # path -> (size, mtime) observed in the previous poll. Used to detect
        # files that have finished copying (two identical stats in a row).
        self._last_stats: dict[str, tuple[int, float]] = {}

        # Signatures of files we've already handled in this session (or that
        # were already prepared on disk before we started). We still let the
        # step functions do their own cache checks on disk, but this avoids
        # logging "queued" on every tick for finished files.
        self._done: set[tuple[str, int, float]] = set()

        self._stop_requested = False

    def request_stop(self, *_args) -> None:
        if not self._stop_requested:
            log_warn("Stop requested — finishing current file, then exiting.")
        self._stop_requested = True

    def _is_stable(self, cand: Candidate) -> bool:
        prev = self._last_stats.get(cand.path)
        self._last_stats[cand.path] = (cand.size, cand.mtime)
        if prev is None:
            return False
        return prev == (cand.size, cand.mtime)

    def _seed_done_from_disk(self, candidates: list[Candidate]) -> None:
        """On startup, mark already-prepared files as done so we don't spam logs."""
        if self.force:
            return
        for cand in candidates:
            if _already_prepared(cand.path, self.tmp_dir):
                self._done.add(cand.signature)
                base = os.path.splitext(os.path.basename(cand.path))[0]
                log_info(f"  {C_DIM}skip{C_RESET} {base} — already prepared")

    def scan_once(self) -> list[Candidate]:
        """One poll cycle: returns the list of stable, not-yet-processed files."""
        candidates = list(_iter_input_videos(self.input_dir))
        # Forget stats for files that disappeared, so coming-back files are
        # re-evaluated cleanly.
        current_paths = {c.path for c in candidates}
        for gone in [p for p in self._last_stats if p not in current_paths]:
            self._last_stats.pop(gone, None)

        ready: list[Candidate] = []
        for cand in candidates:
            if cand.signature in self._done and not self.force:
                continue
            if not self._is_stable(cand):
                continue
            # Skip if disk already has the prepared outputs (unless --force).
            if not self.force and _already_prepared(cand.path, self.tmp_dir):
                self._done.add(cand.signature)
                continue
            ready.append(cand)
        return ready

    def run(self, once: bool = False) -> int:
        if not os.path.isdir(self.input_dir):
            log_err(f"Input dir does not exist: {self.input_dir}")
            return 2

        log_hl(f"Watching {os.path.abspath(self.input_dir)} (interval {self.interval:.1f}s)")
        log_info(
            "Will run step 00 (convert_source) on each new video, then trim → transcribe → review via 00b + run_pipeline."
        )
        if self.force:
            log_warn("--force active: cache will be ignored.")

        # Seed state with whatever is already in the folder so existing,
        # fully-prepared files don't get re-announced.
        initial = list(_iter_input_videos(self.input_dir))
        self._seed_done_from_disk(initial)

        log_info(
            f"Currently {len(initial)} file(s) in {DEFAULT_INPUT_DIR}/. "
            f"Waiting for changes…"
        )

        processed_any = False
        while not self._stop_requested:
            try:
                ready = self.scan_once()
            except Exception as e:
                log_err(f"Scan error: {e}")
                traceback.print_exc()
                ready = []

            for cand in ready:
                if self._stop_requested:
                    break
                ok = process_candidate(cand, self.tmp_dir)
                self._done.add(cand.signature)
                processed_any = processed_any or ok

            if once:
                # In --once mode the first scan can't establish stability
                # (needs two observations). Do a short second pass so users
                # running one-shot / CI invocations actually process files.
                if not ready and not processed_any:
                    time.sleep(min(1.5, self.interval))
                    ready = self.scan_once()
                    for cand in ready:
                        if self._stop_requested:
                            break
                        ok = process_candidate(cand, self.tmp_dir)
                        self._done.add(cand.signature)
                        processed_any = processed_any or ok
                if not processed_any:
                    log_info("No new files to process (--once).")
                return 0

            # Short, interruptible sleep so Ctrl+C is snappy.
            slept = 0.0
            while slept < self.interval and not self._stop_requested:
                time.sleep(min(0.2, self.interval - slept))
                slept += 0.2

        log_info("Watcher stopped.")
        return 0


# ── CLI ────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Always-on watcher: auto-runs step 00 + 01 on new videos in input/.",
    )
    p.add_argument("--input-dir", default=DEFAULT_INPUT_DIR,
                   help=f"Folder to watch (default: {DEFAULT_INPUT_DIR}).")
    p.add_argument("--tmp-dir", default=DEFAULT_TMP_DIR,
                   help=f"Intermediate output folder (default: {DEFAULT_TMP_DIR}).")
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL,
                   help=f"Poll interval in seconds (default: {DEFAULT_INTERVAL}).")
    p.add_argument("--once", action="store_true",
                   help="Scan once, process any stable new files, then exit.")
    p.add_argument("--force", action="store_true",
                   help="Ignore on-disk cache — reprocess every file found.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    watcher = Watcher(
        input_dir=args.input_dir,
        tmp_dir=args.tmp_dir,
        interval=max(0.2, float(args.interval)),
        force=bool(args.force),
    )
    signal.signal(signal.SIGINT, watcher.request_stop)
    signal.signal(signal.SIGTERM, watcher.request_stop)

    return watcher.run(once=bool(args.once))


if __name__ == "__main__":
    sys.exit(main())
