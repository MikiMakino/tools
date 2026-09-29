"""Build an evaluation ZIP in a new run directory; never install or delete files."""
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import zipfile


class BuildError(RuntimeError):
    pass


def build(source=None):
    source = Path(source or Path(__file__).parent).resolve()
    run_id = datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '_' + uuid.uuid4().hex[:8]
    run = source / 'build_runs' / run_id
    logs = run / 'logs'
    logs.mkdir(parents=True, exist_ok=False)
    summary = {'status': 'running', 'run_id': run_id, 'stages': [],
               'python': sys.executable, 'executable_tested': False}
    summary_path = run / 'build_summary.json'
    env = os.environ.copy()
    env.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
               PYINSTALLER_CONFIG_DIR=str(run / 'pyinstaller_cache'))

    def save_summary():
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')

    def command_stage(number, name, command):
        log = logs / ('%02d_%s.log' % (number, name))
        stage = {'name': name, 'log': str(log.relative_to(run)), 'status': 'running'}
        summary['stages'].append(stage)
        save_summary()
        print('Running: ' + name, flush=True)
        with log.open('w', encoding='utf-8') as stream:
            stream.write(json.dumps(command, ensure_ascii=False) + '\n')
            stream.flush()
            try:
                result = subprocess.run(command, cwd=source, env=env,
                                        stdout=stream, stderr=subprocess.STDOUT, check=False)
            except OSError as error:
                stream.write('FAILED: ' + str(error) + '\n')
                raise
        stage.update(returncode=result.returncode, status='passed' if result.returncode == 0 else 'failed')
        save_summary()
        if result.returncode:
            raise BuildError('%s failed (exit %s). See %s' % (name, result.returncode, log))

    save_summary()
    print('Build records: ' + str(run), flush=True)
    try:
        diagnostic = run / 'environment_check.json'
        command_stage(1, 'environment', [sys.executable, '-B', 'check_environment.py',
                                         '--build', '--output', str(diagnostic)])
        report = json.loads(diagnostic.read_text(encoding='utf-8-sig'))
        if report.get('ok') is not True:
            raise BuildError('Environment diagnostics did not pass.')
        command_stage(2, 'tests', [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', 'tests', '-v'])
        # Every output and PyInstaller cache is new. No --clean or old dist removal.
        command_stage(3, 'pyinstaller', [
            sys.executable, '-B', '-m', 'PyInstaller', '--noconfirm', '--noupx',
            '--windowed', '--onedir', '--name', 'KanpoChecker',
            '--collect-all', 'pypdf', '--collect-all', 'cryptography',
            '--collect-all', 'openpyxl', '--collect-submodules', 'tkinter',
            '--hidden-import', '_tkinter', '--add-data', str(source / 'windows_ocr.ps1') + ';.',
            '--workpath', str(run / 'work'), '--distpath', str(run / 'dist'),
            '--specpath', str(run / 'spec'), str(source / 'app.py'),
        ])
        distribution = run / 'dist' / 'KanpoChecker'
        package_log = logs / '04_package.log'
        stage = {'name': 'package', 'log': str(package_log.relative_to(run)), 'status': 'running'}
        summary['stages'].append(stage)
        save_summary()
        with package_log.open('w', encoding='utf-8') as log:
            try:
                if not (distribution / 'KanpoChecker.exe').is_file():
                    raise BuildError('The new run did not produce KanpoChecker.exe.')
                if not any(path.is_file() for path in (distribution / 'windows_ocr.ps1',
                                                       distribution / '_internal' / 'windows_ocr.ps1')):
                    raise BuildError('The new distribution is missing windows_ocr.ps1.')
                for original, name in [('README.md', 'README.md'),
                                       ('sample/ledger.csv', 'sample_ledger.csv'),
                                       ('run_evaluation.bat', 'run_evaluation.bat'),
                                       ('社内PCでのEXE作成と評価.md', '社内PCでのEXE作成と評価.md'),
                                       ('自動取得の確認結果.md', '自動取得の確認結果.md'),
                                       ('0.7の実装と検証.md', '0.7の実装と検証.md')]:
                    shutil.copyfile(source / original, distribution / name)
                    log.write('Copied: ' + name + '\n')
                archive = run / 'KanpoChecker_evaluation.zip'
                incomplete = archive.with_suffix('.zip.incomplete')
                with zipfile.ZipFile(incomplete, 'x', compression=zipfile.ZIP_DEFLATED) as bundle:
                    for path in sorted(distribution.rglob('*')):
                        if path.is_symlink():
                            raise BuildError('Unexpected symbolic link in distribution: ' + str(path))
                        path.resolve().relative_to(distribution.resolve())
                        if path.is_file():
                            relative = path.relative_to(distribution).as_posix()
                            bundle.write(path, 'KanpoChecker/' + relative)
                            log.write('Archived: ' + relative + '\n')
                with zipfile.ZipFile(incomplete) as bundle:
                    if bundle.testzip() is not None:
                        raise BuildError('ZIP integrity verification failed.')
                incomplete.rename(archive)
                log.write('ZIP integrity verified. Executable launch is not yet verified.\n')
            except Exception as error:
                log.write('FAILED: ' + str(error) + '\n')
                stage['status'] = 'failed'
                raise
        stage['status'] = 'passed'
        summary.update(status='packaged', archive=str(archive.relative_to(run)))
        save_summary()
        return archive
    except Exception as error:
        summary.update(status='failed', error=str(error))
        if summary['stages']:
            summary['stages'][-1]['status'] = 'failed'
        save_summary()
        raise BuildError('%s\nBuild records: %s' % (error, run)) from error


def main():
    try:
        archive = build()
    except (OSError, BuildError) as error:
        print('Build failed. Do not distribute output from this run.\n' + str(error))
        return 1
    print('Evaluation ZIP: ' + str(archive))
    print('EXE launch is not yet verified. Test the extracted folder on the evaluation PC.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
