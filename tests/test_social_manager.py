"""Automated unit and headless UI tests for Social Manager."""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

import requests

# Ensure project root is on sys.path
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import config
import db
import facebook
import main


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if isinstance(self._json, Exception):
            raise self._json
        return self._json


class TestConfig(unittest.TestCase):
    def test_parse_env_bom_quotes_comments_and_no_empty_shadowing(self):
        with tempfile.TemporaryDirectory() as td:
            p1 = Path(td) / "local.env"
            p2 = Path(td) / "home.env"
            # UTF-8 BOM + empty key + quoted value + inline comment
            p1.write_text(
                "\ufeffFB_APP_ID=\n"
                'CUSTOM_A="hello # not comment"\n'
                "CUSTOM_B=value123 # trailing comment\n",
                encoding="utf-8",
            )
            p2.write_text("FB_APP_ID=999888\nFB_APP_SECRET='sec_val'\n", encoding="utf-8")

            d1 = config._load_env_file(p1)
            d2 = config._load_env_file(p2)
            self.assertNotIn("FB_APP_ID", d1)
            self.assertEqual(d1["CUSTOM_A"], "hello # not comment")
            self.assertEqual(d1["CUSTOM_B"], "value123")
            self.assertEqual(d2["FB_APP_ID"], "999888")
            self.assertEqual(d2["FB_APP_SECRET"], "sec_val")

    def test_save_and_load_settings_with_keyring(self):
        with tempfile.TemporaryDirectory() as td:
            settings_path = Path(td) / "settings.json"
            store = {}

            def fake_set(svc, key, val):
                store[(svc, key)] = val

            def fake_get(svc, key):
                return store.get((svc, key))

            with (
                mock.patch.object(config, "SETTINGS_PATH", settings_path),
                mock.patch.object(config.keyring, "set_password", side_effect=fake_set),
                mock.patch.object(config.keyring, "get_password", side_effect=fake_get),
            ):
                config.save_fb_settings("12345", "supersecret", "23.0")
                self.assertEqual(config.get_fb_app_id(), "12345")
                self.assertEqual(config.get_fb_app_secret(), "supersecret")
                self.assertEqual(config.get_graph_version(), "v23.0")
                # Secret must not be stored in plaintext settings.json when keyring works
                raw_text = settings_path.read_text(encoding="utf-8")
                self.assertNotIn("supersecret", raw_text)


class TestDB(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(db, "DB_PATH", Path(self.td.name) / "test.db")
        self.db_patch.start()
        db.init()

    def tearDown(self):
        self.db_patch.stop()
        self.td.cleanup()

    def test_accounts_crud_and_connection_cleanup(self):
        db.save_account("facebook", "101", "Alpha Page")
        db.save_account("facebook", "102", "Beta Page")
        db.save_account("facebook", "101", "Alpha Renamed")
        rows = db.list_accounts()
        self.assertEqual(rows, [("facebook", "101", "Alpha Renamed"), ("facebook", "102", "Beta Page")])
        db.delete_account("facebook", "101")
        self.assertEqual(db.list_accounts(), [("facebook", "102", "Beta Page")])

    def test_post_history_crud_and_filter(self):
        id1 = db.add_post_history(
            platform="facebook",
            page_id="101",
            page_name="Page A",
            message="Hello world",
            link="https://example.com",
            image_paths=["a.png", "b.png"],
            status="published",
            remote_post_id="101_555",
        )
        id2 = db.add_post_history(
            platform="facebook",
            page_id="102",
            page_name="Page B",
            message="Oops",
            status="failed",
            error="Token expired",
        )
        self.assertEqual(len(db.list_post_history("all")), 2)
        self.assertEqual(len(db.list_post_history("published")), 1)
        self.assertEqual(len(db.list_post_history("failed")), 1)
        entry = db.get_post_history(id1)
        self.assertEqual(entry["image_paths"], ["a.png", "b.png"])
        db.delete_post_history(id2)
        self.assertEqual(len(db.list_post_history("all")), 1)
        db.clear_post_history()
        self.assertEqual(len(db.list_post_history("all")), 0)

    def test_scheduled_queue_due_jobs(self):
        past = (datetime.now(timezone.utc).astimezone() - timedelta(minutes=5)).isoformat(timespec="seconds")
        future = (datetime.now(timezone.utc).astimezone() + timedelta(hours=2)).isoformat(timespec="seconds")
        j1 = db.add_scheduled_job(
            run_at_iso=past,
            message="Due now",
            targets=[{"platform": "facebook", "page_id": "101", "name": "Page A"}],
        )
        db.add_scheduled_job(
            run_at_iso=future,
            message="Later",
            targets=[{"platform": "facebook", "page_id": "101", "name": "Page A"}],
        )
        due = db.get_due_scheduled_jobs()
        self.assertEqual([j["id"] for j in due], [j1])
        db.update_scheduled_job_status(j1, "done")
        self.assertEqual(db.get_due_scheduled_jobs(), [])

    def test_templates_crud(self):
        t1 = db.add_template("แคปชันขายของ", "caption", "สั่งเลยวันนี้ส่งฟรี!")
        t2 = db.add_template("แฮชแท็กกาแฟ", "hashtag", "#กาแฟสด #คาเฟ่")
        self.assertEqual(len(db.list_templates("all")), 2)
        self.assertEqual(len(db.list_templates("caption")), 1)
        self.assertEqual(len(db.list_templates("hashtag")), 1)
        db.delete_template(t1)
        self.assertEqual([t["id"] for t in db.list_templates()], [t2])


class TestFacebook(unittest.TestCase):
    def test_oauth_ignores_favicon_and_validates_state(self):
        state = "test_state_xyz"
        received = {}

        def server_worker():
            received["code"] = facebook._wait_for_code(state, timeout=10, open_browser=False)

        t = threading.Thread(target=server_worker)
        t.start()
        time.sleep(0.25)

        # Stray /favicon.ico must NOT abort login
        r_fav = requests.get(f"http://127.0.0.1:{facebook.PORT}/favicon.ico", timeout=3)
        self.assertEqual(r_fav.status_code, 404)

        # Valid /callback with matching state succeeds
        r_cb = requests.get(
            f"http://127.0.0.1:{facebook.PORT}/callback",
            params={"state": state, "code": "AUTH_CODE_123"},
            timeout=3,
        )
        self.assertEqual(r_cb.status_code, 200)
        t.join(timeout=5)
        self.assertEqual(received.get("code"), "AUTH_CODE_123")

    def test_oauth_cancel_event(self):
        ev = threading.Event()
        err = {}

        def worker():
            try:
                facebook._wait_for_code("abc", timeout=30, cancel_event=ev, open_browser=False)
            except Exception as exc:  # noqa: BLE001
                err["exc"] = exc

        t = threading.Thread(target=worker)
        t.start()
        time.sleep(0.2)
        ev.set()
        t.join(timeout=3)
        self.assertIsInstance(err.get("exc"), facebook.OAuthCancelled)

    def test_parse_response_non_json_error(self):
        bad = FakeResponse(status_code=502, json_data=ValueError("no json"), text="<html>Bad Gateway</html>")
        with self.assertRaises(RuntimeError) as ctx:
            facebook._parse_response(bad)
        self.assertIn("HTTP 502", str(ctx.exception))

    def test_validate_fb_schedule_time_bounds(self):
        now = datetime.now(timezone.utc).astimezone()
        with self.assertRaises(ValueError):
            facebook.validate_fb_schedule_time(now + timedelta(minutes=5))
        with self.assertRaises(ValueError):
            facebook.validate_fb_schedule_time(now + timedelta(days=80))
        ts = facebook.validate_fb_schedule_time(now + timedelta(minutes=30))
        self.assertGreater(ts, int(now.timestamp()))

    def test_connect_uses_post_headers_and_pagination(self):
        post_calls = []
        get_calls = []

        def fake_post(url, data=None, files=None, headers=None, timeout=None):
            post_calls.append((url, data))
            if data and data.get("grant_type") == "fb_exchange_token":
                return FakeResponse(200, {"access_token": "LONG_TOK"})
            return FakeResponse(200, {"access_token": "SHORT_TOK"})

        def fake_get(url, params=None, headers=None, timeout=None):
            get_calls.append((url, params, headers))
            if len(get_calls) == 1:
                return FakeResponse(
                    200,
                    {
                        "data": [{"id": "1", "name": "Page 1", "access_token": "PT1"}],
                        "paging": {"next": "https://graph.facebook.com/v22.0/me/accounts?after=cur1&access_token=LONG_TOK"},
                    },
                )
            return FakeResponse(
                200,
                {"data": [{"id": "2", "name": "Page 2", "access_token": "PT2"}]},
            )

        saved_tokens = {}
        with (
            mock.patch.object(config, "get_fb_app_id", return_value="APP1"),
            mock.patch.object(config, "get_fb_app_secret", return_value="SEC1"),
            mock.patch.object(facebook, "_wait_for_code", return_value="CODE1"),
            mock.patch.object(facebook.requests, "post", side_effect=fake_post),
            mock.patch.object(facebook.requests, "get", side_effect=fake_get),
            mock.patch.object(
                facebook.keyring,
                "set_password",
                side_effect=lambda s, k, v: saved_tokens.__setitem__(k, v),
            ),
        ):
            pages = facebook.connect()

        self.assertEqual(pages, [("1", "Page 1"), ("2", "Page 2")])
        self.assertEqual(saved_tokens, {"facebook:1": "PT1", "facebook:2": "PT2"})
        # Verify access_token was stripped from pagination URL and sent via Authorization header
        self.assertEqual(len(get_calls), 2)
        self.assertNotIn("access_token=", get_calls[1][0])
        self.assertEqual(get_calls[1][2]["Authorization"], "Bearer LONG_TOK")

    def test_post_to_page_text_link_single_and_multi_photo(self):
        with tempfile.TemporaryDirectory() as td:
            img1 = Path(td) / "1.png"
            img2 = Path(td) / "2.png"
            img1.write_bytes(b"\x89PNG\r\n\x1a\n")
            img2.write_bytes(b"\x89PNG\r\n\x1a\n")

            calls = []

            def fake_post(url, data=None, files=None, headers=None, timeout=None):
                calls.append((url, data, bool(files), headers))
                if url.endswith("/photos") and data.get("published") == "false" and "scheduled_publish_time" not in data:
                    return FakeResponse(200, {"id": f"MEDIA_{len(calls)}"})
                if url.endswith("/photos"):
                    return FakeResponse(200, {"id": "PHOTO_1", "post_id": "1_PHOTO_POST"})
                return FakeResponse(200, {"id": "1_FEED_POST"})

            with (
                mock.patch.object(facebook, "_get_page_token", return_value="PAGE_TOK"),
                mock.patch.object(config, "get_fb_app_secret", return_value="SEC1"),
                mock.patch.object(facebook.requests, "post", side_effect=fake_post),
            ):
                # 1) Text + Link + Native Schedule
                sched = datetime.now(timezone.utc).astimezone() + timedelta(hours=1)
                pid = facebook.post_to_page("1", "Hi", link="https://example.com", scheduled_time=sched)
                self.assertEqual(pid, "1_FEED_POST")
                self.assertEqual(calls[-1][1]["published"], "false")
                self.assertIn("scheduled_publish_time", calls[-1][1])
                self.assertEqual(calls[-1][3]["Authorization"], "Bearer PAGE_TOK")

                # 2) Single Photo
                pid_single = facebook.post_to_page("1", "Caption", image_paths=[str(img1)])
                self.assertEqual(pid_single, "1_PHOTO_POST")
                self.assertTrue(calls[-1][2])

                # 3) Multi-photo (2 unpublished uploads + 1 feed post)
                calls.clear()
                pid_multi = facebook.post_to_page("1", "Album", image_paths=[str(img1), str(img2)])
                self.assertEqual(pid_multi, "1_FEED_POST")
                self.assertEqual(len(calls), 3)
                self.assertIn("attached_media[0]", calls[2][1])
                self.assertIn("attached_media[1]", calls[2][1])

    def test_verify_credentials_direct_token_and_backend_oauth(self):
        # 1) verify_app_credentials
        with mock.patch.object(
            facebook.requests,
            "post",
            return_value=FakeResponse(200, {"access_token": "APP|TOK"}),
        ):
            self.assertTrue(facebook.verify_app_credentials("123", "sec"))

        # 2) connect_with_token (direct Page Access Token fallback to /me)
        saved = {}

        def fake_get(url, params=None, headers=None, timeout=None):
            if url.endswith("/me/accounts"):
                return FakeResponse(400, {"error": {"message": "Not a user token"}})
            return FakeResponse(200, {"id": "777", "name": "Direct Token Page"})

        with (
            mock.patch.object(facebook.requests, "get", side_effect=fake_get),
            mock.patch.object(
                facebook.keyring,
                "set_password",
                side_effect=lambda s, k, v: saved.__setitem__(k, v),
            ),
        ):
            pages = facebook.connect_with_token("EAADirectPageToken")
        self.assertEqual(pages, [("777", "Direct Token Page")])
        self.assertEqual(saved["facebook:777"], "EAADirectPageToken")

        # 3) connect_via_backend + check_backend_health
        with mock.patch.object(
            facebook.requests,
            "get",
            return_value=FakeResponse(200, {"ok": True, "configured": True}),
        ):
            h = facebook.check_backend_health("https://auth.example.com/")
            self.assertTrue(h["configured"])

        saved_backend = {}
        with (
            mock.patch.object(facebook, "_wait_for_callback", return_value="SESS_ONE_TIME"),
            mock.patch.object(
                facebook.requests,
                "post",
                return_value=FakeResponse(
                    200,
                    {"pages": [{"id": "901", "name": "Backend Page", "access_token": "BK_TOK"}]},
                ),
            ),
            mock.patch.object(
                facebook.keyring,
                "set_password",
                side_effect=lambda s, k, v: saved_backend.__setitem__(k, v),
            ),
        ):
            bpages = facebook.connect_via_backend("https://auth.example.com")
        self.assertEqual(bpages, [("901", "Backend Page")])
        self.assertEqual(saved_backend["facebook:901"], "BK_TOK")

    def test_graph_get_rejects_non_facebook_pagination_host(self):
        with self.assertRaises(RuntimeError):
            facebook._graph_get("https://evil.example.com/steal", token="TOK")

    def test_list_page_posts_and_multiple_comments(self):
        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PT"),
            mock.patch.object(
                facebook.requests,
                "get",
                return_value=FakeResponse(
                    200,
                    {
                        "data": [
                            {
                                "id": "101_500",
                                "message": "Hello post",
                                "created_time": "2026-11-19T10:00:00+0000",
                            }
                        ]
                    },
                ),
            ),
        ):
            posts = facebook.list_page_posts("101")
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]["id"], "101_500")

        posted = []

        def fake_post(url, data=None, files=None, headers=None, timeout=None):
            posted.append((url, data["message"]))
            if "FAIL" in data["message"]:
                return FakeResponse(400, {"error": {"message": "Blocked"}})
            return FakeResponse(200, {"id": f"CMT_{len(posted)}"})

        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PT"),
            mock.patch.object(facebook.requests, "post", side_effect=fake_post),
        ):
            res = facebook.post_multiple_comments(
                "101",
                "101_500",
                ["คอมเมนต์ 1", "FAIL อัน 2", "คอมเมนต์ 3"],
                delay_seconds=0,
            )

        self.assertEqual(len(res), 3)
        self.assertEqual(res[0][2], "CMT_1")
        self.assertIsNone(res[0][3])
        self.assertIn("Blocked", res[1][3])
        self.assertEqual(res[2][2], "CMT_3")
        self.assertIsNone(res[2][3])

    def test_video_upload_and_post_url_resolver(self):
        with tempfile.TemporaryDirectory() as td:
            vid = Path(td) / "clip.mp4"
            vid.write_bytes(b"\x00\x00\x00\x18ftypmp42")
            img = Path(td) / "a.png"
            img.write_bytes(b"\x89PNG\r\n\x1a\n")

            calls = []

            def fake_post(url, data=None, files=None, headers=None, timeout=None):
                calls.append((url, data, bool(files)))
                return FakeResponse(200, {"id": "VID_999"})

            with (
                mock.patch.object(facebook, "_get_page_token", return_value="PT"),
                mock.patch.object(facebook.requests, "post", side_effect=fake_post),
            ):
                # Reject combining images and video
                with self.assertRaises(ValueError):
                    facebook.post_to_page("101", "hi", image_paths=[str(img)], video_path=str(vid))

                vid_id = facebook.post_to_page("101", "My Reel", video_path=str(vid))
                self.assertEqual(vid_id, "VID_999")
                self.assertIn("graph-video.facebook.com", calls[0][0])
                self.assertTrue(calls[0][2])

        # URL -> Post ID parsing
        self.assertEqual(
            facebook.extract_post_id_from_input("101", "https://www.facebook.com/mypage/posts/555666"),
            ("101_555666", False),
        )
        self.assertEqual(
            facebook.extract_post_id_from_input("101", "https://www.facebook.com/permalink.php?story_fbid=888&id=101"),
            ("101_888", False),
        )
        self.assertEqual(
            facebook.extract_post_id_from_input("101", "https://www.facebook.com/reel/777888/"),
            ("777888", False),
        )
        self.assertEqual(
            facebook.extract_post_id_from_input("101", "https://www.facebook.com/photo/?fbid=444333"),
            ("444333", False),
        )
        # pfbid URL resolves via list_page_posts permalink_url match
        with mock.patch.object(
            facebook,
            "list_page_posts",
            return_value=[
                {
                    "id": "101_999000",
                    "message": "Promo",
                    "created_time": "2026-11-19",
                    "permalink_url": "https://www.facebook.com/mypage/posts/pfbid02ABC123xyz",
                }
            ],
        ):
            resolved = facebook.resolve_post_id_for_page(
                "101", "https://www.facebook.com/mypage/posts/pfbid02ABC123xyz"
            )
            self.assertEqual(resolved, "101_999000")


class TestMainWindowHeadless(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = main.QApplication.instance() or main.QApplication([])

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.db_patch = mock.patch.object(db, "DB_PATH", Path(self.td.name) / "ui.db")
        self.settings_patch = mock.patch.object(
            config, "SETTINGS_PATH", Path(self.td.name) / "settings.json"
        )
        self.db_patch.start()
        self.settings_patch.start()
        db.init()
        db.save_account("facebook", "101", "My Shop Page")
        db.save_account("facebook", "102", "My News Page")
        self.win = main.MainWindow()

    def tearDown(self):
        self.win.close()
        self.db_patch.stop()
        self.settings_patch.stop()
        self.td.cleanup()

    def test_ui_controls_preview_theme_and_repost(self):
        # Accounts loaded + select all / none
        self.assertEqual(self.win.accounts.count(), 2)
        self.win._set_all_accounts(True)
        self.assertEqual(len(self.win.checked_targets()), 2)
        self.assertEqual(self.win.lbl_selected_count.text(), "เลือก 2 / 2")

        # Composer char counter + preview update
        self.win.editor.setPlainText("สวัสดีครับ")
        self.win.input_link.setText("https://example.com")
        self.assertIn("10 ตัวอักษร", self.win.lbl_char_count.text())
        self.assertIn("สวัสดีครับ", self.win.lbl_preview_text.text())
        self.assertIn("https://example.com", self.win.lbl_preview_meta.text())

        # Theme toggle persists + DayNightThemeToggle widget state sync
        self.assertIsInstance(self.win.btn_theme, main.DayNightThemeToggle)
        initial = self.win.current_theme
        self.win.toggle_theme()
        self.assertNotEqual(self.win.current_theme, initial)
        self.assertEqual(config.get_theme(), self.win.current_theme)
        self.assertEqual(self.win.btn_theme._is_dark, self.win.current_theme == "dark")

        # History + Repost loads into composer
        db.add_post_history(
            platform="facebook",
            page_id="101",
            page_name="My Shop Page",
            message="Old Post Text",
            link="https://shop.example.com",
            status="published",
            remote_post_id="101_999",
        )
        self.win.refresh_history()
        self.assertEqual(self.win.tbl_history.rowCount(), 1)
        self.win.tbl_history.selectRow(0)
        self.win.repost_selected_history()
        self.assertEqual(self.win.editor.toPlainText(), "Old Post Text")
        self.assertEqual(self.win.input_link.text(), "https://shop.example.com")
        self.assertEqual(self.win.tabs.currentIndex(), 0)

        # SettingsDialog 3-tab wizard & backend mode toggle
        dlg = main.SettingsDialog(self.win)
        self.assertEqual(dlg.tab_widget.count(), 3)
        dlg.chk_use_backend.setChecked(True)
        dlg.input_backend_url.setText("https://auth.example.com")
        dlg._save()
        self.assertEqual(config.get_auth_mode(), "backend_oauth")
        self.assertEqual(config.get_backend_url(), "https://auth.example.com")
        self.assertTrue(self.win._is_auth_configured())
        dlg.close()

        # Composer follow-up comments & Multi-Comment Manager view
        self.win.input_followup_comment.setText("พิกัดสินค้า 1")
        self.win.add_followup_comment()
        self.win.input_followup_comment.setText("พิกัดสินค้า 2")
        self.win.add_followup_comment()
        self.assertEqual(self.win.follow_up_comments, ["พิกัดสินค้า 1", "พิกัดสินค้า 2"])
        self.assertEqual(self.win.list_followup_comments.count(), 2)

        # Jump from History to Comments view
        self.win.tbl_history.selectRow(0)
        self.win.comment_selected_history()
        self.assertEqual(self.win.pages_stack.currentIndex(), 3)
        self.assertEqual(self.win.input_comment_post_id.text(), "101_999")

        # Multi-line bulk add + reorder + remove
        self.win.editor_comment_input.setPlainText("เมนต์ A\nเมนต์ B\nเมนต์ C")
        self.win.add_multiline_queued_comments()
        self.assertEqual(self.win.queued_comments, ["เมนต์ A", "เมนต์ B", "เมนต์ C"])
        self.win.list_comments.setCurrentRow(2)
        self.win._move_queued_comment(-1)
        self.assertEqual(self.win.queued_comments, ["เมนต์ A", "เมนต์ C", "เมนต์ B"])

        # Templates quick-insert + Video attach preview
        db.add_template("แฮชแท็กหลัก", "hashtag", "#โปรเด็ด #ส่งฟรี")
        self.win.refresh_templates()
        self.win.editor.setPlainText("ข้อความหลัก")
        self.win.insert_selected_template()
        self.assertIn("#โปรเด็ด #ส่งฟรี", self.win.editor.toPlainText())

        vid_file = Path(self.td.name) / "demo.mp4"
        vid_file.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        self.win.set_attached_video(str(vid_file))
        self.assertEqual(self.win.attached_video, str(vid_file))
        self.assertIn("demo.mp4", self.win.post_preview.lbl_video_name.text())
        self.win.clear_video()
        self.assertEqual(self.win.attached_video, "")

    def test_bug_hunt_regressions(self):
        # M2: btn_insert_template stays disabled after _set_busy(False) when 0 templates exist
        self.assertFalse(self.win.btn_insert_template.isEnabled())
        self.win._set_busy(True)
        self.win._set_busy(False)
        self.assertFalse(self.win.btn_insert_template.isEnabled())

        # H1 & H2: connect_with_token rejects user token with 0 pages and does NOT attach wrong appsecret_proof
        get_calls = []

        def fake_get(url, params=None, headers=None, timeout=None):
            get_calls.append((url, params))
            if "me/accounts" in url:
                return FakeResponse(200, {"data": []})
            return FakeResponse(200, {"id": "U1", "name": "Personal User"})

        with (
            mock.patch.object(config, "get_fb_app_secret", return_value="WRONG_SECRET"),
            mock.patch.object(facebook.requests, "get", side_effect=fake_get),
        ):
            with self.assertRaises(RuntimeError):
                facebook.connect_with_token("USER_TOK_NO_PAGES")
        self.assertNotIn("appsecret_proof", get_calls[0][1] or {})

        # H3: numeric video ID preserved, bare pfbid requires lookup, fb.com recognized as URL
        self.assertEqual(facebook.extract_post_id_from_input("101", "9876543210"), ("9876543210", False))
        self.assertEqual(facebook.extract_post_id_from_input("101", "pfbid02AbCd"), ("pfbid02AbCd", True))
        self.assertEqual(facebook.extract_post_id_from_input("101", "fb.com/101/posts/555"), ("101_555", False))

        # H4: SettingsDialog worker stops cleanly on reject()
        dlg = main.SettingsDialog(self.win)
        dlg._run_dialog_task(lambda: (time.sleep(0.15), True)[1], lambda _: None, lambda _: None, dlg.btn_test_creds)
        w_ref = dlg._worker
        dlg.reject()
        self.assertFalse(w_ref.isRunning())

        # M4 & L1: naive datetime in get_due_scheduled_jobs and count_post_history
        self.assertEqual(db.get_due_scheduled_jobs(now_dt=datetime.now()), [])
        self.assertEqual(db.count_post_history("published"), 0)

        # M5 & M7: search filter clears hidden currentItem & _set_all_accounts(False) unchecks hidden items too
        self.win._set_all_accounts(True)
        self.assertEqual(len(self.win.checked_targets()), 2)
        self.win.accounts.setCurrentRow(0)  # Row 0 is "My News Page"
        self.win.input_page_search.setText("Shop")
        self.assertIsNone(self.win.accounts.currentItem())
        self.win._set_all_accounts(False)
        self.assertEqual(len(self.win.checked_targets()), 0)
        self.win.input_page_search.clear()

        # M6: repost with missing video warns in status
        db.add_post_history(
            platform="facebook",
            page_id="101",
            page_name="My Shop Page",
            message="Video post",
            video_path=str(Path(self.td.name) / "missing_video.mp4"),
            status="published",
            remote_post_id="999888",
        )
        self.assertEqual(db.count_post_history("published"), 1)
        self.win.refresh_history()
        self.win.tbl_history.selectRow(0)
        self.win.repost_selected_history()
        self.assertIn("ไม่พบไฟล์วิดีโอเดิม", self.win.status.text())

        # P2-H1: Photo upload retries without appsecret_proof after rewinding file stream
        img_file = Path(self.td.name) / "retry.png"
        img_file.write_bytes(b"\x89PNG\r\n\x1a\nPAYLOAD")
        upload_attempts = []

        def fake_photo_post(url, data=None, files=None, headers=None, timeout=None):
            f_tuple = files["source"]
            body_bytes = f_tuple[1].read()
            upload_attempts.append((dict(data or {}), body_bytes))
            if len(upload_attempts) == 1:
                return FakeResponse(400, {"error": {"message": "Invalid appsecret_proof provided"}})
            return FakeResponse(200, {"id": "PHOTO_OK", "post_id": "101_PHOTO_OK"})

        with (
            mock.patch.object(config, "get_auth_mode", return_value="self_app"),
            mock.patch.object(config, "get_fb_app_secret", return_value="MISMATCH_SEC"),
            mock.patch.object(facebook, "_get_page_token", return_value="DIRECT_TOK"),
            mock.patch.object(facebook.requests, "post", side_effect=fake_photo_post),
        ):
            res_id = facebook.post_to_page("101", "hello", image_paths=[str(img_file)])
        self.assertEqual(res_id, "101_PHOTO_OK")
        self.assertEqual(len(upload_attempts), 2)
        self.assertIn("appsecret_proof", upload_attempts[0][0])
        self.assertNotIn("appsecret_proof", upload_attempts[1][0])
        self.assertEqual(upload_attempts[1][1], b"\x89PNG\r\n\x1a\nPAYLOAD")

        # P2-M1: Failed / crash-recovered scheduled jobs stay visible in list_scheduled_jobs
        jid = db.add_scheduled_job(
            run_at_iso=datetime.now().astimezone().isoformat(timespec="seconds"),
            message="Crash job",
            targets=[{"platform": "facebook", "page_id": "101", "name": "My Shop Page"}],
        )
        db.update_scheduled_job_status(jid, "running")
        db.recover_stuck_scheduled_jobs()
        visible_jobs = db.list_scheduled_jobs(include_done=False)
        self.assertEqual([j["id"] for j in visible_jobs], [jid])
        self.assertEqual(visible_jobs[0]["status"], "failed")

        # P2-M3: watch/?v=..., /share/p/..., and fb.watch/... URL resolution
        self.assertEqual(
            facebook.extract_post_id_from_input("101", "https://www.facebook.com/watch/?v=333444555"),
            ("333444555", False),
        )
        class FakeRedirectResp:
            headers = {"Location": "https://www.facebook.com/101/posts/777888999"}
            url = ""
            def close(self):
                pass
        with mock.patch.object(facebook.requests, "get", return_value=FakeRedirectResp()):
            self.assertEqual(
                facebook.resolve_post_id_for_page("101", "https://www.facebook.com/share/p/1AbCdEf/"),
                "101_777888999",
            )
            self.assertEqual(
                facebook.resolve_post_id_for_page("101", "https://fb.watch/xyz987/"),
                "101_777888999",
            )

    def test_security_hardening(self):
        # S-H1: Reject plaintext HTTP for remote backend URLs; allow localhost & HTTPS
        with self.assertRaises(RuntimeError):
            facebook.check_backend_health("http://remote-backend.example.com")
        with self.assertRaises(RuntimeError):
            facebook.connect_via_backend("http://remote-backend.example.com")
        self.assertEqual(facebook._validate_backend_url("http://127.0.0.1:8000/"), "http://127.0.0.1:8000")
        self.assertEqual(facebook._validate_backend_url("https://auth.example.com/"), "https://auth.example.com")

        # S-M1: Reject path-injection characters in Page ID and Post ID
        with self.assertRaises(ValueError):
            facebook.comment_on_post("101", "../me/feed?x=1", "hi")
        with self.assertRaises(ValueError):
            facebook.post_to_page("101/../../me", "hi")
        with self.assertRaises(ValueError):
            facebook.list_page_posts("101?fields=access_token")

        # S-M4: _is_facebook_host enforces https://
        self.assertFalse(facebook._is_facebook_host("http://www.facebook.com/share/p/123"))
        self.assertTrue(facebook._is_facebook_host("https://www.facebook.com/share/p/123"))
        self.assertFalse(facebook._is_facebook_host("https://facebook.com.evil.com/share/p/123"))

        # S-M5: External/user QLabels enforce PlainText (no HTML rendering)
        from PySide6.QtCore import Qt
        self.assertEqual(self.win.status.textFormat(), Qt.PlainText)
        self.assertEqual(self.win.post_preview.lbl_body.textFormat(), Qt.PlainText)
        self.assertEqual(self.win.post_preview.lbl_page_name.textFormat(), Qt.PlainText)
        self.assertEqual(self.win.post_preview.lbl_link_url.textFormat(), Qt.PlainText)

        # S-R22: Graph API version injection prevention, userinfo/bad-port URL rejection, non-ASCII OAuth state safety
        self.assertEqual(config._normalize_graph_version("v22.0@evil.com"), config.DEFAULT_GRAPH_VERSION)
        self.assertEqual(config._normalize_graph_version("v22.0/../../me"), config.DEFAULT_GRAPH_VERSION)
        self.assertEqual(config._normalize_graph_version("22.0"), "v22.0")
        self.assertFalse(facebook._is_facebook_host("https://evil.com@www.facebook.com/posts/1"))
        self.assertFalse(facebook._is_facebook_host("https://www.facebook.com:8080/posts/1"))
        self.assertFalse(facebook._is_facebook_host("https://www.facebook.com:99999/posts/1"))
        with self.assertRaises(RuntimeError):
            facebook._validate_backend_url("https://auth.example.com:99999")
        with self.assertRaises(RuntimeError):
            facebook._validate_backend_url("https://user:pass@auth.example.com")
        self.assertFalse(main._is_valid_http_url("https://example.com:99999/a"))
        self.assertFalse(main._is_valid_http_url("https://user:pass@example.com/a"))
        self.assertFalse(main._is_valid_http_url("https://example.com/a b"))

    def test_saved_groups_and_quick_share_helper(self):
        # 1) Permalink & URL validation helpers
        self.assertEqual(
            facebook.build_post_permalink("101", "101_555666"),
            "https://www.facebook.com/101/posts/555666",
        )
        self.assertEqual(
            facebook.build_post_permalink("101", "987654321"),
            "https://www.facebook.com/101/videos/987654321",
        )
        self.assertEqual(
            facebook.validate_facebook_group_url("http://www.facebook.com/groups/coffeeclub"),
            "https://www.facebook.com/groups/coffeeclub",
        )
        with self.assertRaises(ValueError):
            facebook.validate_facebook_group_url("https://evil.example.com/groups/123")
        with self.assertRaises(ValueError):
            facebook.validate_facebook_group_url("javascript:alert(1)")

        sharer = facebook.build_facebook_share_dialog_url("https://www.facebook.com/101/posts/555666")
        self.assertIn("facebook.com/sharer/sharer.php?u=", sharer)

        # 2) DB CRUD & UI workflow
        self.win.input_group_name.setText("กลุ่มคนรักกาแฟ")
        self.win.input_group_url.setText("https://www.facebook.com/groups/coffeelovers")
        self.win.add_saved_group()
        self.assertEqual(self.win.list_saved_groups.count(), 1)
        self.win._set_all_saved_groups(True)
        self.assertEqual(len(self.win.checked_saved_groups()), 1)

        # Jump from History -> Share view
        db.add_post_history(
            platform="facebook",
            page_id="101",
            page_name="My Shop Page",
            message="โปรเด็ดวันนี้",
            status="published",
            remote_post_id="101_888999",
        )
        self.win.refresh_history()
        self.win.tbl_history.selectRow(0)
        self.win.share_selected_history()
        self.assertEqual(self.win.pages_stack.currentIndex(), 4)
        self.assertEqual(self.win.input_share_url.text(), "https://www.facebook.com/101/posts/888999")

        opened_urls = []
        with mock.patch.object(main.webbrowser, "open", side_effect=opened_urls.append):
            self.win.open_checked_groups_for_share()
            self.win.open_facebook_share_dialog()
        self.assertEqual(opened_urls[0], "https://www.facebook.com/groups/coffeelovers")
        self.assertIn("facebook.com/sharer/sharer.php?u=", opened_urls[1])
        self.assertIn("โปรเด็ดวันนี้", main.QApplication.clipboard().text())

        # Share directly to checked own Pages via Graph API
        self.win._set_all_accounts(True)
        self.assertIn("(2 เพจ)", self.win.btn_share_to_pages.text())
        self.win.input_share_url.setText("https://www.facebook.com/groups/1030945750038242/posts/1045946478538169/")
        self.win.editor_share_caption.setPlainText("สวัสดีประเทศไทย")
        shared_calls = []

        def fake_post_page(page_id, message, **kwargs):
            shared_calls.append((page_id, message, kwargs.get("link")))
            return f"{page_id}_SHARED_1"

        with mock.patch.object(facebook, "post_to_page", side_effect=fake_post_page):
            self.win.share_to_selected_pages()
            if self.win.worker:
                self.win.worker.wait(5000)
                self.app.processEvents()
        self.assertEqual(len(shared_calls), 2)
        self.assertEqual(shared_calls[0][1], "สวัสดีประเทศไทย")
        self.assertEqual(
            shared_calls[0][2],
            "https://www.facebook.com/groups/1030945750038242/posts/1045946478538169/",
        )

        # 3) Quick Comment Helper for external pages (copy + open in browser + advance cursor)
        self.win.input_comment_post_id.setText("https://www.facebook.com/otherpage/posts/123456")
        self.win.editor_comment_input.setPlainText("คอมเมนต์ที่ 1\nคอมเมนต์ที่ 2")
        self.win.add_multiline_queued_comments()
        self.win.list_comments.setCurrentRow(0)
        opened_cmt = []
        with mock.patch.object(main.webbrowser, "open", side_effect=opened_cmt.append):
            self.win.quick_comment_open_browser()
        self.assertEqual(opened_cmt, ["https://www.facebook.com/otherpage/posts/123456"])
        self.assertEqual(main.QApplication.clipboard().text(), "คอมเมนต์ที่ 1")
        # Cursor advanced to #2, clicking copy_next copies #2 without reopening browser
        self.win.copy_next_queued_comment()
        self.assertEqual(main.QApplication.clipboard().text(), "คอมเมนต์ที่ 2")

    def test_edit_published_post(self):
        # 1) API edit_post (feed post -> message, video post -> description, explicit is_video=False, fallback, rejects path injection)
        calls = []

        def fake_post(url, data=None, files=None, headers=None, timeout=None):
            calls.append((url, dict(data or {})))
            if url.endswith("/101_VIDEO_STORY") and "message" in (data or {}):
                return FakeResponse(400, {"error": {"message": "Unsupported field message"}})
            return FakeResponse(200, {"success": True})

        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PT"),
            mock.patch.object(facebook.requests, "post", side_effect=fake_post),
        ):
            self.assertTrue(facebook.edit_post("101", "101_555", "ข้อความใหม่"))
            self.assertEqual(calls[-1][1]["message"], "ข้อความใหม่")
            self.assertTrue(facebook.edit_post("101", "987654321", "คำอธิบายวิดีโอใหม่", is_video=True))
            self.assertEqual(calls[-1][1]["description"], "คำอธิบายวิดีโอใหม่")
            # Explicit is_video=False on numeric photo ID uses 'message' primary field
            self.assertTrue(facebook.edit_post("101", "987654321", "แคปชันรูปใหม่", is_video=False))
            self.assertEqual(calls[-1][1]["message"], "แคปชันรูปใหม่")
            # Fallback from 'message' to 'description' when a video story ID (101_VIDEO_STORY) rejects 'message'
            self.assertTrue(facebook.edit_post("101", "101_VIDEO_STORY", "แก้คำอธิบายวิดีโอ"))
            self.assertEqual(calls[-1][1]["description"], "แก้คำอธิบายวิดีโอ")
            with self.assertRaises(ValueError):
                facebook.edit_post("101", "../bad", "hi")
            with self.assertRaises(ValueError):
                facebook.edit_post("101", "101_555", "   ")

        # 2) DB update_post_history_message + update_post_history_message_by_remote_id + EditPostDialog UI
        hid = db.add_post_history(
            platform="facebook",
            page_id="101",
            page_name="My Shop Page",
            message="ข้อความเดิม",
            status="published",
            remote_post_id="101_777",
        )
        db.update_post_history_message(hid, "ข้อความที่แก้แล้ว")
        self.assertEqual(db.get_post_history(hid)["message"], "ข้อความที่แก้แล้ว")
        db.update_post_history_message_by_remote_id("facebook", "101", "101_777", "อัปเดตผ่าน remote_id")
        self.assertEqual(db.get_post_history(hid)["message"], "อัปเดตผ่าน remote_id")

        dlg = main.EditPostDialog("My Shop Page", "101_777", "ข้อความเดิม", parent=self.win)
        dlg.editor.setPlainText("แก้ไขผ่าน Dialog")
        self.assertEqual(dlg.new_message(), "แก้ไขผ่าน Dialog")
        dlg.close()

        # 3) URL parser rejects non-Facebook URLs & invalid bare IDs, and marks fbid=pfbid... for lookup
        with self.assertRaises(ValueError):
            facebook.extract_post_id_from_input("101", "https://evil.example.com/posts/12345")
        with self.assertRaises(ValueError):
            facebook.extract_post_id_from_input("101", "invalid post id with spaces")
        self.assertEqual(
            facebook.extract_post_id_from_input("101", "https://www.facebook.com/photo/?fbid=pfbid02PhotoAbc"),
            ("pfbid02PhotoAbc", True),
        )

        # 4) Switching page in combo_comment_page clears stale list_page_posts from previous page
        it = main.QListWidgetItem("[2026-11-20 18:00] Old post")
        it.setData(main.Qt.UserRole, "101_777")
        it.setData(main.Qt.UserRole + 1, "Old post")
        it.setData(main.Qt.UserRole + 2, "2026-11-20 18:00")
        it.setData(main.Qt.UserRole + 3, ("101", "My Shop Page"))
        self.win.list_page_posts.addItem(it)
        cur_pid = self.win.combo_comment_page.currentData()[1]
        self.win._loaded_posts_page_id = cur_pid
        self.win._sync_edited_post_in_page_list("101_777", "Synced post text")
        self.assertIn("Synced post text", self.win.list_page_posts.item(0).text())
        # Switch combo_comment_page to a different page -> clears stale posts list
        other_idx = 1 if self.win.combo_comment_page.currentIndex() == 0 else 0
        self.win.combo_comment_page.setCurrentIndex(other_idx)
        self.assertEqual(self.win.list_page_posts.count(), 0)

    def test_token_health_insights_inbox_csv_and_page_delay(self):
        # 1) Token health check API
        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PT"),
            mock.patch.object(
                facebook.requests,
                "get",
                return_value=FakeResponse(200, {"id": "101", "name": "My Shop Page"}),
            ),
        ):
            ok, detail = facebook.check_page_token_health("101")
            self.assertTrue(ok)
            self.assertIn("ปกติ", detail)

        # 2) Post engagement metrics (Insights)
        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PT"),
            mock.patch.object(
                facebook.requests,
                "get",
                return_value=FakeResponse(
                    200,
                    {
                        "id": "101_555",
                        "likes": {"summary": {"total_count": 42}},
                        "comments": {"summary": {"total_count": 7}},
                        "shares": {"count": 15},
                        "permalink_url": "https://www.facebook.com/101/posts/555",
                    },
                ),
            ),
        ):
            eng = facebook.get_post_engagement("101", "101_555")
            self.assertEqual(eng["likes"], 42)
            self.assertEqual(eng["comments"], 7)
            self.assertEqual(eng["shares"], 15)

        # 3) Comment Inbox list & reply
        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PT"),
            mock.patch.object(
                facebook.requests,
                "get",
                return_value=FakeResponse(
                    200,
                    {
                        "data": [
                            {
                                "id": "555_999",
                                "from": {"name": "Somchai"},
                                "message": "สนใจสินค้าครับ",
                                "created_time": "2026-11-21T02:00:00+0000",
                                "comment_count": 1,
                            }
                        ]
                    },
                ),
            ),
            mock.patch.object(
                facebook.requests,
                "post",
                return_value=FakeResponse(200, {"id": "555_1000"}),
            ),
        ):
            cmts = facebook.list_post_comments("101", "101_555")
            self.assertEqual(len(cmts), 1)
            self.assertEqual(cmts[0]["author"], "Somchai")
            rid = facebook.reply_to_comment("101", "555_999", "ทักแชทได้เลยครับ")
            self.assertEqual(rid, "555_1000")

        # 4) CSV Import into Scheduled Queue (including quoted multiline captions, Excel DD/MM/YYYY format & atomic validation)
        csv_file = Path(self.td.name) / "schedule.csv"
        csv_file.write_text(
            "run_at,message,link\n"
            '"2026-12-01 10:30","โพสต์จาก CSV 1\nบรรทัดที่ 2","https://example.com/1"\n'
            "02/12/2026 14:00,โพสต์จาก CSV 2 (Excel DD/MM/YYYY),\n",
            encoding="utf-8-sig",
        )
        targets = [{"platform": "facebook", "page_id": "101", "name": "My Shop Page"}]
        imported = main.import_scheduled_jobs_from_csv(str(csv_file), targets)
        self.assertEqual(imported, 2)
        jobs = db.list_scheduled_jobs()
        self.assertGreaterEqual(len(jobs), 2)
        self.assertIn("บรรทัดที่ 2", jobs[0]["message"])

        before_cnt = len(db.list_scheduled_jobs())
        bad_csv = Path(self.td.name) / "bad.csv"
        bad_csv.write_text(
            "run_at,message,link,image_paths,video_path\n"
            "2026-12-05 10:30,valid row 1,,,\n"
            "2026-12-06 10:30,invalid row 2,,a.png,v.mp4\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            main.import_scheduled_jobs_from_csv(str(bad_csv), targets)
        # Atomic validation: row 1 of bad_csv must NOT have been inserted
        self.assertEqual(len(db.list_scheduled_jobs()), before_cnt)

        # 5) Online SQLite WAL-safe backup_database
        bkp_path = Path(self.td.name) / "backup.db"
        db.backup_database(bkp_path)
        self.assertTrue(bkp_path.is_file())
        with self.assertRaises(ValueError):
            db.backup_database(db.DB_PATH)

        # 6) Inter-page delay spinboxes exist and default to 0
        self.assertEqual(self.win.spin_page_delay_min.value(), 0)
        self.assertEqual(self.win.spin_page_delay_max.value(), 0)

    def test_round26_ten_pro_features(self):
        # Feature #1: Page Presets (DB + UI apply)
        pid = db.save_page_preset("ชุดเพจร้านกาแฟ", ["facebook:101"])
        self.assertGreater(pid, 0)
        presets = db.list_page_presets()
        self.assertEqual(len(presets), 1)
        self.assertEqual(presets[0]["name"], "ชุดเพจร้านกาแฟ")
        self.win.refresh_page_presets()
        self.win._set_all_accounts(False)
        self.win.combo_page_presets.setCurrentIndex(1)
        checked = self.win.checked_targets()
        self.assertEqual(len(checked), 1)
        self.assertEqual(checked[0][1], "101")
        db.delete_page_preset(pid)
        self.assertEqual(len(db.list_page_presets()), 0)

        # Feature #2: Spintax {A|B|C} & Dynamic Variables {page_name}, {date}, {time}
        fixed_dt = datetime(2026, 11, 21, 14, 30, 0)
        rendered = facebook.render_spintax_and_variables(
            "{สวัสดี|ทักทาย} จากเพจ {page_name} วันที่ {date} เวลา {time}",
            page_name="Coffee Roaster",
            now_dt=fixed_dt,
        )
        self.assertIn("Coffee Roaster", rendered)
        self.assertIn("2026-11-21", rendered)
        self.assertIn("14:30", rendered)
        self.assertTrue(
            rendered.startswith("สวัสดี ") or rendered.startswith("ทักทาย ")
        )

        # Feature #3 & #4: Delete Published Post + Hide/Delete Comment via Graph API
        delete_calls = []
        post_calls = []

        def fake_delete(url, params=None, headers=None, timeout=None):
            delete_calls.append(url)
            return FakeResponse(200, {"success": True})

        def fake_post(url, data=None, files=None, headers=None, timeout=None):
            post_calls.append((url, dict(data or {}), bool(files)))
            return FakeResponse(200, {"id": "CMT_IMG_1", "success": True})

        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PT"),
            mock.patch.object(facebook.requests, "delete", side_effect=fake_delete),
            mock.patch.object(facebook.requests, "post", side_effect=fake_post),
        ):
            self.assertTrue(facebook.delete_post("101", "101_999"))
            self.assertTrue(delete_calls[-1].endswith("/101_999"))
            self.assertTrue(facebook.set_comment_hidden("101", "999_111", True))
            self.assertEqual(post_calls[-1][1]["is_hidden"], "true")
            self.assertTrue(facebook.delete_comment("101", "999_111"))
            self.assertTrue(delete_calls[-1].endswith("/999_111"))

            # Feature #9: Image Comment upload (text + image & image-only)
            from PySide6.QtGui import QColor, QImage

            img_file = Path(self.td.name) / "comment_pic.png"
            qimg = QImage(64, 64, QImage.Format_ARGB32)
            qimg.fill(QColor("#2563EB"))
            qimg.save(str(img_file), "PNG")

            cid = facebook.comment_on_post(
                "101", "101_999", "รีวิวสินค้าจริง", image_path=str(img_file)
            )
            self.assertEqual(cid, "CMT_IMG_1")
            self.assertTrue(post_calls[-1][2])  # multipart files passed

            spec = facebook.format_comment_spec("ส่งรูปในเม้น", str(img_file))
            msg_part, img_part = facebook.split_comment_spec(spec)
            self.assertEqual(msg_part, "ส่งรูปในเม้น")
            self.assertEqual(img_part, str(img_file))

        # DB delete_post_history_by_remote_id
        db.add_post_history(
            platform="facebook",
            page_id="101",
            page_name="My Shop Page",
            message="โพสต์ที่จะลบ",
            status="published",
            remote_post_id="101_DEL_ME",
        )
        removed = db.delete_post_history_by_remote_id("facebook", "101", "101_DEL_ME")
        self.assertEqual(removed, 1)

        # Feature #7: Minimize to System Tray config & tray icon initialization
        self.assertFalse(config.get_minimize_to_tray())
        config.save_minimize_to_tray(True)
        self.assertTrue(config.get_minimize_to_tray())
        config.save_minimize_to_tray(False)
        self.assertTrue(hasattr(self.win, "tray_icon"))

        # Feature #8 & #11: Edit Scheduled Queue Job, Retry Failed Jobs & Recurring Schedule
        future_iso = (datetime.now().astimezone() + timedelta(hours=1)).isoformat(
            timespec="seconds"
        )
        jid = db.add_scheduled_job(
            run_at_iso=future_iso,
            message="โพสต์รายวันเดิม",
            link="https://example.com/old",
            image_paths=[],
            targets=[{"platform": "facebook", "page_id": "101", "name": "My Shop Page"}],
            repeat_rule="daily",
        )
        job_obj = db.get_scheduled_job(jid)
        self.assertEqual(job_obj["repeat_rule"], "daily")

        # Edit queue job via db.update_scheduled_job & EditQueueJobDialog
        ed_dlg = main.EditQueueJobDialog(job_obj, parent=self.win)
        ed_dlg.editor.setPlainText("โพสต์รายสัปดาห์แก้ไขแล้ว")
        ed_dlg.input_link.setText("https://example.com/new")
        ed_dlg.combo_repeat.setCurrentIndex(2)  # weekly
        vals = ed_dlg.updated_values()
        ed_dlg.close()
        db.update_scheduled_job(
            jid,
            run_at_iso=vals["run_at_iso"],
            message=vals["message"],
            link=vals["link"],
            repeat_rule=vals["repeat_rule"],
        )
        updated_job = db.get_scheduled_job(jid)
        self.assertEqual(updated_job["message"], "โพสต์รายสัปดาห์แก้ไขแล้ว")
        self.assertEqual(updated_job["repeat_rule"], "weekly")

        # Advance recurring job moves run_at forward by >= 7 days and keeps status='pending'
        next_iso = db.advance_recurring_scheduled_job(jid)
        self.assertIsNotNone(next_iso)
        after_adv = db.get_scheduled_job(jid)
        self.assertEqual(after_adv["status"], "pending")
        self.assertGreater(
            datetime.fromisoformat(after_adv["run_at"]),
            datetime.fromisoformat(updated_job["run_at"]),
        )

        # Retry all failed jobs
        db.update_scheduled_job_status(jid, "failed", "Network error")
        retried_cnt = db.retry_failed_scheduled_jobs()
        self.assertGreaterEqual(retried_cnt, 1)
        self.assertEqual(db.get_scheduled_job(jid)["status"], "pending")

        # Feature #10: Auto-Save Composer Draft
        self.win.editor.setPlainText("ร่างโพสต์ที่บันทึกอัตโนมัติ")
        self.win.input_link.setText("https://example.com/draft")
        self.win.chk_watermark.setChecked(True)
        self.win.input_watermark_text.setText("© {page_name}")
        self.win._save_draft()
        saved_draft = config.load_composer_draft()
        self.assertEqual(saved_draft["text"], "ร่างโพสต์ที่บันทึกอัตโนมัติ")
        self.assertTrue(saved_draft["watermark_enabled"])
        self.win._clear_composer()
        self.assertEqual(config.load_composer_draft()["text"], "")

        # Feature #12: Auto Text Watermark on real image
        wm_src = Path(self.td.name) / "orig_photo.png"
        src_img = QImage(320, 240, QImage.Format_ARGB32)
        src_img.fill(QColor("#10B981"))
        src_img.save(str(wm_src), "PNG")
        out_path, is_temp = main.apply_text_watermark(str(wm_src), "© My Shop Page")
        try:
            self.assertTrue(is_temp)
            self.assertNotEqual(out_path, str(wm_src))
            self.assertTrue(Path(out_path).is_file())
        finally:
            if is_temp:
                Path(out_path).unlink(missing_ok=True)

    def test_round31_bug_hunt_fixes(self):
        # Bug #1: Image-only comment spec (' ||| path' and stripped '||| path') parses cleanly
        spec_img_only = facebook.format_comment_spec("", "C:/photos/pic.png")
        self.assertEqual(facebook.split_comment_spec(spec_img_only), ("", "C:/photos/pic.png"))
        self.assertEqual(facebook.split_comment_spec(spec_img_only.strip()), ("", "C:/photos/pic.png"))

        # Bug #2a: Spintax with {page_name} inside options AND page name containing {A|B}
        rendered_nested = facebook.render_spintax_and_variables(
            "{ยินดีต้อนรับสู่ {page_name}|สวัสดีจาก {page_name}}",
            page_name="Cafe {TH|EN}",
        )
        self.assertIn("Cafe {TH|EN}", rendered_nested)
        self.assertTrue(
            rendered_nested == "ยินดีต้อนรับสู่ Cafe {TH|EN}"
            or rendered_nested == "สวัสดีจาก Cafe {TH|EN}"
        )

        # Bug #2b: post_multiple_comments preserves image-only comment specs without stripping delimiter
        uploaded = []

        def fake_comment(page_id, post_id, message="", *, image_path=""):
            uploaded.append((message, image_path))
            return "C_IMG_ONLY"

        with mock.patch.object(facebook, "comment_on_post", side_effect=fake_comment):
            res = facebook.post_multiple_comments(
                "101", "101_555", [spec_img_only], delay_seconds=0
            )
        self.assertEqual(len(res), 1)
        self.assertIsNone(res[0][3])
        self.assertEqual(uploaded, [("", "C:/photos/pic.png")])

        # Bug #3: Scheduled queue stores its own watermark_text independently of live Composer UI
        future_iso = (datetime.now().astimezone() + timedelta(hours=2)).isoformat(timespec="seconds")
        jid = db.add_scheduled_job(
            run_at_iso=future_iso,
            message="โพสต์คิวมีลายน้ำเฉพาะตัว",
            targets=[{"platform": "facebook", "page_id": "101", "name": "My Shop Page"}],
            watermark_text="© Queue Watermark",
        )
        self.assertEqual(db.get_scheduled_job(jid)["watermark_text"], "© Queue Watermark")

        # Bug #4: Hidden window (System Tray) does NOT pop blocking QMessageBox.warning on _failed
        self.win.hide()
        with (
            mock.patch.object(main.QMessageBox, "warning") as mock_warn,
            mock.patch.object(self.win, "_notify_tray") as mock_notify,
        ):
            self.win._failed("Simulated background error")
            mock_warn.assert_not_called()
            mock_notify.assert_called_once()

        # Bug #5: Debounced auto-save draft timer exists and schedules on edit
        self.assertTrue(hasattr(self.win, "_draft_timer"))
        self.win.editor.setPlainText("ทดสอบ debounce")
        self.assertTrue(self.win._draft_timer.isActive())
        self.win._save_draft()
        self.assertFalse(self.win._draft_timer.isActive())

        # Bug #7: Image-only queued comment does NOT wipe clipboard in copy_next_queued_comment
        main.QApplication.clipboard().setText("KEEP_ME")
        self.win.queued_comments = [spec_img_only]
        self.win._refresh_queued_comments()
        with mock.patch.object(main.QMessageBox, "information"):
            self.win.copy_next_queued_comment()
        self.assertEqual(main.QApplication.clipboard().text(), "KEEP_ME")

    def test_round32_bug_hunt_fixes(self):
        # Bug #2: Thai Windows Excel CSV encoded in cp874 + optional repeat_rule & watermark_text columns
        cp874_csv = Path(self.td.name) / "thai_excel.csv"
        cp874_csv.write_bytes(
            "เวลา,ข้อความ,ลิงก์,รูปภาพ,วิดีโอ,ทำซ้ำ,ลายน้ำ\n"
            "2026-12-10 09:00,สวัสดีจากไฟล์ Excel ภาษาไทย,,,,รายวัน,(c) {page_name}\n".encode("cp874")
        )
        targets = [{"platform": "facebook", "page_id": "101", "name": "My Shop Page"}]
        imported = main.import_scheduled_jobs_from_csv(str(cp874_csv), targets)
        self.assertEqual(imported, 1)
        jobs = db.list_scheduled_jobs()
        matched = [j for j in jobs if "Excel ภาษาไทย" in j["message"]]
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched[0]["repeat_rule"], "daily")
        self.assertEqual(matched[0]["watermark_text"], "(c) {page_name}")

        # Bug #3: EditQueueJobDialog edits watermark_text & warns on past run_at
        jid = matched[0]["id"]
        ed = main.EditQueueJobDialog(matched[0], parent=self.win)
        self.assertEqual(ed.input_watermark.text(), "(c) {page_name}")
        ed.input_watermark.setText("© New Watermark")
        vals = ed.updated_values()
        self.assertEqual(vals["watermark_text"], "© New Watermark")
        db.update_scheduled_job(
            jid,
            run_at_iso=vals["run_at_iso"],
            message=vals["message"],
            link=vals["link"],
            repeat_rule=vals["repeat_rule"],
            watermark_text=vals["watermark_text"],
        )
        self.assertEqual(db.get_scheduled_job(jid)["watermark_text"], "© New Watermark")
        # Calling update_scheduled_job without watermark_text preserves existing value
        db.update_scheduled_job(
            jid,
            run_at_iso=vals["run_at_iso"],
            message="แก้ข้อความอย่างเดียว",
            link=vals["link"],
            repeat_rule=vals["repeat_rule"],
        )
        self.assertEqual(db.get_scheduled_job(jid)["watermark_text"], "© New Watermark")
        # Past run_at prompts confirmation and cancels accept when user chooses No
        ed.dt_run_at.setDateTime(main.QDateTime.currentDateTime().addSecs(-3600))
        with mock.patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.No) as m_q:
            ed._validate_and_accept()
            m_q.assert_called_once()
        ed.close()

        # Bug #4: repost_selected_history clears stale follow_up_comments
        self.win.follow_up_comments = ["คอมเมนต์เก่าที่ไม่เกี่ยว"]
        self.win._refresh_followup_list()
        db.add_post_history(
            platform="facebook",
            page_id="101",
            page_name="My Shop Page",
            message="โพสต์เดิมสำหรับ Repost",
            status="published",
            remote_post_id="101_REPOST_1",
        )
        self.win.refresh_history()
        self.win.tbl_history.selectRow(0)
        self.win.repost_selected_history()
        self.assertEqual(self.win.follow_up_comments, [])

    def test_round33_bug_hunt_fixes(self):
        # Bug #1: Malformed IPv6 URL ('https://[') never crashes live preview or validators
        self.win.input_link.setText("https://[")
        self.win._update_preview()
        self.assertFalse(self.win.post_preview.link_card.isHidden())
        self.assertEqual(self.win.post_preview.lbl_link_domain.text(), "HTTPS://[")
        self.assertFalse(main._is_valid_http_url("https://["))
        self.assertFalse(facebook._is_facebook_host("https://["))
        with self.assertRaises(RuntimeError):
            facebook._validate_backend_url("https://[")
        with self.assertRaises(ValueError):
            facebook.build_facebook_share_dialog_url("https://[")
        self.win.input_link.clear()

        # Bug #2: Auto-orient image loading + watermarking works cleanly
        from PySide6.QtGui import QImage
        wm_src = Path(self.td.name) / "orient_test.png"
        src_img = QImage(160, 90, QImage.Format_ARGB32)
        src_img.fill(0xFF1E40AF)
        self.assertTrue(src_img.save(str(wm_src)))
        wm_out, is_tmp = main.apply_text_watermark(str(wm_src), "© เพจไทย")
        try:
            self.assertTrue(is_tmp)
            self.assertTrue(Path(wm_out).is_file())
        finally:
            if is_tmp:
                Path(wm_out).unlink(missing_ok=True)

        # Bug #3: PostCommentsDialog preserves is_hidden from API and toggles Hide <-> Unhide
        dlg = main.PostCommentsDialog("101", "My Shop Page", "101_999", parent=self.win)
        dlg._populate_comments(
            [
                {
                    "id": "c_hid",
                    "author": "Alice",
                    "message": "สแปม",
                    "created_time": "2026-11-21T08:00:00+0000",
                    "reply_count": 0,
                    "is_hidden": True,
                }
            ]
        )
        self.assertTrue(dlg.list_inbox.item(0).text().startswith("[ซ่อนแล้ว] "))
        dlg.list_inbox.setCurrentRow(0)
        self.assertEqual(dlg.btn_hide_comment.text(), "เลิกซ่อนคอมเมนต์")
        with mock.patch("facebook.set_comment_hidden", return_value=True) as m_unhide:
            dlg.hide_selected_comment()
            self.assertIsNotNone(dlg._worker)
            dlg._worker.wait(3000)
            self.app.processEvents()
            m_unhide.assert_called_once_with("101", "c_hid", False)
        self.assertFalse(dlg.list_inbox.item(0).text().startswith("[ซ่อนแล้ว] "))
        self.assertEqual(dlg.btn_hide_comment.text(), "ซ่อนคอมเมนต์")
        dlg.close()

        # Bug #4: Empty-target scheduled queue job is marked failed (never falsely 'done')
        jid = db.add_scheduled_job(
            run_at_iso="2020-01-01T00:00:00+07:00",
            message="ไม่มีเพจ",
            targets=[],
        )
        job = db.get_scheduled_job(jid)
        with mock.patch.object(main.QMessageBox, "warning"):
            self.win._dispatch_queue_job(job)
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(3000)
            self.app.processEvents()
        self.assertEqual(db.get_scheduled_job(jid)["status"], "failed")

        # Bug #5: SettingsDialog validates backend URL when One-Click Backend mode is enabled
        sdlg = main.SettingsDialog(self.win)
        sdlg.chk_use_backend.setChecked(True)
        sdlg.input_backend_url.setText("http://remote-insecure.example.com")
        with mock.patch.object(main.QMessageBox, "warning") as m_warn:
            sdlg._save()
            m_warn.assert_called_once()
        self.assertNotEqual(sdlg.result(), main.QDialog.Accepted)
        sdlg.close()

        # Bug #6: send_queued_comments validates missing image files before running worker
        self.win.refresh_accounts()
        self.win.input_comment_post_id.setText("101_555")
        self.win.queued_comments = [
            facebook.format_comment_spec("คอมเมนต์มีรูป", str(Path(self.td.name) / "missing.png"))
        ]
        self.win._refresh_queued_comments()
        with mock.patch.object(main.QMessageBox, "warning") as m_warn, mock.patch(
            "facebook.post_multiple_comments"
        ) as m_post_c:
            self.win.send_queued_comments()
            m_warn.assert_called_once()
            m_post_c.assert_not_called()

    def test_round34_bug_hunt_fixes(self):
        # Bug #1: _retire_worker parks slow workers in _ORPHAN_WORKERS and cleans up on finish
        gate = threading.Event()
        slow_w = main.Worker(lambda: gate.wait(2.0))
        slow_w.start()
        main._retire_worker(slow_w, wait_ms=10)
        self.assertIn(slow_w, main._ORPHAN_WORKERS)
        gate.set()
        slow_w.wait(3000)
        self.app.processEvents()
        self.assertNotIn(slow_w, main._ORPHAN_WORKERS)

        # Bug #2: Modern Facebook SEO post URLs (/posts/<slug>/<pfbid_or_id>)
        self.assertEqual(
            facebook.extract_post_id_from_input(
                "101", "https://www.facebook.com/MyPage/posts/promo-slug-title/pfbid02ABC123/"
            ),
            ("pfbid02ABC123", True),
        )
        self.assertEqual(
            facebook.extract_post_id_from_input(
                "101", "https://www.facebook.com/MyPage/posts/promo-slug-title/987654321/"
            ),
            ("101_987654321", False),
        )
        with self.assertRaises(ValueError):
            facebook.extract_post_id_from_input(
                "101", "https://www.facebook.com/MyPage/posts/not-an-id-slug"
            )

        # Bug #3: comment_selected_history warns if original page was removed instead of targeting wrong page
        db.add_post_history(
            platform="facebook",
            page_id="999_DELETED",
            page_name="Deleted Page",
            message="โพสต์จากเพจที่ถูกลบ",
            status="published",
            remote_post_id="999_DELETED_123",
        )
        self.win.refresh_history()
        self.win.tbl_history.selectRow(0)
        self.win._switch_page(2)
        with mock.patch.object(main.QMessageBox, "warning") as m_warn:
            self.win.comment_selected_history()
            m_warn.assert_called_once()
        self.assertEqual(self.win.pages_stack.currentIndex(), 2)

        # Bug #4: CSV datetime parser handles extra whitespace & Thai Buddhist Era (พ.ศ.) including leap year
        be_csv = Path(self.td.name) / "thai_be_dates.csv"
        be_csv.write_text(
            "run_at,message\n"
            "2569-12-01  09:00,ทดสอบ พ.ศ. แบบ YMD\n"
            "29/02/2567 09:30,ทดสอบ พ.ศ. ปีอธิกสุรทิน\n",
            encoding="utf-8-sig",
        )
        targets = [{"platform": "facebook", "page_id": "101", "name": "My Shop Page"}]
        cnt = main.import_scheduled_jobs_from_csv(str(be_csv), targets)
        self.assertEqual(cnt, 2)
        jobs = db.list_scheduled_jobs()
        be_ymd = [j for j in jobs if "แบบ YMD" in j["message"]][0]
        be_leap = [j for j in jobs if "ปีอธิกสุรทิน" in j["message"]][0]
        self.assertTrue(be_ymd["run_at"].startswith("2026-12-01T09:00"))
        self.assertTrue(be_leap["run_at"].startswith("2024-02-29T09:30"))

    def test_round35_bug_hunt_fixes(self):
        import socket
        from PySide6.QtCore import QMimeData, QPoint, QUrl, Qt
        from PySide6.QtGui import QDropEvent, QImage

        # Bug #1: OAuth callback server drops idle preconnect sockets cleanly via Handler.timeout
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]

        cb_result: dict[str, str] = {}

        def run_cb():
            try:
                cb_result["code"] = facebook._wait_for_callback(
                    "st_r35",
                    expected_param="code",
                    timeout=15,
                    open_browser=False,
                )
            except Exception as exc:  # noqa: BLE001
                cb_result["err"] = str(exc)

        with mock.patch.object(facebook, "PORT", free_port):
            t = threading.Thread(target=run_cb, daemon=True)
            t.start()
            idle_sock = None
            try:
                deadline = time.monotonic() + 3.0
                while time.monotonic() < deadline:
                    try:
                        idle_sock = socket.create_connection(("127.0.0.1", free_port), timeout=5)
                        break
                    except OSError:
                        time.sleep(0.05)
                self.assertIsNotNone(idle_sock)
                # Keep idle_sock open without sending bytes; Handler.timeout=2.0 should unblock
                resp = requests.get(
                    f"http://127.0.0.1:{free_port}/callback",
                    params={"state": "st_r35", "code": "OK123"},
                    timeout=8,
                )
                self.assertEqual(resp.status_code, 200)
            finally:
                if idle_sock is not None:
                    idle_sock.close()
                t.join(timeout=10)
        self.assertEqual(cb_result.get("code"), "OK123")

        # Bug #2: Circular pagination guard in _fetch_all_pages
        loop_payload = {
            "data": [],
            "paging": {"next": "https://graph.facebook.com/v22.0/me/accounts?after=LOOP"},
        }
        with mock.patch.object(facebook, "_graph_get", return_value=loop_payload) as m_get:
            pages = facebook._fetch_all_pages("UTOKEN")
        self.assertEqual(pages, [])
        self.assertEqual(m_get.call_count, 2)

        # Bug #3: Page preset re-apply via activated + clears search filter so checked pages are visible
        db.save_page_preset("All Shops", [("facebook", "101"), ("facebook", "102")])
        self.win.refresh_page_presets()
        self.win.combo_page_presets.setCurrentIndex(1)
        self.assertEqual(len(self.win.checked_targets()), 2)
        self.win.input_page_search.setText("Shop")
        self.assertTrue(self.win.accounts.item(0).isHidden())  # My News Page hidden
        self.win._set_all_accounts(False)
        self.assertEqual(len(self.win.checked_targets()), 0)
        # Re-select the same preset index (index 1) via activated signal
        self.win.combo_page_presets.activated.emit(1)
        self.assertEqual(self.win.input_page_search.text(), "")
        self.assertEqual(len(self.win.checked_targets()), 2)
        self.assertFalse(self.win.accounts.item(0).isHidden())
        self.assertFalse(self.win.accounts.item(1).isHidden())

        # Bug #4: Pre-1970 run_at on Windows & config._save_settings auto-creates parent dir
        ed = main.EditQueueJobDialog(
            {"id": 99, "run_at": "1969-01-01T00:00:00", "message": "old"},
            parent=self.win,
        )
        self.assertGreater(ed.dt_run_at.dateTime().date().year(), 2000)
        ed.close()

        nested_settings = Path(self.td.name) / "missing_dir" / "sub" / "settings.json"
        with mock.patch.object(config, "SETTINGS_PATH", nested_settings):
            config._save_settings({"theme": "light"})
        self.assertTrue(nested_settings.is_file())

        # Bug #5: Image path deduplication across slashes/case + reject directory named like .jpg
        pic = Path(self.td.name) / "pic.png"
        img = QImage(32, 32, QImage.Format_ARGB32)
        img.fill(0xFF10B981)
        self.assertTrue(img.save(str(pic)))
        self.win.clear_images()
        self.win._add_image_paths([str(pic).replace("\\", "/"), str(pic).upper()])
        self.assertEqual(len(self.win.attached_images), 1)

        fake_dir = Path(self.td.name) / "album.jpg"
        fake_dir.mkdir(exist_ok=True)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(fake_dir))])
        ev = QDropEvent(
            QPoint(10, 10),
            Qt.CopyAction,
            mime,
            Qt.LeftButton,
            Qt.NoModifier,
        )
        dropped: list[list[str]] = []
        self.win.drop_zone.files_dropped.connect(dropped.append)
        self.win.drop_zone.dropEvent(ev)
        self.assertEqual(dropped, [])

    def test_round36_bug_hunt_fixes(self):
        # Bug #1: _retire_worker disconnects stale `finished` UI slot while still cleaning up orphan
        gate = threading.Event()
        slow_w = main.Worker(lambda: gate.wait(2.0))
        stale_fired: list[bool] = []
        slow_w.finished.connect(lambda: stale_fired.append(True))
        slow_w.start()
        main._retire_worker(slow_w, wait_ms=10)
        self.assertIn(slow_w, main._ORPHAN_WORKERS)
        gate.set()
        slow_w.wait(3000)
        self.app.processEvents()
        self.assertEqual(stale_fired, [])
        self.assertNotIn(slow_w, main._ORPHAN_WORKERS)

        # Bug #2: Windows pre-1970 naive datetime.astimezone() guards across date pickers, CSV, and DB
        self.assertEqual(self.win.dt_schedule.minimumDateTime().date().year(), 2000)
        ed = main.EditQueueJobDialog(
            {"id": 88, "run_at": "1969-01-01T00:00:00", "message": "old"},
            parent=self.win,
        )
        self.assertEqual(ed.dt_run_at.minimumDateTime().date().year(), 2000)
        self.assertGreaterEqual(ed.dt_run_at.dateTime().date().year(), 2000)
        ed.close()

        pre1970_csv = Path(self.td.name) / "pre1970.csv"
        pre1970_csv.write_text("run_at,message\n1900-01-01 09:00,test\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            main.import_scheduled_jobs_from_csv(
                str(pre1970_csv),
                [{"platform": "facebook", "page_id": "101", "name": "My Shop Page"}],
            )

        jid = db.add_scheduled_job(
            run_at_iso="1969-01-01T00:00:00",
            message="pre1970 recurring job",
            targets=[{"platform": "facebook", "page_id": "101", "name": "My Shop Page"}],
            repeat_rule="daily",
        )
        due = db.get_due_scheduled_jobs()
        self.assertFalse(any(j["id"] == jid for j in due))
        next_iso = db.advance_recurring_scheduled_job(jid)
        self.assertIsNotNone(next_iso)
        self.assertGreater(int(next_iso[:4]), 2000)

        # Bug #3: Clipboard copy helpers expand Spintax & {page_name} variables
        self.win.queued_comments = ["{ทักแชท|สอบถาม} {page_name}"]
        self.win._refresh_queued_comments()
        self.win.copy_next_queued_comment()
        clip_cmt = main.QApplication.clipboard().text()
        self.assertIn("My ", clip_cmt)
        self.assertNotIn("{", clip_cmt)
        self.assertNotIn("|", clip_cmt)

        self.win._set_all_accounts(True)
        self.win.input_share_url.setText("https://www.facebook.com/101/posts/1")
        self.win.editor_share_caption.setPlainText("{โปรดี|ลดแรง} {page_name}")
        self.win.copy_share_text()
        clip_share = main.QApplication.clipboard().text()
        self.assertIn("https://www.facebook.com/101/posts/1", clip_share)
        self.assertIn("My ", clip_share)
        self.assertNotIn("{", clip_share)
        self.assertNotIn("|", clip_share)

    def test_round38_bulk_edit_and_media_replace(self):
        from PySide6.QtGui import QImage

        # 1) Multi-page post batch assigns shared batch_id & select_same_batch_history selects all pages in batch
        db.clear_post_history()
        self.win._set_all_accounts(True)
        self.win.editor.setPlainText("โพสต์ต้นฉบับหลายเพจ")
        with mock.patch(
            "facebook.post_to_page",
            side_effect=lambda pid, msg, **kw: f"{pid}_ORIG_1",
        ):
            self.win.post_selected()
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(5000)
            self.app.processEvents()

        entries = db.list_post_history()
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0]["batch_id"])
        self.assertEqual(entries[0]["batch_id"], entries[1]["batch_id"])

        self.win.refresh_history()
        self.win.tbl_history.selectRow(0)
        self.win.select_same_batch_history()
        self.assertEqual(len(self.win._selected_history_ids()), 2)

        # 2) Bulk Multi-Page In-Place Text Edit (even when starting from 1 selected row via auto batch toggle)
        self.win.tbl_history.clearSelection()
        self.win.tbl_history.selectRow(0)
        edit_calls: list[tuple[str, str, str]] = []

        def fake_edit_post(pid, post_id, new_msg, **kw):
            edit_calls.append((pid, post_id, new_msg))
            return True

        orig_exec = main.EditPostDialog.exec

        def auto_accept_text_edit(dlg_self):
            # Verify whole-batch switch is auto-checked for 2 pages & no destructive media replace controls exist
            self.assertIsNotNone(dlg_self.chk_edit_whole_batch)
            self.assertTrue(dlg_self.chk_edit_whole_batch.isChecked())
            self.assertEqual(len(dlg_self.target_entries()), 2)
            self.assertFalse(hasattr(dlg_self, "will_replace_post"))
            self.assertTrue(hasattr(dlg_self, "btn_open_fb_web"))
            dlg_self.editor.setPlainText("ข้อความแก้ใหม่สำหรับ {page_name}")
            return main.QDialog.Accepted

        with (
            mock.patch.object(main.EditPostDialog, "exec", auto_accept_text_edit),
            mock.patch("facebook.edit_post", side_effect=fake_edit_post),
            mock.patch("facebook.delete_post") as m_never_del,
        ):
            self.win.edit_selected_history_post()
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(5000)
            self.app.processEvents()

        self.assertEqual(len(edit_calls), 2)
        m_never_del.assert_not_called()
        for e in db.list_post_history():
            self.assertIn(e["page_name"], e["message"])
            self.assertIn("ข้อความแก้ใหม่สำหรับ", e["message"])
            self.assertTrue(e["remote_post_id"].endswith("_ORIG_1"))

        # 3) Bulk Multi-Page Delete from Facebook
        self.win.tbl_history.selectRow(0)
        self.win.select_same_batch_history()
        bulk_del_calls: list[tuple[str, str]] = []
        with (
            mock.patch.object(
                main.QMessageBox, "question", return_value=main.QMessageBox.Yes
            ),
            mock.patch(
                "facebook.delete_post",
                side_effect=lambda pid, post_id: (bulk_del_calls.append((pid, post_id)), True)[1],
            ),
        ):
            self.win.delete_selected_history_from_fb()
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(5000)
            self.app.processEvents()

        self.assertEqual(len(bulk_del_calls), 2)
        self.assertEqual(len(db.list_post_history()), 0)

        # 4) edit_selected_page_post (from View 4: Comments tab) edits text in-place without deleting original post
        db.add_post_history(
            platform=facebook.PLATFORM,
            page_id="101",
            page_name="My Page 1",
            message="โพสต์จากหน้าคอมเมนต์",
            remote_post_id="101_PAGE_ORIG",
            status="published",
        )
        self.win.refresh_history()
        self.win.list_page_posts.clear()
        item = main.QListWidgetItem("[2026-11-22 02:00] โพสต์จากหน้าคอมเมนต์")
        item.setData(main.Qt.UserRole, "101_PAGE_ORIG")
        item.setData(main.Qt.UserRole + 1, "โพสต์จากหน้าคอมเมนต์")
        item.setData(main.Qt.UserRole + 2, "2026-11-22 02:00")
        item.setData(main.Qt.UserRole + 3, ("101", "My Page 1"))
        self.win.list_page_posts.addItem(item)
        self.win.list_page_posts.setCurrentItem(item)
        self.win.input_comment_post_id.setText("101_PAGE_ORIG")

        def auto_accept_page_post_edit(dlg_self):
            dlg_self.editor.setPlainText("แก้ไขข้อความจากหน้า 4 เรียบร้อย")
            return main.QDialog.Accepted

        with (
            mock.patch.object(main.EditPostDialog, "exec", auto_accept_page_post_edit),
            mock.patch("facebook.edit_post", return_value=True) as m_page_edit,
            mock.patch("facebook.delete_post") as m_page_del,
        ):
            self.win.edit_selected_page_post()
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(5000)
            self.app.processEvents()

        m_page_edit.assert_called_once_with(
            "101", "101_PAGE_ORIG", "แก้ไขข้อความจากหน้า 4 เรียบร้อย", is_video=False
        )
        m_page_del.assert_not_called()
        self.assertEqual(self.win.input_comment_post_id.text(), "101_PAGE_ORIG")
        self.assertEqual(
            self.win.list_page_posts.currentItem().data(main.Qt.UserRole),
            "101_PAGE_ORIG",
        )
        updated_hist = db.get_post_history_by_remote_id(
            facebook.PLATFORM, "101", "101_PAGE_ORIG"
        )
        self.assertIsNotNone(updated_hist)
        self.assertEqual(updated_hist["message"], "แก้ไขข้อความจากหน้า 4 เรียบร้อย")

    def test_round39_bug_hunt_fixes(self):
        from datetime import datetime, timedelta
        from PySide6.QtGui import QImage

        pic_path = Path(self.td.name) / "r39_pic.png"
        qimg = QImage(32, 32, QImage.Format_ARGB32)
        qimg.fill(0xFF10B981)
        self.assertTrue(qimg.save(str(pic_path)))

        # Bug #1: EditPostDialog is strictly in-place text editor with open-on-Facebook-web button
        dlg = main.EditPostDialog(
            "My Page 1",
            "101_TEST_1",
            "ข้อความเดิม",
            parent=self.win,
            page_id="101",
            current_place_id="1155726107773264",
            current_place_name="สยามพารากอน",
        )
        self.assertFalse(hasattr(dlg, "chk_replace_media"))
        self.assertFalse(hasattr(dlg, "drop_zone"))
        self.assertFalse(hasattr(dlg, "combo_activity_action"))
        opened_urls: list[str] = []
        with mock.patch("main.webbrowser.open", side_effect=opened_urls.append):
            dlg._open_post_on_facebook()
        self.assertEqual(opened_urls, ["https://www.facebook.com/101/posts/TEST_1"])
        dlg.close()

        # Bug #2: Editing a scheduled post updates text in-place while preserving 'scheduled' status & Post ID
        from datetime import datetime, timedelta

        db.clear_post_history()
        future_iso = (datetime.now().astimezone() + timedelta(hours=3)).isoformat(
            timespec="seconds"
        )
        sched_entry_id = db.add_post_history(
            platform=facebook.PLATFORM,
            page_id="101",
            page_name="My Page 1",
            message="โพสต์ตั้งเวลาล่วงหน้า",
            scheduled_for=future_iso,
            status="scheduled",
            remote_post_id="101_SCHED_ORIG",
            batch_id="batch_sched_r39",
        )
        self.win.refresh_history()
        self.win.tbl_history.selectRow(0)

        def auto_accept_sched_text_edit(dlg_self):
            dlg_self.editor.setPlainText("โพสต์ตั้งเวลาแก้ข้อความแล้ว")
            return main.QDialog.Accepted

        with (
            mock.patch.object(main.EditPostDialog, "exec", auto_accept_sched_text_edit),
            mock.patch("facebook.edit_post", return_value=True) as m_sched_edit,
            mock.patch("facebook.delete_post") as m_sched_del,
        ):
            self.win.edit_selected_history_post()
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(5000)
            self.app.processEvents()

        m_sched_edit.assert_called_once()
        m_sched_del.assert_not_called()
        updated_sched = db.get_post_history(sched_entry_id)
        self.assertEqual(updated_sched["status"], "scheduled")
        self.assertEqual(updated_sched["remote_post_id"], "101_SCHED_ORIG")
        self.assertEqual(updated_sched["message"], "โพสต์ตั้งเวลาแก้ข้อความแล้ว")

        # Bug #3: edit_selected_page_post (View 4) supports multi-page batch editing across same batch_id
        db.clear_post_history()
        db.add_post_history(
            platform=facebook.PLATFORM,
            page_id="101",
            page_name="My Page 1",
            message="ชุดโพสต์ 2 เพจ",
            status="published",
            remote_post_id="101_B4",
            batch_id="batch_v4_r39",
        )
        db.add_post_history(
            platform=facebook.PLATFORM,
            page_id="202",
            page_name="My Page 2",
            message="ชุดโพสต์ 2 เพจ",
            status="published",
            remote_post_id="202_B4",
            batch_id="batch_v4_r39",
        )
        self.win.refresh_history()
        self.win.list_page_posts.clear()
        it_v4 = main.QListWidgetItem("[2026-11-22 02:30] ชุดโพสต์ 2 เพจ")
        it_v4.setData(main.Qt.UserRole, "101_B4")
        it_v4.setData(main.Qt.UserRole + 1, "ชุดโพสต์ 2 เพจ")
        it_v4.setData(main.Qt.UserRole + 2, "2026-11-22 02:30")
        it_v4.setData(main.Qt.UserRole + 3, ("101", "My Page 1"))
        self.win.list_page_posts.addItem(it_v4)
        self.win.list_page_posts.setCurrentItem(it_v4)

        v4_edited_pages: list[str] = []

        def auto_accept_v4_batch(dlg_self):
            self.assertIsNotNone(dlg_self.chk_edit_whole_batch)
            self.assertTrue(dlg_self.chk_edit_whole_batch.isChecked())
            self.assertEqual(len(dlg_self.target_entries()), 2)
            dlg_self.editor.setPlainText("แก้จากหน้า 4 ทีเดียว 2 เพจ ({page_name})")
            return main.QDialog.Accepted

        with (
            mock.patch.object(main.EditPostDialog, "exec", auto_accept_v4_batch),
            mock.patch(
                "facebook.edit_post",
                side_effect=lambda pid, post_id, msg, **kw: (v4_edited_pages.append(pid), True)[1],
            ),
        ):
            self.win.edit_selected_page_post()
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(5000)
            self.app.processEvents()

        self.assertEqual(sorted(v4_edited_pages), ["101", "202"])

        # Bug #4: EditQueueJobDialog (View 2: Local Scheduled Queue) supports adding/changing photos & video
        q_job_id = db.add_scheduled_job(
            run_at_iso=future_iso,
            message="คิวตั้งเวลารอเพิ่มรูป",
            targets=[{"platform": "facebook", "page_id": "101", "name": "My Page 1"}],
        )
        q_job = db.get_scheduled_job(q_job_id)
        q_dlg = main.EditQueueJobDialog(q_job, parent=self.win)
        q_dlg._add_image_paths([str(pic_path)])
        vals = q_dlg.updated_values()
        q_dlg.close()
        self.assertEqual(vals["image_paths"], [str(pic_path)])
        db.update_scheduled_job(
            q_job_id,
            run_at_iso=vals["run_at_iso"],
            message=vals["message"],
            link=vals["link"],
            repeat_rule=vals["repeat_rule"],
            watermark_text=vals["watermark_text"],
            image_paths=vals["image_paths"],
            video_path=vals["video_path"],
        )
        reloaded_q = db.get_scheduled_job(q_job_id)
        self.assertEqual(reloaded_q["image_paths"], [str(pic_path)])

    def test_round40_checkin_and_activity(self):
        from datetime import datetime, timedelta
        from PySide6.QtGui import QImage

        # 1) Place/Page ID extraction from numeric ID & Facebook URLs
        self.assertEqual(
            facebook.extract_place_or_page_id("1155726107773264"),
            "1155726107773264",
        )
        self.assertEqual(
            facebook.extract_place_or_page_id("https://www.facebook.com/pages/Siam-Paragon/123456789012/"),
            "123456789012",
        )
        self.assertEqual(
            facebook.extract_place_or_page_id("https://www.facebook.com/profile.php?id=987654321098"),
            "987654321098",
        )
        self.assertEqual(
            facebook.extract_place_or_page_id("https://www.facebook.com/p/Xbox-100064123456789/"),
            "100064123456789",
        )
        with self.assertRaises(ValueError):
            facebook.extract_place_or_page_id("https://evil.example.com/123456789")

        # 2) facebook.search_places Graph API lookup
        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PTOKEN"),
            mock.patch.object(
                facebook,
                "_graph_get",
                return_value={
                    "data": [
                        {
                            "id": "1155726107773264",
                            "name": "Siam Paragon",
                            "category": "Shopping Mall",
                            "location": {"city": "Bangkok", "country": "Thailand"},
                        }
                    ]
                },
            ),
        ):
            found = facebook.search_places("101", "Siam Paragon")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["id"], "1155726107773264")
        self.assertIn("Bangkok", found[0]["location"])

        # 3) facebook.post_to_page with Check-in + Activity across feed & single photo
        pic_path = Path(self.td.name) / "r40_pic.png"
        qimg = QImage(32, 32, QImage.Format_ARGB32)
        qimg.fill(0xFF3B82F6)
        self.assertTrue(qimg.save(str(pic_path)))

        posted_calls: list[tuple[str, dict]] = []

        def fake_graph_post(path, token=None, data=None, files=None, **kw):
            posted_calls.append((path, dict(data or {})))
            if path.endswith("/photos") and (data or {}).get("published") == "false":
                return {"id": "MEDIA_777"}
            return {"id": "101_POST_CTX"}

        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PTOKEN"),
            mock.patch.object(facebook, "_graph_post", side_effect=fake_graph_post),
        ):
            # Feed post with Check-in (legacy og_* args are ignored and never sent to Graph API)
            pid_feed = facebook.post_to_page(
                "101",
                "เช็คอินกัน!",
                place_id="1155726107773264",
                og_action_type_id="520095228026772",
                og_object_id="16547831029",
            )
            self.assertEqual(pid_feed, "101_POST_CTX")
            self.assertEqual(posted_calls[-1][0], "101/feed")
            self.assertEqual(posted_calls[-1][1]["place"], "1155726107773264")
            self.assertNotIn("og_action_type_id", posted_calls[-1][1])
            self.assertNotIn("og_object_id", posted_calls[-1][1])

            # Single photo with Check-in -> /photos with place (no og_* params)
            facebook.post_to_page(
                "101",
                "รูปเช็คอิน",
                image_paths=[str(pic_path)],
                place_id="1155726107773264",
                og_action_type_id="520095228026772",
                og_object_id="16547831029",
            )
            self.assertEqual(posted_calls[-1][0], "101/photos")
            self.assertEqual(posted_calls[-1][1]["place"], "1155726107773264")
            self.assertNotIn("og_action_type_id", posted_calls[-1][1])

        # 4) Saved places presets CRUD
        p_id = db.save_place_preset("place", "สยามพารากอน", "1155726107773264")
        self.assertEqual(len(db.list_place_presets("place")), 1)
        db.delete_place_preset(p_id)
        self.assertEqual(len(db.list_place_presets()), 0)

        # 5) MainWindow Composer + Live Preview + Multi-Page Post + History + Repost + Queue Edit (Check-in only; Activity removed)
        db.clear_post_history()
        self.assertFalse(hasattr(self.win, "combo_activity_action"))
        self.win._set_all_accounts(True)
        self.win.editor.setPlainText("เช็คอินสยามพารากอน")
        self.win._set_composer_context(
            place_id="1155726107773264",
            place_name="สยามพารากอน",
        )
        self.assertFalse(self.win.post_preview.lbl_context_meta.isHidden())
        ctx_preview_txt = self.win.post_preview.lbl_context_meta.text()
        self.assertIn("สยามพารากอน", ctx_preview_txt)

        post_kwargs_seen: list[dict] = []

        def capture_post(pid, msg, **kw):
            post_kwargs_seen.append(dict(kw))
            return f"{pid}_CTX_1"

        with mock.patch("facebook.post_to_page", side_effect=capture_post):
            self.win.post_selected()
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(5000)
            self.app.processEvents()

        self.assertEqual(len(post_kwargs_seen), 2)
        for kw in post_kwargs_seen:
            self.assertEqual(kw["place_id"], "1155726107773264")
            self.assertEqual(kw["og_action_type_id"], "")
            self.assertEqual(kw["og_object_id"], "")

        entries = db.list_post_history()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["place_id"], "1155726107773264")

        # Composer cleared after post; repost_selected_history restores Check-in
        self.assertEqual(self.win.input_place_id.text(), "")
        self.win.tbl_history.selectRow(0)
        self.win.repost_selected_history()
        self.assertEqual(self.win.input_place_id.text(), "1155726107773264")
        self.assertEqual(self.win.input_place_name.text(), "สยามพารากอน")

        # EditQueueJobDialog persists Check-in (without removed Activity controls)
        future_iso = (datetime.now().astimezone() + timedelta(hours=2)).isoformat(
            timespec="seconds"
        )
        qid = db.add_scheduled_job(
            run_at_iso=future_iso,
            message="คิวเช็คอิน",
            targets=[{"platform": "facebook", "page_id": "101", "name": "My Page 1"}],
        )
        q_dlg = main.EditQueueJobDialog(db.get_scheduled_job(qid), parent=self.win)
        self.assertFalse(hasattr(q_dlg, "combo_activity_action"))
        q_dlg.input_place_id.setText("1155726107773264")
        q_dlg.input_place_name.setText("สยามพารากอน")
        q_vals = q_dlg.updated_values()
        q_dlg.close()
        self.assertEqual(q_vals["place_id"], "1155726107773264")
        self.assertEqual(q_vals["og_action_type_id"], "")
        self.assertEqual(q_vals["og_object_id"], "")

    def test_round42_bug_hunt_fixes(self):
        from datetime import datetime, timedelta

        # Bug #1: extract_place_or_page_id rejects Thai Unicode digits & bogus non-FB strings; accepts fb.com without scheme
        with self.assertRaises(ValueError):
            facebook.extract_place_or_page_id("๑๒๓๔๕๖")
        with self.assertRaises(ValueError):
            facebook.extract_place_or_page_id("not-a-url-1234567")
        self.assertEqual(
            facebook.extract_place_or_page_id("fb.com/1234567890"),
            "1234567890",
        )

        # Bug #2: Composer View (Page 0) uses scroll area so minimumSizeHint stays compact & editor >= 100px tall
        page0_min = self.win.pages_stack.widget(0).minimumSizeHint()
        self.assertLess(page0_min.width(), 900)
        self.assertLess(page0_min.height(), 400)
        self.win.resize(1260, 780)
        self.win.show()
        self.app.processEvents()
        self.assertGreaterEqual(self.win.editor.height(), 100)
        self.win.hide()

        # Bug #3 & #4: EditPostDialog accepts valid text and rejects empty text without touching media
        fake_vid = Path(self.td.name) / "r42_clip.mp4"
        fake_vid.write_bytes(b"fake_mp4_bytes")
        dlg_norm = main.EditPostDialog(
            "My Page 1",
            "101_R42",
            "ข้อความเดิม",
            parent=self.win,
            page_id="101",
            current_place_id="1155726107773264",
        )
        dlg_norm.editor.setPlainText("ข้อความใหม่หลังแก้")
        dlg_norm._validate_and_accept()
        self.assertEqual(dlg_norm.result(), main.QDialog.Accepted)
        dlg_norm.close()

        # Video + Check-in in MainWindow.post_selected() proceeds directly without blocking popup
        self.win._set_all_accounts(True)
        self.win.editor.setPlainText("ทดสอบวิดีโอกับเช็คอิน")
        self.win.set_attached_video(str(fake_vid))
        self.win.input_place_id.setText("1155726107773264")
        with (
            mock.patch.object(main.QMessageBox, "question") as m_q2,
            mock.patch("facebook.post_to_page", return_value="101_VID_CTX_R42") as m_post,
        ):
            self.win.post_selected()
            m_q2.assert_not_called()
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(5000)
            self.app.processEvents()
            self.assertEqual(m_post.call_count, 2)
        self.win._clear_composer()

        # Bug #5: EditQueueJobDialog rejects missing attached image/video files on disk
        future_iso = (datetime.now().astimezone() + timedelta(hours=3)).isoformat(
            timespec="seconds"
        )
        q_dlg = main.EditQueueJobDialog(
            {
                "id": 77,
                "run_at": future_iso,
                "message": "คิวทดสอบไฟล์หาย",
                "image_paths": [str(Path(self.td.name) / "missing_photo_r42.png")],
            },
            parent=self.win,
        )
        with mock.patch.object(main.QMessageBox, "warning") as m_warn:
            q_dlg._validate_and_accept()
            m_warn.assert_called_once()
            self.assertEqual(q_dlg.result(), 0)
        q_dlg.close()

    def test_round43_bug_hunt_fixes(self):
        from datetime import datetime, timedelta

        # Bug #1: extract_post_id_from_input rejects Thai Unicode digits in URLs
        for bad_url in (
            "https://www.facebook.com/watch/?v=๑๒๓๔๕๖",
            "https://www.facebook.com/MyPage/posts/๑๒๓๔๕๖",
            "https://www.facebook.com/MyPage/videos/๑๒๓๔๕๖",
            "https://www.facebook.com/MyPage/photos/a.1/๑๒๓๔๕๖",
        ):
            with self.assertRaises(ValueError):
                facebook.extract_post_id_from_input("101", bad_url)

        # Bug #2: refresh_scheduled_queue renders Check-in/Activity snippet & badges
        future_iso = (datetime.now().astimezone() + timedelta(hours=4)).isoformat(
            timespec="seconds"
        )
        qid = db.add_scheduled_job(
            run_at_iso=future_iso,
            message="",
            targets=[{"platform": "facebook", "page_id": "101", "name": "My Page 1"}],
            place_id="1155726107773264",
            place_name="สยามพารากอน",
            og_action_type_id="520095228026772",
            og_object_id="16547831029",
            og_label="XBOX",
        )
        self.win.refresh_scheduled_queue()
        found_row = -1
        for r in range(self.win.tbl_queue.rowCount()):
            if int(self.win.tbl_queue.item(r, 0).data(main.Qt.UserRole)) == qid:
                found_row = r
                break
        self.assertGreaterEqual(found_row, 0)
        self.assertIn("กำลังเล่น XBOX", self.win.tbl_queue.item(found_row, 3).text())
        media_cell = self.win.tbl_queue.item(found_row, 4).text()
        self.assertIn("📍 เช็คอิน", media_cell)
        self.assertIn("🎯 กิจกรรม", media_cell)

        # Bug #3: Video post preserves Check-in/Activity metadata in post_history when posted successfully
        db.clear_post_history()
        fake_vid = Path(self.td.name) / "r43_vid.mp4"
        fake_vid.write_bytes(b"mp4bytes")
        with mock.patch("facebook.post_to_page", return_value="101_VID_R43"):
            self.win._execute_post_batch(
                targets=[("facebook", "101", "My Page 1")],
                message="คลิปวิดีโอ",
                link="",
                images=[],
                video_path=str(fake_vid),
                scheduled_dt=None,
                place_id="1155726107773264",
                place_name="สยามพารากอน",
                og_action_type_id="520095228026772",
                og_object_id="16547831029",
                og_label="XBOX",
                clear_composer_on_success=False,
            )
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(5000)
            self.app.processEvents()
        hist = db.list_post_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["place_id"], "1155726107773264")
        self.assertEqual(hist[0]["og_action_type_id"], "520095228026772")

        # Bug #4: CSV import supports Check-in and safely ignores legacy deprecated og_* columns
        csv_ok = Path(self.td.name) / "r43_ctx.csv"
        csv_ok.write_text(
            "run_at,message,place_id,place_name,og_action_type_id,og_object_id,og_label\n"
            "2026-12-15 10:00,,1155726107773264,สยามพารากอน,520095228026772,16547831029,XBOX\n",
            encoding="utf-8-sig",
        )
        added_cnt = main.import_scheduled_jobs_from_csv(
            str(csv_ok), [{"platform": "facebook", "page_id": "101", "name": "My Page 1"}]
        )
        self.assertEqual(added_cnt, 1)
        imported = [j for j in db.list_scheduled_jobs() if j["run_at"].startswith("2026-12-15T10:00")][0]
        self.assertEqual(imported["place_id"], "1155726107773264")
        self.assertEqual(imported["og_action_type_id"], "")
        self.assertEqual(imported["og_object_id"], "")

        # Row with ONLY legacy og_* and no message/media/place is safely skipped (not imported as empty post)
        csv_legacy_only = Path(self.td.name) / "r43_legacy_only.csv"
        csv_legacy_only.write_text(
            "run_at,message,og_action_type_id,og_object_id\n"
            "2026-12-15 11:00,,520095228026772,16547831029\n",
            encoding="utf-8-sig",
        )
        self.assertEqual(
            main.import_scheduled_jobs_from_csv(
                str(csv_legacy_only), [{"platform": "facebook", "page_id": "101", "name": "My Page 1"}]
            ),
            0,
        )

    def test_round44_bug_hunt_fixes(self):
        # Bug #1: config._load_env_file handles cp874 Thai .env cleanly, ASCII graph version, atomic unique tmp
        env_cp874 = Path(self.td.name) / "thai_cp874.env"
        env_cp874.write_bytes("# คอมเมนต์ภาษาไทย\nFB_APP_ID=123456789\n".encode("cp874"))
        parsed_env = config._load_env_file(env_cp874)
        self.assertEqual(parsed_env.get("FB_APP_ID"), "123456789")
        self.assertEqual(config._normalize_graph_version("v๒๒.๐"), config.DEFAULT_GRAPH_VERSION)

        cfg_path = Path(self.td.name) / "r44_settings.json"
        with mock.patch.object(config, "SETTINGS_PATH", cfg_path):
            config._save_settings({"theme": "dark"})
        self.assertTrue(cfg_path.is_file())
        self.assertEqual(list(Path(self.td.name).glob("*.tmp")), [])

        # Bug #2: facebook.build_facebook_share_dialog_url rejects whitespace & user:pass@host
        for bad_share_url in (
            "https://example.com/a b",
            "https://user:pass@example.com/post/1",
            "https://facebook.com/p\n1",
        ):
            with self.assertRaises(ValueError):
                facebook.build_facebook_share_dialog_url(bad_share_url)
        self.assertEqual(
            facebook.build_post_permalink("101", "๑๒๓๔๕"),
            "https://www.facebook.com/๑๒๓๔๕",
        )

        # Bug #3: SettingsDialog ignores Thai digits in App ID when opening Meta Developers URL
        sdlg = main.SettingsDialog(parent=self.win)
        sdlg.input_app_id.setText("๑๒๓๔๕")
        opened_urls: list[str] = []
        with mock.patch("main.webbrowser.open", side_effect=opened_urls.append):
            sdlg._open_meta_basic_page()
            sdlg._open_meta_login_settings()
        sdlg.close()
        self.assertEqual(
            opened_urls,
            ["https://developers.facebook.com/apps/", "https://developers.facebook.com/apps/"],
        )

        # Bug #4: oauth_backend/app.py enforces https scheme on pagination next & ASCII graph version
        backend_src = (
            Path(__file__).resolve().parent.parent / "oauth_backend" / "app.py"
        ).read_text(encoding="utf-8")
        self.assertIn('parsed.scheme != "https" or parsed.netloc.lower() != "graph.facebook.com"', backend_src)
        self.assertIn(r'r"v[0-9]{1,2}\.[0-9]{1,2}"', backend_src)

    def test_round45_bug_hunt_fixes(self):
        # Bug #1: _graph_get rejects http:// URLs and wraps RequestException into clean Thai RuntimeError
        with self.assertRaises(RuntimeError) as ctx_http:
            facebook._graph_get("http://graph.facebook.com/v22.0/me/accounts", token="TOK")
        self.assertIn("http://", str(ctx_http.exception))

        with mock.patch(
            "facebook.requests.get", side_effect=requests.ConnectionError("offline")
        ):
            with self.assertRaises(RuntimeError) as ctx_net:
                facebook._graph_get("me", token="TOK")
            self.assertIn("เชื่อมต่อ Facebook ไม่สำเร็จ", str(ctx_net.exception))

        # Bug #2: quick_comment_open_browser rejects non-Facebook / non-PostID text instead of opening 404 page
        self.win.input_comment_post_id.setText("google.com/not-a-fb-post")
        with (
            mock.patch.object(main.QMessageBox, "warning") as m_warn,
            mock.patch("main.webbrowser.open") as m_open,
        ):
            self.win.quick_comment_open_browser()
            m_warn.assert_called_once()
            m_open.assert_not_called()

    def test_round48_clean_api_alignment_no_og_and_text_only_edit(self):
        from PySide6.QtGui import QImage

        pic1 = Path(self.td.name) / "r48_pic1.png"
        qimg = QImage(32, 32, QImage.Format_ARGB32)
        qimg.fill(0xFF3B82F6)
        self.assertTrue(qimg.save(str(pic1)))
        vid1 = Path(self.td.name) / "r48_clip.mp4"
        vid1.write_bytes(b"fake_mp4_payload")

        # 1) Video + Check-in uploads directly to /{page_id}/videos with place and NEVER sends deprecated og_*
        calls: list[tuple[str, dict]] = []

        def fake_post_v(path, token=None, data=None, files=None, **kw):
            calls.append((path, dict(data or {})))
            return {"id": "3021009801590375"}

        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PTOK"),
            mock.patch.object(facebook, "_graph_post", side_effect=fake_post_v),
        ):
            pid_out = facebook.post_to_page(
                "101",
                "โพสต์คลิปพร้อมเช็คอิน",
                video_path=str(vid1),
                place_id="474723252",
                og_action_type_id="520095228026772",
                og_object_id="16547831029",
            )
        self.assertEqual(pid_out, "3021009801590375")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "101/videos")
        self.assertEqual(calls[0][1]["place"], "474723252")
        self.assertNotIn("og_action_type_id", calls[0][1])
        self.assertNotIn("og_object_id", calls[0][1])

        # 2) Mixing images + video raises clear ValueError (and Composer switches cleanly with status note)
        with mock.patch.object(facebook, "_get_page_token", return_value="PTOK"):
            with self.assertRaises(ValueError):
                facebook.post_to_page(
                    "101", "ผสมรูป+คลิป", image_paths=[str(pic1)], video_path=str(vid1)
                )
        self.win._add_image_paths([str(pic1)])
        self.assertEqual(self.win.attached_images, [str(pic1)])
        self.win.set_attached_video(str(vid1))
        self.assertEqual(self.win.attached_images, [])
        self.assertEqual(self.win.attached_video, str(vid1))
        self.assertIn("ไม่รองรับการผสมรูปภาพ+วิดีโอ", self.win.status.text())
        self.win._add_image_paths([str(pic1)])
        self.assertEqual(self.win.attached_video, "")
        self.assertEqual(self.win.attached_images, [str(pic1)])
        self.assertIn("ไม่รองรับการผสมรูปภาพ+วิดีโอ", self.win.status.text())
        self.win._clear_composer()

        # 3) Editing a published Video post sends `description` directly and NEVER calls GET ?fields=attachments
        edit_calls: list[tuple[str, dict]] = []

        def fake_video_edit(path, token=None, data=None, **kw):
            edit_calls.append((path, dict(data or {})))
            return {"success": True}

        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PTOK"),
            mock.patch.object(facebook, "_graph_post", side_effect=fake_video_edit),
            mock.patch.object(facebook, "_graph_get") as m_never_get,
        ):
            ok = facebook.edit_post("101", "3021009801590375", "แก้แคปชันวิดีโอ", is_video=True)
        self.assertTrue(ok)
        self.assertEqual(edit_calls, [("3021009801590375", {"description": "แก้แคปชันวิดีโอ"})])
        m_never_get.assert_not_called()

        # 4) _run_bulk_post_edit NEVER deletes or replaces published posts — strictly updates text in-place
        db.clear_post_history()
        eid = db.add_post_history(
            platform=facebook.PLATFORM,
            page_id="101",
            page_name="My Page 1",
            message="โพสต์รูปพร้อมเช็คอิน",
            image_paths=[str(pic1)],
            place_id="1911903895735521",
            place_name="สยามพารากอน",
            remote_post_id="101_KEEP_SAFE",
            status="published",
        )
        entry = db.get_post_history(eid)
        with (
            mock.patch("facebook.edit_post", return_value=True) as m_edit_ok,
            mock.patch("facebook.delete_post") as m_del_never,
            mock.patch("facebook.post_to_page") as m_repost_never,
        ):
            self.win._run_bulk_post_edit([entry], raw_new_msg="ข้อความใหม่เท่านั้น")
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(5000)
            self.app.processEvents()

        m_edit_ok.assert_called_once_with("101", "101_KEEP_SAFE", "ข้อความใหม่เท่านั้น", is_video=False)
        m_del_never.assert_not_called()
        m_repost_never.assert_not_called()
        reloaded = db.get_post_history(eid)
        self.assertEqual(reloaded["remote_post_id"], "101_KEEP_SAFE")
        self.assertEqual(reloaded["message"], "ข้อความใหม่เท่านั้น")
        self.assertEqual(reloaded["image_paths"], [str(pic1)])
        self.assertEqual(reloaded["place_id"], "1911903895735521")

    def test_round49_page_analytics_comparison_and_rt_dashboard(self):
        from datetime import datetime, timezone

        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # 1) facebook.get_page_analytics: Meta deprecated metric fallback + post interaction rollup
        def fake_graph_get(path, token=None, params=None, **kw):
            params = params or {}
            if path == "101":
                return {"id": "101", "name": "My Page 1", "followers_count": 12500}
            if path == "101/posts":
                return {
                    "data": [
                        {
                            "id": "101_1",
                            "created_time": now_iso,
                            "reactions": {"summary": {"total_count": 120}},
                            "comments": {"summary": {"total_count": 30}},
                            "shares": {"count": 15},
                        }
                    ]
                }
            if path == "101/insights":
                m = params.get("metric")
                if m == "page_impressions_unique":
                    # Simulate Meta v22 deprecating page_impressions_unique -> must fall back cleanly!
                    raise RuntimeError("(#100) The value must be a valid insights metric")
                if m == "page_total_media_view_unique":
                    # 7 prev days (100/day = 700) + 7 cur days (200/day = 1400) -> +100.0%
                    return {"data": [{"values": [{"value": 100}] * 7 + [{"value": 200}] * 7}]}
                if m == "page_impressions":
                    return {"data": [{"values": [{"value": 300}] * 7 + [{"value": 500}] * 7}]}
                if m == "page_post_engagements":
                    # Simulate deprecated engagement rollup -> must compute from reactions+comments+shares+clicks
                    raise RuntimeError("(#100) The value must be a valid insights metric")
                if m == "page_consumptions_by_consumption_type":
                    return {"data": [{"values": [{"value": {"link clicks": 5}}] * 7}]}
            return {}

        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PTOK"),
            mock.patch.object(facebook, "_graph_get", side_effect=fake_graph_get),
        ):
            res = facebook.get_page_analytics("101", "My Page 1", period_days=7)

        self.assertEqual(res["reach"], 1400)
        self.assertEqual(res["prev_reach"], 700)
        self.assertEqual(res["reach_delta_pct"], 100.0)
        self.assertEqual(res["impressions"], 3500)
        self.assertEqual(res["reactions"], 120)
        self.assertEqual(res["comments"], 30)
        self.assertEqual(res["shares"], 15)
        self.assertEqual(res["link_clicks"], 35)
        self.assertEqual(res["engagement"], 120 + 30 + 15 + 35)
        self.assertEqual(res["followers"], 12500)
        self.assertEqual(res["status"], "ok")

        # 2) Fatal token error (#190) MUST re-raise immediately (never swallowed)
        with (
            mock.patch.object(facebook, "_get_page_token", return_value="BADTOK"),
            mock.patch.object(
                facebook,
                "_graph_get",
                side_effect=RuntimeError("(#190) Error validating access token"),
            ),
        ):
            with self.assertRaises(RuntimeError):
                facebook.get_page_analytics("101", "My Page 1", period_days=7)

        # 3) facebook.analyze_page_comparison identifies leaders, growth, drops, stale/error pages
        sample_rows = [
            {
                "page_id": "101",
                "page_name": "Page Alpha",
                "reach": 5000,
                "impressions": 9000,
                "engagement": 400,
                "reactions": 250,
                "comments": 80,
                "shares": 40,
                "link_clicks": 30,
                "followers": 10000,
                "reach_delta_pct": 25.0,
                "engagement_delta_pct": 10.0,
                "status": "ok",
            },
            {
                "page_id": "102",
                "page_name": "Page Beta",
                "reach": 2000,
                "impressions": 4000,
                "engagement": 950,
                "reactions": 600,
                "comments": 200,
                "shares": 100,
                "link_clicks": 50,
                "followers": 8000,
                "reach_delta_pct": -30.5,
                "engagement_delta_pct": 15.0,
                "status": "ok",
            },
            {
                "page_id": "103",
                "page_name": "Page Quiet",
                "reach": 0,
                "impressions": 0,
                "engagement": 0,
                "reactions": 0,
                "comments": 0,
                "shares": 0,
                "link_clicks": 0,
                "followers": 500,
                "reach_delta_pct": None,
                "engagement_delta_pct": None,
                "status": "no_data",
            },
        ]
        cmp_res = facebook.analyze_page_comparison(sample_rows)
        self.assertEqual(cmp_res["total_reach"], 7000)
        self.assertEqual(cmp_res["total_engagement"], 1350)
        self.assertEqual(cmp_res["top_reach"]["page_name"], "Page Alpha")
        self.assertEqual(cmp_res["top_engagement"]["page_name"], "Page Beta")
        self.assertEqual([r["page_name"] for r in cmp_res["declining_reach"]], ["Page Beta"])
        self.assertEqual([r["page_name"] for r in cmp_res["growing_pages"]], ["Page Alpha", "Page Beta"])
        self.assertEqual([r["page_name"] for r in cmp_res["stale_or_no_data"]], ["Page Quiet"])

        # 4) MainWindow RT Dashboard UI + Dedicated Worker + Scope Filter + Auto Refresh + CSV Export
        self.win._switch_page(5)
        self.assertEqual(self.win.pages_stack.currentIndex(), 5)

        def fake_get_analytics(pid, pname="", *, period_days=7, prev_snapshot=None):
            if pid == "101":
                return {**sample_rows[0], "platform": facebook.PLATFORM, "period_days": period_days, "prev_reach": 4000, "prev_engagement": 360, "latest_post_at": now_iso, "error": "", "fetched_at": now_iso}
            return {**sample_rows[1], "page_id": pid, "page_name": pname or "My Page 2", "platform": facebook.PLATFORM, "period_days": period_days, "prev_reach": 2878, "prev_engagement": 826, "latest_post_at": now_iso, "error": "", "fetched_at": now_iso}

        with mock.patch("facebook.get_page_analytics", side_effect=fake_get_analytics):
            self.win.refresh_page_analytics(silent=False)
            self.assertIsNotNone(self.win.analytics_worker)
            self.win.analytics_worker.wait(5000)
            self.app.processEvents()

        self.assertEqual(self.win.tbl_analytics.rowCount(), 2)
        self.assertIn("Page Alpha", self.win.lbl_cmp_top_reach.text())
        self.assertIn("-30.5%", self.win.lbl_cmp_declining.text())

        # Scope filter to only checked page (101)
        self.win._set_all_accounts(False)
        for i in range(self.win.accounts.count()):
            it = self.win.accounts.item(i)
            if it.data(main.Qt.UserRole)[1] == "101":
                it.setCheckState(main.Qt.Checked)
        self.win.combo_analytics_scope.setCurrentIndex(1)  # selected
        self.app.processEvents()
        self.assertEqual(self.win.tbl_analytics.rowCount(), 1)
        self.win.combo_analytics_scope.setCurrentIndex(0)  # back to all
        self.app.processEvents()
        self.assertEqual(self.win.tbl_analytics.rowCount(), 2)

        # Auto Refresh toggle starts/stops analytics_timer cleanly
        self.win.combo_analytics_interval.setCurrentIndex(0)  # 1 min
        self.win.chk_analytics_auto_refresh.setChecked(True)
        self.assertTrue(self.win.analytics_timer.isActive())
        self.assertEqual(self.win.analytics_timer.interval(), 60_000)
        self.win.chk_analytics_auto_refresh.setChecked(False)
        self.assertFalse(self.win.analytics_timer.isActive())

        # Export CSV (atomic write — no leftover .tmp_* files)
        csv_out = Path(self.td.name) / "analytics_export.csv"
        with mock.patch.object(
            main.QFileDialog, "getSaveFileName", return_value=(str(csv_out), "CSV (*.csv)")
        ):
            self.win.export_analytics_csv()
        self.assertTrue(csv_out.is_file())
        self.assertEqual(list(Path(self.td.name).glob("*.tmp_*")), [])
        csv_text = csv_out.read_text(encoding="utf-8-sig")
        self.assertIn("Reach,Reach Delta (%),Impressions,Engagement,Reactions,Comments,Shares,Link Clicks,Followers", csv_text)
        self.assertIn("Page Alpha", csv_text)

        # Removing an account immediately clears it from Analytics UI & cache
        self.win._set_all_accounts(False)
        for i in range(self.win.accounts.count()):
            it = self.win.accounts.item(i)
            if it.data(main.Qt.UserRole)[1] == "102":
                it.setCheckState(main.Qt.Checked)
        with (
            mock.patch.object(main.QMessageBox, "question", return_value=main.QMessageBox.Yes),
            mock.patch("main._forget_for_platform"),
        ):
            self.win.remove_selected()
        self.assertEqual(self.win.tbl_analytics.rowCount(), 1)
        self.assertEqual([r["page_id"] for r in self.win.analytics_rows], ["101"])
        # Restore page 102 for clean tearDown / isolation
        db.save_account(facebook.PLATFORM, "102", "My Page 2")
        self.win.refresh_accounts()

    def test_round50_bug_audit_fixes(self):
        import ast
        from datetime import datetime, timezone

        # 1) _split_period_totals rejects uneven windows (< 2*period_days) & get_page_analytics clamps lookback <= 90 days
        self.assertEqual(facebook._split_period_totals([10] * 90, 60), (600, 0))
        self.assertEqual(facebook._split_period_totals([10] * 14, 7), (70, 70))

        seen_ranges: list[tuple[int, int]] = []

        def fake_get_clamp(path, token=None, params=None, **kw):
            params = params or {}
            if path == "101":
                return {"id": "101", "name": "My Page 1", "followers_count": 100}
            if path == "101/posts":
                return {"data": []}
            if path == "101/insights":
                if "since" in params and "until" in params:
                    seen_ranges.append((int(params["since"]), int(params["until"])))
                return {"data": [{"values": [{"value": 10}] * 60}]}
            return {}

        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PTOK"),
            mock.patch.object(facebook, "_graph_get", side_effect=fake_get_clamp),
        ):
            res_60 = facebook.get_page_analytics("101", "My Page 1", period_days=60)
        self.assertTrue(seen_ranges)
        for s_ts, u_ts in seen_ranges:
            self.assertLessEqual(u_ts - s_ts, 90 * 86400 + 5)
        self.assertIsNone(res_60["reach_delta_pct"])

        # 2) db.get_latest_page_analytics excludes orphaned snapshots whose account was deleted
        db.save_page_analytics_snapshot(
            platform=facebook.PLATFORM,
            page_id="999_DELETED",
            page_name="Deleted Ghost Page",
            period_days=7,
            reach=9999,
            engagement=500,
            status="ok",
        )
        latest_ids = {r["page_id"] for r in db.get_latest_page_analytics(period_days=7)}
        self.assertNotIn("999_DELETED", latest_ids)

        # 3) Empty _render_analytics_dashboard shows neutral dashes, never false 'ปกติ ✅'
        self.win._render_analytics_dashboard([])
        self.assertEqual(self.win.tbl_analytics.rowCount(), 0)
        self.assertEqual(self.win.lbl_cmp_top_reach.text(), "🏆 Reach สูงสุด: —")
        self.assertEqual(self.win.lbl_cmp_declining.text(), "📉 Reach ลดลง: —")
        self.assertIn("—", self.win.lbl_cmp_stale.text())
        self.assertNotIn("ปกติ", self.win.lbl_cmp_stale.text())

        # 4) no_data row with permission warning shows error visibly in column 10
        now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.win._render_analytics_dashboard(
            [
                {
                    "page_id": "101",
                    "page_name": "My Page 1",
                    "reach": 0,
                    "impressions": 0,
                    "engagement": 0,
                    "reactions": 0,
                    "comments": 0,
                    "shares": 0,
                    "link_clicks": 0,
                    "followers": 100,
                    "reach_delta_pct": None,
                    "engagement_delta_pct": None,
                    "status": "no_data",
                    "error": "missing read_insights",
                    "fetched_at": now_iso,
                }
            ]
        )
        self.assertEqual(self.win.tbl_analytics.rowCount(), 1)
        self.assertIn("missing read_insights", self.win.tbl_analytics.item(0, 10).text())

        # 5) _parse_csv_datetime enforces re.ASCII (rejects Thai Unicode numerals cleanly)
        csv_thai = Path(self.td.name) / "thai_digits.csv"
        csv_thai.write_text("run_at,message\n๒๕๖๙-๑๑-๒๔ 10:00,ทดสอบเลขไทย\n", encoding="utf-8-sig")
        with self.assertRaises(ValueError):
            main.import_scheduled_jobs_from_csv(
                str(csv_thai), [{"platform": facebook.PLATFORM, "page_id": "101", "name": "My Page 1"}]
            )

        # 6) AST Zero-Tolerance check: zero `except Exception: pass` or bare `except: pass` in production .py files
        root_dir = Path(__file__).resolve().parent.parent
        for fn in ("config.py", "db.py", "facebook.py", "main.py", "ui_icons.py", "ui_theme.py", "ui_widgets.py"):
            tree = ast.parse((root_dir / fn).read_text(encoding="utf-8"), filename=fn)
            for node in ast.walk(tree):
                if isinstance(node, ast.ExceptHandler):
                    is_pass = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
                    if is_pass:
                        ename = ast.unparse(node.type) if node.type else "BARE"
                        self.assertNotIn(
                            ename,
                            ("Exception", "BaseException", "BARE"),
                            f"Forbidden silent swallow in {fn}:{node.lineno}",
                        )

    def test_round51_ui_polish_and_multipage_features(self):
        from datetime import datetime, timedelta
        import ui_theme

        # A1 & A3: Emoji font stack + transparent QScrollArea in both themes
        self.assertIn("Segoe UI Emoji", ui_theme.FONT_STACK)
        self.assertIn("QScrollArea", ui_theme.DARK_QSS)
        self.assertIn("QScrollArea", ui_theme.LIGHT_QSS)

        # A2: Composer has no horizontal scrollbar & Analytics table has 11 columns with header tooltips
        self.assertEqual(
            self.win.comp_scroll.horizontalScrollBarPolicy(), main.Qt.ScrollBarAlwaysOff
        )
        self.assertEqual(self.win.tbl_analytics.columnCount(), 11)
        for col in range(11):
            h_item = self.win.tbl_analytics.horizontalHeaderItem(col)
            self.assertIsNotNone(h_item)
            self.assertTrue(h_item.toolTip())

        # B4: Double-click row in tbl_analytics selects only that page and jumps to Composer
        self.win._switch_page(5)
        self.win._render_analytics_dashboard(
            [
                {
                    "platform": facebook.PLATFORM,
                    "page_id": "102",
                    "page_name": "My Page 2",
                    "reach": 500,
                    "impressions": 800,
                    "engagement": 50,
                    "reactions": 30,
                    "comments": 10,
                    "shares": 5,
                    "link_clicks": 5,
                    "followers": 1000,
                    "reach_delta_pct": 12.0,
                    "engagement_delta_pct": 5.0,
                    "status": "ok",
                }
            ]
        )
        self.win._on_analytics_row_double_clicked(0, 0)
        self.assertEqual(self.win.pages_stack.currentIndex(), 0)
        checked = self.win.checked_targets()
        self.assertEqual(len(checked), 1)
        self.assertEqual(checked[0][1], "102")

        # B4: _open_page_on_facebook validates Page ID and opens URL
        opened_urls: list[str] = []
        with mock.patch("main.webbrowser.open", side_effect=opened_urls.append):
            self.assertTrue(self.win._open_page_on_facebook("102"))
        self.assertEqual(opened_urls, ["https://www.facebook.com/102"])
        with mock.patch.object(main.QMessageBox, "warning") as m_bad_id:
            self.assertFalse(self.win._open_page_on_facebook("bad/id/../evil"))
            m_bad_id.assert_called_once()

        # B5: Startup auto token check runs silently on dedicated worker, updates badges, and persists across refresh_accounts()
        def fake_tok_health(pid: str):
            if pid == "101":
                return True, "Token ใช้งานได้ปกติ"
            return False, "Token หมดอายุ"

        with (
            mock.patch("facebook.check_page_token_health", side_effect=fake_tok_health),
            mock.patch.object(main.QMessageBox, "warning") as m_no_modal,
        ):
            self.win._run_startup_token_check()
            self.assertIsNotNone(self.win.token_check_worker)
            self.win.token_check_worker.wait(5000)
            self.app.processEvents()
            m_no_modal.assert_not_called()

        texts_by_pid = {
            self.win.accounts.item(i).data(main.Qt.UserRole)[1]: self.win.accounts.item(i).text()
            for i in range(self.win.accounts.count())
        }
        self.assertTrue(texts_by_pid["101"].startswith("🟢 "))
        self.assertTrue(texts_by_pid["102"].startswith("🔴 "))
        # Persists across refresh_accounts()
        self.win.refresh_accounts()
        texts_after = {
            self.win.accounts.item(i).data(main.Qt.UserRole)[1]: self.win.accounts.item(i).text()
            for i in range(self.win.accounts.count())
        }
        self.assertTrue(texts_after["101"].startswith("🟢 "))
        self.assertTrue(texts_after["102"].startswith("🔴 "))

        # B5: Single-page token check updates badge
        with mock.patch(
            "facebook.check_page_token_health", return_value=(True, "ต่ออายุแล้ว")
        ):
            self.win.check_single_page_token_health(facebook.PLATFORM, "102", "My Page 2")
            self.assertIsNotNone(self.win.token_check_worker)
            self.win.token_check_worker.wait(5000)
            self.app.processEvents()
        texts_single = {
            self.win.accounts.item(i).data(main.Qt.UserRole)[1]: self.win.accounts.item(i).text()
            for i in range(self.win.accounts.count())
        }
        self.assertTrue(texts_single["102"].startswith("🟢 "))

        # B6: Anti-spam stagger preset updates spinboxes, saves in draft, and staggers FB Native scheduled times
        self.win.combo_stagger_preset.setCurrentIndex(2)  # กันสแปม (30–90 วิ)
        self.assertEqual(self.win.spin_page_delay_min.value(), 30)
        self.assertEqual(self.win.spin_page_delay_max.value(), 90)
        self.win._save_draft()
        draft_saved = config.load_composer_draft()
        self.assertEqual(draft_saved.get("page_delay_min"), 30)
        self.assertEqual(draft_saved.get("page_delay_max"), 90)

        base_sched = datetime.now().astimezone() + timedelta(hours=2)
        seen_scheds: list[datetime] = []

        def capture_stagger_post(pid, msg, **kw):
            seen_scheds.append(kw["scheduled_time"])
            return f"{pid}_STAGGER_1"

        with mock.patch("facebook.post_to_page", side_effect=capture_stagger_post):
            self.win._execute_post_batch(
                targets=[
                    (facebook.PLATFORM, "101", "My Page 1"),
                    (facebook.PLATFORM, "102", "My Page 2"),
                ],
                message="โพสต์กระจายเวลา",
                link="",
                images=[],
                video_path="",
                scheduled_dt=base_sched,
                page_delay_min=60.0,
                page_delay_max=60.0,
                clear_composer_on_success=False,
            )
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(5000)
            self.app.processEvents()

        self.assertEqual(len(seen_scheds), 2)
        self.assertEqual(seen_scheds[0], base_sched)
        self.assertEqual(int((seen_scheds[1] - seen_scheds[0]).total_seconds()), 60)

    def test_round52_graph_get_timeout_and_signature_audit(self):
        import ast
        import inspect

        # 1) Real _graph_get accepts timeout=... and forwards it to requests.get
        seen_timeouts: list[int] = []

        class _FakeResp:
            status_code = 200
            text = '{"id": "101", "name": "Page 1", "followers_count": 42}'

            def json(self):
                return {"id": "101", "name": "Page 1", "followers_count": 42}

        def fake_req_get(url, **kw):
            seen_timeouts.append(kw.get("timeout"))
            return _FakeResp()

        with (
            mock.patch.object(facebook, "_get_page_token", return_value="PTOK"),
            mock.patch("facebook.requests.get", side_effect=fake_req_get),
        ):
            res = facebook.get_page_analytics("101", "Page 1", period_days=7)
        self.assertEqual(res["followers"], 42)
        self.assertIn(20, seen_timeouts)
        self.assertIn(25, seen_timeouts)

        # 2) Cross-module AST keyword-argument signature check across all production files
        root_dir = Path(__file__).resolve().parent.parent
        mods = {"config": config, "db": db, "facebook": facebook, "main": main}
        mismatches: list[str] = []
        for fn in ("config.py", "db.py", "facebook.py", "main.py", "ui_widgets.py"):
            tree = ast.parse((root_dir / fn).read_text(encoding="utf-8"), filename=fn)
            cur_mod = mods.get(fn[:-3])
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target_fn = None
                label = ""
                if isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name):
                    m = mods.get(node.func.value.id)
                    if m is not None and hasattr(m, node.func.attr):
                        cand = getattr(m, node.func.attr)
                        if inspect.isfunction(cand):
                            target_fn = cand
                            label = f"{node.func.value.id}.{node.func.attr}"
                elif isinstance(node.func, ast.Name) and cur_mod is not None and hasattr(cur_mod, node.func.id):
                    cand = getattr(cur_mod, node.func.id)
                    if inspect.isfunction(cand):
                        target_fn = cand
                        label = node.func.id
                if target_fn is None:
                    continue
                sig = inspect.signature(target_fn)
                if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()):
                    continue
                bad_kw = [k.arg for k in node.keywords if k.arg is not None and k.arg not in sig.parameters]
                if bad_kw:
                    mismatches.append(f"{fn}:{node.lineno} {label} invalid kwargs={bad_kw}")
        self.assertEqual(mismatches, [])

    def test_round53_reels_autoreply_presets_and_autobackup(self):
        import json
        import sqlite3
        from datetime import datetime, timedelta, timezone
        from PySide6.QtWidgets import QApplication

        # Ensure only account 101 ("My Shop Page") is checked for deterministic single-page assertions
        self.win._set_all_accounts(False)
        for idx in range(self.win.accounts.count()):
            it = self.win.accounts.item(idx)
            data = it.data(main.Qt.UserRole)
            if data and data[1] == "101":
                it.setCheckState(main.Qt.Checked)

        class _MockHttpResp:
            def __init__(self, payload: dict, status_code: int = 200):
                self._payload = payload
                self.status_code = status_code
                self.text = json.dumps(payload, ensure_ascii=False)

            def json(self):
                return self._payload

        # --- 0) _graph_get appsecret_proof fallback forwards custom timeout ---
        seen_get_timeouts: list = []
        get_attempt = {"n": 0}

        def fake_retry_get(url, **kw):
            seen_get_timeouts.append(kw.get("timeout"))
            get_attempt["n"] += 1
            if get_attempt["n"] == 1:
                return _MockHttpResp(
                    {"error": {"message": "Invalid appsecret_proof provided"}},
                    status_code=400,
                )
            return _MockHttpResp({"id": "101", "name": "My Shop Page"}, status_code=200)

        with (
            mock.patch.object(config, "get_auth_mode", return_value="self_app"),
            mock.patch.object(config, "get_fb_app_secret", return_value="top_secret"),
            mock.patch("facebook.requests.get", side_effect=fake_retry_get),
        ):
            r_retry = facebook._graph_get("101", "TOK_101", timeout=19)
        self.assertEqual(r_retry["id"], "101")
        self.assertEqual(seen_get_timeouts, [19, 19])

        # --- 1) Facebook Reels 3-step upload (Composer -> Worker -> post_to_page -> post_reel_to_page -> HTTP) ---
        reel_mp4 = Path(self.td.name) / "promo_reel.mp4"
        reel_bytes = b"\x00\x00\x00\x18ftypmp42" + (b"R" * 240)
        reel_mp4.write_bytes(reel_bytes)

        captured_posts: list[dict] = []

        def fake_reel_http_post(url, data=None, files=None, headers=None, timeout=None):
            raw_body = b""
            if hasattr(data, "read"):
                raw_body = data.read()
            captured_posts.append(
                {
                    "url": url,
                    "data": dict(data) if isinstance(data, dict) else None,
                    "raw_len": len(raw_body),
                    "raw_head": raw_body[:16],
                    "headers": dict(headers or {}),
                    "timeout": timeout,
                }
            )
            if "rupload.facebook.com" in url:
                return _MockHttpResp({"success": True})
            if isinstance(data, dict) and data.get("upload_phase") == "start":
                return _MockHttpResp(
                    {
                        "video_id": "987654321",
                        "upload_url": "https://rupload.facebook.com/video-upload/v22.0/987654321",
                    }
                )
            if isinstance(data, dict) and data.get("upload_phase") == "finish":
                return _MockHttpResp({"success": True, "post_id": "987654321"})
            return _MockHttpResp({"id": "comment_999"})

        # Verify Composer horizontal scroll safety at minimum window size (1020x660) with new controls
        self.win.resize(1020, 660)
        self.win.show()
        QApplication.processEvents()
        vp_w = self.win.comp_scroll.viewport().width()
        content_min_w = self.win.comp_scroll.widget().minimumSizeHint().width()
        self.win.hide()
        self.assertLessEqual(
            content_min_w,
            vp_w,
            f"Composer horizontal overflow: content={content_min_w} > viewport={vp_w}",
        )

        self.win.editor.setPlainText("โปรคลิปสั้น {เด็ดมาก|คุ้มสุด} เพจ {page_name}")
        self.win.input_link.setText("https://example.com/reel-deal")
        self.win.set_attached_video(str(reel_mp4))
        self.win.chk_video_reel.setChecked(True)
        QApplication.processEvents()
        self.assertIn("Reels", self.win.lbl_attached_video.text())
        self.assertIn("Reels", self.win.post_preview.lbl_video_badge.text())

        with (
            mock.patch("facebook.keyring.get_password", return_value="PAGE_TOK_101"),
            mock.patch("facebook.requests.post", side_effect=fake_reel_http_post),
        ):
            self.win.post_selected()
            self.assertIsNotNone(self.win.worker)
            self.win.worker.wait(5000)
        QApplication.processEvents()

        self.assertEqual(len(captured_posts), 3)
        # Step 1: start
        self.assertTrue(captured_posts[0]["url"].endswith("/101/video_reels"))
        self.assertEqual(captured_posts[0]["data"]["upload_phase"], "start")
        self.assertEqual(captured_posts[0]["headers"].get("Authorization"), "Bearer PAGE_TOK_101")
        # Step 2: rupload.facebook.com binary stream with OAuth header (NOT Bearer)
        self.assertEqual(
            captured_posts[1]["url"],
            "https://rupload.facebook.com/video-upload/v22.0/987654321",
        )
        self.assertEqual(captured_posts[1]["headers"].get("Authorization"), "OAuth PAGE_TOK_101")
        self.assertEqual(captured_posts[1]["headers"].get("offset"), "0")
        self.assertEqual(captured_posts[1]["headers"].get("file_size"), str(len(reel_bytes)))
        self.assertEqual(captured_posts[1]["raw_len"], len(reel_bytes))
        self.assertEqual(captured_posts[1]["raw_head"], reel_bytes[:16])
        # Step 3: finish
        self.assertTrue(captured_posts[2]["url"].endswith("/101/video_reels"))
        self.assertEqual(captured_posts[2]["data"]["upload_phase"], "finish")
        self.assertEqual(captured_posts[2]["data"]["video_id"], "987654321")
        self.assertEqual(captured_posts[2]["data"]["video_state"], "PUBLISHED")
        self.assertIn("My Shop Page", captured_posts[2]["data"]["description"])
        self.assertIn("https://example.com/reel-deal", captured_posts[2]["data"]["description"])

        hist = db.list_post_history(limit=5)
        self.assertTrue(hist[0]["is_reel"])
        self.assertEqual(
            facebook.build_post_permalink("101", "987654321", is_reel=True),
            "https://www.facebook.com/reel/987654321",
        )
        self.win.refresh_history()
        self.assertIn("🎬 Reels", self.win.tbl_history.item(0, 4).text())

        # Reels schedule bounds & security rejections
        now_utc = datetime.now(timezone.utc)
        ts_ok = facebook.validate_fb_reel_schedule_time(now_utc + timedelta(days=5))
        self.assertGreater(ts_ok, int(now_utc.timestamp()))
        with self.assertRaises(ValueError):
            facebook.validate_fb_reel_schedule_time(now_utc + timedelta(days=30))
        with self.assertRaises(ValueError):
            facebook.post_to_page("101", "no video", is_reel=True)
        with self.assertRaises(RuntimeError):
            facebook._upload_reel_binary("https://evil.example.com/upload", "TOK", reel_mp4)
        empty_mp4 = Path(self.td.name) / "empty.mp4"
        empty_mp4.write_bytes(b"")
        with self.assertRaises(ValueError):
            facebook.post_reel_to_page("101", str(empty_mp4), "bad")

        # CSV import with is_reel=1 + EditQueueJobDialog round-trip
        reel_csv = Path(self.td.name) / "reels_queue.csv"
        reel_csv.write_text(
            f"run_at,message,video_path,is_reel\n2026-12-20 14:00,คิวคลิปสั้น,{reel_mp4},1\n",
            encoding="utf-8-sig",
        )
        cnt_q = main.import_scheduled_jobs_from_csv(
            str(reel_csv), [{"platform": "facebook", "page_id": "101", "name": "My Shop Page"}]
        )
        self.assertEqual(cnt_q, 1)
        q_job = [j for j in db.list_scheduled_jobs() if j["message"] == "คิวคลิปสั้น"][0]
        self.assertTrue(q_job["is_reel"])
        ed_q = main.EditQueueJobDialog(q_job, parent=self.win)
        self.assertTrue(ed_q.chk_video_reel.isChecked())
        self.assertTrue(ed_q.updated_values()["is_reel"])

        # --- 2) Keyword Auto-Reply (Dialog + Worker + Spintax {commenter_name} + Hide + Self-Skip + Dedup) ---
        ar_dlg = main.AutoReplyRulesDialog(parent=self.win)
        ar_dlg.input_rule_name.setText("ถามราคา")
        ar_dlg.input_keywords.setText("ราคา, สนใจ")
        ar_dlg.chk_hide_after.setChecked(True)
        ar_dlg.editor_reply_template.setPlainText(
            "สวัสดีครับคุณ {commenter_name} เพจ {page_name} ส่งรายละเอียดทางแชทแล้วครับ"
        )
        ar_dlg._add_new_rule()

        ar_dlg.input_rule_name.setText("CF ด่วน")
        index_exact = ar_dlg.combo_match_mode.findData("exact")
        ar_dlg.combo_match_mode.setCurrentIndex(index_exact)
        ar_dlg.input_keywords.setText("cf")
        ar_dlg.chk_hide_after.setChecked(False)
        ar_dlg.editor_reply_template.setPlainText("รับยอด CF คุณ {commenter_name} ครับ")
        ar_dlg._add_new_rule()
        self.assertEqual(len(db.list_auto_reply_rules(only_enabled=True)), 2)

        ar_post_calls: list[tuple[str, dict]] = []

        def fake_ar_get(url, params=None, headers=None, timeout=None):
            if url.endswith("/101/posts"):
                return _MockHttpResp({"data": [{"id": "101_777", "message": "โพสต์ขายของ"}]})
            if url.endswith("/101_777/comments"):
                return _MockHttpResp(
                    {
                        "data": [
                            {
                                "id": "c_self",
                                "from": {"id": "101", "name": "My Shop Page"},
                                "message": "สนใจสอบถามราคาได้เลยครับ",
                            },
                            {
                                "id": "c_cust_1",
                                "from": {"id": "888", "name": "สมหญิง {VIP|PRO}"},
                                "message": "ขอทราบราคาหน่อยค่ะ",
                            },
                            {
                                "id": "c_cust_nonexact",
                                "from": {"id": "889", "name": "ก้อง"},
                                "message": "cf ของแถมไหม",
                            },
                            {
                                "id": "c_cust_cf",
                                "from": {"id": "890", "name": "มินท์"},
                                "message": "CF",
                            },
                        ]
                    }
                )
            return _MockHttpResp({"data": []})

        def fake_ar_post(url, data=None, files=None, headers=None, timeout=None):
            ar_post_calls.append((url, dict(data or {})))
            if url.endswith("/comments"):
                return _MockHttpResp({"id": f"reply_for_{len(ar_post_calls)}"})
            return _MockHttpResp({"success": True})

        with (
            mock.patch("facebook.keyring.get_password", return_value="PAGE_TOK_101"),
            mock.patch("facebook.requests.get", side_effect=fake_ar_get),
            mock.patch("facebook.requests.post", side_effect=fake_ar_post),
            mock.patch.object(facebook, "run_keyword_auto_reply_pass", wraps=facebook.run_keyword_auto_reply_pass),
        ):
            self.win.run_auto_reply_now(manual=True)
            self.assertIsNotNone(self.win.auto_reply_worker)
            self.win.auto_reply_worker.wait(5000)
            QApplication.processEvents()

            # Expected HTTP POSTs on pass #1:
            # 1) reply to c_cust_1, 2) hide c_cust_1, 3) reply to c_cust_cf (c_self & c_cust_nonexact skipped!)
            self.assertEqual(len(ar_post_calls), 3)
            self.assertTrue(ar_post_calls[0][0].endswith("/c_cust_1/comments"))
            # External commenter name containing `{VIP|PRO}` must NOT be mangled by Spintax!
            self.assertIn("สมหญิง {VIP|PRO}", ar_post_calls[0][1]["message"])
            self.assertIn("My Shop Page", ar_post_calls[0][1]["message"])
            self.assertTrue(ar_post_calls[1][0].endswith("/c_cust_1"))
            self.assertEqual(ar_post_calls[1][1].get("is_hidden"), "true")
            self.assertTrue(ar_post_calls[2][0].endswith("/c_cust_cf/comments"))
            self.assertIn("มินท์", ar_post_calls[2][1]["message"])

            # Pass #2 against identical comments must send ZERO new replies (100% deduplicated)
            ar_post_calls.clear()
            self.win.run_auto_reply_now(manual=True)
            self.assertIsNotNone(self.win.auto_reply_worker)
            self.win.auto_reply_worker.wait(5000)
            QApplication.processEvents()
            self.assertEqual(ar_post_calls, [])

        # --- 3) Post Presets / Content Packs save, load & missing-media resilience ---
        self.win._clear_composer()
        self.win.editor.setPlainText("แคปชันชุดขายดีประจำสัปดาห์")
        self.win.input_link.setText("https://shop.example.com/pack1")
        self.win.set_attached_video(str(reel_mp4))
        self.win.chk_video_reel.setChecked(True)
        self.win.chk_watermark.setChecked(True)
        self.win.input_watermark_text.setText("© {page_name}")
        self.win.follow_up_comments = ["สั่งซื้อคลิก https://shop.example.com"]
        self.win.spin_followup_delay.setValue(6)

        preset_dlg = main.PostPresetsDialog(
            composer_snapshot=self.win._snapshot_composer_for_preset(),
            parent=self.win,
        )
        preset_dlg.input_preset_name.setText("ชุดโปร Reels ขายดี")
        preset_dlg._save_current_from_composer()
        saved_presets = db.list_post_presets()
        self.assertEqual(len(saved_presets), 1)
        self.assertTrue(saved_presets[0]["is_reel"])

        self.win._clear_composer()
        self.assertEqual(self.win.editor.toPlainText(), "")
        self.assertFalse(self.win.chk_video_reel.isChecked())

        self.win.apply_post_preset(saved_presets[0])
        self.assertEqual(self.win.editor.toPlainText(), "แคปชันชุดขายดีประจำสัปดาห์")
        self.assertEqual(self.win.input_link.text(), "https://shop.example.com/pack1")
        self.assertEqual(self.win.attached_video, str(reel_mp4))
        self.assertTrue(self.win.chk_video_reel.isChecked())
        self.assertTrue(self.win.chk_watermark.isChecked())
        self.assertEqual(self.win.follow_up_comments, ["สั่งซื้อคลิก https://shop.example.com"])
        self.assertEqual(self.win.spin_followup_delay.value(), 6)

        # Missing file resilience when video is moved/deleted
        reel_mp4.unlink()
        self.win.apply_post_preset(saved_presets[0])
        self.assertEqual(self.win.attached_video, "")
        self.assertFalse(self.win.chk_video_reel.isChecked())
        self.assertIn("ไม่พบไฟล์สื่อเดิม", self.win.status.text())
        ed_q.close()
        ar_dlg.close()
        preset_dlg.close()

        # --- 4) Daily Rolling Auto-Backup (keep newest 7) & Validated Restore DB ---
        # Verify MainWindow.__init__() already created today's startup auto-backup automatically
        startup_backups = list(db.get_backup_dir().glob("auto_backup_*.db"))
        self.assertEqual(len(startup_backups), 1)

        for day in range(1, 11):
            b_out = db.run_daily_auto_backup(keep_days=7, today_str=f"2099-01-{day:02d}")
            self.assertIsNotNone(b_out)
            self.assertTrue(b_out.is_file())
        kept = sorted(p.name for p in db.get_backup_dir().glob("auto_backup_*.db"))
        self.assertEqual(
            kept,
            [f"auto_backup_2099-01-{d:02d}.db" for d in range(4, 11)],
        )

        # Reject invalid / non-SQLite / non-SocialManager SQLite / live DB_PATH
        bad_txt = Path(self.td.name) / "not_sqlite.db"
        bad_txt.write_text("hello world", encoding="utf-8")
        with self.assertRaises(ValueError):
            db.validate_sqlite_backup_file(bad_txt)

        foreign_db = Path(self.td.name) / "other_app.db"
        c_other = sqlite3.connect(foreign_db)
        c_other.execute("CREATE TABLE something_else(id INTEGER PRIMARY KEY)")
        c_other.commit()
        c_other.close()
        with self.assertRaises(ValueError):
            db.validate_sqlite_backup_file(foreign_db)

        with self.assertRaises(ValueError):
            db.validate_sqlite_backup_file(db.DB_PATH)

        # Mutate DB after snapshot, then restore snapshot via SettingsDialog._restore_database()
        snap_target = db.get_backup_dir() / "auto_backup_2026-11-10.db"
        db.backup_database(Path(self.td.name) / "clean_snap.db")
        snap_target = Path(self.td.name) / "clean_snap.db"
        db.save_account(facebook.PLATFORM, "999999", "Temporary Added Page")
        self.assertTrue(any(pid == "999999" for _, pid, _ in db.list_accounts()))

        settings_dlg = main.SettingsDialog(self.win)
        with (
            mock.patch.object(
                main.QFileDialog,
                "getOpenFileName",
                return_value=(str(snap_target), "SQLite Database (*.db)"),
            ),
            mock.patch.object(
                main.QMessageBox,
                "question",
                return_value=main.QMessageBox.Yes,
            ),
            mock.patch.object(main.QMessageBox, "information"),
        ):
            settings_dlg._restore_database()
        self.assertTrue(settings_dlg.db_restored)
        self.assertFalse(any(pid == "999999" for _, pid, _ in db.list_accounts()))
        pre_files = list(db.get_backup_dir().glob("pre_restore_*.db"))
        self.assertGreaterEqual(len(pre_files), 1)
        settings_dlg.close()

    def test_round54_insights_modern_metrics_read_insights_and_post_fallback(self):
        import json

        # 1) SCOPES must include read_insights in both facebook.py and oauth_backend/app.py
        self.assertIn("read_insights", facebook.SCOPES.split(","))
        backend_app = Path(__file__).resolve().parent.parent / "oauth_backend" / "app.py"
        backend_src = backend_app.read_text(encoding="utf-8")
        self.assertIn(f'SCOPES = "{facebook.SCOPES}"', backend_src)

        class _Resp:
            def __init__(self, payload: dict, status_code: int = 200):
                self._payload = payload
                self.status_code = status_code
                self.text = json.dumps(payload, ensure_ascii=False)

            def json(self):
                return self._payload

        # 2) Modern metrics queried FIRST + post-level fallback when page-level insights return empty
        queried_page_metrics: list[tuple[str, str]] = []

        def fake_get_post_fallback(url, params=None, headers=None, timeout=None):
            p = dict(params or {})
            if url.endswith("/101"):
                return _Resp({"id": "101", "name": "Page 101", "followers_count": 350})
            if url.endswith("/101/posts"):
                return _Resp(
                    {
                        "data": [
                            {
                                "id": "101_p1",
                                "created_time": "2026-11-24T10:00:00+0000",
                                "reactions": {"summary": {"total_count": 20}},
                                "comments": {"summary": {"total_count": 5}},
                                "shares": {"count": 3},
                            },
                            {
                                "id": "101_p2",
                                "created_time": "2026-11-24T11:00:00+0000",
                                "reactions": {"summary": {"total_count": 10}},
                                "comments": {"summary": {"total_count": 2}},
                                "shares": {"count": 1},
                            },
                        ]
                    }
                )
            if url.endswith("/101/insights"):
                m = p.get("metric", "")
                per = p.get("period", "")
                queried_page_metrics.append((m, per))
                if m in ("page_impressions_unique", "page_impressions"):
                    return _Resp(
                        {"error": {"message": "(#100) The value must be a valid insights metric"}},
                        status_code=400,
                    )
                return _Resp({"data": []})
            if url.endswith("/101_p1/insights") or url.endswith("/101_p2/insights"):
                m = p.get("metric", "")
                if m == "post_total_media_view_unique":
                    return _Resp({"data": [{"values": [{"value": 150}]}]})
                if m == "post_media_view":
                    return _Resp({"data": [{"values": [{"value": 240}]}]})
                return _Resp({"data": []})
            return _Resp({"data": []})

        with (
            mock.patch("facebook.keyring.get_password", return_value="TOK_101"),
            mock.patch("facebook.requests.get", side_effect=fake_get_post_fallback),
        ):
            res = facebook.get_page_analytics("101", "Page 101", period_days=7)

        # Verify modern metric was queried BEFORE deprecated metric
        self.assertEqual(queried_page_metrics[0], ("page_total_media_view_unique", "day"))
        self.assertEqual(res["reach"], 300)        # 150 * 2 posts
        self.assertEqual(res["impressions"], 480)  # 240 * 2 posts
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["error"], "")

        # 3) When modern metrics return HTTP 200 empty (e.g. missing read_insights) and legacy fails (#100),
        # must NOT leak stale (#100) error — must surface clear Thai read_insights hint instead!
        def fake_get_missing_perm(url, params=None, headers=None, timeout=None):
            p = dict(params or {})
            if url.endswith("/101"):
                return _Resp({"id": "101", "name": "Page 101", "followers_count": 350})
            if url.endswith("/101/posts"):
                return _Resp(
                    {
                        "data": [
                            {
                                "id": "101_p1",
                                "created_time": "2026-11-24T10:00:00+0000",
                                "reactions": {"summary": {"total_count": 12}},
                                "comments": {"summary": {"total_count": 2}},
                                "shares": {"count": 1},
                            }
                        ]
                    }
                )
            if url.endswith("/101/insights"):
                m = p.get("metric", "")
                if m in ("page_impressions_unique", "page_impressions"):
                    return _Resp(
                        {"error": {"message": "(#100) The value must be a valid insights metric"}},
                        status_code=400,
                    )
                return _Resp({"data": []})
            return _Resp({"data": []})

        with (
            mock.patch("facebook.keyring.get_password", return_value="TOK_101"),
            mock.patch("facebook.requests.get", side_effect=fake_get_missing_perm),
        ):
            res_empty = facebook.get_page_analytics("101", "Page 101", period_days=7)

        self.assertEqual(res_empty["reach"], 0)
        self.assertEqual(res_empty["impressions"], 0)
        self.assertEqual(res_empty["status"], "partial")
        self.assertIn("read_insights", res_empty["error"])
        self.assertNotIn("valid insights metric", res_empty["error"])

        # 4) Rolling-period ('week') fallback populates reach/impressions when 'day' is empty
        def fake_get_rolling(url, params=None, headers=None, timeout=None):
            p = dict(params or {})
            if url.endswith("/101"):
                return _Resp({"id": "101", "name": "Page 101", "followers_count": 500})
            if url.endswith("/101/posts"):
                return _Resp({"data": []})
            if url.endswith("/101/insights"):
                m = p.get("metric", "")
                per = p.get("period", "")
                if per == "week" and m == "page_total_media_view_unique":
                    return _Resp({"data": [{"values": [{"value": 800}]}]})
                if per == "week" and m == "page_media_view":
                    return _Resp({"data": [{"values": [{"value": 1200}]}]})
                return _Resp({"data": []})
            return _Resp({"data": []})

        with (
            mock.patch("facebook.keyring.get_password", return_value="TOK_101"),
            mock.patch("facebook.requests.get", side_effect=fake_get_rolling),
        ):
            res_roll = facebook.get_page_analytics("101", "Page 101", period_days=7)
        self.assertEqual(res_roll["reach"], 800)
        self.assertEqual(res_roll["impressions"], 1200)
        self.assertEqual(res_roll["status"], "ok")

    def test_round55_game_style_patch_launcher_and_release_packager(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import json
        import os
        import threading
        from urllib.parse import unquote
        from PySide6.QtWidgets import QApplication
        import build_release
        import launcher
        import updater
        import version

        # 1) SemVer parsing & comparison (strict ASCII)
        self.assertEqual(updater.parse_version("1.5.0"), (1, 5, 0))
        self.assertEqual(updater.parse_version("v2.10.3"), (2, 10, 3))
        self.assertTrue(updater.is_newer_version("1.6.0", "1.5.0"))
        self.assertFalse(updater.is_newer_version("1.5.0", "1.5.0"))
        self.assertFalse(updater.is_newer_version("1.4.9", "1.5.0"))
        for bad_ver in ("", "1.5", "๑.๕.๐", "abc"):
            with self.assertRaises(ValueError):
                updater.parse_version(bad_ver)

        # 2) URL & filename security validation
        self.assertEqual(
            updater.normalize_manifest_url("myorg/social-manager"),
            "https://github.com/myorg/social-manager/releases/latest/download/version.json",
        )
        self.assertEqual(
            updater.normalize_manifest_url("https://github.com/myorg/social-manager"),
            "https://github.com/myorg/social-manager/releases/latest/download/version.json",
        )
        for bad_url in (
            "http://evil.example.com/version.json",
            "https://user:pass@github.com/a/b/version.json",
            "ftp://github.com/a/b/version.json",
            "https://github.com/bad url/version.json",
        ):
            with self.assertRaises(ValueError):
                updater.normalize_manifest_url(bad_url)

        install_dir = Path(self.td.name) / "launcher_install"
        install_dir.mkdir(parents=True, exist_ok=True)
        for bad_name in ("../evil.exe", "sub/a.exe", "C:\\evil.exe", "run.bat", "script.ps1", "..", ""):
            with self.assertRaises(ValueError):
                updater.validate_target_filename(bad_name, install_dir)

        # 3) GitHub Releases API payload parsing
        gh_api_payload = {
            "tag_name": "v2.1.0",
            "published_at": "2026-11-25T10:00:00Z",
            "body": "- ฟีเจอร์ใหม่จาก GitHub Releases",
            "assets": [
                {
                    "name": "SocialManager.exe",
                    "browser_download_url": "https://github.com/myorg/social-manager/releases/download/v2.1.0/SocialManager.exe",
                    "size": 1024,
                    "digest": "sha256:" + ("a" * 64),
                },
                {
                    "name": "version.json",
                    "browser_download_url": "https://github.com/myorg/social-manager/releases/download/v2.1.0/version.json",
                    "size": 100,
                },
            ],
        }
        gh_mf = updater.parse_manifest_payload(gh_api_payload, install_dir=install_dir)
        self.assertEqual(gh_mf.version, "2.1.0")
        self.assertEqual(len(gh_mf.files), 1)
        self.assertEqual(gh_mf.files[0].name, "SocialManager.exe")
        self.assertEqual(gh_mf.files[0].sha256, "a" * 64)

        # 4) build_release.py bundle packager (SHA-256 + launcher_config + release_web/version.json)
        fake_dist = Path(self.td.name) / "fake_dist"
        fake_out = Path(self.td.name) / "fake_release_out"
        fake_web = Path(self.td.name) / "fake_web"
        fake_dist.mkdir()
        fake_web.mkdir()
        (fake_dist / "SocialManager.exe").write_bytes(b"FAKE_APP_BINARY_V150")
        (fake_dist / "SocialManagerLauncher.exe").write_bytes(b"FAKE_LAUNCHER_BINARY")
        (fake_dist / "คู่มือการใช้งาน.txt").write_text("คู่มือทดสอบ v1.5.0", encoding="utf-8")

        built_mf = build_release.build_release_bundle(
            repo="myorg/social-manager",
            version_str=version.APP_VERSION,
            notes="- ทดสอบแพ็กเกจแพตช์",
            force_update=False,
            dist_dir=fake_dist,
            output_dir=fake_out,
            web_dir=fake_web,
        )
        self.assertEqual(built_mf["version"], version.APP_VERSION)
        self.assertTrue((fake_out / "version.json").is_file())
        self.assertTrue((fake_out / "launcher_config.json").is_file())
        self.assertTrue((fake_web / "version.json").is_file())
        self.assertIn("launcher", built_mf)
        self.assertEqual(len(built_mf["files"]), 2)

        # 5) Real local HTTP Patch Server + LauncherWindow end-to-end check, force_update lock, download & swap
        new_exe_bytes = b"NEW_SOCIAL_MANAGER_V200_" + (b"X" * 4096)
        new_manual_bytes = "คู่มือเวอร์ชัน 2.0.0 ใหม่ล่าสุด".encode("utf-8")
        new_exe_sha = updater.compute_sha256(
            (Path(self.td.name) / "src_new.exe").write_bytes(new_exe_bytes)
            and (Path(self.td.name) / "src_new.exe")
        )
        src_manual = Path(self.td.name) / "src_new_manual.txt"
        src_manual.write_bytes(new_manual_bytes)
        new_manual_sha = updater.compute_sha256(src_manual)

        server_box: dict = {}

        class _PatchHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002
                return

            def do_GET(self):  # noqa: N802
                base = f"http://127.0.0.1:{self.server.server_port}"
                path = unquote(self.path)
                if path == "/version.json":
                    body = json.dumps(
                        {
                            "version": "2.0.0",
                            "released_at": "2026-11-25T17:00:00+07:00",
                            "notes": "- แพตช์ใหญ่ v2.0.0\n- อัปเดตความเร็วและคู่มือ",
                            "force_update": False,
                            "files": [
                                {
                                    "name": "SocialManager.exe",
                                    "url": f"{base}/SocialManager.exe",
                                    "sha256": new_exe_sha,
                                    "size": len(new_exe_bytes),
                                },
                                {
                                    "name": "คู่มือการใช้งาน.txt",
                                    "url": f"{base}/คู่มือการใช้งาน.txt",
                                    "sha256": new_manual_sha,
                                    "size": len(new_manual_bytes),
                                },
                            ],
                        },
                        ensure_ascii=False,
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/bad_sha.json":
                    body = json.dumps(
                        {
                            "version": "2.1.0",
                            "files": [
                                {
                                    "name": "SocialManager.exe",
                                    "url": f"{base}/SocialManager.exe",
                                    "sha256": "0" * 64,
                                    "size": len(new_exe_bytes),
                                }
                            ],
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/SocialManager.exe":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(len(new_exe_bytes)))
                    self.end_headers()
                    self.wfile.write(new_exe_bytes)
                elif path == "/คู่มือการใช้งาน.txt":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.send_header("Content-Length", str(len(new_manual_bytes)))
                    self.end_headers()
                    self.wfile.write(new_manual_bytes)
                else:
                    self.send_response(404)
                    self.end_headers()

        srv = ThreadingHTTPServer(("127.0.0.1", 0), _PatchHandler)
        srv_thread = threading.Thread(target=srv.serve_forever, daemon=True)
        srv_thread.start()
        try:
            port = srv.server_port
            (install_dir / "SocialManager.exe").write_bytes(b"OLD_V150_EXE")
            (install_dir / "คู่มือการใช้งาน.txt").write_bytes(b"OLD_MANUAL")
            updater.save_installed_version(install_dir, "1.5.0", ["SocialManager.exe"])

            updater.save_launcher_config(
                install_dir,
                manifest_url=f"http://127.0.0.1:{port}/version.json",
                allow_local_http=True,
            )
            lwin = launcher.LauncherWindow(install_dir=install_dir, allow_local_http=True)
            # Zero end-user settings UI in Launcher
            self.assertFalse(hasattr(launcher, "LauncherSettingsDialog"))
            self.assertFalse(hasattr(lwin, "btn_settings"))
            self.assertFalse(hasattr(lwin, "btn_top_settings"))

            lwin.check_for_updates()
            self.assertIsNotNone(lwin._worker)
            lwin._worker.wait(5000)
            QApplication.processEvents()

            self.assertIsNotNone(lwin.manifest)
            self.assertEqual(lwin.manifest.version, "2.0.0")
            self.assertTrue(lwin._update_available)
            # Update-before-launch: Launch button MUST stay locked until user updates (even with force_update=False)
            self.assertFalse(lwin.btn_launch.isEnabled())
            self.assertEqual(lwin.list_patch_files.count(), 2)
            self.assertIn("แพตช์ใหญ่ v2.0.0", lwin.txt_patch_notes.toPlainText())

            # Run real patch download & swap through LauncherWindow
            lwin.start_or_cancel_update()
            self.assertIsNotNone(lwin._worker)
            lwin._worker.wait(5000)
            QApplication.processEvents()

            self.assertEqual(lwin.progress_bar.value(), 100)
            self.assertFalse(lwin._update_available)
            self.assertTrue(lwin.btn_launch.isEnabled())
            self.assertEqual((install_dir / "SocialManager.exe").read_bytes(), new_exe_bytes)
            self.assertEqual((install_dir / "คู่มือการใช้งาน.txt").read_bytes(), new_manual_bytes)
            self.assertEqual(updater.get_installed_version(install_dir), "2.0.0")
            self.assertEqual(list(install_dir.glob(".patch_*")), [])
            self.assertEqual(list(install_dir.glob(".bak_*")), [])

            # launch_app() must pass --from-launcher so main.py opens directly
            spawned: list = []
            with mock.patch("launcher.subprocess.Popen", side_effect=lambda cmd, **kw: spawned.append(cmd)):
                lwin.launch_app()
            self.assertEqual(len(spawned), 1)
            self.assertIn("--from-launcher", spawned[0])
            lwin.close()

            # main._maybe_redirect_to_launcher() redirects when frozen without --from-launcher
            fake_exe_dir = Path(self.td.name) / "frozen_dir"
            fake_exe_dir.mkdir()
            (fake_exe_dir / "SocialManager.exe").write_bytes(b"APP")
            (fake_exe_dir / "SocialManagerLauncher.exe").write_bytes(b"LAUNCHER")
            redir_cmds: list = []
            with (
                mock.patch.object(main.sys, "frozen", True, create=True),
                mock.patch.object(main.sys, "executable", str(fake_exe_dir / "SocialManager.exe")),
                mock.patch("subprocess.Popen", side_effect=lambda cmd, **kw: redir_cmds.append(cmd)),
            ):
                self.assertTrue(main._maybe_redirect_to_launcher(["SocialManager.exe"]))
                self.assertEqual(len(redir_cmds), 1)
                self.assertTrue(redir_cmds[0][0].endswith("SocialManagerLauncher.exe"))
                # When launched with --from-launcher, must NOT redirect again
                self.assertFalse(
                    main._maybe_redirect_to_launcher(["SocialManager.exe", "--from-launcher"])
                )

            # 6) SHA-256 mismatch MUST reject tamper and keep installed v2.0.0 intact
            bad_mf = updater.fetch_patch_manifest(
                f"http://127.0.0.1:{port}/bad_sha.json",
                install_dir=install_dir,
                allow_local_http=True,
            )
            with self.assertRaises(RuntimeError) as ctx_sha:
                updater.download_and_apply_patch(
                    bad_mf,
                    install_dir,
                    allow_local_http=True,
                )
            self.assertIn("SHA-256", str(ctx_sha.exception))
            self.assertEqual((install_dir / "SocialManager.exe").read_bytes(), new_exe_bytes)
            self.assertEqual(list(install_dir.glob(".patch_*")), [])

            # 7) Atomic rollback if 2nd file is locked during multi-file swap
            good_mf = updater.fetch_patch_manifest(
                f"http://127.0.0.1:{port}/version.json",
                install_dir=install_dir,
                allow_local_http=True,
            )
            (install_dir / "SocialManager.exe").write_bytes(b"PRE_ROLLBACK_EXE")
            (install_dir / "คู่มือการใช้งาน.txt").write_bytes(b"PRE_ROLLBACK_TXT")
            real_replace = os.replace
            swap_calls = {"n": 0}

            def flaky_replace(src, dst):
                if ".bak_" in str(dst):
                    swap_calls["n"] += 1
                    if swap_calls["n"] == 2:
                        raise PermissionError("file in use")
                return real_replace(src, dst)

            with mock.patch("updater.os.replace", side_effect=flaky_replace):
                with self.assertRaises(RuntimeError) as ctx_lock:
                    updater.download_and_apply_patch(good_mf, install_dir, allow_local_http=True)
            self.assertIn("ปิดโปรแกรมหลัก", str(ctx_lock.exception))
            self.assertEqual((install_dir / "SocialManager.exe").read_bytes(), b"PRE_ROLLBACK_EXE")
            self.assertEqual((install_dir / "คู่มือการใช้งาน.txt").read_bytes(), b"PRE_ROLLBACK_TXT")
        finally:
            srv.shutdown()
            srv.server_close()

        # 8) Download Web Portal HTML sanity & XSS safety check (no innerHTML)
        web_html = (Path(__file__).resolve().parent.parent / "release_web" / "index.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("SocialManagerLauncher.exe", web_html)
        self.assertIn("safeHttpsUrl", web_html)
        self.assertNotIn("innerHTML", web_html)


if __name__ == "__main__":
    unittest.main()
