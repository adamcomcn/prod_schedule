"""Railway builds this app with Railpack from prod_schedule/railpack.json. The PDF reports need the
WenQuanYi font installed in the image, and the Python version must match .python-version."""
import json
import os
import unittest

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RailpackConfigTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(APP_DIR, 'railpack.json'), encoding='utf-8') as handle:
            self.config = json.load(handle)

    def test_chinese_pdf_font_is_installed(self):
        self.assertIn('fonts-wqy-microhei', self.config['deploy']['aptPackages'])

    def test_python_version_matches_python_version_file(self):
        with open(os.path.join(APP_DIR, '.python-version'), encoding='utf-8') as handle:
            self.assertEqual(self.config['packages']['python'], handle.read().strip())


if __name__ == '__main__':
    unittest.main()
