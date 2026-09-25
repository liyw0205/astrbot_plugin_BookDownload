import asyncio
import io
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from PIL import Image

from bookdownload.commands import normalize_result_mode, parse_natural_book_search, parse_search_arguments
from bookdownload.download import BookDownloadService, DownloadError
from bookdownload.models import SearchResult
from bookdownload.presentation import _cover_candidates, _fetch_cover, _font, _result_id, format_text_results, render_result_card


class CommandParsingTests(unittest.TestCase):
    def test_natural_book_search_query(self):
        self.assertEqual(parse_natural_book_search("搜一下碧蓝航线的本子"), "碧蓝航线")
        self.assertEqual(parse_natural_book_search("帮我找 某作品的同人本。"), "某作品")
        self.assertEqual(parse_natural_book_search("介绍一下碧蓝航线"), "")

    def test_result_modes(self):
        self.assertEqual(normalize_result_mode("图卡"), "card")
        self.assertEqual(normalize_result_mode("图文"), "image_text")
        self.assertEqual(normalize_result_mode("", "text"), "text")
        with self.assertRaises(ValueError):
            normalize_result_mode("视频")

    def test_search_arguments_preserve_query_and_optional_mode(self):
        self.assertEqual(parse_search_arguments("blue archive"), ("blue archive", 1, ""))
        self.assertEqual(parse_search_arguments("blue archive 3 图文"), ("blue archive", 3, "image_text"))
        self.assertEqual(parse_search_arguments("图卡"), ("", 1, "card"))

    def test_numeric_eh_id_requires_eh_source_context(self):
        with self.assertRaises(DownloadError):
            BookDownloadService({})._parse_url("12345")

    def test_jm_album_urls_are_validated_and_normalized(self):
        self.assertEqual(
            BookDownloadService._parse_url("https://18comic.vip/album/12345/"),
            ("jmcomic", "12345"),
        )
        with self.assertRaises(DownloadError):
            BookDownloadService._parse_url("https://attacker.example/album/12345/")

    def test_result_ids_support_jm_urls_and_text_results(self):
        result = SearchResult("jmcomic", "JM title", "https://18comic.vip/album/12345/")
        self.assertEqual(_result_id(result), "12345")
        self.assertIn("ID: 12345", format_text_results([result]))

    def test_result_card_renders_a_valid_image_with_gallery_id(self):
        path = asyncio.run(
            render_result_card(
                [SearchResult("nhentai", "A sample title", "https://nhentai.net/g/12345/", page_count=8)],
                proxy=None,
                timeout=5,
            )
        )
        try:
            with Image.open(path) as card:
                self.assertEqual(card.format, "PNG")
                self.assertGreater(card.width, 500)
                self.assertGreater(card.height, 300)
                self.assertEqual(card.getpixel((0, 0)), (243, 246, 244))
        finally:
            Path(path).unlink(missing_ok=True)

    def test_bundled_font_contains_chinese_glyphs(self):
        font = _font(24)
        self.assertTrue(str(font.path).endswith("WenQuanYiZenHei.ttc"))
        self.assertIsNotNone(font.getmask("本子搜索").getbbox())
        self.assertIsNotNone(font.getmask("NHENTAI 2").getbbox())

    def test_nhentai_cover_candidates_try_mirrors(self):
        result = SearchResult(
            "nhentai",
            "A sample title",
            "https://nhentai.net/g/12345/",
            cover_url="https://t1.nhentai.net/galleries/789/cover.jpg",
        )
        candidates = _cover_candidates(result)
        self.assertEqual(candidates[0], result.cover_url)
        self.assertIn("https://t.nhentai.net/galleries/789/cover.jpg", candidates)
        self.assertIn("https://t5.nhentai.net/galleries/789/cover.jpg", candidates)

    def test_cover_fetch_tries_fallback_and_decodes_generic_mime(self):
        async def exercise():
            image_buffer = io.BytesIO()
            Image.new("RGB", (32, 48), "red").save(image_buffer, format="WEBP")
            image_data = image_buffer.getvalue()

            async def redirect(_request):
                raise web.HTTPFound(location="/cover")

            async def missing(_request):
                raise web.HTTPNotFound()

            async def cover(request):
                response = web.StreamResponse(headers={"Content-Type": "application/octet-stream"})
                await response.prepare(request)
                split = len(image_data) // 3
                await response.write(image_data[:split])
                await asyncio.sleep(0.02)
                await response.write(image_data[split : split * 2])
                await asyncio.sleep(0.02)
                await response.write(image_data[split * 2 :])
                await response.write_eof()
                return response

            app = web.Application()
            app.router.add_get("/missing", missing)
            app.router.add_get("/redirect", redirect)
            app.router.add_get("/cover", cover)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            result = SearchResult(
                "nhentai",
                "A sample title",
                "https://nhentai.net/g/12345/",
                cover_url=f"http://127.0.0.1:{port}/missing",
            )
            try:
                with patch(
                    "bookdownload.presentation.BookSearchService.is_display_image_url",
                    side_effect=lambda url: url.startswith(f"http://127.0.0.1:{port}/"),
                ), patch(
                    "bookdownload.presentation._cover_candidates",
                    return_value=[
                        f"http://127.0.0.1:{port}/missing",
                        f"http://127.0.0.1:{port}/redirect",
                    ],
                ):
                    async with aiohttp.ClientSession() as session:
                        fetched = await _fetch_cover(session, None, result)
                self.assertIsNotNone(fetched)
                self.assertEqual(fetched.size, (32, 48))
                fetched.close()
            finally:
                await runner.cleanup()

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
