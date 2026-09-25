import asyncio
import sys
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from bookdownload.details import DetailError, GalleryDetailService, parse_gallery_reference
from bookdownload.download import BookDownloadService
from bookdownload.jmcomic_source import jm_album_id_from_url, search_jmcomic


class FakeAlbum:
    album_id = "12345"
    name = "A JM title"
    tags = ["中文", "blue archive"]
    authors = ["artist name"]
    works = ["blue archive"]
    page_count = 24
    episode_list = []


class FakeSearchPage:
    def iter_id_title_tag(self):
        yield "12345", "A JM title", ["中文", "blue archive"]


class FakeClient:
    album = FakeAlbum()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def search_site(self, query, page):
        self.search_args = (query, page)
        return FakeSearchPage()

    async def get_album_detail(self, _album_id):
        return self.album


class FakeOption:
    config = None

    @classmethod
    def construct(cls, config):
        cls.config = config
        return cls()

    def new_jm_async_client(self, **_kwargs):
        return FakeClient()


def fake_jmcomic(download_fn=None):
    module = types.ModuleType("jmcomic")
    module.JmOption = FakeOption
    module.JmcomicText = types.SimpleNamespace(
        get_album_cover_url=lambda album_id, size="": f"https://cdn-msp.jmapiproxy1.cc/media/albums/{album_id}{size}.jpg"
    )
    module.download_album_async = download_fn or AsyncMock()
    return module


class JmComicSourceTests(unittest.TestCase):
    def test_jm_reference_requires_supported_source_or_site(self):
        self.assertEqual(jm_album_id_from_url("https://18comic.vip/album/12345/"), "12345")
        self.assertEqual(jm_album_id_from_url("https://www.jmcomic.me/album/?id=12345"), "12345")
        self.assertEqual(jm_album_id_from_url("https://attacker.example/album/12345/"), "")

    def test_detail_reference_accepts_jm_urls_and_numeric_ids(self):
        self.assertEqual(
            parse_gallery_reference("https://18comic.vip/album/12345/"),
            ("jmcomic", "12345", "https://18comic.vip/album/12345/"),
        )
        self.assertEqual(parse_gallery_reference("12345", "jmcomic"), ("jmcomic", "12345", ""))
        self.assertEqual(parse_gallery_reference("JM12345", "jmcomic"), ("jmcomic", "12345", ""))
        with self.assertRaises(DetailError):
            parse_gallery_reference("12345")
        with self.assertRaises(DetailError):
            parse_gallery_reference("https://18comic.vip/g/12345/abcdef/")

    def test_existing_gallery_reference_parsing_is_unchanged(self):
        self.assertEqual(
            parse_gallery_reference("https://nhentai.net/g/12345/"),
            ("nhentai", "12345", "https://nhentai.net/g/12345/"),
        )
        self.assertEqual(
            parse_gallery_reference("https://e-hentai.org/g/12345/abcdef/"),
            ("ehentai", "12345", "https://e-hentai.org/g/12345/abcdef/"),
        )

    def test_search_maps_library_results_and_passes_proxy_and_timeout(self):
        async def exercise():
            with patch.dict(sys.modules, {"jmcomic": fake_jmcomic()}):
                results = await search_jmcomic(
                    "blue archive",
                    2,
                    proxy="socks5h://127.0.0.1:1080",
                    timeout=17,
                )
            return results

        results = asyncio.run(exercise())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source, "jmcomic")
        self.assertEqual(results[0].url, "https://18comic.vip/album/12345/")
        self.assertEqual(results[0].cover_url, "https://cdn-msp.jmapiproxy1.cc/media/albums/12345_3x4.jpg")
        self.assertEqual(FakeOption.config["client"]["timeout"], 17)
        self.assertEqual(
            FakeOption.config["client"]["postman"]["meta_data"]["proxies"],
            "socks5h://127.0.0.1:1080",
        )

    def test_detail_fetch_maps_jm_metadata_without_preview_downloads(self):
        async def exercise():
            service = GalleryDetailService({})
            with patch.dict(sys.modules, {"jmcomic": fake_jmcomic()}):
                return await service.fetch("12345", source_hint="jmcomic", include_previews=False)

        detail = asyncio.run(exercise())
        self.assertEqual(detail.source, "jmcomic")
        self.assertEqual(detail.languages, ("中文", "chinese"))
        self.assertEqual(detail.artists, ("artist name",))
        self.assertEqual(detail.groups, ("blue archive",))

    def test_download_refuses_over_limit_before_starting_library_download(self):
        download_fn = AsyncMock()

        async def exercise(temp_root):
            service = BookDownloadService({"download_max_pages": 10})
            with patch("bookdownload.download.get_astrbot_temp_path", return_value=temp_root):
                with patch.dict(sys.modules, {"jmcomic": fake_jmcomic(download_fn)}):
                    with self.assertRaisesRegex(ValueError, "超过当前下载上限"):
                        await service.download("12345", source_hint="jmcomic")

        with tempfile.TemporaryDirectory() as temp_root:
            asyncio.run(exercise(temp_root))
        download_fn.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
