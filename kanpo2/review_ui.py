"""Local preview with review marks. Never writes to the original PDF."""
import math
import tkinter as tk
from tkinter import ttk, messagebox


def normalized_box(start, end, width, height):
    """Convert a dragged display rectangle into a bounded original-page region."""
    if width <= 0 or height <= 0:
        return None
    x1, x2 = sorted((max(0, min(width, start[0])), max(0, min(width, end[0]))))
    y1, y2 = sorted((max(0, min(height, start[1])), max(0, min(height, end[1]))))
    if x2 - x1 < 4 or y2 - y1 < 4:
        return None
    return [x1 / width, y1 / height, (x2 - x1) / width, (y2 - y1) / height]


class ReviewPreview(tk.Toplevel):
    def __init__(self, parent, row, image_path, reread):
        super().__init__(parent)
        self.title(f"原文と確認の印 — {row['original_name']} / PDF {row['page']}ページ")
        self.geometry('1220x860')
        self.row, self.reread = row, reread
        self.region = None
        self.start = None
        self.original = tk.PhotoImage(file=str(image_path), master=self)
        self.factor = max(1, math.ceil(self.original.width() / 840), math.ceil(self.original.height() / 710))
        tools = ttk.Frame(self, padding=8)
        tools.pack(fill='x')
        ttk.Label(tools, text='赤枠は確認の手がかりです。印のない箇所も原文を確認してください。').pack(side='left')
        ttk.Button(tools, text='拡大', command=lambda: self.zoom(-1)).pack(side='right', padx=4)
        ttk.Button(tools, text='縮小', command=lambda: self.zoom(1)).pack(side='right')
        body = ttk.Frame(self)
        body.pack(fill='both', expand=True)
        preview = ttk.Frame(body)
        preview.pack(side='left', fill='both', expand=True)
        self.canvas = tk.Canvas(preview, background='#ececec', highlightthickness=0)
        ys = ttk.Scrollbar(preview, orient='vertical', command=self.canvas.yview)
        xs = ttk.Scrollbar(preview, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        ys.pack(side='right', fill='y')
        xs.pack(side='bottom', fill='x')
        self.canvas.pack(fill='both', expand=True)
        self.canvas.bind('<ButtonPress-1>', self.press)
        self.canvas.bind('<B1-Motion>', self.drag)
        self.canvas.bind('<ButtonRelease-1>', self.release)
        side = ttk.Frame(body, width=340, padding=10)
        side.pack(side='right', fill='y')
        side.pack_propagate(False)
        ttk.Label(side, text='機械の要確認箇所（クリックで移動）').pack(anchor='w')
        self.listing = tk.Listbox(side, width=42, height=13, exportselection=False)
        self.listing.pack(fill='x', pady=8)
        self.listing.bind('<<ListboxSelect>>', self.select_flag)
        self.reason = tk.StringVar()
        ttk.Label(side, textvariable=self.reason, wraplength=315).pack(anchor='w', pady=8)
        ttk.Separator(side).pack(fill='x', pady=12)
        ttk.Label(side, text='紙面をドラッグして選んだ範囲だけ、再読取できます。結果は未確認の補足として保存します。', wraplength=315).pack(anchor='w')
        self.selection_label = tk.StringVar(value='再読取範囲：未選択')
        ttk.Label(side, textvariable=self.selection_label, wraplength=315).pack(anchor='w', pady=8)
        self.direction = tk.StringVar(value='4方向')
        ttk.Combobox(side, state='readonly', textvariable=self.direction,
                     values=['4方向', '0度', '90度', '180度', '270度'], width=15).pack(anchor='w')
        self.retry_button = ttk.Button(side, text='選択範囲を再読取', command=self.retry)
        self.retry_button.pack(anchor='w', pady=8)
        ttk.Label(side, text='この表示は原本から作った確認用画像です。電子署名・注釈等は「原文PDF」で確認してください。原本への書込みは行いません。', wraplength=315).pack(anchor='w', pady=12)
        ttk.Button(side, text='閉じる', command=self.destroy).pack(anchor='w', pady=8)
        self.update_analysis(row.get('analysis', {}))
        self.draw()

    def update_analysis(self, analysis):
        self.flags = analysis.get('flags', [])
        self.listing.delete(0, 'end')
        for index, flag in enumerate(self.flags, 1):
            self.listing.insert('end', f"{index}. " + flag.get('reason', '')[:50])
        self.reason.set('位置が分からない指摘はページ全体を確認します。' if self.flags else
                        '特定の印はありません。読取の正確性を保証するものではありません。')
        if hasattr(self, 'display'):
            self.draw()

    def draw(self):
        self.display = self.original.subsample(self.factor, self.factor)
        self.width, self.height = self.display.width(), self.display.height()
        self.canvas.delete('all')
        self.canvas.create_image(0, 0, image=self.display, anchor='nw')
        self.canvas.configure(scrollregion=(0, 0, self.width, self.height))
        for index, flag in enumerate(self.flags, 1):
            box = flag.get('bbox')
            if not box:
                continue
            try:
                x, y, w, h = (float(box[k]) for k in ('x', 'y', 'width', 'height'))
            except (KeyError, TypeError, ValueError):
                continue
            self.canvas.create_rectangle(x*self.width, y*self.height, (x+w)*self.width,
                                         (y+h)*self.height, outline='#c92929', width=2)
            self.canvas.create_text(x*self.width+3, y*self.height, anchor='nw', text=str(index),
                                    fill='#b51c1c', font=('Yu Gothic UI', 12, 'bold'))
        if self.region:
            x, y, w, h = self.region
            self.canvas.create_rectangle(x*self.width, y*self.height, (x+w)*self.width,
                                         (y+h)*self.height, outline='#1473d2', width=2, tags='selection')

    def zoom(self, delta):
        self.factor = max(1, min(8, self.factor + delta))
        self.draw()

    def point(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def press(self, event):
        self.start = self.point(event)
        self.region = None
        self.canvas.delete('selection')

    def drag(self, event):
        if self.start is not None:
            self.canvas.delete('selection')
            self.canvas.create_rectangle(*self.start, *self.point(event), outline='#1473d2',
                                         width=2, tags='selection')

    def release(self, event):
        if self.start is None:
            return
        self.region = normalized_box(self.start, self.point(event), self.width, self.height)
        self.start = None
        self.selection_label.set('再読取範囲：青枠の部分' if self.region else '再読取範囲：未選択（少し大きく囲んでください）')
        self.draw()

    def select_flag(self, event=None):
        selected = self.listing.curselection()
        if not selected:
            return
        flag = self.flags[selected[0]]
        self.reason.set(flag.get('reason', '') + '\n\n' + flag.get('text', ''))
        box = flag.get('bbox')
        if box:
            self.canvas.xview_moveto(max(0, box['x'] - 0.05))
            self.canvas.yview_moveto(max(0, box['y'] - 0.05))

    def retry(self):
        if not self.region:
            messagebox.showinfo('再読取範囲', '原文の上をドラッグして範囲を選んでください。', parent=self)
            return
        rotations = (0, 90, 180, 270) if self.direction.get() == '4方向' else (int(self.direction.get().replace('度', '')),)
        self.reread(self.row, self.region, rotations, self)
