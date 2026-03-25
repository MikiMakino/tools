# compress_pdf

PDFファイルを指定サイズ（デフォルト: 1MB）以下に圧縮するツールです。
GUIアプリ（Flet製）とコマンドラインの両方で動作します。

## 必要なもの

- **Ghostscript**（推奨）: 画像の解像度を下げて強力に圧縮します
  - ダウンロード: https://www.ghostscript.com/releases/gsdnld.html
  - インストール後、`gswin64c` にPATHが通っていれば自動認識されます
- Ghostscriptなし: pikepdfによる基本圧縮（ストリーム圧縮のみ）

## インストール

### 実行ファイル（exe）を使う場合

Releasesページから `compress_pdf.exe` をダウンロードし、任意のフォルダに置いて実行するだけです。

### Pythonで動かす場合（conda 推奨）

```powershell
# 専用の仮想環境を作成
conda create -n compress_pdf python=3.11
conda activate compress_pdf

# pikepdf は conda-forge から取得（依存ライブラリが自動解決されるため推奨）
conda install -c conda-forge pikepdf

# flet と pyinstaller は conda-forge 未対応のため pip で追加
pip install flet pyinstaller

# 起動
python main.py
```

### Pythonで動かす場合（pip のみ）

```powershell
pip install pikepdf flet
python main.py
```

## GUI の使い方

```
┌─────────────────────────────────────┐
│  PDF 圧縮ツール                       │
├─────────────────────────────────────┤
│  [ファイルを追加] [フォルダを追加]  [クリア] │
│  ┌─────────────────────────────────┐ │
│  │ ファイル名        サイズ  状態  │ │
│  │ document.pdf     3.2MB  待機   │ │
│  │ report.pdf       5.1MB  待機   │ │
│  └─────────────────────────────────┘ │
│  目標サイズ: [1.0] MB                 │
│  出力先: [同じフォルダ ▼]             │
│                                     │
│         [圧縮を開始]                  │
│  ████████████░░░░  2/3 完了          │
└─────────────────────────────────────┘
```

- ファイルはドラッグ&ドロップでも追加できます
- 出力先は「同じフォルダ」か「別のフォルダを指定」を選べます
- 圧縮完了したファイルは状態欄に結果サイズ（例: `✓ 0.8MB`）が表示されます

## CUIの使い方

```powershell
compress_pdf [options] <input> [input ...]
```

### 引数

| 引数 | 説明 |
|------|------|
| `input` | 入力PDFファイル（複数可、glob可、ディレクトリ可） |
| `-o, --output` | 出力先（単一→ファイルパス、複数→ディレクトリパス） |
| `--target MB` | 目標ファイルサイズ（MB、デフォルト: `1.0`） |
| `--suffix TEXT` | 出力ファイルのサフィックス（デフォルト: `_compressed`） |

### 使用例

```powershell
# 単一ファイル → input_compressed.pdf が生成される
compress_pdf input.pdf

# 複数ファイルを指定
compress_pdf a.pdf b.pdf c.pdf

# ワイルドカードで一括処理
compress_pdf *.pdf

# ディレクトリ内の全PDFを再帰的に処理
compress_pdf ./docs/

# 出力ファイル名を指定
compress_pdf input.pdf -o output.pdf

# 複数ファイルを出力ディレクトリにまとめる
compress_pdf *.pdf -o ./compressed/

# 目標サイズを2MBに変更
compress_pdf input.pdf --target 2.0
```

## 圧縮の仕組み

Ghostscriptがある場合、以下の品質を順に試して目標サイズ以下になった時点で採用します。

| 品質 | 解像度 | 用途 |
|------|--------|------|
| `ebook` | 150 dpi | 通常の圧縮（デフォルトで最初に試行） |
| `screen` | 72 dpi | ebook で目標未達の場合に試行 |

Ghostscriptがない場合は pikepdf でストリームの再圧縮のみ行います（画像は再エンコードしません）。

## ビルド方法（開発者向け）

```powershell
conda activate compress_pdf
$FLET_DESKTOP = python -c "import flet_desktop, os; print(os.path.dirname(flet_desktop.__file__))"
$FLET_PKG     = python -c "import flet, os; print(os.path.dirname(flet.__file__))"

pyinstaller --onefile --name compress_pdf `
  --add-data "$FLET_DESKTOP/app;flet_desktop/app" `
  --add-data "$FLET_PKG/controls/material/icons.json;flet/controls/material" `
  --add-data "$FLET_PKG/controls/cupertino/cupertino_icons.json;flet/controls/cupertino" `
  --add-data "guide.html;." `
  main.py
```

生成された `dist/compress_pdf.exe` を配布してください。`guide.html` は exe に同梱されています。
