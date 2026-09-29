from pathlib import Path
import tempfile
import unittest

from runtime import AlreadyRunningError, DataLock


class RuntimeTests(unittest.TestCase):
    def test_same_data_folder_is_locked_until_close(self):
        with tempfile.TemporaryDirectory() as folder:
            first = DataLock(folder)
            try:
                with self.assertRaises(AlreadyRunningError):
                    DataLock(folder)
            finally:
                first.close()
            reopened = DataLock(folder)
            reopened.close()
            reopened.close()

    def test_different_test_folders_do_not_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            first = DataLock(Path(folder) / 'one')
            second = DataLock(Path(folder) / 'two')
            first.close()
            second.close()
