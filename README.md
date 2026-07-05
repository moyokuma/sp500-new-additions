# SP500 Watcher

Wikipedia の [List of S&P 500 companies](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies)
にある「Selected changes to the list of S&P 500 components」テーブルを解析し、
新規に追加された銘柄をメールで通知するツールです。

## 全体構成

```
Power Automate (毎日の定時実行)
   └─ HTTP アクション: GitHub API で workflow_dispatch を呼び出し
        └─ GitHub Actions: check_sp500_changes.py を実行
             ├─ Wikipediaから変更履歴テーブルを取得・解析
             ├─ 未通知の新規追加銘柄があればメール送信(SMTP)
             └─ notified.json を更新してリポジトリにコミット
```

GitHub Actions の `schedule` トリガーは実行タイミングが数十分〜遅延することがあるため、
Power Automate 側の定時フローから `workflow_dispatch` を呼び出す構成にしています。

## セットアップ手順

### 1. リポジトリの準備

このフォルダの中身をそのまま GitHub リポジトリにpushしてください。
`.github/workflows/sp500-watcher.yml` がワークフロー定義です。

### 2. Microsoft Entra ID(Azure AD)アプリの登録 (Microsoft Graph 用)

メール送信は Microsoft Graph の `sendMail` API(クライアントクレデンシャルフロー)を
使用します。以下の準備が必要です。

1. Azure ポータル → **Microsoft Entra ID** → **アプリの登録** → **新規登録**
2. 登録したアプリの **API のアクセス許可** → **アクセス許可の追加**
   → **Microsoft Graph** → **アプリケーションの許可** → `Mail.Send` を追加
3. **管理者の同意を与える** をクリックして同意
4. **証明書とシークレット** → **新しいクライアント シークレット** を作成し、値を控えておく
5. 送信元として使うメールボックス(共有メールボックス推奨)のUPNを控えておく

> **セキュリティに関する注意**
> アプリケーション権限の `Mail.Send` は既定では組織内の全メールボックスから
> 送信可能になります。Exchange Online PowerShell の
> `New-ApplicationAccessPolicy` を使い、このアプリが送信できるメールボックスを
> 通知専用の1つに限定することを強く推奨します。

### 3. GitHub Secrets の設定

リポジトリの Settings → Secrets and variables → Actions で以下を登録します。

| Secret名 | 内容 |
|---|---|
| `MS_TENANT_ID` | Azure AD テナントID |
| `MS_CLIENT_ID` | アプリ(クライアント)ID |
| `MS_CLIENT_SECRET` | クライアントシークレットの値 |
| `MS_SENDER_UPN` | 送信元メールボックスのUPN |
| `MAIL_TO` | 通知先アドレス(複数の場合はカンマ区切り) |

### 4. GitHub Personal Access Token(Power Automateから叩くため)

Power Automate から GitHub API を呼ぶための PAT を発行します。

- GitHub → Settings → Developer settings → Fine-grained tokens
- 対象リポジトリを指定
- Permissions: `Actions: Read and write` を付与
- 発行したトークンは Power Automate 側で安全に管理してください(可能であれば
  Azure Key Vault コネクタ経由で参照するのが望ましいです)。

### 5. Power Automate フローの作成

1. トリガー: **定期的な間隔(Recurrence)** を追加し、毎日1回(例: 朝7時)に設定
2. アクション: **HTTP** を追加し、以下を設定

   - Method: `POST`
   - URI:
     ```
     https://api.github.com/repos/{owner}/{repo}/actions/workflows/sp500-watcher.yml/dispatches
     ```
   - Headers:
     ```
     Authorization: Bearer <PAT>
     Accept: application/vnd.github+json
     X-GitHub-Api-Version: 2022-11-28
     ```
   - Body:
     ```json
     { "ref": "main" }
     ```

   `{owner}` `{repo}` は実際のGitHubユーザー名/組織名とリポジトリ名に置き換えてください。
   ブランチ名が `main` でない場合は `ref` を合わせてください。

3. 必要に応じて、HTTPアクションの後に「応答コードが2xx以外なら通知する」等の
   エラーハンドリングを追加すると運用が安定します。

### 6. 動作確認(GitHub Actions側)

- リポジトリの Actions タブから `SP500 Watcher` を手動実行(`workflow_dispatch`)して
  メールが届くか確認してください。
- 初回実行時は `notified.json` が空なので、直近 `LOOKBACK_DAYS`(既定10日)以内に
  変更履歴テーブルに記載されている追加銘柄をまとめて通知します。

## ローカルでの実行

GitHub Actions を使わず、手元のPC(検証用・デバッグ用途)でも実行できます。

```bash
# 1. 依存パッケージのインストール(仮想環境の利用を推奨)
python -m venv venv
venv\Scripts\activate   # Windowsの場合: .venv\Scripts\activate
pip install -r requirements.txt

# 2. .env ファイルを準備
cp .env.example .env
# .env を開いて MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET /
# MS_SENDER_UPN / MAIL_TO を実際の値に書き換える

# 3. 実行
venv\Scripts\python check_sp500_changes.py
```

`.env` があれば `python-dotenv` が自動的に環境変数として読み込みます
(`.env` は `.gitignore`済みなので誤ってコミットされません)。
ローカル実行後も `notified.json` は更新されるため、GitHub Actions側の状態と
混ざらないよう、検証用に別ブランチ・別ディレクトリで試すことをおすすめします。

## 動作の詳細

- 「Date added」列を持つ全銘柄一覧テーブルではなく、変更履歴専用の
  「Selected changes to the list of S&P 500 components」テーブルを直接パースしています。
  こちらの方が「いつ・どの銘柄が追加/除外されたか」を素直に取得できます。
- Wikipediaの編集は反映が数日遅れることがあるため、「前日のみ」ではなく直近
  `LOOKBACK_DAYS` 日分をチェックし、`notified.json` で通知済みかどうかを管理する
  ことで、検知漏れと重複通知の両方を防いでいます。
- `notified.json` は直近90日分のみ保持し、無限に肥大化しないようにしています。

## 既知の制約・改善余地

- Wikipediaはコミュニティ編集のページのため、情報の正確性や反映タイミングは
  保証されません。重要な判断に使う場合は、S&P Dow Jones Indices の公式発表など
  一次情報との突き合わせを検討してください。
- ページのテーブル構造(見出し文言・列名)が将来変わると、パース処理が
  失敗する可能性があります。その場合はメール送信自体が失敗するので、
  Power Automate 側で「HTTPアクション失敗時のアラート」も設定しておくと安心です。
- GitHub Actionsの実行自体をPower Automateではなく外部cronサービス
  (例: cron-job.org)から `repository_dispatch` を叩く形にしても同等の構成が
  組めます。Power Automateを他の用途で使う予定がなければ、そちらの方が
  運用がシンプルになる場合があります。
