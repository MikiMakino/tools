#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os
import subprocess
import sys
from pathlib import Path

ONEDRIVE_DIR = Path(os.environ.get("OneDriveCommercial") or os.environ.get("OneDrive", ""))
OUT_DIR = ONEDRIVE_DIR / "kanpo-downloads"

if not ONEDRIVE_DIR.exists():
    print("ERROR: OneDriveのフォルダが見つかりません。", file=sys.stderr)
    sys.exit(1)

script = Path(__file__).parent / "kanpo_downloader.py"
result = subprocess.run(
    [sys.executable, str(script), "--out-dir", str(OUT_DIR), *sys.argv[1:]],
)
sys.exit(result.returncode)
