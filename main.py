#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF圧縮ツール エントリーポイント
  引数なし → GUIを起動
  引数あり → CLIとして動作
"""
import sys


def main():
    if len(sys.argv) > 1:
        from compress_pdf.cli import main as cli_main
        cli_main()
    else:
        from compress_pdf.gui import run
        run()


if __name__ == "__main__":
    main()
