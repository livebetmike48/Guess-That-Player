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
    ffmpeg = _ffmpeg_path()
    # MLB serves two very different things behind ".mp4":
    #   - real highlight MP4s (batter/HR path) -> python download works
    #   - fastball-clips.mlb.com URLs (Film Room K path) which are HLS /
    #     range-gated segments -- a plain GET returns an invalid partial
    #     blob ("moov atom not found"). ffmpeg reading the URL DIRECTLY
    #     handles the streaming protocol and CDN headers natively.
    # So: let ffmpeg fetch the URL itself. Fall back to a local download
    # only if the direct read fails (some sources 403 hotlinked ffmpeg).
    UA = ("Mozilla/5.0 (compatible; GuessBot/1.0)")

    def _run(input_arg: str, start_seconds: float, headers: bool):
        cmd = [ffmpeg, "-y"]
        if headers:
            cmd += ["-user_agent", UA]
        cmd += [
            "-i", input_arg,
            "-ss", f"{start_seconds:.2f}",
            "-t", str(CLIP_SECONDS),
            "-vf", f"scale={SCALE_WIDTH}:-2,{BLUR_STRENGTH}",
            "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-pix_fmt", "yuv420p",
            out_path,
        ]
        return subprocess.run(cmd, capture_output=True, timeout=180)

    def _duration_of(input_arg: str, headers: bool):
        try:
            probe = [ffmpeg]
            if headers:
                probe += ["-user_agent", UA]
            probe += ["-i", input_arg]
            r = subprocess.run(probe, capture_output=True, timeout=60)
            m = _DURATION_RE.search(r.stderr or b"")
            if not m:
                return None
            h, mnt, s, frac = m.groups()
            return int(h)*3600 + int(mnt)*60 + int(s) + float(f"0.{frac.decode()}")
        except Exception:
            return None

    tmp_path = None
    try:
        # 1) direct-from-URL (works for HLS/fastball-clips AND plain mp4)
        duration = _duration_of(mp4_url, headers=True)
        start_seconds = 0.0
        if duration and duration > CLIP_SECONDS:
            start_seconds = min(duration * start_frac, duration - CLIP_SECONDS)
        result = _run(mp4_url, start_seconds, headers=True)
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True
        direct_tail = (result.stderr or b"").decode("utf-8", "replace")[-400:]

        # 2) fallback: download then read locally (some plain mp4s only)
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp_path = tmp.name
        resp = requests.get(mp4_url, timeout=60, stream=True,
                            headers={"User-Agent": UA})
        resp.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)
        dl_size = os.path.getsize(tmp_path)
        if dl_size < 1024:
            log.error("clip source gave no real video (%dB) and direct read failed for %s",
                      dl_size, mp4_url)
            return False
        duration = _duration_of(tmp_path, headers=False)
        start_seconds = 0.0
        if duration and duration > CLIP_SECONDS:
            start_seconds = min(duration * start_frac, duration - CLIP_SECONDS)
        result = _run(tmp_path, start_seconds, headers=False)
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return True
        tail = (result.stderr or b"").decode("utf-8", "replace")[-400:]
        log.error("clip FAILED both ways for %s\n  direct: %s\n  download(%dB): %s",
                  mp4_url, direct_tail, dl_size, tail)
        return False
    except Exception as e:
        log.error("clip processing exception for %s: %s", mp4_url, e, exc_info=True)
        return False
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
