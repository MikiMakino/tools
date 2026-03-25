# -*- coding: utf-8 -*-
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path


def resource_path(relative: str) -> str:
    """PyInstaller bundle / 通常実行の両方でリソースファイルのパスを返す"""
    if hasattr(sys, "_MEIPASS"):
        base = sys._MEIPASS
    else:
        # compress_pdf/core.py → 2階層上がプロジェクトルート
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, relative)


@dataclass
class CompressResult:
    input_path: str
    output_path: str
    input_mb: float
    output_mb: float
    success: bool
    message: str


def get_file_size_mb(path: str) -> float:
    return os.path.getsize(path) / (1024 * 1024)


def find_ghostscript() -> str | None:
    """Ghostscriptの実行ファイルを探す"""
    for cmd in ["gs", "gswin64c", "gswin32c"]:
        if shutil.which(cmd):
            return cmd
    for pattern in [
        r"C:\Program Files\gs\gs*\bin\gswin64c.exe",
        r"C:\Program Files (x86)\gs\gs*\bin\gswin32c.exe",
    ]:
        matches = glob.glob(pattern)
        if matches:
            return matches[-1]
    return None


def _compress_gs(gs: str, input_path: str, output_path: str, quality: str) -> bool:
    setting = {"screen": "/screen", "ebook": "/ebook", "printer": "/printer", "prepress": "/prepress"}.get(quality, "/ebook")
    cmd = [
        gs, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS={setting}", "-dNOPAUSE", "-dQUIET", "-dBATCH",
        f"-sOutputFile={output_path}", input_path,
    ]
    try:
        return subprocess.run(cmd, capture_output=True).returncode == 0
    except Exception:
        return False


def _compress_pikepdf(input_path: str, output_path: str) -> bool:
    try:
        import pikepdf
        with pikepdf.open(input_path) as pdf:
            pdf.save(
                output_path,
                compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                normalize_content=True,
            )
        return True
    except Exception:
        return False


def compress_one(
    gs: str | None,
    input_path: str,
    output_path: str,
    target_mb: float,
    on_status=None,  # callback(message: str) | None
) -> CompressResult:
    """1ファイルを圧縮して CompressResult を返す"""
    input_mb = get_file_size_mb(input_path)
    target_bytes = target_mb * 1024 * 1024

    # すでに目標サイズ以下ならコピー
    if os.path.getsize(input_path) <= target_bytes:
        shutil.copy2(input_path, output_path)
        return CompressResult(input_path, output_path, input_mb, get_file_size_mb(output_path), True, "すでに目標サイズ以下")

    if gs:
        for quality in ["ebook", "screen"]:
            if on_status:
                on_status(f"圧縮中 [{quality}]...")
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                ok = _compress_gs(gs, input_path, tmp_path, quality)
                if ok and os.path.exists(tmp_path):
                    if os.path.getsize(tmp_path) <= target_bytes:
                        shutil.move(tmp_path, output_path)
                        out_mb = get_file_size_mb(output_path)
                        return CompressResult(input_path, output_path, input_mb, out_mb, True, f"完了 [{quality}]")
                    os.unlink(tmp_path)
            except Exception:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        # 目標未達でも最小品質で保存
        if on_status:
            on_status("目標未達。最低品質で保存...")
        _compress_gs(gs, input_path, output_path, "screen")
    else:
        if on_status:
            on_status("pikepdfで圧縮中...")
        if not _compress_pikepdf(input_path, output_path):
            return CompressResult(input_path, output_path, input_mb, 0.0, False, "圧縮失敗")

    if not os.path.exists(output_path):
        return CompressResult(input_path, output_path, input_mb, 0.0, False, "出力ファイルが見つかりません")

    out_mb = get_file_size_mb(output_path)
    success = out_mb <= target_mb
    message = "完了" if success else f"目標未達 ({out_mb:.2f} MB)"
    return CompressResult(input_path, output_path, input_mb, out_mb, success, message)


def resolve_inputs(raw_inputs: list[str]) -> list[str]:
    """glob展開・ディレクトリ内のPDF収集を行い、PDFパスのリストを返す"""
    paths = []
    for item in raw_inputs:
        expanded = glob.glob(item, recursive=True)
        if expanded:
            for p in expanded:
                if os.path.isdir(p):
                    paths.extend(str(f) for f in Path(p).rglob("*.pdf"))
                elif p.lower().endswith(".pdf"):
                    paths.append(p)
        else:
            paths.append(item)
    return paths
