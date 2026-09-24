import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image

from bookdownload.detail_presentation import render_detail_card
from bookdownload.models import GalleryDetail


class DetailPresentationTests(unittest.TestCase):
    def test_detail_card_renders_six_preview_slots(self):
        async def fake_fetch(_session, _proxy, _url, _referer):
            return Image.new("RGB", (80, 120), "white")

        detail = GalleryDetail(
            source="nhentai",
            gallery_id="123456",
            title="A sample title",
            url="https://nhentai.net/g/123456/",
            cover_url="https://t1.nhentai.net/galleries/123/cover.jpg",
            page_count=20,
            page_urls=tuple(f"https://i.nhentai.net/galleries/123/{index}.jpg" for index in range(1, 9)),
        )
        with patch("bookdownload.detail_presentation._fetch_image", new=AsyncMock(side_effect=fake_fetch)) as fetch:
            path = asyncio.run(render_detail_card(detail, proxy=None, timeout=5))
        try:
            with Image.open(path) as card:
                self.assertEqual(card.format, "PNG")
                self.assertEqual(card.size, (820, 1808))
            self.assertEqual(fetch.await_count, 7)
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
