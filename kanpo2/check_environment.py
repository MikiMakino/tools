"""Read-only Windows/source-build diagnostics. Never opens the production database."""
import argparse
from datetime import datetime, timezone
import importlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import struct
import sys


def diagnose(require_build=False):
    checks = []

    def record(name, passed, **details):
        checks.append({'name': name, 'ok': bool(passed), **details})

    record('windows', sys.platform == 'win32', value=platform.platform())
    record('64_bit_python', struct.calcsize('P') == 8, bits=struct.calcsize('P') * 8)
    record('python_3_12', sys.version_info[:2] == (3, 12), version=platform.python_version(),
           executable=sys.executable)
    conda_prefix = os.environ.get('CONDA_PREFIX', '')
    active_conda = bool(conda_prefix and
                        Path(conda_prefix).resolve() == Path(sys.prefix).resolve() and
                        (Path(conda_prefix) / 'conda-meta').is_dir())
    record('active_conda_environment', active_conda, prefix=conda_prefix,
           message='社内承認済みMinicondaから有効化した環境で実行してください。承認の有無は社内で確認してください。')

    modules = {}
    dependencies = [('tkinter', None), ('sqlite3', None), ('pypdf', 'pypdf'),
                    ('cryptography', 'cryptography'), ('openpyxl', 'openpyxl')]
    if require_build:
        dependencies.append(('PyInstaller', 'pyinstaller'))
    for module_name, package_name in dependencies:
        try:
            module = importlib.import_module(module_name)
            modules[module_name] = module
            if package_name:
                version = metadata.version(package_name)
            elif module_name == 'sqlite3':
                version = module.sqlite_version
            else:
                version = str(module.TkVersion)
            record('import_' + module_name, True, version=version)
        except Exception as error:
            record('import_' + module_name, False, error=str(error))

    root = None
    try:
        tkinter = modules.get('tkinter')
        if tkinter is None:
            raise RuntimeError('tkinterが読み込めません。')
        root = tkinter.Tk()
        root.withdraw()
        root.update_idletasks()
        tcl_version = root.tk.call('info', 'patchlevel')
        root.destroy()
        root = None
        record('gui_create_and_destroy', True, tcl_version=tcl_version)
    except Exception as error:
        record('gui_create_and_destroy', False, error=str(error))
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass

    script = Path(__file__).resolve().with_name('windows_ocr.ps1')
    record('ocr_script', script.is_file(), path=str(script))
    try:
        from windows_ocr import availability
        status = availability()
        record('windows_japanese_ocr', status.get('available', False), details=status)
    except Exception as error:
        record('windows_japanese_ocr', False, error=str(error))

    return {'checked_at': datetime.now(timezone.utc).isoformat(),
            'mode': 'build' if require_build else 'source_run',
            'ok': all(check['ok'] for check in checks), 'checks': checks,
            'note': '診断はアプリの保存先・台帳・PDF・データベースを開いたり変更したりしません。'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build', action='store_true', help='PyInstallerも必須として確認する')
    parser.add_argument('--output', type=Path, help='診断JSONを指定ファイルにも保存する')
    args = parser.parse_args(argv)
    report = diagnose(args.build)
    content = json.dumps(report, ensure_ascii=False, indent=2)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    print(content)
    if args.output:
        args.output.write_text(content + '\n', encoding='utf-8')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
