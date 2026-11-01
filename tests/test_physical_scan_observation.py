import io
import json
import logging
from pathlib import Path
import tempfile
import unittest

from evolvmem.physical_integrity import physical_scan


class ObservationTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.log = logging.getLogger('evolvmem.physical_integrity')
        self.handler = logging.StreamHandler(self.output)
        self.before = self.log.level
        self.log.setLevel(logging.WARNING)
        self.log.addHandler(self.handler)

    def tearDown(self):
        self.log.removeHandler(self.handler)
        self.log.setLevel(self.before)

    def test_begin_end_never_contain_path_sql_content_or_exception_text(self):
        with tempfile.TemporaryDirectory(prefix='private-physical-test-') as directory:
            path = Path(directory) / 'PRIVATE_DB_PATH'
            path.write_bytes(b'')
            @physical_scan('readonly')
            def scanner(path, expected):
                raise RuntimeError('PRIVATE BODY SELECT * FROM private_table')
            with self.assertRaises(RuntimeError):
                scanner(path, ())
        text = self.output.getvalue()
        self.assertNotIn('PRIVATE', text)
        self.assertNotIn('SELECT', text)
        self.assertNotIn(directory, text)
        records = [json.loads(line.split('physical_scan ', 1)[1]) for line in text.splitlines()]
        self.assertEqual([r['phase'] for r in records], ['begin', 'end'])
        self.assertEqual(records[0]['scan_id'], records[1]['scan_id'])
        self.assertEqual(records[0]['db_tag'], records[1]['db_tag'])
        self.assertEqual(len(records[0]['db_tag']), 16)
        self.assertEqual(records[1]['outcome'], 'error')

    def test_keyword_arguments_remain_supported(self):
        @physical_scan('readonly')
        def scanner(*, path, expected):
            return ()
        self.assertEqual(scanner(path=Path('absent-synthetic-db'), expected=()), ())

    def test_logging_failure_does_not_change_scanner_result(self):
        original = self.log.warning
        def broken(*args, **kwargs):
            raise RuntimeError('synthetic logger error')
        self.log.warning = broken
        try:
            @physical_scan('readonly')
            def scanner(path, expected):
                return ('quick_check_failed',)
            self.assertEqual(scanner(Path('absent-synthetic-db'), ()), ('quick_check_failed',))
        finally:
            self.log.warning = original


if __name__ == '__main__':
    unittest.main()
