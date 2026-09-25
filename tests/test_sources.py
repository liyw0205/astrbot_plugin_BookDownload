import unittest
import asyncio
from unittest.mock import AsyncMock, patch

from bookdownload.image_input import ImageInputError, _public_https_url, _read_component_image
from bookdownload.models import SearchResult
from bookdownload.network import mask_proxy, normalize_proxy
from bookdownload.service import BookSearchService
from bookdownload.sources import ehentai_search_url, nhentai_result, parse_ehentai_results


class SourceParserTests(unittest.TestCase):
    def test_nhentai_result_normalizes_title_cover_tags_and_pages(self):
        result = nhentai_result(
            {
                "id": 123,
                "media_id": 456,
                "title": {"english": "English title", "pretty": "Pretty title"},
                "images": {"cover": {"t": "w"}, "pages": [{}, {}]},
                "num_pages": 2,
                "tags": [{"name": "language:chinese"}, {"name": "artist:test"}],
            }
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.title, "English title")
        self.assertEqual(result.cover_url, "https://t.nhentai.net/galleries/456/cover.webp")
        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.tags, ("language:chinese", "artist:test"))

    def test_nhentai_v2_search_result_uses_title_and_thumbnail_path(self):
        result = nhentai_result(
            {
                "id": 683707,
                "media_id": "4204841",
                "english_title": "English title",
                "japanese_title": "Japanese title",
                "thumbnail": "galleries/4204841/thumb.webp",
                "num_pages": 30,
                "tag_ids": [1033, 1643],
            }
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.title, "English title")
        self.assertEqual(
            result.cover_url,
            "https://t1.nhentai.net/galleries/4204841/thumb.webp",
        )
        self.assertEqual(result.page_count, 30)
        self.assertEqual(result.tags, ())

    def test_invalid_nhentai_id_is_ignored(self):
        self.assertIsNone(nhentai_result({"id": "bad"}))

    def test_ehentai_parser_deduplicates_and_extracts_gallery(self):
        html = """
        <div class="gl1t">
          <a href="https://e-hentai.org/g/123456/abcdef1234/">
            <img src="https://ehgt.org/thumb.jpg" />
            <div class="glink">Sample gallery</div>
            <div>24 pages</div>
          </a>
        </div>
        <a href="https://e-hentai.org/g/123456/abcdef1234/">duplicate</a>
        <a href="https://example.com/g/123456/abcdef1234/">external</a>
        """
        results = parse_ehentai_results(html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Sample gallery")
        self.assertEqual(results[0].page_count, 24)
        self.assertEqual(results[0].cover_url, "https://ehgt.org/thumb.jpg")

    def test_ehentai_parser_handles_table_layout(self):
        html = """
        <table><tr>
          <td><a href="https://e-hentai.org/g/987654/abcdef1234/">
            <img src="https://ehgt.org/table-thumb.jpg" />
          </a></td>
          <td><a href="https://e-hentai.org/g/987654/abcdef1234/">
            <div class="glink">Clean gallery title</div>
          </a> untranslated tags artist:sample 42 pages</td>
        </tr></table>
        """
        results = parse_ehentai_results(html)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "Clean gallery title")
        self.assertEqual(results[0].page_count, 42)
        self.assertEqual(results[0].cover_url, "https://ehgt.org/table-thumb.jpg")

    def test_ehentai_search_url_uses_zero_based_page(self):
        self.assertEqual(
            ehentai_search_url("blue archive", 2),
            "https://e-hentai.org/?f_search=blue+archive&page=1",
        )


class SearchServiceTests(unittest.TestCase):
    def test_proxy_schemes_are_supported_and_normalized(self):
        for value in (
            "http://127.0.0.1:8080",
            "https://user:pass@example.com:8443",
            "socks5://127.0.0.1:1080",
            "socks5h://user:pass@127.0.0.1:1080",
        ):
            self.assertEqual(normalize_proxy(value), value)
        self.assertIsNone(normalize_proxy(""))
        with self.assertRaises(ValueError):
            normalize_proxy("ftp://127.0.0.1:21")
        self.assertEqual(
            mask_proxy("socks5h://admin:secret@127.0.0.1:1080"),
            "socks5h://admin:********@127.0.0.1:1080",
        )
        self.assertNotIn("secret", mask_proxy("https://user:secret@example.com:8443"))

    def test_source_aliases_and_validation(self):
        self.assertEqual(BookSearchService._normalize_source("all"), ["nhentai", "ehentai", "jmcomic"])
        self.assertEqual(BookSearchService._normalize_source("eh"), ["ehentai"])
        self.assertEqual(BookSearchService._normalize_source("jm"), ["jmcomic"])
        with self.assertRaises(ValueError):
            BookSearchService._normalize_source("unknown")

    def test_empty_and_out_of_range_config_values_are_normalized(self):
        service = BookSearchService({"timeout": "", "max_results": 100, "max_image_mb": 0})
        self.assertEqual(service.timeout, 30)
        self.assertEqual(service.max_results, 12)
        self.assertEqual(service.max_image_bytes, 1024 * 1024)

    def test_result_image_allowlist_requires_https_and_known_hosts(self):
        allowed = BookSearchService.is_display_image_url
        self.assertTrue(allowed("https://t.nhentai.net/cover.jpg"))
        self.assertTrue(allowed("https://ehgt.org/cover.jpg"))
        self.assertTrue(allowed("https://cdn-msp.jmapiproxy1.cc/cover.jpg"))
        self.assertFalse(allowed("http://t.nhentai.net/cover.jpg"))
        self.assertFalse(allowed("https://attacker.example/cover.jpg"))

    def test_all_sources_are_interleaved_before_result_limit(self):
        service = BookSearchService({"max_results": 4})
        nhentai = [
            SearchResult("nhentai", "NH 1", "https://nhentai.net/g/1/"),
            SearchResult("nhentai", "NH 2", "https://nhentai.net/g/2/"),
        ]
        ehentai = [SearchResult("ehentai", "EH 1", "https://e-hentai.org/g/1/abc/")]
        jmcomic = [SearchResult("jmcomic", "JM 1", "https://18comic.vip/album/1/")]

        async def search():
            with patch.object(service, "_search_nhentai", new=AsyncMock(return_value=nhentai)):
                with patch.object(service, "_search_ehentai", new=AsyncMock(return_value=ehentai)):
                    with patch("bookdownload.service.search_jmcomic", new=AsyncMock(return_value=jmcomic)):
                        return await service.search_text("sample")

        results, errors = asyncio.run(search())
        self.assertEqual([result.source for result in results], ["nhentai", "ehentai", "jmcomic", "nhentai"])
        self.assertEqual(errors, [])

    def test_auto_image_engine_uses_configured_engine(self):
        service = BookSearchService({"reverse_engine": "saucenao"})

        async def search():
            with patch.object(service, "_reverse_saucenao", new=AsyncMock(return_value=[])) as reverse:
                await service.search_image(b"image", engine="auto")
                reverse.assert_awaited_once_with(b"image")

        asyncio.run(search())

    def test_jm_image_engine_uses_saucenao_and_filters_to_jm_matches(self):
        service = BookSearchService({})
        candidates = [
            SearchResult("jmcomic", "JM", "https://18comic.vip/album/1/"),
            SearchResult("saucenao", "Other", "https://example.com/work"),
        ]

        async def search():
            with patch.object(service, "_reverse_saucenao", new=AsyncMock(return_value=candidates)) as reverse:
                results = await service.search_image(b"image", engine="jmcomic")
                reverse.assert_awaited_once_with(b"image")
                return results

        self.assertEqual([result.source for result in asyncio.run(search())], ["jmcomic"])

    def test_jm_search_does_not_send_language_operator_to_site_search(self):
        service = BookSearchService({"language_filter_enabled": True, "daily_push_language": "chinese"})

        async def search():
            with patch("bookdownload.service.search_jmcomic", new=AsyncMock(return_value=[])) as jm_search:
                await service.search_text("language:chinese blue archive", source="jm")
                return jm_search

        jm_search = asyncio.run(search())
        self.assertEqual(jm_search.await_args.args[:2], ("blue archive", 1))


class ImageUrlSecurityTests(unittest.TestCase):
    def test_external_image_urls_must_be_public_https(self):
        self.assertTrue(asyncio.run(_public_https_url("https://8.8.8.8/image.jpg")))
        self.assertFalse(asyncio.run(_public_https_url("http://8.8.8.8/image.jpg")))
        self.assertFalse(asyncio.run(_public_https_url("https://127.0.0.1/image.jpg")))
        self.assertFalse(asyncio.run(_public_https_url("https://[::1]/image.jpg")))

    def test_oversized_data_url_is_rejected_before_decoding(self):
        class Component:
            url = "data:image/png;base64," + ("!" * 2048)

        with self.assertRaisesRegex(ImageInputError, "超过大小限制"):
            asyncio.run(_read_component_image(Component(), 1024, 30, None))


if __name__ == "__main__":
    unittest.main()
