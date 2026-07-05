#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
S&P500 新規採用銘柄 検知ツール
--------------------------------
Wikipedia の "List of S&P 500 companies" ページにある
「Selected changes to the list of S&P 500 components」テーブルを解析し、
直近 LOOKBACK_DAYS 日以内に追加された銘柄のうち、まだ通知していないものを
Microsoft Graph の sendMail API でメール通知する。

通知済みの (日付, ティッカー) の組は notified.json に保存し、
GitHub Actions 側でリポジトリにコミットして永続化する想定。

ローカル実行時は、リポジトリ直下に .env ファイルを置いておけば
python-dotenv が自動的に読み込む(.env.example を参照)。

必要な環境変数(Microsoft Entra ID アプリ登録 / Graph API用):
  MS_TENANT_ID      Azure AD テナントID
  MS_CLIENT_ID      アプリ(クライアント)ID
  MS_CLIENT_SECRET  クライアントシークレット
  MS_SENDER_UPN     送信元メールボックスのUPN(例: notifier@yourtenant.onmicrosoft.com)
  MAIL_TO           通知先メールアドレス(カンマ区切りで複数可)
  LOOKBACK_DAYS     何日分遡ってチェックするか(省略時 10)

事前準備(Azure AD / Microsoft Graph 側):
  1. Microsoft Entra ID → アプリの登録 で新規アプリを登録
  2. API のアクセス許可 → Microsoft Graph → アプリケーションの許可 → Mail.Send を追加
  3. 管理者の同意を付与
  4. 証明書とシークレット でクライアントシークレットを発行
  (アプリケーション権限のMail.Sendは既定で組織内の全メールボックスから送信可能なため、
   セキュリティ上、Exchange Online の New-ApplicationAccessPolicy で
   送信元メールボックスを限定することを推奨)
"""

import os
import json
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
import pandas as pd
from io import StringIO

try:
    from dotenv import load_dotenv
    load_dotenv()  # .env が存在すれば読み込む(ローカル実行用。無ければ何もしない)
except ImportError:
    pass

WIKI_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
STATE_FILE = "notified.json"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "10"))
STATE_RETENTION_DAYS = 90


def fetch_changes_table() -> pd.DataFrame:
    """Wikipediaページから『Selected changes』テーブルを取得してDataFrame化する"""
    headers = {
        # Wikipediaのロボットポリシーに配慮し、連絡先を含むUser-Agentを設定してください
        "User-Agent": "sp500-watcher-bot/1.0 (contact: your-email@example.com)"
    }
    resp = requests.get(WIKI_URL, headers=headers, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    heading = None
    for tag in soup.find_all(["h2", "h3"]):
        if "selected changes" in tag.get_text(strip=True).lower():
            heading = tag
            break
    if heading is None:
        raise RuntimeError(
            "『Selected changes』の見出しが見つかりませんでした。"
            "Wikipediaのページ構成が変わった可能性があります。"
        )

    table = heading.find_next("table")
    if table is None:
        raise RuntimeError("changesテーブルが見つかりませんでした。")

    df = pd.read_html(StringIO(str(table)))[0]

    def flatten(col):
        if isinstance(col, tuple):
            parts = [str(c) for c in col if "Unnamed" not in str(c)]
            seen = []
            for p in parts:
                if p not in seen:
                    seen.append(p)
            return "_".join(seen)
        return str(col)

    df.columns = [flatten(c) for c in df.columns]
    return df


def parse_date(s: str):
    s = str(s).strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"notified": []}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_graph_token() -> str:
    """クライアントクレデンシャルフローでMicrosoft Graph用アクセストークンを取得"""
    tenant_id = os.environ["MS_TENANT_ID"]
    client_id = os.environ["MS_CLIENT_ID"]
    client_secret = os.environ["MS_CLIENT_SECRET"]

    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://graph.microsoft.com/.default",
        "grant_type": "client_credentials",
    }
    resp = requests.post(url, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()["access_token"]


def send_mail(subject: str, body: str) -> None:
    """Microsoft Graph の sendMail API でメールを送信する"""
    sender_upn = os.environ["MS_SENDER_UPN"]
    mail_to = [addr.strip() for addr in os.environ["MAIL_TO"].split(",") if addr.strip()]

    token = get_graph_token()
    url = f"https://graph.microsoft.com/v1.0/users/{sender_upn}/sendMail"
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": addr}} for addr in mail_to],
        },
        "saveToSentItems": "false",
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code >= 300:
        raise RuntimeError(
            f"Graph sendMail に失敗しました: {resp.status_code} {resp.text}"
        )


def main():
    state = load_state()
    notified_keys = set(tuple(x) for x in state.get("notified", []))

    df = fetch_changes_table()

    date_col = next((c for c in df.columns if "date" in c.lower()), None)
    added_ticker_col = next(
        (c for c in df.columns if "added" in c.lower() and "ticker" in c.lower()), None
    )
    added_security_col = next(
        (c for c in df.columns if "added" in c.lower() and "security" in c.lower()), None
    )

    if not date_col or not added_ticker_col:
        raise RuntimeError(
            "必要な列(Date / Added_Ticker)が見つかりませんでした。"
            f"検出された列: {list(df.columns)}"
        )

    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).date()

    new_events = []
    for _, row in df.iterrows():
        d = parse_date(row[date_col])
        ticker = str(row[added_ticker_col]).strip()
        if not d or not ticker or ticker.lower() == "nan":
            continue
        if d < cutoff:
            continue
        key = (d.isoformat(), ticker)
        if key in notified_keys:
            continue
        security = str(row[added_security_col]).strip() if added_security_col else ""
        new_events.append((d, ticker, security))

    new_events.sort(key=lambda x: x[0])

    if new_events:
        lines = [f"・{d.isoformat()}  {ticker} ({security}) を追加" for d, ticker, security in new_events]
        body = "S&P 500 に新たに追加された銘柄を検知しました。\n\n" + "\n".join(lines) \
               + f"\n\n(参照元: {WIKI_URL} )"
        subject = f"[SP500 Watcher] 新規追加銘柄あり ({len(new_events)}件)"
    else:
        body = f"本日時点で、直近{LOOKBACK_DAYS}日以内の新規追加銘柄はありません。"
        subject = "[SP500 Watcher] 追加なし"

    send_mail(subject, body)
    print(subject)
    print(body)

    for d, ticker, _ in new_events:
        notified_keys.add((d.isoformat(), ticker))

    keep_cutoff = (datetime.now(timezone.utc) - timedelta(days=STATE_RETENTION_DAYS)).date().isoformat()
    state["notified"] = sorted([list(k) for k in notified_keys if k[0] >= keep_cutoff])
    save_state(state)


if __name__ == "__main__":
    main()
