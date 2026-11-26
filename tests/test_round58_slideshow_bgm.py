"""Round 58-61 tests: Launcher 5-video play-once sequential crossfade wallpaper & BGM."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtWidgets import QApplication

import launcher
import ui_icons


class TestLauncherVideoAndBgm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._app = QApplication.instance() or QApplication([])

    def test_bundled_videos_and_bgm_assets_exist(self):
        root = Path(launcher.__file__).resolve().parent
        videos_dir = root / "assets" / "launcher_videos"
        bgm_file = root / "assets" / "launcher_audio" / "launcher_bgm.mp3"
        videos = sorted(videos_dir.glob("video_*.mp4"))
        self.assertEqual(len(videos), 5, "Expected all 5 user hero MP4 videos in assets/launcher_videos")
        for vf in videos:
            self.assertGreater(vf.stat().st_size, 1_000_000)
        self.assertTrue(bgm_file.is_file() and bgm_file.stat().st_size > 100_000)

    def test_volume_svg_icons_render(self):
        for name in ("volume_on", "volume_off"):
            pm = ui_icons.svg_pixmap(name, "#FDE047", 16)
            self.assertFalse(pm.isNull(), f"Icon {name} failed to render")

    def test_cover_source_rect_aspect_fill_math(self):
        r1 = launcher.cover_source_rect(1280, 720, 1020, 580)
        self.assertAlmostEqual(r1.height(), 720.0)
        self.assertAlmostEqual(r1.width() / r1.height(), 1020 / 580, places=3)
        self.assertGreater(r1.x(), 0.0)
        r2 = launcher.cover_source_rect(1000, 1000, 1600, 900)
        self.assertAlmostEqual(r2.width(), 1000.0)
        self.assertAlmostEqual(r2.height(), 562.5, places=2)
        self.assertGreater(r2.y(), 0.0)
        r3 = launcher.cover_source_rect(0, 0, 1020, 580)
        self.assertEqual(r3.width(), 0.0)

    def test_ease_in_out_cosine_bounds_and_symmetry(self):
        self.assertAlmostEqual(launcher.ease_in_out_cosine(-0.5), 0.0)
        self.assertAlmostEqual(launcher.ease_in_out_cosine(0.0), 0.0)
        self.assertAlmostEqual(launcher.ease_in_out_cosine(0.5), 0.5)
        self.assertAlmostEqual(launcher.ease_in_out_cosine(1.0), 1.0)
        self.assertAlmostEqual(launcher.ease_in_out_cosine(1.5), 1.0)

    def test_cue_slot_and_slide_index_sync_helper(self):
        self.assertEqual(launcher.slide_index_for_position_ms(-100, 5), 0)
        self.assertEqual(launcher.slide_index_for_position_ms(0, 5), 0)
        self.assertEqual(launcher.slide_index_for_position_ms(19000, 5), 1)
        self.assertEqual(launcher.slide_index_for_position_ms(41000, 5), 2)
        self.assertEqual(launcher.slide_index_for_position_ms(61000, 5), 3)
        self.assertEqual(launcher.slide_index_for_position_ms(82000, 5), 4)
        self.assertEqual(launcher.slide_index_for_position_ms(106500, 5), 0)
        self.assertEqual(launcher.slide_index_for_position_ms(50000, 0), 0)

    def test_launcher_loads_5_videos_play_once_sequential_loop(self):
        with tempfile.TemporaryDirectory() as td:
            win = launcher.LauncherWindow(install_dir=Path(td), allow_local_http=True)
            try:
                self.assertTrue(win._canvas.has_videos)
                self.assertEqual(win._canvas.slide_count, 5)
                self.assertEqual(len(win._slide_dots), 5)
                self.assertIsNotNone(win._bgm_path)
                self.assertEqual(win._canvas.current_slide_index, 0)
                # Multi-video mode plays each clip ONCE (loops == 1), never repeating twice
                self.assertIsNotNone(win._canvas._video_player)
                self.assertEqual(win._canvas._video_player.loops(), 1)

                # EndOfMedia advances sequentially 0 -> 1 -> 2 -> 3 -> 4 -> 0
                for expected in (1, 2, 3, 4, 0, 1):
                    win._canvas._on_video_media_status(QMediaPlayer.EndOfMedia)
                    self.assertEqual(win._canvas.current_slide_index, expected)

                # Audio position ticks must NOT interrupt video playback mid-clip
                win._canvas.sync_to_audio_position(42000)
                self.assertEqual(win._canvas.current_slide_index, 1)

                # Manual dot jump & mute toggle work cleanly
                win._slide_dots[4].click()
                self.assertEqual(win._canvas.current_slide_index, 4)
                self.assertFalse(win._bgm_muted)
                win.toggle_bgm_mute()
                self.assertTrue(win._bgm_muted)
                win.toggle_bgm_mute()
                self.assertFalse(win._bgm_muted)

                # Repaint before first frame must stay pure dark #060913 (no LIVE STUDIO flash)
                pix = win.grab()
                self.assertFalse(pix.isNull())
                img = pix.toImage()
                sample = img.pixelColor(int(img.width() * 0.72), int(img.height() * 0.42))
                self.assertLess(sample.red(), 20)
                self.assertLess(sample.green(), 25)
                self.assertLess(sample.blue(), 40)
            finally:
                win.close()
                self._app.processEvents()


if __name__ == "__main__":
    unittest.main()
