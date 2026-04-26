# Power Automate フロー設定手順

官報テキストファイルを OneDrive から SharePoint へ自動転送するフローです。

## 前提

| 項目 | 値 |
|---|---|
| OneDrive 監視フォルダ | `/kanpo-downloads` |
| SharePoint サイト | `https://yourcompany.sharepoint.com/sites/kanpo` |
| ドキュメントライブラリ | `kanpo` |

---

## 手順

### 1. Power Automate を開く

[https://make.powerautomate.com](https://make.powerautomate.com) にアクセスし、会社アカウントでサインイン。

---

### 2. フローを新規作成する

1. 左メニューの「**作成**」をクリック
2. 「**自動化したクラウドフロー**」を選択
3. フロー名を入力（例: `官報 OneDrive → SharePoint`）

---

### 3. トリガーを設定する

1. 検索欄に `OneDrive` と入力
2. **「OneDrive for Business - ファイルが作成されたとき」** を選択

| 設定項目 | 値 |
|---|---|
| フォルダー | `/kanpo-downloads` （フォルダアイコンから選択） |

> **注意:** サブフォルダ内のファイルも対象にするには「サブフォルダーを含める」を **はい** にする。

---

### 4. アクションを追加する

「**＋ 新しいステップ**」をクリックし、`SharePoint` を検索。  
**「SharePoint - ファイルの作成」** を選択。

| 設定項目 | 値 |
|---|---|
| サイトのアドレス | `https://yourcompany.sharepoint.com/sites/kanpo` |
| フォルダーのパス | `/kanpo` |
| ファイル名 | 動的コンテンツ →「**ファイル名**」を選択 |
| ファイルコンテンツ | 動的コンテンツ →「**ファイルコンテンツ**」を選択 |

---

### 5. 保存してテストする

1. 右上の「**保存**」をクリック
2. 「**テスト**」→「**手動**」を選択
3. ローカルで以下を実行してファイルを生成する

```powershell
cd C:\Users\Owner\tools
kanpo\.venv\Scripts\python kanpo\run.py --max-pages 1
```

4. Power Automate のテスト画面で「**フローの実行**」をクリック
5. SharePoint の `kanpo` ライブラリにファイルが届いていることを確認

---

## フロー全体の流れ

```
kanpo\run.py 実行
    ↓ text/*.txt 生成
OneDrive /kanpo-downloads/ に保存
    ↓ 自動同期（OneDrive クライアント）
Power Automate トリガー発火
「OneDrive for Business - ファイルが作成されたとき」
    ↓
SharePoint ドキュメントライブラリ「kanpo」にコピー
    ↓
Copilot Studio がナレッジソースとして読み込む
    ↓
Teams チャットで質問に回答
```

---

## トラブルシューティング

| 症状 | 確認箇所 |
|---|---|
| フローが発火しない | OneDrive クライアントの同期が完了しているか確認 |
| SharePoint にファイルが届かない | サイト URL とライブラリ名のスペルを確認 |
| 「アクセス許可がありません」エラー | SharePoint サイトへの書き込み権限を管理者に確認 |
