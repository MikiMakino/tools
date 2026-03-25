# -*- coding: utf-8 -*-
import os
import threading
import webbrowser
from pathlib import Path

import flet as ft

from .core import compress_one, find_ghostscript, resource_path


def run():
    gs = find_ghostscript()

    async def main(page: ft.Page):
        page.title = "PDF 圧縮ツール"
        page.padding = 16

        # ---- state ----
        file_entries: list[dict] = []
        output_dir: list[str | None] = [None]

        # ---- file list ----
        list_view = ft.ListView(expand=True, spacing=2, padding=4)

        def build_row(entry: dict) -> ft.Container:
            return ft.Container(
                content=ft.Row([
                    ft.Text(
                        Path(entry["path"]).name,
                        expand=True,
                        no_wrap=True,
                        overflow=ft.TextOverflow.ELLIPSIS,
                        tooltip=entry["path"],
                    ),
                    ft.Text(f"{entry['size_mb']:.1f} MB", width=75, text_align=ft.TextAlign.RIGHT),
                    ft.Text(entry["status"], width=130, text_align=ft.TextAlign.RIGHT, color=entry["color"]),
                ]),
                padding=ft.padding.symmetric(horizontal=8, vertical=6),
                bgcolor="#f5f5f5",
                border_radius=4,
            )

        def refresh_list():
            list_view.controls.clear()
            for e in file_entries:
                list_view.controls.append(build_row(e))
            page.update()

        # ---- file pickers ----
        # Flet 0.82+ では overlay への追加不要、直接 await で呼び出す
        file_picker = ft.FilePicker()
        folder_picker = ft.FilePicker()
        output_picker = ft.FilePicker()

        async def add_files(e):
            files = await file_picker.pick_files(
                allowed_extensions=["pdf"],
                allow_multiple=True,
            )
            if not files:
                return
            existing = {x["path"] for x in file_entries}
            for f in files:
                if f.path and f.path not in existing:
                    file_entries.append({
                        "path": f.path,
                        "size_mb": os.path.getsize(f.path) / (1024 * 1024),
                        "status": "待機",
                        "color": "grey600",
                    })
            refresh_list()

        async def add_folder(e):
            path = await folder_picker.get_directory_path()
            if not path:
                return
            existing = {x["path"] for x in file_entries}
            for pdf in Path(path).rglob("*.pdf"):
                path_str = str(pdf)
                if path_str not in existing:
                    file_entries.append({
                        "path": path_str,
                        "size_mb": pdf.stat().st_size / (1024 * 1024),
                        "status": "待機",
                        "color": "grey600",
                    })
            refresh_list()

        async def pick_output(e):
            path = await output_picker.get_directory_path()
            if path:
                output_dir[0] = path
                output_path_text.value = path
                page.update()

        # ---- compression ----
        def start_compression(e):
            pending = [f for f in file_entries if f["status"] == "待機"]
            if not pending:
                return
            try:
                target_mb = float(target_field.value or "1.0")
            except ValueError:
                target_mb = 1.0

            start_btn.disabled = True
            progress_bar.visible = True
            progress_text.visible = True
            progress_bar.value = 0
            page.update()

            def run_all():
                total = len(pending)
                for i, entry in enumerate(pending):
                    entry["status"] = "処理中..."
                    entry["color"] = "orange"
                    refresh_list()

                    inp = entry["path"]
                    p = Path(inp)
                    if dest_dropdown.value == "other" and output_dir[0]:
                        out = os.path.join(output_dir[0], f"{p.stem}_compressed.pdf")
                    else:
                        out = str(p.parent / f"{p.stem}_compressed.pdf")

                    def update_status(msg, _entry=entry):
                        _entry["status"] = msg
                        _entry["color"] = "orange"
                        refresh_list()

                    result = compress_one(gs, inp, out, target_mb, on_status=update_status)

                    if result.success:
                        entry["status"] = f"✓ {result.output_mb:.1f} MB"
                        entry["color"] = "green"
                    else:
                        entry["status"] = f"✗ {result.message}"
                        entry["color"] = "red"

                    progress_bar.value = (i + 1) / total
                    progress_text.value = f"{i + 1} / {total} 完了"
                    refresh_list()

                start_btn.disabled = False
                page.update()

            threading.Thread(target=run_all, daemon=True).start()

        # ---- widgets ----
        target_field = ft.TextField(
            value="1.0",
            width=100,
            suffix=ft.Text("MB"),
            text_align=ft.TextAlign.RIGHT,
            keyboard_type=ft.KeyboardType.NUMBER,
        )

        output_path_text = ft.Text("（入力ファイルと同じフォルダ）", color="grey600", expand=True)

        output_folder_row = ft.Row([
            output_path_text,
            ft.IconButton(
                icon=ft.Icons.FOLDER_OPEN,
                tooltip="フォルダを選択",
                on_click=pick_output,
            ),
        ], visible=False)

        def on_dest_change(e):
            is_other = dest_dropdown.value == "other"
            output_folder_row.visible = is_other
            if not is_other:
                output_dir[0] = None
            page.update()

        dest_dropdown = ft.Dropdown(
            value="same",
            width=200,
            options=[
                ft.dropdown.Option("same", "同じフォルダ"),
                ft.dropdown.Option("other", "別のフォルダを指定"),
            ],
            on_select=on_dest_change,
        )

        start_btn = ft.ElevatedButton(
            "圧縮を開始",
            icon=ft.Icons.PLAY_ARROW,
            on_click=start_compression,
        )

        progress_bar = ft.ProgressBar(value=0, visible=False)
        progress_text = ft.Text("", visible=False, text_align=ft.TextAlign.CENTER)

        gs_label = f"Ghostscript: {gs}" if gs else "Ghostscript: 未検出（pikepdfで基本圧縮）"

        def open_guide(e):
            path = resource_path("guide.html")
            webbrowser.open(f"file:///{path.replace(os.sep, '/')}")

        # ---- layout ----
        page.add(
            ft.Column([
                ft.Row([
                    ft.Text("PDF 圧縮ツール", size=20, weight=ft.FontWeight.BOLD),
                    ft.Row([
                        ft.Text(gs_label, size=11, color="grey600"),
                        ft.IconButton(
                            icon=ft.Icons.HELP_OUTLINE,
                            tooltip="使い方ガイドを開く",
                            on_click=open_guide,
                        ),
                    ]),
                ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ft.Divider(),
                ft.Row([
                    ft.ElevatedButton("ファイルを追加", icon=ft.Icons.ADD, on_click=add_files),
                    ft.ElevatedButton("フォルダを追加", icon=ft.Icons.FOLDER_OPEN, on_click=add_folder),
                    ft.TextButton("クリア", on_click=lambda e: (file_entries.clear(), refresh_list())),
                ]),
                ft.Container(
                    content=ft.Row([
                        ft.Text("ファイル名", expand=True, weight=ft.FontWeight.BOLD),
                        ft.Text("サイズ", width=75, text_align=ft.TextAlign.RIGHT, weight=ft.FontWeight.BOLD),
                        ft.Text("状態", width=130, text_align=ft.TextAlign.RIGHT, weight=ft.FontWeight.BOLD),
                    ]),
                    padding=ft.padding.symmetric(horizontal=8),
                ),
                ft.Container(
                    content=list_view,
                    border=ft.border.all(1, "grey400"),
                    border_radius=4,
                    expand=True,
                    padding=4,
                ),
                ft.Divider(),
                ft.Row([
                    ft.Text("目標サイズ:"),
                    target_field,
                    ft.Text("　出力先:"),
                    dest_dropdown,
                ]),
                output_folder_row,
                ft.Divider(),
                ft.Row([start_btn], alignment=ft.MainAxisAlignment.CENTER),
                progress_bar,
                progress_text,
            ], expand=True)
        )

    ft.app(target=main)
