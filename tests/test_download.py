import tempfile
import unittest
from pathlib import Path

from PIL import Image

from bookdownload.download import BookDownloadService, DownloadError, normalize_download_format


class DownloadUtilityTests(unittest.TestCase):
    def test_download_format_aliases(self):
        self.assertEqual(normalize_download_format("压缩包"), "archive")
        self.assertEqual(normalize_download_format("图片"), "images")
        self.assertEqual(normalize_download_format("长图"), "long_image")
        self.assertEqual(normalize_download_format("", "pdf"), "pdf")
        with self.assertRaises(ValueError):
            normalize_download_format("video")

    def test_gallery_url_validation(self):
        self.assertEqual(BookDownloadService._parse_url("https://nhentai.net/g/123"), ("nhentai", "123"))
        self.assertEqual(BookDownloadService._parse_url("https://e-hentai.org/g/123/abc/"), ("ehentai", "123"))
        with self.assertRaises(DownloadError):
            BookDownloadService._parse_url("http://nhentai.net/g/123/")

    def test_long_image_is_split_every_ten_pages(self):
        root = Path(tempfile.mkdtemp())
        try:
            paths = []
            for index in range(11):
                path = root / f"{index}.png"
                Image.new("RGB", (10, 20), "white").save(path)
                paths.append(path)
            outputs = BookDownloadService({})._build_outputs("long_image", "sample", root, paths)
            self.assertEqual(len(outputs), 2)
            with Image.open(outputs[0]) as first:
                self.assertEqual(first.height, 200)
            with Image.open(outputs[1]) as second:
                self.assertEqual(second.height, 20)
        finally:
            for path in root.iterdir():
                path.unlink()
            root.rmdir()


if __name__ == "__main__":
    unittest.main()
