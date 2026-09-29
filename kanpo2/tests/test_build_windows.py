import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import build_windows


class BuildWindowsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name)
        (self.source / 'sample').mkdir()
        for name in ('README.md', 'sample/ledger.csv', 'run_evaluation.bat',
                     '社内PCでのEXE作成と評価.md', '自動取得の確認結果.md', '0.7の実装と検証.md'):
            (self.source / name).write_text('evaluation fixture', encoding='utf-8')
        # These files must never enter the distribution.
        (self.source / 'real_customer_ledger.csv').write_text('private fixture', encoding='utf-8')
        (self.source / 'original.pdf').write_bytes(b'private fixture')
        (self.source / 'kanpo.sqlite3').write_bytes(b'private fixture')
        old = self.source / 'dist' / 'KanpoChecker'
        old.mkdir(parents=True)
        (old / 'KanpoChecker.exe').write_bytes(b'old exe')
        self.commands = []

    def runner(self, fail=None, create_exe=True, create_ocr=True):
        def run(command, **kwargs):
            self.commands.append(command)
            if 'check_environment.py' in command:
                stage = 'environment'
                report = Path(command[command.index('--output') + 1])
                report.write_text(json.dumps({'ok': fail != stage}), encoding='utf-8')
            elif 'unittest' in command:
                stage = 'tests'
            else:
                stage = 'pyinstaller'
                self.assertIn('PyInstaller', command)
                if fail != stage and create_exe:
                    dist = Path(command[command.index('--distpath') + 1]) / 'KanpoChecker'
                    (dist / '_internal').mkdir(parents=True)
                    (dist / 'KanpoChecker.exe').write_bytes(b'new exe')
                    (dist / '_internal' / 'runtime.dll').write_bytes(b'runtime')
                    if create_ocr:
                        (dist / '_internal' / 'windows_ocr.ps1').write_text('test script', encoding='utf-8')
            kwargs['stdout'].write('mock ' + stage + '\n')
            self.assertEqual(kwargs['cwd'], self.source)
            self.assertEqual(kwargs['env']['PYTHONUTF8'], '1')
            self.assertIn('build_runs', kwargs['env']['PYINSTALLER_CONFIG_DIR'])
            return subprocess.CompletedProcess(command, 1 if fail == stage else 0)
        return run

    def test_new_runs_package_only_fresh_distribution_and_keep_logs(self):
        with patch.object(build_windows.subprocess, 'run', side_effect=self.runner()):
            first = build_windows.build(self.source)
            second = build_windows.build(self.source)
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual((self.source / 'dist/KanpoChecker/KanpoChecker.exe').read_bytes(), b'old exe')
        for archive in (first, second):
            with zipfile.ZipFile(archive) as bundle:
                self.assertEqual(set(bundle.namelist()), {
                    'KanpoChecker/KanpoChecker.exe', 'KanpoChecker/_internal/runtime.dll',
                    'KanpoChecker/_internal/windows_ocr.ps1',
                    'KanpoChecker/README.md', 'KanpoChecker/sample_ledger.csv',
                    'KanpoChecker/run_evaluation.bat',
                    'KanpoChecker/社内PCでのEXE作成と評価.md', 'KanpoChecker/自動取得の確認結果.md',
                    'KanpoChecker/0.7の実装と検証.md',
                })
                self.assertEqual(bundle.read('KanpoChecker/KanpoChecker.exe'), b'new exe')
            self.assertTrue((archive.parent / 'environment_check.json').is_file())
            self.assertEqual(len(list((archive.parent / 'logs').glob('*.log'))), 4)
            report = json.loads((archive.parent / 'build_summary.json').read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'packaged')
            self.assertFalse(report['executable_tested'])
        compiler = self.commands[2]
        self.assertIn('--onedir', compiler)
        self.assertIn(str(self.source / 'windows_ocr.ps1') + ';.', compiler)
        self.assertNotIn('--clean', compiler)

    def test_failed_external_stage_prevents_later_stages_and_zip(self):
        for stage, expected_calls in [('environment', 1), ('tests', 2), ('pyinstaller', 3)]:
            with self.subTest(stage=stage):
                self.commands.clear()
                with patch.object(build_windows.subprocess, 'run', side_effect=self.runner(fail=stage)):
                    with self.assertRaises(build_windows.BuildError):
                        build_windows.build(self.source)
                self.assertEqual(len(self.commands), expected_calls)
        self.assertFalse(list((self.source / 'build_runs').rglob('*.zip')))
        for path in (self.source / 'build_runs').glob('*/build_summary.json'):
            report = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(report['status'], 'failed')
            self.assertEqual(report['stages'][-1]['status'], 'failed')

    def test_missing_new_exe_or_ocr_script_cannot_be_packaged(self):
        for options, message in [({'create_exe': False}, 'did not produce'),
                                 ({'create_ocr': False}, 'missing windows_ocr.ps1')]:
            with self.subTest(options=options):
                with patch.object(build_windows.subprocess, 'run', side_effect=self.runner(**options)):
                    with self.assertRaisesRegex(build_windows.BuildError, message):
                        build_windows.build(self.source)
        self.assertFalse(list((self.source / 'build_runs').rglob('*.zip')))

    def test_false_diagnostic_report_stops_even_with_zero_exit_code(self):
        original_runner = self.runner(fail='environment')
        def zero_exit(command, **kwargs):
            original_runner(command, **kwargs)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(build_windows.subprocess, 'run', side_effect=zero_exit):
            with self.assertRaisesRegex(build_windows.BuildError, 'diagnostics did not pass'):
                build_windows.build(self.source)
        self.assertEqual(len(self.commands), 1)
        self.assertFalse(list((self.source / 'build_runs').rglob('*.zip')))
        report_path = next((self.source / 'build_runs').glob('*/build_summary.json'))
        report = json.loads(report_path.read_text(encoding='utf-8'))
        self.assertEqual(report['stages'][-1]['status'], 'failed')

    def test_zip_write_failure_does_not_publish_completed_zip(self):
        with patch.object(build_windows.subprocess, 'run', side_effect=self.runner()):
            with patch.object(zipfile.ZipFile, 'write', side_effect=OSError('simulated disk failure')):
                with self.assertRaisesRegex(build_windows.BuildError, 'simulated disk failure'):
                    build_windows.build(self.source)
        self.assertFalse(list((self.source / 'build_runs').rglob('*.zip')))
        self.assertEqual(len(list((self.source / 'build_runs').rglob('*.zip.incomplete'))), 1)


if __name__ == '__main__':
    unittest.main()
