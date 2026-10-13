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


class ClientTests(unittest.TestCase):
    def test_missing_api_key(self):
        with self.assertRaisesRegex(mv.PipelineError, "MOOTION_API_KEY"):
            mv.MootionClient("")

    def test_progress_note(self):
        task = {"progress": {"current": 2, "total": 5}, "estimated_remaining_time": 40}
        self.assertEqual(mv._progress_note(task), " (step 2/5, ~40s left)")
        self.assertEqual(mv._progress_note({"progress": None, "estimated_remaining_time": None}), "")


TASK_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"


def task_data(status, video_url=None, failure_reason=None, current=0):
    """A Text2VideoDataSchema object as defined in docs/mootion-api.json."""
    return {
        "id": TASK_ID, "create_time": "2026-10-13T10:00:00Z", "status": status, "type": "text2video",
        "progress": {"current": current, "total": 3}, "failure_reason": failure_reason,
        "estimated_remaining_time": 30, "elapsed_total_time": 5, "video_url": video_url,
    }


class FakeMootionAPI(http.server.BaseHTTPRequestHandler):
    """Fake Mootion API: 'processing' on the first poll, then 'success' (or 'failure')."""

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

    def _authorized(self):
        if self.headers.get("Authorization") != "Bearer test-key":
            self._json(401, {"detail": "Invalid token"})
            return False
        return True

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append(("POST", self.path, self.headers.get("Authorization"), body))
        if self.path != "/api/v1/text2video" or not self._authorized():
            return None if self.path == "/api/v1/text2video" else self._json(404, {"detail": "Not Found"})
        if "storyboard" not in body or "prompt" not in body["storyboard"].get("user_input", {}):
            return self._json(422, {"detail": [{"loc": ["body", "storyboard"], "msg": "Field required",
                                                "type": "missing"}]})
        if body.get("size") not in mv.VIDEO_SIZES:
            return self._json(422, {"detail": [{"loc": ["body", "size"], "msg": "Input should be valid",
                                                "type": "enum"}]})
        self._json(200, {"code": 0, "message": "ok", "data": task_data("created")})

    def do_GET(self):
        cls = type(self)
        cls.requests.append(("GET", self.path, self.headers.get("Authorization"), None))
        if self.path == f"/api/v1/text2video/{TASK_ID}":
            if not self._authorized():
                return None
            cls.polls += 1
            if cls.fail:
                return self._json(200, {"code": 0, "message": "ok",
                                        "data": task_data("failure", failure_reason={"msg": "content policy"})})
            if cls.polls < 2:
                return self._json(200, {"code": 0, "message": "ok", "data": task_data("processing", current=1)})
            # Different hostname from the API base (127.0.0.1), like a real storage/CDN link.
            url = f"http://localhost:{self.server.server_port}/files/clip.mp4"
            return self._json(200, {"code": 0, "message": "ok", "data": task_data("success", video_url=url, current=3)})
        if self.path == "/files/clip.mp4":
            self.send_response(200)
            self.send_header("Content-Length", str(len(cls.clip_bytes)))
            self.end_headers()
            self.wfile.write(cls.clip_bytes)
            return None
        self._json(404, {"detail": "Not Found"})


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

        FakeMootionAPI.clip_bytes = self.clip.read_bytes()
        FakeMootionAPI.polls = 0
        FakeMootionAPI.fail = False
        FakeMootionAPI.requests = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeMootionAPI)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.api_base = f"http://127.0.0.1:{self.server.server_port}"

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

        post = FakeMootionAPI.requests[0]
        self.assertEqual(post[:3], ("POST", "/api/v1/text2video", "Bearer test-key"))
        self.assertEqual(post[3], {"size": "1080x1920", "storyboard": {
            "style": "photorealistic", "user_input": {"prompt": mv.DEFAULT_PROMPT, "language": "en-US"}}})
        self.assertEqual(FakeMootionAPI.polls, 2)
        # The API key must not leak to the storage host the clip is downloaded from.
        download = [r for r in FakeMootionAPI.requests if r[1] == "/files/clip.mp4"][0]
        self.assertIsNone(download[2])

    def test_local_clip_skips_api(self):
        output = self.tmp / "final.mp4"
        rc = mv.main(["--music-dir", str(self.music), "--quotes", str(self.quotes),
                      "-o", str(output), "--clip", str(self.clip)])
        self.assertEqual(rc, 0)
        self.assert_valid_output(output)
        self.assertEqual(FakeMootionAPI.requests, [])

    def test_failed_job_returns_error(self):
        FakeMootionAPI.fail = True
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
        self.assertIn("HTTP 401: Invalid token", "\n".join(logs.output))

    def test_validation_error_is_readable(self):
        client = mv.MootionClient("test-key", self.api_base)
        with self.assertRaisesRegex(mv.PipelineError, "HTTP 422: body.size: Input should be valid"):
            client.create_task("clouds", "720x1280", "photorealistic", "en-US")

    def test_inputs_validated_before_api_call(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertLogs("motivation_video", "ERROR"):
            rc = mv.main(["--music-dir", str(empty), "--quotes", str(self.quotes),
                          "--api-key", "test-key", "--api-base", self.api_base])
        self.assertEqual(rc, 1)
        self.assertEqual(FakeMootionAPI.requests, [])


if __name__ == "__main__":
    unittest.main()
