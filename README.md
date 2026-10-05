# 発狂号泣霊夢 Discord Bot (reimuLLM)

精神が限界突破し、発狂しながら号泣している「博麗霊夢」と会話できる Discord Bot です。
Discord.py と Google Gemini API を利用しています。

---

## 📁 ディレクトリ構成

- `main.py`: Discord Bot メインプログラム
- `systemPrompt.txt`: 霊夢のシステムプロンプト（**Botを起動したまま編集・保存しても、次の会話から即座に反映されます**）
- `.env`: APIキーやトークンの設定ファイル（Git除外）
- `.env.example`: 設定項目のテンプレート
- `requirements.txt`: 必要なPythonライブラリ一覧

---

## 🚀 セットアップ手順

### 1. 仮想環境のアクティベートとライブラリのインストール

```powershell
# 仮想環境のアクティベート
.\venv\Scripts\Activate.ps1

# 依存ライブラリのインストール
pip install -r requirements.txt
```

### 2. `.env` の設定

[.env](file:///c:/Users/keita/Desktop/localProjects/reimuLLM/.env) に以下の値を設定してください。

```env
DISCORD_BOT_TOKEN=あなたのDiscordボットトークン
GEMINI_API_KEY=あなたのGemini_APIキー
GEMINI_MODEL=gemini-2.5-flash
```

> [!NOTE]
> - **Discord Bot Token**: [Discord Developer Portal](https://discord.com/developers/applications) から取得し、Bot の `MESSAGE CONTENT INTENT` を ON にしてください。
> - **Gemini API Key**: [Google AI Studio](https://aistudio.google.com/) から取得できます。

---

## 🏃 起動方法

```powershell
python main.py
```

---

## 💬 使い方

1. Discord サーバーで Bot を招待します。
2. チャンネルで Bot にメンション（`@Bot名 こんにちは`）するか、Botにダイレクトメッセージ（DM）を送ります。
3. 霊夢が情緒不安定に泣き叫びながら返信してきます。

---

## ✏️ プロンプトの調整

[prompt.txt](file:///c:/Users/keita/Desktop/localProjects/reimuLLM/prompt.txt) を開いてプロンプトを自由に編集してください。
メッセージを受信するたびに自動でファイルを再読込するため、**Botを再起動せずにプロンプトを練り直してテストできます**。
