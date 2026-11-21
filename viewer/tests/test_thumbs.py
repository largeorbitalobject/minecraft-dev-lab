import unittest

import helpers  # noqa: F401
import make_fixtures as mf
from mcdevlab_viewer import thumbs


class Thumbs(unittest.TestCase):
    def test_pure_python_path(self):
        big = mf.png(w=400, h=250, seed=2)
        data, ext, size = thumbs.make_thumb(big, max_w=100, force_pure=True)
        self.assertEqual(ext, "png")
        self.assertEqual(size, (400, 250))
        self.assertEqual(thumbs.png_size(data), (100, 62))
        w, h, ch, rows = thumbs.decode_png(data)
        self.assertEqual((w, h, ch, len(rows)), (100, 62, 3, 62))

    def test_pil_path_when_available(self):
        if not thumbs.HAVE_PIL:
            self.skipTest("no Pillow")
        data, ext, size = thumbs.make_thumb(mf.png(w=400, h=250), max_w=100)
        self.assertEqual(ext, "jpg")
        self.assertEqual(data[:2], b"\xff\xd8")

    def test_rejects_non_png(self):
        with self.assertRaises(ValueError):
            thumbs.make_thumb(b"not a png at all, sorry....")


if __name__ == "__main__":
    unittest.main()
