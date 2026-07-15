"""
Downloads a highlight mp4 and produces a blurred, trimmed, Discord-sized
version. Uses imageio-ffmpeg, which bundles a static ffmpeg binary via pip
-- no system package installs needed on Railway (deliberately avoiding the
apt/system-dependency route after this week's build failures).
"""
import os
import subprocess
import tempfile

import requests

# Trim + downscale + heavy blur, tuned to stay well under Discord's upload
# limit: ~8 seconds, 640px wide, strong boxblur so jersey numbers, names,
# and faces are unreadable while the play itself stays followable.
CLIP_SECONDS = 8
BLUR_STRENGTH = "boxblur=10:2"
SCALE = "scale=640:-2"


def _ffmpeg_path() -> str:
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def make_blurred_clip(mp4_url: str, out_path: str) -> bool:
    """Downloads the highlight and writes a blurred clip to out_path.
    Returns True on success."""
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        resp = requests.get(mp4_url, timeout=60, stream=True)
        resp.raise_for_status()
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1 << 16):
                f.write(chunk)

        cmd = [
            _ffmpeg_path(), "-y",
            "-i", tmp_path,
            "-t", str(CLIP_SECONDS),
            "-vf", f"{SCALE},{BLUR_STRENGTH}",
            "-an",  # strip audio -- announcer usually names the player!
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        return result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0
    except Exception:
        return False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
