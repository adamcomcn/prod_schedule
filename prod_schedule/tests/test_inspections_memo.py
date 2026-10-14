"""List pages share one parsed copy of the inspection file until it changes."""
import os
import sys
import tempfile
import unittest

os.environ.setdefault('APP_DATA_DIR', tempfile.mkdtemp(prefix='prod-schedule-tests-'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key')
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app


class InspectionsMemoTests(unittest.TestCase):
    def test_parsed_once_and_refreshed_after_a_save(self):
        app.save_json(app.INSPECTIONS_CACHE, {'MEL|PO-1|A': [{'result': 'Pass'}]})
        first = app.load_inspections()
        self.assertIs(app.load_inspections(), first)                  # not parsed again
        app.save_json(app.INSPECTIONS_CACHE, {'MEL|PO-1|A': [{'result': 'Fail'}]})   # same size, new file
        self.assertEqual(app.load_inspections()['MEL|PO-1|A'][0]['result'], 'Fail')

    def test_missing_file(self):
        os.remove(app.INSPECTIONS_CACHE) if os.path.exists(app.INSPECTIONS_CACHE) else None
        self.assertEqual(app.load_inspections(), {})


if __name__ == '__main__':
    unittest.main()
