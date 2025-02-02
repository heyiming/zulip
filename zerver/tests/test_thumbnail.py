import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from io import StringIO
from unittest.mock import patch

import orjson
import pyvips
from django.conf import settings
from django.http.request import MediaType
from django.test import override_settings

from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.test_helpers import get_test_image_file, ratelimit_rule, read_test_image_file
from zerver.lib.thumbnail import (
    BadImageError,
    BaseThumbnailFormat,
    StoredThumbnailFormat,
    ThumbnailFormat,
    get_image_thumbnail_path,
    missing_thumbnails,
    resize_emoji,
    split_thumbnail_path,
)
from zerver.lib.upload import all_message_attachments
from zerver.models import Attachment, ImageAttachment
from zerver.views.upload import closest_thumbnail_format
from zerver.worker.thumbnail import ensure_thumbnails


class ThumbnailRedirectEndpointTest(ZulipTestCase):
    """Tests for the legacy /thumbnail endpoint."""

    def test_thumbnail_upload_redirect(self) -> None:
        self.login("hamlet")
        fp = StringIO("zulip!")
        fp.name = "zulip.jpeg"

        result = self.client_post("/json/user_uploads", {"file": fp})
        self.assert_json_success(result)
        json = orjson.loads(result.content)
        self.assertIn("uri", json)
        self.assertIn("url", json)
        url = json["url"]
        self.assertEqual(json["uri"], url)
        base = "/user_uploads/"
        self.assertEqual(base, url[: len(base)])

        result = self.client_get("/thumbnail", {"url": url[1:], "size": "full"})
        self.assertEqual(result.status_code, 302, result)
        self.assertEqual(url, result["Location"])

        self.login("iago")
        result = self.client_get("/thumbnail", {"url": url[1:], "size": "full"})
        self.assertEqual(result.status_code, 403, result)
        self.assert_in_response("You are not authorized to view this file.", result)

    def test_thumbnail_external_redirect(self) -> None:
        url = "https://www.google.com/images/srpr/logo4w.png"
        result = self.client_get("/thumbnail", {"url": url, "size": "full"})
        self.assertEqual(result.status_code, 302, result)
        base = "https://external-content.zulipcdn.net/external_content/56c362a24201593891955ff526b3b412c0f9fcd2/68747470733a2f2f7777772e676f6f676c652e636f6d2f696d616765732f737270722f6c6f676f34772e706e67"
        self.assertEqual(base, result["Location"])

        url = "http://www.google.com/images/srpr/logo4w.png"
        result = self.client_get("/thumbnail", {"url": url, "size": "full"})
        self.assertEqual(result.status_code, 302, result)
        base = "https://external-content.zulipcdn.net/external_content/7b6552b60c635e41e8f6daeb36d88afc4eabde79/687474703a2f2f7777772e676f6f676c652e636f6d2f696d616765732f737270722f6c6f676f34772e706e67"
        self.assertEqual(base, result["Location"])

        url = "//www.google.com/images/srpr/logo4w.png"
        result = self.client_get("/thumbnail", {"url": url, "size": "full"})
        self.assertEqual(result.status_code, 302, result)
        base = "https://external-content.zulipcdn.net/external_content/676530cf4b101d56f56cc4a37c6ef4d4fd9b0c03/2f2f7777772e676f6f676c652e636f6d2f696d616765732f737270722f6c6f676f34772e706e67"
        self.assertEqual(base, result["Location"])

    @override_settings(RATE_LIMITING=True)
    def test_thumbnail_redirect_for_spectator(self) -> None:
        self.login("hamlet")
        fp = StringIO("zulip!")
        fp.name = "zulip.jpeg"

        result = self.client_post("/json/user_uploads", {"file": fp})
        self.assert_json_success(result)
        json = orjson.loads(result.content)
        url = json["url"]
        self.assertEqual(json["uri"], url)

        with ratelimit_rule(86400, 1000, domain="spectator_attachment_access_by_file"):
            # Deny file access for non-web-public stream
            self.subscribe(self.example_user("hamlet"), "Denmark")
            host = self.example_user("hamlet").realm.host
            body = f"First message ...[zulip.txt](http://{host}" + url + ")"
            self.send_stream_message(self.example_user("hamlet"), "Denmark", body, "test")

            self.logout()
            response = self.client_get("/thumbnail", {"url": url[1:], "size": "full"})
            self.assertEqual(response.status_code, 403)

            # Allow file access for web-public stream
            self.login("hamlet")
            self.make_stream("web-public-stream", is_web_public=True)
            self.subscribe(self.example_user("hamlet"), "web-public-stream")
            body = f"First message ...[zulip.txt](http://{host}" + url + ")"
            self.send_stream_message(self.example_user("hamlet"), "web-public-stream", body, "test")

            self.logout()
            response = self.client_get("/thumbnail", {"url": url[1:], "size": "full"})
            self.assertEqual(response.status_code, 302)

        # Deny file access since rate limited
        with ratelimit_rule(86400, 0, domain="spectator_attachment_access_by_file"):
            response = self.client_get("/thumbnail", {"url": url[1:], "size": "full"})
            self.assertEqual(response.status_code, 403)

        # Deny random file access
        response = self.client_get(
            "/thumbnail",
            {
                "url": "user_uploads/2/71/QYB7LA-ULMYEad-QfLMxmI2e/zulip-non-existent.txt",
                "size": "full",
            },
        )
        self.assertEqual(response.status_code, 403)


class ThumbnailEmojiTest(ZulipTestCase):
    def animated_test(self, filename: str) -> None:
        animated_unequal_img_data = read_test_image_file(filename)
        original_image = pyvips.Image.new_from_buffer(animated_unequal_img_data, "n=-1")
        resized_img_data, still_img_data = resize_emoji(
            animated_unequal_img_data, filename, size=50
        )
        assert still_img_data is not None
        emoji_image = pyvips.Image.new_from_buffer(resized_img_data, "n=-1")
        self.assertEqual(emoji_image.get("vips-loader"), "gifload_buffer")
        self.assertEqual(emoji_image.get_n_pages(), original_image.get_n_pages())
        self.assertEqual(emoji_image.get("page-height"), 50)
        self.assertEqual(emoji_image.height, 150)
        self.assertEqual(emoji_image.width, 50)

        still_image = pyvips.Image.new_from_buffer(still_img_data, "")
        self.assertEqual(still_image.get("vips-loader"), "pngload_buffer")
        self.assertEqual(still_image.get_n_pages(), 1)
        self.assertEqual(still_image.height, 50)
        self.assertEqual(still_image.width, 50)

    def test_resize_animated_square(self) -> None:
        """An animated image which is square"""
        self.animated_test("animated_large_img.gif")

    def test_resize_animated_emoji(self) -> None:
        """An animated image which is not square"""
        self.animated_test("animated_unequal_img.gif")

    def test_resize_corrupt_emoji(self) -> None:
        corrupted_img_data = read_test_image_file("corrupt.gif")
        with self.assertRaises(BadImageError):
            resize_emoji(corrupted_img_data, "corrupt.gif")

    def test_resize_too_many_pixels(self) -> None:
        """An image file with too many pixels is not resized"""
        with patch("zerver.lib.thumbnail.IMAGE_BOMB_TOTAL_PIXELS", 100):
            animated_large_img_data = read_test_image_file("animated_large_img.gif")
            with self.assertRaises(BadImageError):
                resize_emoji(animated_large_img_data, "animated_large_img.gif", size=50)

            bomb_img_data = read_test_image_file("bomb.png")
            with self.assertRaises(BadImageError):
                resize_emoji(bomb_img_data, "bomb.png", size=50)

    def test_resize_still_gif(self) -> None:
        """A non-animated square emoji resize"""
        still_large_img_data = read_test_image_file("still_large_img.gif")
        resized_img_data, no_still_data = resize_emoji(
            still_large_img_data, "still_large_img.gif", size=50
        )
        emoji_image = pyvips.Image.new_from_buffer(resized_img_data, "n=-1")
        self.assertEqual(emoji_image.get("vips-loader"), "gifload_buffer")
        self.assertEqual(emoji_image.height, 50)
        self.assertEqual(emoji_image.width, 50)
        self.assertEqual(emoji_image.get_n_pages(), 1)
        assert no_still_data is None

    def test_resize_still_jpg(self) -> None:
        """A non-animatatable format resize"""
        still_large_img_data = read_test_image_file("img.jpg")
        resized_img_data, no_still_data = resize_emoji(still_large_img_data, "img.jpg", size=50)
        emoji_image = pyvips.Image.new_from_buffer(resized_img_data, "")
        self.assertEqual(emoji_image.get("vips-loader"), "jpegload_buffer")
        self.assertEqual(emoji_image.height, 50)
        self.assertEqual(emoji_image.width, 50)
        self.assertEqual(emoji_image.get_n_pages(), 1)
        assert no_still_data is None

    def test_non_image_format_wrong_content_type(self) -> None:
        """A file that is not an image"""
        non_img_data = read_test_image_file("text.txt")
        with self.assertRaises(BadImageError):
            resize_emoji(non_img_data, "text.png", size=50)


class ThumbnailClassesTest(ZulipTestCase):
    def test_class_equivalence(self) -> None:
        self.assertNotEqual(
            ThumbnailFormat("webp", 150, 100, animated=True, opts="Q=90"),
            "150x100-anim.webp",
        )

        self.assertEqual(
            ThumbnailFormat("webp", 150, 100, animated=True, opts="Q=90"),
            ThumbnailFormat("webp", 150, 100, animated=True, opts="Q=10"),
        )

        self.assertEqual(
            ThumbnailFormat("webp", 150, 100, animated=True, opts="Q=90"),
            BaseThumbnailFormat("webp", 150, 100, animated=True),
        )

        self.assertNotEqual(
            ThumbnailFormat("jpeg", 150, 100, animated=True, opts="Q=90"),
            ThumbnailFormat("webp", 150, 100, animated=True, opts="Q=90"),
        )

        self.assertNotEqual(
            ThumbnailFormat("webp", 300, 100, animated=True, opts="Q=90"),
            ThumbnailFormat("webp", 150, 100, animated=True, opts="Q=90"),
        )

        self.assertNotEqual(
            ThumbnailFormat("webp", 150, 100, animated=False, opts="Q=90"),
            ThumbnailFormat("webp", 150, 100, animated=True, opts="Q=90"),
        )

        # We can compare stored thumbnails, with much more metadata,
        # to the thumbnail formats that spec how they are generated
        self.assertEqual(
            ThumbnailFormat("webp", 150, 100, animated=False, opts="Q=90"),
            StoredThumbnailFormat(
                "webp",
                150,
                100,
                animated=False,
                content_type="image/webp",
                width=120,
                height=100,
                byte_size=123,
            ),
        )

        # But differences in the base four properties mean they are not equal
        self.assertNotEqual(
            ThumbnailFormat("webp", 150, 100, animated=False, opts="Q=90"),
            StoredThumbnailFormat(
                "webp",
                150,
                100,
                animated=True,  # Note this change
                content_type="image/webp",
                width=120,
                height=100,
                byte_size=123,
            ),
        )

    def test_stringification(self) -> None:
        # These formats need to be stable, since they are written into URLs in the messages.
        self.assertEqual(
            str(ThumbnailFormat("webp", 150, 100, animated=False)),
            "150x100.webp",
        )

        self.assertEqual(
            str(ThumbnailFormat("webp", 150, 100, animated=True)),
            "150x100-anim.webp",
        )

        # And they should round-trip into BaseThumbnailFormat, losing the opts= which we do not serialize
        thumb_format = ThumbnailFormat("webp", 150, 100, animated=True, opts="Q=90")
        self.assertEqual(thumb_format.extension, "webp")
        self.assertEqual(thumb_format.max_width, 150)
        self.assertEqual(thumb_format.max_height, 100)
        self.assertEqual(thumb_format.animated, True)

        round_trip = BaseThumbnailFormat.from_string(str(thumb_format))
        assert round_trip is not None
        self.assertEqual(thumb_format, round_trip)
        self.assertEqual(round_trip.extension, "webp")
        self.assertEqual(round_trip.max_width, 150)
        self.assertEqual(round_trip.max_height, 100)
        self.assertEqual(round_trip.animated, True)

        self.assertIsNone(BaseThumbnailFormat.from_string("bad.webp"))


class TestStoreThumbnail(ZulipTestCase):
    @patch(
        "zerver.lib.thumbnail.THUMBNAIL_OUTPUT_FORMATS",
        [ThumbnailFormat("webp", 100, 75, animated=True)],
    )
    def test_upload_image(self) -> None:
        assert settings.LOCAL_FILES_DIR
        self.login_user(self.example_user("hamlet"))

        with self.captureOnCommitCallbacks(execute=True):
            with get_test_image_file("animated_unequal_img.gif") as image_file:
                response = self.assert_json_success(
                    self.client_post("/json/user_uploads", {"file": image_file})
                )
            path_id = re.sub(r"/user_uploads/", "", response["url"])
            self.assertEqual(Attachment.objects.filter(path_id=path_id).count(), 1)

            image_attachment = ImageAttachment.objects.get(path_id=path_id)
            self.assertEqual(image_attachment.original_height_px, 56)
            self.assertEqual(image_attachment.original_width_px, 128)
            self.assertEqual(image_attachment.frames, 3)
            self.assertEqual(image_attachment.thumbnail_metadata, [])

            self.assertEqual(
                [r[0] for r in all_message_attachments(include_thumbnails=True)],
                [path_id],
            )

            # The worker triggers when we exit this block and call the pending callbacks
        image_attachment = ImageAttachment.objects.get(path_id=path_id)
        self.assert_length(image_attachment.thumbnail_metadata, 1)
        generated_thumbnail = StoredThumbnailFormat(**image_attachment.thumbnail_metadata[0])

        self.assertEqual(str(generated_thumbnail), "100x75-anim.webp")
        self.assertEqual(generated_thumbnail.animated, True)
        self.assertEqual(generated_thumbnail.width, 100)
        self.assertEqual(generated_thumbnail.height, 44)
        self.assertEqual(generated_thumbnail.content_type, "image/webp")
        self.assertGreater(generated_thumbnail.byte_size, 200)
        self.assertLess(generated_thumbnail.byte_size, 2 * 1024)

        self.assertEqual(
            get_image_thumbnail_path(image_attachment, generated_thumbnail),
            f"thumbnail/{path_id}/100x75-anim.webp",
        )
        parsed_path = split_thumbnail_path(f"thumbnail/{path_id}/100x75-anim.webp")
        self.assertEqual(parsed_path[0], path_id)
        self.assertIsInstance(parsed_path[1], BaseThumbnailFormat)
        self.assertEqual(str(parsed_path[1]), str(generated_thumbnail))

        self.assertEqual(
            sorted([r[0] for r in all_message_attachments(include_thumbnails=True)]),
            sorted([path_id, f"thumbnail/{path_id}/100x75-anim.webp"]),
        )

        self.assertEqual(ensure_thumbnails(image_attachment), 0)

        bigger_thumb_format = ThumbnailFormat("webp", 150, 100, opts="Q=90", animated=False)
        with patch("zerver.lib.thumbnail.THUMBNAIL_OUTPUT_FORMATS", [bigger_thumb_format]):
            self.assertEqual(ensure_thumbnails(image_attachment), 1)
        self.assert_length(image_attachment.thumbnail_metadata, 2)

        bigger_thumbnail = StoredThumbnailFormat(**image_attachment.thumbnail_metadata[1])

        self.assertEqual(str(bigger_thumbnail), "150x100.webp")
        self.assertEqual(bigger_thumbnail.animated, False)
        # We don't scale up, so these are the original dimensions
        self.assertEqual(bigger_thumbnail.width, 128)
        self.assertEqual(bigger_thumbnail.height, 56)
        self.assertEqual(bigger_thumbnail.content_type, "image/webp")
        self.assertGreater(bigger_thumbnail.byte_size, 200)
        self.assertLess(bigger_thumbnail.byte_size, 2 * 1024)

        self.assertEqual(
            sorted([r[0] for r in all_message_attachments(include_thumbnails=True)]),
            sorted(
                [
                    path_id,
                    f"thumbnail/{path_id}/100x75-anim.webp",
                    f"thumbnail/{path_id}/150x100.webp",
                ]
            ),
        )

    @patch(
        "zerver.lib.thumbnail.THUMBNAIL_OUTPUT_FORMATS",
        [ThumbnailFormat("webp", 100, 75, animated=False)],
    )
    def test_bad_upload(self) -> None:
        assert settings.LOCAL_FILES_DIR
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)

        with self.captureOnCommitCallbacks(execute=True):
            with get_test_image_file("truncated.gif") as image_file:
                response = self.assert_json_success(
                    self.client_post("/json/user_uploads", {"file": image_file})
                )
            path_id = re.sub(r"/user_uploads/", "", response["url"])
            self.assertEqual(Attachment.objects.filter(path_id=path_id).count(), 1)

            # This doesn't generate an ImageAttachment row because it's corrupted
            self.assertEqual(ImageAttachment.objects.filter(path_id=path_id).count(), 0)

        # Fake making one, based on if just part of the file is readable
        image_attachment = ImageAttachment.objects.create(
            realm_id=hamlet.realm_id,
            path_id=path_id,
            original_height_px=128,
            original_width_px=128,
            frames=1,
            thumbnail_metadata=[],
        )
        self.assert_length(missing_thumbnails(image_attachment), 1)
        with self.assertLogs("zerver.worker.thumbnail", level="ERROR") as error_log:
            self.assertEqual(ensure_thumbnails(image_attachment), 0)
            libvips_version = (pyvips.version(0), pyvips.version(1))
            # This error message changed
            if libvips_version < (8, 13):  # nocoverage # branch varies with version
                expected_message = "gifload_buffer: Insufficient data to do anything"
            else:  # nocoverage # branch varies with version
                expected_message = "gifload_buffer: no frames in GIF"
            self.assertTrue(expected_message in error_log.output[0])

        # It should have now been removed
        self.assertEqual(ImageAttachment.objects.filter(path_id=path_id).count(), 0)

    def test_missing_thumbnails(self) -> None:
        image_attachment = ImageAttachment(
            path_id="example",
            original_width_px=150,
            original_height_px=100,
            frames=1,
            thumbnail_metadata=[],
        )
        with patch("zerver.lib.thumbnail.THUMBNAIL_OUTPUT_FORMATS", []):
            self.assertEqual(missing_thumbnails(image_attachment), [])

        still_webp = ThumbnailFormat("webp", 100, 75, animated=False, opts="Q=90")
        with patch("zerver.lib.thumbnail.THUMBNAIL_OUTPUT_FORMATS", [still_webp]):
            self.assertEqual(missing_thumbnails(image_attachment), [still_webp])

        anim_webp = ThumbnailFormat("webp", 100, 75, animated=True, opts="Q=90")
        with patch("zerver.lib.thumbnail.THUMBNAIL_OUTPUT_FORMATS", [still_webp, anim_webp]):
            # It's not animated, so the animated format doesn't appear at all
            self.assertEqual(missing_thumbnails(image_attachment), [still_webp])

        still_jpeg = ThumbnailFormat("jpeg", 100, 75, animated=False, opts="Q=90")
        with patch(
            "zerver.lib.thumbnail.THUMBNAIL_OUTPUT_FORMATS", [still_webp, anim_webp, still_jpeg]
        ):
            # But other still formats do
            self.assertEqual(missing_thumbnails(image_attachment), [still_webp, still_jpeg])

        # If we have a rendered 150x100.webp, then we're not missing it
        rendered_still_webp = StoredThumbnailFormat(
            "webp",
            100,
            75,
            animated=False,
            width=150,
            height=50,
            content_type="image/webp",
            byte_size=1234,
        )
        image_attachment.thumbnail_metadata = [asdict(rendered_still_webp)]
        with patch(
            "zerver.lib.thumbnail.THUMBNAIL_OUTPUT_FORMATS", [still_webp, anim_webp, still_jpeg]
        ):
            self.assertEqual(missing_thumbnails(image_attachment), [still_jpeg])

        # If we have the still, and it's animated, we do still need the animated
        image_attachment.frames = 10
        with patch(
            "zerver.lib.thumbnail.THUMBNAIL_OUTPUT_FORMATS", [still_webp, anim_webp, still_jpeg]
        ):
            self.assertEqual(missing_thumbnails(image_attachment), [anim_webp, still_jpeg])


class TestThumbnailRetrieval(ZulipTestCase):
    @contextmanager
    def mock_formats(self, thumbnail_formats: list[ThumbnailFormat]) -> Iterator[None]:
        with (
            patch("zerver.lib.thumbnail.THUMBNAIL_OUTPUT_FORMATS", thumbnail_formats),
            patch("zerver.views.upload.THUMBNAIL_OUTPUT_FORMATS", thumbnail_formats),
        ):
            yield

    def test_get_thumbnail(self) -> None:
        assert settings.LOCAL_FILES_DIR
        hamlet = self.example_user("hamlet")
        self.login_user(hamlet)

        webp_anim = ThumbnailFormat("webp", 100, 75, animated=True)
        webp_still = ThumbnailFormat("webp", 100, 75, animated=False)
        with self.mock_formats([webp_anim, webp_still]):
            with (
                self.captureOnCommitCallbacks(execute=True),
                get_test_image_file("animated_unequal_img.gif") as image_file,
            ):
                json_response = self.assert_json_success(
                    self.client_post("/json/user_uploads", {"file": image_file})
                )
                path_id = re.sub(r"/user_uploads/", "", json_response["url"])

                # Image itself is available immediately
                response = self.client_get(f"/user_uploads/{path_id}")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["Content-Type"], "image/gif")

                # Format we don't have
                response = self.client_get(f"/user_uploads/thumbnail/{path_id}/1x1.png")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.headers["Content-Type"], "image/png")

                # Exit the block, triggering the thumbnailing worker

            thumbnail_response = self.client_get(
                f"/user_uploads/thumbnail/{path_id}/{webp_still!s}"
            )
            self.assertEqual(thumbnail_response.status_code, 200)
            self.assertEqual(thumbnail_response.headers["Content-Type"], "image/webp")
            self.assertLess(
                int(thumbnail_response.headers["Content-Length"]),
                int(response.headers["Content-Length"]),
            )

            animated_response = self.client_get(f"/user_uploads/thumbnail/{path_id}/{webp_anim!s}")
            self.assertEqual(animated_response.status_code, 200)
            self.assertEqual(animated_response.headers["Content-Type"], "image/webp")
            self.assertLess(
                int(thumbnail_response.headers["Content-Length"]),
                int(animated_response.headers["Content-Length"]),
            )

            # Invalid thumbnail format
            response = self.client_get(f"/user_uploads/thumbnail/{path_id}/bogus")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.headers["Content-Type"], "image/png")

            # path_id for a non-image
            with (
                self.captureOnCommitCallbacks(execute=True),
                get_test_image_file("text.txt") as text_file,
            ):
                json_response = self.assert_json_success(
                    self.client_post("/json/user_uploads", {"file": text_file})
                )
                text_path_id = re.sub(r"/user_uploads/", "", json_response["url"])
            response = self.client_get(f"/user_uploads/thumbnail/{text_path_id}/{webp_still!s}")
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.headers["Content-Type"], "image/png")

        # Shrink the list of formats, and check that we can still get
        # the thumbnails that were generated at the time
        with self.mock_formats([webp_still]):
            response = self.client_get(f"/user_uploads/thumbnail/{path_id}/{webp_still!s}")
            self.assertEqual(response.status_code, 200)

            response = self.client_get(f"/user_uploads/thumbnail/{path_id}/{webp_anim!s}")
            self.assertEqual(response.status_code, 200)

        # Grow the format list, and check that fetching that new
        # format generates all of the missing formats
        jpeg_still = ThumbnailFormat("jpg", 100, 75, animated=False)
        big_jpeg_still = ThumbnailFormat("jpg", 200, 150, animated=False)
        with (
            self.mock_formats([webp_still, jpeg_still, big_jpeg_still]),
            patch.object(
                pyvips.Image, "thumbnail_buffer", wraps=pyvips.Image.thumbnail_buffer
            ) as thumb_mock,
        ):
            small_response = self.client_get(f"/user_uploads/thumbnail/{path_id}/{jpeg_still!s}")
            self.assertEqual(small_response.status_code, 200)
            self.assertEqual(small_response.headers["Content-Type"], "image/jpeg")
            # This made two thumbnails
            self.assertEqual(thumb_mock.call_count, 2)

            thumb_mock.reset_mock()
            big_response = self.client_get(f"/user_uploads/thumbnail/{path_id}/{big_jpeg_still!s}")
            self.assertEqual(big_response.status_code, 200)
            self.assertEqual(big_response.headers["Content-Type"], "image/jpeg")
            thumb_mock.assert_not_called()

            self.assertLess(
                int(small_response.headers["Content-Length"]),
                int(big_response.headers["Content-Length"]),
            )

        # Upload a static image, and verify that we only generate the still versions
        with self.mock_formats([webp_anim, webp_still, jpeg_still]):
            with (
                self.captureOnCommitCallbacks(execute=True),
                get_test_image_file("img.tif") as image_file,
            ):
                json_response = self.assert_json_success(
                    self.client_post("/json/user_uploads", {"file": image_file})
                )
                path_id = re.sub(r"/user_uploads/", "", json_response["url"])
                # Exit the block, triggering the thumbnailing worker

            still_response = self.client_get(f"/user_uploads/thumbnail/{path_id}/{webp_still!s}")
            self.assertEqual(still_response.status_code, 200)
            self.assertEqual(still_response.headers["Content-Type"], "image/webp")

            # We can request -anim -- we didn't render it, but we the
            # "closest we rendered" logic kicks in, and we get the
            # still webp, rather than a 404
            animated_response = self.client_get(f"/user_uploads/thumbnail/{path_id}/{webp_anim!s}")
            self.assertEqual(animated_response.status_code, 200)
            self.assertEqual(animated_response.headers["Content-Type"], "image/webp")
            # Double-check that we don't actually have the animated version, by comparing file sizes
            self.assertEqual(
                animated_response.headers["Content-Length"],
                still_response.headers["Content-Length"],
            )

            response = self.client_get(f"/user_uploads/thumbnail/{path_id}/{jpeg_still!s}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["Content-Type"], "image/jpeg")

    def test_closest_format(self) -> None:
        self.login_user(self.example_user("hamlet"))

        webp_anim = ThumbnailFormat("webp", 100, 75, animated=True)
        webp_still = ThumbnailFormat("webp", 100, 75, animated=False)
        tiny_webp_still = ThumbnailFormat("webp", 10, 10, animated=False)
        gif_still = ThumbnailFormat("gif", 100, 75, animated=False)
        with (
            self.mock_formats([webp_anim, webp_still, tiny_webp_still, gif_still]),
            self.captureOnCommitCallbacks(execute=True),
            get_test_image_file("animated_img.gif") as image_file,
        ):
            json_response = self.assert_json_success(
                self.client_post("/json/user_uploads", {"file": image_file})
            )
            path_id = re.sub(r"/user_uploads/", "", json_response["url"])
            # Exit the block, triggering the thumbnailing worker

        image_attachment = ImageAttachment.objects.get(path_id=path_id)
        rendered_formats = [
            StoredThumbnailFormat(**data) for data in image_attachment.thumbnail_metadata
        ]
        accepts = [MediaType("image/webp"), MediaType("image/*"), MediaType("*/*;q=0.8")]

        # Prefer to match -animated, even though we have a .gif
        self.assertEqual(
            str(
                closest_thumbnail_format(
                    ThumbnailFormat("gif", 100, 75, animated=True), accepts, rendered_formats
                )
            ),
            "100x75-anim.webp",
        )

        # Match the extension, even if we're an exact match for a different size
        self.assertEqual(
            str(
                closest_thumbnail_format(
                    ThumbnailFormat("gif", 10, 10, animated=False), accepts, rendered_formats
                )
            ),
            "100x75.gif",
        )

        # If they request an extension we don't do, then we look for the formats they prefer
        self.assertEqual(
            str(
                closest_thumbnail_format(
                    ThumbnailFormat("tif", 10, 10, animated=False), accepts, rendered_formats
                )
            ),
            "10x10.webp",
        )
        self.assertEqual(
            str(
                closest_thumbnail_format(
                    ThumbnailFormat("tif", 10, 10, animated=False),
                    [MediaType("image/webp;q=0.9"), MediaType("image/gif")],
                    rendered_formats,
                )
            ),
            "100x75.gif",
        )
        self.assertEqual(
            str(
                closest_thumbnail_format(
                    ThumbnailFormat("tif", 10, 10, animated=False),
                    [MediaType("image/gif")],
                    rendered_formats,
                )
            ),
            "100x75.gif",
        )

        # Closest width
        self.assertEqual(
            str(
                closest_thumbnail_format(
                    ThumbnailFormat("webp", 20, 100, animated=False), accepts, rendered_formats
                )
            ),
            "10x10.webp",
        )
        self.assertEqual(
            str(
                closest_thumbnail_format(
                    ThumbnailFormat("webp", 80, 10, animated=False), accepts, rendered_formats
                )
            ),
            "100x75.webp",
        )

        # Smallest filesize if they have no media preference
        self.assertEqual(
            str(
                closest_thumbnail_format(
                    ThumbnailFormat("tif", 100, 75, animated=False),
                    [MediaType("image/gif"), MediaType("image/webp")],
                    rendered_formats,
                )
            ),
            "100x75.webp",
        )
