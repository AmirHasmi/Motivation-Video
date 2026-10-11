# Motivation Video

`motivation_video.py` builds a short motivational video:

1. Calls the **Motion AI** API to generate a cloud video clip.
2. Picks a random instrumental track from your music folder.
3. Picks a random quote from your quotes file.
4. Uses **FFmpeg** to put the quote over the clip, add the music, and export an MP4.

It needs Python 3.10+ and `ffmpeg`/`ffprobe` on your PATH. It has no pip dependencies.

## Setup

```bash
export MOTION_API_KEY="your-key"
# Put instrumental tracks (.mp3 .wav .m4a .aac .flac .ogg .opus) in ./music/
# Edit quotes.txt: one quote per line, "Quote text — Author" puts the author on its own line
```

## Usage

```bash
python3 motivation_video.py                          # 9:16, 10 s -> output/motivation_<time>.mp4
python3 motivation_video.py -o final.mp4 --aspect-ratio 16:9 --duration 8
python3 motivation_video.py --prompt "Storm clouds parting to reveal sunlight, cinematic"
python3 motivation_video.py --clip my_clip.mp4       # skip the API, use a local clip
python3 motivation_video.py --music-dir ~/Music/instrumental --quotes ~/quotes.txt --seed 42
```

Run `python3 motivation_video.py --help` to see every option, including font, text fade, music volume and fade-out, polling, and `--keep-clip`.

## How it works

- **API:** `POST {base}/sessions` with `prompt`, `aspect_ratio`, `duration` and a `Bearer` key. The script then polls `GET {base}/sessions/{id}` until the status is completed and downloads the video URL. The base URL defaults to `https://api.motion.so/api/motion` and can be overridden with `--api-base` or `$MOTION_API_BASE`. The response parsing accepts several common field names (`job_id`/`id`, `status`/`state`, `video_url`/`output_url`/`url`/`outputs[]`). If your account's API differs, edit `MotionClient`.
- **Validation before cost:** the music folder, quotes file, and font are checked *before* a paid generation starts.
- **Text:** the quote is wrapped to the frame width, and each line is drawn centered with an outline, a shadow, and a fade-in. Text is passed through `textfile=`, so quotes, colons, `%` and the like never need escaping.
- **Audio:** the track is looped if it is shorter than the clip, faded in and out, and trimmed to the clip length.

## Tests

```bash
python3 -m unittest -v
```

The end-to-end tests run a local fake Motion AI server and FFmpeg-generated media. They cover the full API flow (create, poll, download), the failed-job and 401 paths, the local-clip mode, and validating inputs before any API call. They also check the output codecs, size, and duration.
