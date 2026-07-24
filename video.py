"""
Downloads a highlight mp4 and produces a blurred, trimmed, Discord-sized
version. Uses imageio-ffmpeg, which bundles a static ffmpeg binary via pip
-- no system package installs needed on Railway (deliberately avoiding the
apt/system-dependency route after this week's build failures).

July 18 rework:
  - Slice from INSIDE the clip, not the first 8 seconds. MLB highlight
    clips open with broadcast lead-in (pitcher walking around, crowd
    shots, sponsor bumpers), so a first-8-seconds cut often ends before
    the play even starts. We probe the duration and start the cut
    START_FRAC of the way in (default 30%), which lands on the action.
  - Lighter blur on a bigger frame. boxblur=10:2 at 640px made the play
    unfollowable; 854px with boxblur=6:2 keeps names/numbers unreadable
    while the ball and motion stay trackable.
  - Both are Railway-tunable via env vars (no code push to adjust):
      GUESS_CLIP_SECONDS   (default 8)
      GUESS_START_FRAC     (default 0.30 -- fraction of clip to skip)
      GUESS_BLUR           (default boxblur=6:2)
      GUESS_SCALE_WIDTH    (default 854)
"""
import os
import re
import logging
import subprocess
import tempfile
import requests

log = logging.getLogger("guess.video")

CLIP_SECONDS = float(os.getenv("GUESS_CLIP_SECONDS", "8"))
START_FRAC = float(os.getenv("GUESS_START_FRAC", "0.30"))
BLUR_STRENGTH = os.getenv("GUESS_BLUR", "boxblur=6:2")
SCALE_WIDTH = int(os.getenv("GUESS_SCALE_WIDTH", "854"))

_DURATION_RE = re.compile(rb"Duration:\s*(\d+):(\d+):(\d+)\.(\d+)")


def _ffmpeg_path() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _probe_duration_seconds(ffmpeg: str, path: str) -> float | None:
    """imageio-ffmpeg bundles ffmpeg but not ffprobe, so read the duration
    from ffmpeg's own stderr banner ('Duration: 00:00:29.97, ...')."""
    try:
        result = subprocess.run([ffmpeg, "-i", path], capture_output=True, timeout=30)
        m = _DURATION_RE.search(result.stderr or b"")
        if not m:
            return None
        h, mnt, s, frac = m.groups()
        return int(h) * 3600 + int(mnt) * 60 + int(s) + float(f"0.{frac.decode()}")
    except Exception:
        return None


def make_blurred_clip(mp4_url: str, out_path: str, start_frac: float | None = None) -> bool:
    """Downloads the highlight and writes a blurred clip to out_path.
    Returns True on success. start_frac overrides GUESS_START_FRAC for
    this one clip -- single-play Film Room clips start AT the action, so
    the caller passes ~0.05 for those instead of the skip-the-intro
    default tuned for highlight packages."""
    if start_frac is None:
        start_frac = START_FRAC
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        resp = requests.get(mp4_url, timeout=60, stream=True)
        resp.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)

        ffmpeg = _ffmpeg_path()

        # Start the cut partway into the clip to skip broadcast lead-in,
        # clamped so the slice always fits inside the video.
        start_seconds = 0.0
        duration = _probe_duration_seconds(ffmpeg, tmp_path)
        if duration and duration > CLIP_SECONDS:
            start_seconds = min(duration * start_frac, duration - CLIP_SECONDS)

        dl_size = os.path.getsize(tmp_path) if os.path.exists(tmp_path) else 0
        if dl_size < 1024:
            log.error("clip download too small (%d bytes) from %s -- source gave no "
                      "real video (likely a play with no Film Room clip yet)", dl_size, mp4_url)
            return False

        # -ss AFTER -i (accurate seek): with the fast seek before -i, a clip
        # SHORTER than our computed start lands past EOF and ffmpeg writes an
        # empty file -- exactly what short single-play K clips do. Accurate
        # seek is slightly slower but never overruns.
        cmd = [
            ffmpeg, "-y",
            "-i", tmp_path,
            "-ss", f"{start_seconds:.2f}",
            "-t", str(CLIP_SECONDS),
            "-vf", f"scale={SCALE_WIDTH}:-2,{BLUR_STRENGTH}",
            "-an",  # strip audio -- announcer usually names the player!
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-pix_fmt", "yuv420p",  # some MLB sources are yuv422p -> unplayable in Discord
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        out_size = os.path.getsize(out_path) if os.path.exists(out_path) else 0
        if result.returncode != 0 or out_size == 0:
            tail = (result.stderr or b"").decode("utf-8", "replace")[-600:]
            log.error("ffmpeg failed (rc=%s, out=%dB, dur=%s, start=%.2f, in=%dB) for %s\n%s",
                      result.returncode, out_size, duration, start_seconds, dl_size, mp4_url, tail)
            return False
        return True
    except Exception as e:
        log.error("clip processing exception for %s: %s", mp4_url, e, exc_info=True)
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
