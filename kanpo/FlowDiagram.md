# 官報テキスト抽出 → SharePoint → Copilot Agent フロー図

```mermaid
flowchart TD
    A([官報サイト\nkanpo.go.jp]) -->|Playwright で巡回| B

    subgraph LOCAL["ローカル処理（Python）"]
        B["`**kanpo_downloader.py**
        トップページから最新号を探索
        目次 → 会社その他セクション取得`"]
        B --> C[wrapper HTML 取得]
        C --> D[inner HTML 取得]
        D --> E[PDF ダウンロード]
        E --> F["`**pdfminer.six**
        テキスト抽出`"]
        F --> G["`**クリーニング処理**
        ・PUA文字ヘッダー除去
        ・単独「官」「報」除去
        ・URLフラグメント除去
        ・連続空行の整理`"]
        G --> H[("`downloads/
        ├─ text/*.txt ✅
        ├─ pdf/*.pdf
        ├─ manifest.json ✅
        └─ inner/*.html`")]
    end

    subgraph RUN["run.py"]
        I["`出力先を
        OneDrive フォルダに指定`"]
    end

    B -.->|run.py 経由で実行| I
    I --> H2

    subgraph ONEDRIVE["OneDrive（自動同期）"]
        H2[("`kanpo-downloads/
        └─ text/*.txt`")]
    end

    H2 -->|ファイルが作成されたとき| PA

    subgraph POWERAUTOMATE["Power Automate クラウドフロー"]
        PA["`トリガー
        OneDrive for Business
        ファイルが作成されたとき`"]
        PA --> PB["`アクション
        SharePoint
        ファイルの作成`"]
    end

    PB --> SP

    subgraph SHAREPOINT["SharePoint"]
        SP[("`ドキュメントライブラリ
        **kanpo**
        ├─ ...0026.txt
        ├─ ...0027.txt
        └─ ...0032.txt`")]
    end

    SP -->|ナレッジソースとして登録| CS

    subgraph COPILOT["Copilot Studio"]
        CS["`**Copilot Agent**
        ナレッジソース: kanpo ライブラリ
        質問に対して該当テキストを参照`"]
    end

    CS -->|チャットで回答| Teams([Microsoft Teams])

    style LOCAL fill:#e8f4fd,stroke:#2196F3
    style RUN fill:#fff3e0,stroke:#FF9800
    style ONEDRIVE fill:#e3f2fd,stroke:#1565C0
    style POWERAUTOMATE fill:#f3e5f5,stroke:#7B1FA2
    style SHAREPOINT fill:#e8f5e9,stroke:#2E7D32
    style COPILOT fill:#fce4ec,stroke:#C62828
```

## 各コンポーネントの役割

| コンポーネント | 役割 | ファイル |
|---|---|---|
| `kanpo_downloader.py` | 官報サイト巡回・PDF取得・テキスト抽出・クリーニング | `kanpo/kanpo_downloader.py` |
| `run.py` | OneDrive フォルダを出力先にして downloader を実行 | `kanpo/run.py` |
| OneDrive 同期 | ローカルファイルをクラウドへ自動同期 | OS の OneDrive クライアント |
| Power Automate | OneDrive の新規ファイルを SharePoint へ転送 | `kanpo/PowerAutomate.md` 参照 |
| SharePoint ライブラリ `kanpo` | Copilot Studio のナレッジソース置き場 | — |
| Copilot Studio | テキストを読み込み Teams チャットで回答 | — |

## 実行コマンド

```powershell
# 通常実行（OneDrive へ保存）
cd C:\Users\Owner\tools
kanpo\.venv\Scripts\python kanpo\run.py

# ページ数を制限してテスト
kanpo\.venv\Scripts\python kanpo\run.py --max-pages 1
```
