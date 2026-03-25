# -*- coding: utf-8 -*-
import argparse
import os
import sys
from pathlib import Path

# Windowsコンソールの文字化け対策
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from .core import compress_one, find_ghostscript, get_file_size_mb, resolve_inputs


def main():
    parser = argparse.ArgumentParser(
        description="PDFを圧縮して指定サイズ以下にするツール",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  compress_pdf input.pdf
  compress_pdf a.pdf b.pdf c.pdf
  compress_pdf *.pdf
  compress_pdf ./docs/
  compress_pdf input.pdf -o output.pdf
  compress_pdf *.pdf -o ./compressed/
  compress_pdf input.pdf --target 2.0
        """,
    )
    parser.add_argument("inputs", nargs="+", help="入力PDFファイル（複数可・glob可・ディレクトリ可）")
    parser.add_argument("-o", "--output", help="出力先（単一→ファイルパス、複数→ディレクトリパス）")
    parser.add_argument("--target", type=float, default=1.0, metavar="MB", help="目標サイズ MB（デフォルト: 1.0）")
    parser.add_argument("--suffix", default="_compressed", help="出力ファイルのサフィックス（デフォルト: _compressed）")
    args = parser.parse_args()

    input_files = resolve_inputs(args.inputs)
    if not input_files:
        print("エラー: 対象のPDFファイルが見つかりません。")
        sys.exit(1)

    missing = [f for f in input_files if not os.path.exists(f)]
    if missing:
        for m in missing:
            print(f"エラー: ファイルが見つかりません: {m}")
        sys.exit(1)

    output_is_dir = bool(args.output and (os.path.isdir(args.output) or args.output.endswith(("/", "\\"))))
    if args.output and not output_is_dir and len(input_files) > 1:
        print("エラー: 複数ファイル指定時は -o にディレクトリを指定してください。")
        sys.exit(1)
    if output_is_dir:
        os.makedirs(args.output, exist_ok=True)

    gs = find_ghostscript()
    print(f"Ghostscript: {gs}" if gs else "Ghostscript: 未検出（pikepdfで基本圧縮のみ）")

    ok_list, fail_list = [], []

    for input_path in input_files:
        p = Path(input_path)
        if args.output and not output_is_dir:
            output_path = args.output
        elif output_is_dir:
            output_path = os.path.join(args.output, f"{p.stem}{args.suffix}.pdf")
        else:
            output_path = str(p.parent / f"{p.stem}{args.suffix}.pdf")

        print(f"\n[入力] {input_path} ({get_file_size_mb(input_path):.2f} MB)")
        result = compress_one(
            gs, input_path, output_path, args.target,
            on_status=lambda msg: print(f"  {msg}"),
        )
        ratio = (1 - result.output_mb / result.input_mb) * 100 if result.output_mb else 0
        status = "OK" if result.success else "警告: 目標未達"
        print(f"  [出力] {result.output_path} ({result.output_mb:.2f} MB, 圧縮率 {ratio:.1f}%) [{status}]")

        (ok_list if result.success else fail_list).append(input_path)

    print(f"\n{'=' * 40}")
    print(f"完了: {len(ok_list)}/{len(input_files)} ファイル成功")
    if fail_list:
        print("目標未達または失敗:")
        for f in fail_list:
            print(f"  - {f}")

    sys.exit(0 if not fail_list else 1)
