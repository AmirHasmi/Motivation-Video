#!/usr/bin/env python3
"""Generate a motivational video.

Pipeline:
  1. Ask the Mootion API to generate a cloud video clip (or use --clip).
  2. Pick a random instrumental track from a music folder.
  3. Pick a random quote from a quotes file.
  4. Use FFmpeg to overlay the quote, add the music, and export the result.

Only the Python standard library is required, plus ffmpeg/ffprobe on PATH.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("motivation_video")

AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
DEFAULT_API_BASE = "https://api.seedv.cn"
VIDEO_SIZES = ("1080x1920", "1920x1080", "1080x1080")
DEFAULT_PROMPT = (
    "Cinematic time-lapse of soft white clouds drifting across a golden "
    "sunrise sky, slow camera push-in, peaceful and inspiring, no text"
)
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]

# Mootion TaskStatus: created, pending, processing, success, failure, cancelled.
FAILED_STATUSES = {"failure", "cancelled"}


class PipelineError(RuntimeError):
    """Any failure that should stop the pipeline with a readable message."""


# --------------------------------------------------------------------------- #
# Mootion API client (spec: docs/mootion-api.json)
# --------------------------------------------------------------------------- #


class MootionClient:
    """Minimal client for the Mootion text-to-video API.

    Flow: POST {base}/api/v1/text2video -> task id, poll
    GET {base}/api/v1/text2video/{id} until status is "success", then
    download data.video_url. Every response is {code, message, data}.
    """

    def __init__(self, api_key: str, base_url: str = DEFAULT_API_BASE, timeout: float = 60):
        if not api_key:
            raise PipelineError("Mootion API key missing: set MOOTION_API_KEY or pass --api-key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise PipelineError(f"Mootion {method} {path} failed: HTTP {e.code}: {_error_detail(e)}") from e
        except urllib.error.URLError as e:
            raise PipelineError(f"Mootion {method} {path} failed: {e.reason}") from e
        task = payload.get("data")
        if not isinstance(task, dict):
            raise PipelineError(
                f"Mootion {method} {path}: code={payload.get('code')} "
                f"message={payload.get('message')!r}, no task data"
            )
        return task

    def create_task(self, prompt: str, size: str, style: str, language: str) -> str:
        task = self._request("POST", "/api/v1/text2video", {
            "size": size,
            "storyboard": {"style": style, "user_input": {"prompt": prompt, "language": language}},
        })
        if not task.get("id"):
            raise PipelineError(f"Mootion did not return a task id: {task}")
        return str(task["id"])

    def get_task(self, task_id: str) -> dict:
        return self._request("GET", f"/api/v1/text2video/{task_id}")

    def wait_for_video_url(self, task_id: str, poll_interval: float = 10, max_wait: float = 1800) -> str:
        deadline = time.monotonic() + max_wait
        while True:
            task = self.get_task(task_id)
            status = task.get("status")
            log.info("Mootion task %s: %s%s", task_id, status, _progress_note(task))
            if status in FAILED_STATUSES:
                raise PipelineError(f"Mootion task {task_id} {status}: {task.get('failure_reason')}")
            if status == "success":
                if not task.get("video_url"):
                    raise PipelineError(f"Mootion task {task_id} succeeded without a video_url: {task}")
                return task["video_url"]
            if time.monotonic() >= deadline:
                raise PipelineError(f"Timed out after {max_wait:.0f}s waiting for Mootion task {task_id}")
            time.sleep(poll_interval)

    def download(self, url: str, dest: Path) -> Path:
        # video_url is normally a storage link; only send the key to the API host itself.
        headers = {"Authorization": f"Bearer {self.api_key}"} if url.startswith(self.base_url + "/") else {}
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp, open(dest, "wb") as f:
                shutil.copyfileobj(resp, f)
        except urllib.error.URLError as e:
            raise PipelineError(f"Downloading generated clip failed: {e}") from e
        return dest

    def generate_clip(self, prompt: str, size: str, style: str, language: str, dest: Path,
                      poll_interval: float = 10, max_wait: float = 1800) -> Path:
        task_id = self.create_task(prompt, size, style, language)
        log.info("Mootion task created: %s", task_id)
        url = self.wait_for_video_url(task_id, poll_interval, max_wait)
        log.info("Downloading generated clip")
        return self.download(url, dest)


def _error_detail(e: urllib.error.HTTPError) -> str:
    """Readable message from an error body, including FastAPI 422 validation errors."""
    raw = e.read().decode(errors="replace")
    try:
        body = json.loads(raw)
    except ValueError:
        return raw[:500]
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, list):
        return "; ".join(
            f"{'.'.join(str(p) for p in d.get('loc', []))}: {d.get('msg')}" for d in detail if isinstance(d, dict)
        )
    return str(body.get("message") or detail or body)[:500] if isinstance(body, dict) else raw[:500]


def _progress_note(task: dict) -> str:
    parts = []
    progress = task.get("progress")
    if isinstance(progress, dict) and progress.get("total"):
        parts.append(f"step {progress.get('current')}/{progress['total']}")
    if task.get("estimated_remaining_time") is not None:
        parts.append(f"~{task['estimated_remaining_time']}s left")
    return f" ({', '.join(parts)})" if parts else ""


# --------------------------------------------------------------------------- #
# Random picks
# --------------------------------------------------------------------------- #


def pick_music(music_dir: Path, rng: random.Random) -> Path:
    if not music_dir.is_dir():
        raise PipelineError(f"Music folder not found: {music_dir}")
    tracks = sorted(p for p in music_dir.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS)
    if not tracks:
        raise PipelineError(f"No audio files ({', '.join(sorted(AUDIO_EXTENSIONS))}) in {music_dir}")
    return rng.choice(tracks)


def load_quotes(quotes_file: Path) -> list[str]:
    """One quote per line. Blank lines and lines starting with '#' are ignored."""
    if not quotes_file.is_file():
        raise PipelineError(f"Quotes file not found: {quotes_file}")
    quotes = [
        line.strip()
        for line in quotes_file.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if not quotes:
        raise PipelineError(f"No quotes in {quotes_file}")
    return quotes


def pick_quote(quotes_file: Path, rng: random.Random) -> str:
    return rng.choice(load_quotes(quotes_file))


# --------------------------------------------------------------------------- #
# FFmpeg
# --------------------------------------------------------------------------- #


@dataclass
class VideoInfo:
    width: int
    height: int
    duration: float


def probe_video(path: Path) -> VideoInfo:
    out = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height:format=duration",
        "-of", "json", str(path),
    ])
    data = json.loads(out)
    try:
        stream = data["streams"][0]
        return VideoInfo(int(stream["width"]), int(stream["height"]), float(data["format"]["duration"]))
    except (KeyError, IndexError, ValueError) as e:
        raise PipelineError(f"Could not read video info from {path}: {data}") from e


def find_font(explicit: str | None) -> str:
    if explicit:
        if not Path(explicit).is_file():
            raise PipelineError(f"Font file not found: {explicit}")
        return explicit
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    if shutil.which("fc-match"):
        found = subprocess.run(["fc-match", "-f", "%{file}", "sans:bold"], capture_output=True, text=True).stdout
        if found and Path(found).is_file():
            return found
    raise PipelineError("No usable font found; pass --font /path/to/font.ttf")


def wrap_quote(quote: str, width: int, font_size: int) -> list[str]:
    """Wrap so each line fits ~85% of the frame width (bold sans ~0.55em per char)."""
    max_chars = max(10, int(width * 0.85 / (font_size * 0.55)))
    # "Quote text — Author" puts the author on a line of its own.
    body, sep, author = quote.rpartition(" — ")
    if not sep:
        body, author = quote, ""
    lines = textwrap.wrap(body, width=max_chars, break_long_words=True) or [body]
    if author:
        lines += textwrap.wrap(f"— {author}", width=max_chars, break_long_words=True)
    return lines


def _escape_filter_path(path: Path | str) -> str:
    """Escape a path for use as a filter option value inside single quotes."""
    return str(path).replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")


def build_filter(lines: list[str], line_files: list[Path], font: str, font_size: int,
                 info: VideoInfo, fade_in: float, music_volume: float, music_fade_out: float) -> str:
    """Video: darken slightly, then one centered drawtext per line. Audio: fades."""
    line_gap = int(font_size * 0.35)
    block_h = len(lines) * font_size + (len(lines) - 1) * line_gap
    alpha = f"if(lt(t,{fade_in}),t/{fade_in},1)" if fade_in > 0 else "1"
    border = max(2, font_size // 14)

    video = "[0:v]eq=brightness=-0.06:saturation=1.05"
    for i, path in enumerate(line_files):
        y = f"(h-{block_h})/2+{i * (font_size + line_gap)}"
        video += (
            f",drawtext=fontfile='{_escape_filter_path(font)}'"
            f":textfile='{_escape_filter_path(path)}'"
            f":fontsize={font_size}:fontcolor=white:borderw={border}:bordercolor=black@0.7"
            f":shadowx=2:shadowy=2:shadowcolor=black@0.5"
            f":x=(w-text_w)/2:y={y}:alpha='{alpha}'"
        )
    video += ",format=yuv420p[v]"

    fade_start = max(0.0, info.duration - music_fade_out)
    audio = f"[1:a]volume={music_volume},afade=t=in:d=0.5,afade=t=out:st={fade_start:.3f}:d={music_fade_out}[a]"
    return f"{video};{audio}"


def compose_video(clip: Path, music: Path, quote: str, output: Path, font: str,
                  font_scale: float = 1.0, fade_in: float = 1.0, music_volume: float = 1.0,
                  music_fade_out: float = 2.0) -> Path:
    info = probe_video(clip)
    font_size = max(16, int(min(info.width, info.height) * 0.065 * font_scale))
    lines = wrap_quote(quote, info.width, font_size)
    log.info("Clip %dx%d %.1fs; quote in %d line(s) at %dpx", info.width, info.height,
             info.duration, len(lines), font_size)

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="motivation_") as tmp:
        # textfile= sidesteps drawtext's escaping rules for quotes, colons, %, etc.
        line_files = []
        for i, line in enumerate(lines):
            p = Path(tmp) / f"line{i}.txt"
            p.write_text(line, encoding="utf-8")
            line_files.append(p)

        filter_graph = build_filter(lines, line_files, font, font_size, info, fade_in,
                                    music_volume, music_fade_out)

        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(clip),
            "-stream_loop", "-1", "-i", str(music),  # loop music if shorter than the clip
            "-filter_complex", filter_graph,
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "20",
            "-c:a", "aac", "-b:a", "192k",
            "-t", f"{info.duration:.3f}",
            "-movflags", "+faststart",
            str(output),
        ]
        log.debug("FFmpeg command: %s", cmd)
        _run(cmd)
    return output


def _run(cmd: list[str]) -> str:
    if not shutil.which(cmd[0]):
        raise PipelineError(f"{cmd[0]} not found on PATH")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise PipelineError(f"{cmd[0]} failed ({proc.returncode}):\n{proc.stderr.strip()[-2000:]}")
    return proc.stdout


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--music-dir", type=Path, default=Path(os.getenv("MUSIC_DIR", "music")),
                   help="folder of instrumental tracks (default: ./music or $MUSIC_DIR)")
    p.add_argument("--quotes", type=Path, default=Path(os.getenv("QUOTES_FILE", "quotes.txt")),
                   help="quotes file, one per line (default: ./quotes.txt or $QUOTES_FILE)")
    p.add_argument("-o", "--output", type=Path,
                   help="output MP4 (default: output/motivation_<timestamp>.mp4)")
    p.add_argument("--clip", type=Path,
                   help="use this local clip instead of calling Mootion")
    p.add_argument("--keep-clip", type=Path,
                   help="also save the raw Mootion clip here")

    api = p.add_argument_group("Mootion API")
    api.add_argument("--api-key", default=os.getenv("MOOTION_API_KEY"), help="default: $MOOTION_API_KEY")
    api.add_argument("--api-base", default=os.getenv("MOOTION_API_BASE", DEFAULT_API_BASE),
                     help=f"default: $MOOTION_API_BASE or {DEFAULT_API_BASE}")
    api.add_argument("--prompt", default=os.getenv("MOOTION_PROMPT", DEFAULT_PROMPT))
    api.add_argument("--size", choices=VIDEO_SIZES, default="1080x1920",
                     help="1080x1920 (Shorts/Reels, default), 1920x1080, 1080x1080")
    api.add_argument("--video-style", default="photorealistic", help="Mootion storyboard style")
    api.add_argument("--language", default="en-US")
    api.add_argument("--poll-interval", type=float, default=10)
    api.add_argument("--max-wait", type=float, default=1800, help="seconds to wait for generation")

    style = p.add_argument_group("Style")
    style.add_argument("--font", help="TTF/OTF font file (default: auto-detect a bold sans)")
    style.add_argument("--font-scale", type=float, default=1.0)
    style.add_argument("--text-fade-in", type=float, default=1.0, help="seconds; 0 disables")
    style.add_argument("--music-volume", type=float, default=1.0)
    style.add_argument("--music-fade-out", type=float, default=2.0)

    p.add_argument("--seed", type=int, help="seed the random picks for reproducible runs")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    rng = random.Random(args.seed)
    output = args.output or Path("output") / f"motivation_{time.strftime('%Y%m%d_%H%M%S')}.mp4"

    try:
        # Validate the cheap local inputs before paying for a generation.
        music = pick_music(args.music_dir, rng)
        quote = pick_quote(args.quotes, rng)
        font = find_font(args.font)
        log.info("Music: %s", music)
        log.info("Quote: %s", quote)

        with tempfile.TemporaryDirectory(prefix="motion_clip_") as tmp:
            if args.clip:
                if not args.clip.is_file():
                    raise PipelineError(f"Clip not found: {args.clip}")
                clip = args.clip
            else:
                client = MootionClient(args.api_key, args.api_base)
                clip = client.generate_clip(args.prompt, args.size, args.video_style, args.language,
                                            Path(tmp) / "clip.mp4", args.poll_interval, args.max_wait)
                if args.keep_clip:
                    args.keep_clip.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(clip, args.keep_clip)

            compose_video(clip, music, quote, output, font, args.font_scale,
                          args.text_fade_in, args.music_volume, args.music_fade_out)
    except PipelineError as e:
        log.error("%s", e)
        return 1

    log.info("Done: %s", output)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
