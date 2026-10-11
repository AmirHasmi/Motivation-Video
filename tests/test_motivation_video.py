"""Tests for motivation_video.py. Run with: python3 -m unittest -v"""

import http.server
import json
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import motivation_video as mv  # noqa: E402

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def make_clip(path: Path, seconds: int = 3, size: str = "360x640") -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc=size={size}:rate=24:duration={seconds}",
                    "-pix_fmt", "yuv420p", str(path)], check=True)
    return path


def make_tone(path: Path, seconds: int = 1) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"sine=frequency=440:duration={seconds}", str(path)], check=True)
    return path


class PickTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)

    def test_load_quotes_skips_blank_and_comment_lines(self):
        f = self.tmp / "q.txt"
        f.write_text("\ufeff# header\n\nFirst quote\n   \n  # also comment\nSecond: \"quoted\" 100%\n")
        self.assertEqual(mv.load_quotes(f), ["First quote", 'Second: "quoted" 100%'])

    def test_empty_quotes_file_errors(self):
        f = self.tmp / "q.txt"
        f.write_text("# nothing\n\n")
        with self.assertRaisesRegex(mv.PipelineError, "No quotes"):
            mv.load_quotes(f)

    def test_missing_quotes_file_errors(self):
        with self.assertRaisesRegex(mv.PipelineError, "not found"):
            mv.load_quotes(self.tmp / "nope.txt")

    def test_pick_music_only_audio_files_recursively(self):
        (self.tmp / "sub").mkdir()
        for name in ("a.mp3", "sub/b.WAV", "cover.jpg", "notes.txt"):
            (self.tmp / name).write_bytes(b"x")
        picks = {mv.pick_music(self.tmp, random.Random(i)).name for i in range(50)}
        self.assertEqual(picks, {"a.mp3", "b.WAV"})

    def test_pick_music_empty_folder_errors(self):
        with self.assertRaisesRegex(mv.PipelineError, "No audio files"):
            mv.pick_music(self.tmp, random.Random())

    def test_seed_makes_picks_reproducible(self):
        f = self.tmp / "q.txt"
        f.write_text("\n".join(f"quote {i}" for i in range(20)))
        self.assertEqual(mv.pick_quote(f, random.Random(7)), mv.pick_quote(f, random.Random(7)))


class TextTests(unittest.TestCase):
    def test_wrap_respects_frame_width(self):
        lines = mv.wrap_quote("word " * 40, width=720, font_size=60)
        max_chars = int(720 * 0.85 / (60 * 0.55))
        self.assertGreater(len(lines), 1)
        self.assertTrue(all(len(line) <= max_chars for line in lines))

    def test_author_gets_own_line(self):
        lines = mv.wrap_quote("Keep going. — Sam Levenson", width=1080, font_size=70)
        self.assertEqual(lines, ["Keep going.", "— Sam Levenson"])

    def test_filter_escapes_paths_and_has_one_drawtext_per_line(self):
        info = mv.VideoInfo(1080, 1920, 10.0)
        graph = mv.build_filter(["a", "b"], [Path("C:\\tmp\\l0.txt"), Path("/t/it's.txt")],
                                "/f/font.ttf", 70, info, 1.0, 0.8, 2.0)
        self.assertEqual(graph.count("drawtext="), 2)
        self.assertIn("textfile='C\\:/tmp/l0.txt'", graph)
        self.assertIn("it'\\''s.txt", graph)
        self.assertIn("volume=0.8", graph)
        self.assertIn("afade=t=out:st=8.000:d=2.0", graph)


class JobParsingTests(unittest.TestCase):
    def test_video_url_shapes(self):
        cases = [
            {"status": "completed", "video_url": "https://x/a.mp4"},
            {"data": {"status": "completed", "output_url": "https://x/a.mp4"}},
            {"result": {"url": "https://x/a.mp4"}},
            {"outputs": [{"url": "https://x/a.mp4"}]},
            {"outputs": ["https://x/a.mp4"]},
        ]
        for job in cases:
            with self.subTest(job=job):
                self.assertEqual(mv.job_video_url(job), "https://x/a.mp4")
        self.assertIsNone(mv.job_video_url({"status": "processing"}))

    def test_missing_api_key(self):
        with self.assertRaisesRegex(mv.PipelineError, "MOTION_API_KEY"):
            mv.MotionClient("")


class FakeMotionAPI(http.server.BaseHTTPRequestHandler):
    """Fake API: job is 'processing' on the first poll, 'completed' on the second."""

    clip_bytes = b""
    polls = 0
    fail = False
    requests: list = []

    def log_message(self, *a):
        pass

    def _json(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(("POST", self.path, self.headers.get("Authorization"), body))
        if self.headers.get("Authorization") != "Bearer test-key":
            return self._json(401, {"error": "unauthorized"})
        self._json(200, {"job_id": "job123", "status": "queued"})

    def do_GET(self):
        cls = type(self)
        cls.requests.append(("GET", self.path, self.headers.get("Authorization"), None))
        if self.path == "/api/sessions/job123":
            cls.polls += 1
            if cls.fail:
                return self._json(200, {"status": "failed", "error": "content policy"})
            if cls.polls < 2:
                return self._json(200, {"status": "processing"})
            host = self.headers["Host"]
            return self._json(200, {"status": "completed", "video_url": f"http://{host}/files/clip.mp4"})
        if self.path == "/files/clip.mp4":
            self.send_response(200)
            self.send_header("Content-Length", str(len(cls.clip_bytes)))
            self.end_headers()
            self.wfile.write(cls.clip_bytes)
            return
        self._json(404, {"error": "not found"})


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe not installed")
class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.music = self.tmp / "music"
        self.music.mkdir()
        make_tone(self.music / "track.mp3", seconds=1)  # shorter than clip -> must loop
        self.quotes = self.tmp / "quotes.txt"
        self.quotes.write_text("Don't stop: it's 100% \"worth\" it, {really} [truly] \\ trust me; keep going\n")
        self.clip = make_clip(self.tmp / "clip.mp4", seconds=3)

        FakeMotionAPI.clip_bytes = self.clip.read_bytes()
        FakeMotionAPI.polls = 0
        FakeMotionAPI.fail = False
        FakeMotionAPI.requests = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeMotionAPI)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.api_base = f"http://127.0.0.1:{self.server.server_port}/api"

    def assert_valid_output(self, path: Path):
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                              "stream=codec_type,codec_name,width,height:format=duration",
                              "-of", "json", str(path)], capture_output=True, text=True, check=True)
        data = json.loads(out.stdout)
        kinds = {s["codec_type"]: s for s in data["streams"]}
        self.assertEqual(kinds["video"]["codec_name"], "h264")
        self.assertEqual((kinds["video"]["width"], kinds["video"]["height"]), (360, 640))
        self.assertEqual(kinds["audio"]["codec_name"], "aac")
        self.assertAlmostEqual(float(data["format"]["duration"]), 3.0, delta=0.15)

    def test_full_pipeline_with_fake_api(self):
        output = self.tmp / "out" / "final.mp4"
        kept = self.tmp / "raw.mp4"
        rc = mv.main(["--music-dir", str(self.music), "--quotes", str(self.quotes),
                      "-o", str(output), "--keep-clip", str(kept),
                      "--api-key", "test-key", "--api-base", self.api_base,
                      "--poll-interval", "0.05", "--seed", "1"])
        self.assertEqual(rc, 0)
        self.assert_valid_output(output)
        self.assertTrue(kept.is_file())

        post = FakeMotionAPI.requests[0]
        self.assertEqual(post[:3], ("POST", "/api/sessions", "Bearer test-key"))
        self.assertEqual(post[3]["aspect_ratio"], "9:16")
        self.assertEqual(FakeMotionAPI.polls, 2)
        # The API key must not leak to the (non-API) download host path.
        download = [r for r in FakeMotionAPI.requests if r[1] == "/files/clip.mp4"][0]
        self.assertIsNone(download[2])

    def test_local_clip_skips_api(self):
        output = self.tmp / "final.mp4"
        rc = mv.main(["--music-dir", str(self.music), "--quotes", str(self.quotes),
                      "-o", str(output), "--clip", str(self.clip)])
        self.assertEqual(rc, 0)
        self.assert_valid_output(output)
        self.assertEqual(FakeMotionAPI.requests, [])

    def test_failed_job_returns_error(self):
        FakeMotionAPI.fail = True
        output = self.tmp / "final.mp4"
        with self.assertLogs("motivation_video", "ERROR") as logs:
            rc = mv.main(["--music-dir", str(self.music), "--quotes", str(self.quotes),
                          "-o", str(output), "--api-key", "test-key", "--api-base", self.api_base,
                          "--poll-interval", "0.05"])
        self.assertEqual(rc, 1)
        self.assertIn("content policy", "\n".join(logs.output))
        self.assertFalse(output.exists())

    def test_bad_api_key_returns_error(self):
        with self.assertLogs("motivation_video", "ERROR") as logs:
            rc = mv.main(["--music-dir", str(self.music), "--quotes", str(self.quotes),
                          "-o", str(self.tmp / "f.mp4"), "--api-key", "wrong",
                          "--api-base", self.api_base])
        self.assertEqual(rc, 1)
        self.assertIn("HTTP 401", "\n".join(logs.output))

    def test_inputs_validated_before_api_call(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertLogs("motivation_video", "ERROR"):
            rc = mv.main(["--music-dir", str(empty), "--quotes", str(self.quotes),
                          "--api-key", "test-key", "--api-base", self.api_base])
        self.assertEqual(rc, 1)
        self.assertEqual(FakeMotionAPI.requests, [])


if __name__ == "__main__":
    unittest.main()
