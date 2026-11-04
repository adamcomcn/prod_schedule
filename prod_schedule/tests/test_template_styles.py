"""A page's extra_style block is CSS only: a <style> tag in it ends the shared style
element early and shows the CSS after it as text on the page."""
import glob
import os
import re
import unittest

TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'templates')


class ExtraStyleTests(unittest.TestCase):
    def test_no_style_tags_inside_extra_style(self):
        bad = []
        for path in glob.glob(os.path.join(TEMPLATES, '*.html')):
            text = open(path, encoding='utf-8').read()
            for block in re.findall(r'{%\s*block extra_style\s*%}(.*?){%\s*endblock', text, re.S):
                if re.search(r'</?style', block, re.I):
                    bad.append(os.path.basename(path))
        self.assertEqual(bad, [])


if __name__ == '__main__':
    unittest.main()
