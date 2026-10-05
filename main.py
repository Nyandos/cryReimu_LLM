import os
import sys
import json
from pathlib import Path
import random
from datetime import datetime, time, timezone, timedelta
import discord
from discord.ext import tasks
from dotenv import load_dotenv
from google import genai
from google.genai import types

ENV_FILE = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=ENV_FILE)

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 日直機能の設定
DUTY_CHANNEL_ID = int(os.getenv("DUTY_CHANNEL_ID", "1408453515625500754"))
DUTY_ROLE_NAME = os.getenv("DUTY_ROLE_NAME", "日直")
DUTY_STATE_FILE = Path(__file__).parent / "duty_state.json"
JST = timezone(timedelta(hours=9))

# 研究用ログファイルの設定（LLMプロンプトには含めない）
LOG_FILE = Path(__file__).parent / "log.txt"

def append_to_research_log(author_name: str, user_input: str, reply_text: str):
    """ユーザー入力と返答を結びつけてlog.txtに追記保存する"""
    now_str = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"[{now_str}]\n"
        f"User: {author_name}\n"
        f"Input: {user_input}\n"
        f"Response:\n{reply_text}\n"
        f"{'-' * 50}\n"
    )
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(entry)
    except Exception as e:
        print(f"⚠️ [Log Error] log.txt への書き込みに失敗しました: {e}", file=sys.stderr)

def get_today_jst_str() -> str:
    """日本時間(JST)の今日の日付文字列(YYYY-MM-DD)を返す"""
    return datetime.now(JST).strftime("%Y-%m-%d")

def load_duty_state() -> dict:
    """日直の実施記録を読み込む"""
    if DUTY_STATE_FILE.exists():
        try:
            return json.loads(DUTY_STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"⚠️ [日直機能] 記録ファイルの読み込み失敗: {e}")
    return {}

def save_duty_state(date_str: str, member_ids: list[int], member_names: list[str] = None):
    """日直の実施記録を保存する"""
    data = {
        "date": date_str,
        "member_ids": member_ids,
        "member_names": member_names or []
    }
    DUTY_STATE_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def is_duty_done_today() -> bool:
    """今日の抽選が既に完了しているか判定する"""
    state = load_duty_state()
    return state.get("date") == get_today_jst_str()

def get_runtime_config() -> tuple[str, float]:
    """メッセージ受信ごとに .env を再読込し、最新のモデル名と温度を返す"""
    load_dotenv(dotenv_path=ENV_FILE, override=True)
    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    try:
        temp = float(os.getenv("GEMINI_TEMPERATURE", "1.2"))
    except ValueError:
        temp = 1.2
    return model, temp

PROMPT_FILE = Path(__file__).parent / "systemPrompt.txt"
FALLBACK_PROMPT_FILE = Path(__file__).parent / "prompt.txt"

def load_system_prompt() -> str:
    """systemPrompt.txt (または prompt.txt) からシステムプロンプトを読み込む。"""
    for file_path in [PROMPT_FILE, FALLBACK_PROMPT_FILE]:
        if file_path.exists():
            try:
                content = file_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
            except Exception as e:
                print(f"⚠️ {file_path.name} の読み込みに失敗しました: {e}", file=sys.stderr)
    return "あなたは発狂しながら号泣している博麗霊夢です。"

# Gemini クライアントの初期化
gemini_client = None
if GEMINI_API_KEY:
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
else:
    print("⚠️ GEMINI_API_KEY が設定されていません。.env を確認してください。")

# Discord Bot クライアントの設定
intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # ロール所持メンバーを取得するために有効化
client = discord.Client(intents=intents)

async def pick_and_announce_duty(channel: discord.TextChannel) -> bool:
    """日直ロールのユーザーから2名を抽選して指定チャンネルでメンションする"""
    guild = channel.guild
    role = discord.utils.get(guild.roles, name=DUTY_ROLE_NAME)
    if not role:
        print(f"⚠️ [日直機能] サーバー '{guild.name}' にロール '{DUTY_ROLE_NAME}' が見つかりませんでした。")
        return False

    # ロール所持メンバーの抽出（Botは除外）
    members = [m for m in role.members if not m.bot]

    # キャッシュされていない場合のフォールバック取得
    if not members:
        try:
            async for m in guild.fetch_members(limit=None):
                if role in m.roles and not m.bot and m not in members:
                    members.append(m)
        except Exception as e:
            print(f"⚠️ [日直機能] fetch_members でエラー: {e}")

    if not members:
        print(f"⚠️ [日直機能] '{DUTY_ROLE_NAME}' ロールを持つユーザーが存在しませんでした。")
        await channel.send(f"⚠️ **【今日の日直】**\n`{DUTY_ROLE_NAME}` ロールが付いている人が誰もいないわよぉぉぉッ！！")
        return False

    # 最大2名を抽選（1名しかいない場合は1名）
    count = min(2, len(members))
    selected = random.sample(members, count)
    mentions = " ".join(m.mention for m in selected)

    # 実施記録を保存（再起動しても日付を維持）
    today_str = get_today_jst_str()
    save_duty_state(today_str, [m.id for m in selected], [m.name for m in selected])

    print(f"📢 [日直機能] 抽選完了 ({today_str}): {[m.name for m in selected]}")
    await channel.send(f"【今日の日直】\n{mentions}")
    return True

async def is_duty_server(guild: discord.Guild | None) -> bool:
    """指定されたギルドが、DUTY_CHANNEL_IDを含む身内専用サーバーか判定する"""
    if not guild:
        return False
    channel = client.get_channel(DUTY_CHANNEL_ID)
    if channel is None:
        try:
            channel = await client.fetch_channel(DUTY_CHANNEL_ID)
        except Exception:
            return False
    return isinstance(channel, discord.TextChannel) and channel.guild.id == guild.id

# 毎日 日本時間 0:00 (JST / UTC+9) に実行する定期タスク
@tasks.loop(time=time(hour=0, minute=0, tzinfo=JST))
async def daily_duty_task():
    # 既に今日抽選が行われていればスキップ
    if is_duty_done_today():
        print("ℹ️ [日直機能] 今日の日直抽選は既に完了しているためスキップします。")
        return

    try:
        channel = client.get_channel(DUTY_CHANNEL_ID)
        if channel is None:
            channel = await client.fetch_channel(DUTY_CHANNEL_ID)
        if isinstance(channel, discord.TextChannel):
            await pick_and_announce_duty(channel)
        else:
            print(f"⚠️ [日直機能] チャンネル ID {DUTY_CHANNEL_ID} がテキストチャンネルではありません。")
    except Exception as e:
        print(f"❌ [日直機能] 定期タスク実行中にエラーが発生しました: {e}", file=sys.stderr)

@client.event
async def on_ready():
    initial_model, initial_temp = get_runtime_config()
    print(f"✅ ログイン完了: {client.user.name} ({client.user.id})")
    print(f"📡 使用モデル: {initial_model}")
    print(f"🌡️ 生成温度 (temperature): {initial_temp}")
    print(f"📝 プロンプトファイル: {PROMPT_FILE.resolve()}")
    print(f"⏰ 日直通知: 毎日 00:00 JST / チャンネル ID: {DUTY_CHANNEL_ID}")
    print("--------------------------------------------------")

    # 定期タスクの開始
    if not daily_duty_task.is_running():
        daily_duty_task.start()

async def send_split_message(channel, text: str, reply_to: discord.Message = None):
    """Discordの2000文字制限に対応してメッセージを分割送信する"""
    chunk_size = 1900
    chunks = [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
    for i, chunk in enumerate(chunks):
        if i == 0 and reply_to:
            await reply_to.reply(chunk)
        else:
            await channel.send(chunk)

# 危険な単語（NGワード）の設定
DANGEROUS_WORDS = ["ひんや"]
WARNING_MESSAGE = "⚠️ **危険な単語を検知しました。** この単語に関するやり取りは安全のため禁止されています。"

def contains_dangerous_word(text: str) -> bool:
    """テキスト内に危険な単語が含まれているか判定する"""
    return any(word in text for word in DANGEROUS_WORDS)

# 共有会話履歴管理 (サーバー全員で共有、最大12ターン = 12往復)
# 形式: [{"author": str, "user": str, "model": str}, ...]
MAX_HISTORY_TURNS = 12
shared_history: list[dict[str, str]] = []

def build_request_contents(author_name: str, current_user_input: str) -> tuple[list[types.Content], int]:
    """共有履歴から Gemini 送信用 contents を構築する。
    直近の (MAX_HISTORY_TURNS - 1) ターン + 今回の入力を結合して最大 12 ターンに収める。
    """
    recent_turns = shared_history[-(MAX_HISTORY_TURNS - 1):] if MAX_HISTORY_TURNS > 1 else []

    contents = []
    for turn in recent_turns:
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=turn["user"])]))
        contents.append(types.Content(role="model", parts=[types.Part.from_text(text=turn["model"])]))

    # 今回のユーザー発言（ニックネーム優先で明記）を追加
    formatted_user_input = f"{author_name}: {current_user_input}"
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=formatted_user_input)]))
    stack_count = len(recent_turns) + 1
    return contents, stack_count

def save_shared_turn(author_name: str, user_text: str, model_text: str):
    """1ターン分の会話を共有スタックに保存し、最大12ターンを超えた古い履歴を削除する"""
    formatted_user_input = f"{author_name}: {user_text}"
    shared_history.append({"author": author_name, "user": formatted_user_input, "model": model_text})
    if len(shared_history) > MAX_HISTORY_TURNS:
        # 古いターンを完全に切り捨てる
        del shared_history[:len(shared_history) - MAX_HISTORY_TURNS]

@client.event
async def on_message(message: discord.Message):
    # Bot自身の発言は無視
    if message.author == client.user:
        return

    # 日直の手動抽選コマンド (!日直) - 身内専用（DUTY_CHANNEL_ID が含まれるサーバーのみ有効）
    if message.content.strip() in ["!日直", "!nichoku"]:
        if not await is_duty_server(message.guild):
            # 対象チャンネルが含まれるサーバー以外（余所のサーバーやDM）では完全に無視する
            return

        if is_duty_done_today():
            state = load_duty_state()
            m_names = state.get("member_names", [])
            # 名前が保存されていない場合はIDから名前を取得
            if not m_names:
                m_ids = state.get("member_ids", [])
                for mid in m_ids:
                    user = client.get_user(mid)
                    m_names.append(user.display_name if user else f"ID:{mid}")

            names_str = "、".join(f"`{name}`" for name in m_names) if m_names else "（記録なし）"
            # メンション通知を完全に無効化して返信（スパム防止）
            await message.reply(
                f"今日の日直はもう決まってるわよぉぉぉッ！！再抽選なんてさせないからね！！\n【今日の日直】 {names_str}",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none()
            )
            return

        # まだ今日の抽選が行われていない場合のみ、指定チャンネルで抽選を実行
        try:
            target_channel = client.get_channel(DUTY_CHANNEL_ID)
            if target_channel is None:
                target_channel = await client.fetch_channel(DUTY_CHANNEL_ID)
            if isinstance(target_channel, discord.TextChannel):
                success = await pick_and_announce_duty(target_channel)
                if success and message.channel.id != target_channel.id:
                    await message.reply(f"今日の {target_channel.mention} の日直を抽選しておいたわよ！")
            else:
                await message.reply(f"⚠️ 指定チャンネル（ID: {DUTY_CHANNEL_ID}）が見つかりません！")
        except Exception as e:
            await message.reply(f"⚠️ 日直抽選中にエラーが発生したわよ！\n```{e}```")
        return

    # Botへのメンション、またはDMの場合に応答
    is_mentioned = client.user in message.mentions
    is_dm = isinstance(message.channel, discord.DMChannel)

    if not (is_mentioned or is_dm):
        return

    # メンション部分を除去してユーザーの入力メッセージを抽出
    user_input = message.content
    if client.user:
        user_input = user_input.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()

    # サーバー内のニックネームを優先（なければユーザー名）
    author_name = message.author.display_name

    # 危険な単語の入力検知
    if contains_dangerous_word(user_input):
        print(f"🚨 [Dangerous Word Blocked / Input] User: {author_name} | Text: {user_input}")
        await message.reply(WARNING_MESSAGE)
        return

    # 履歴リセットコマンドの処理（全員の共有履歴をクリア）
    if user_input.lower() in ["!reset", "/reset", "リセット", "忘れて"]:
        shared_history.clear()
        print(f"🧹 [Reset] 全員の共有会話履歴をクリアしました (実行者: {author_name})")
        await message.reply("うわあああん！！みんなとのこと全部忘れてやったわよぉぉぉッ！！誰よあんたらああああ！！")
        return

    if not user_input:
        user_input = "（無言で見つめている）"

    if not gemini_client:
        await message.reply("⚠️ GEMINI_API_KEY が設定されていません。.env を確認してください。")
        return

    # prompt.txt / systemPrompt.txt を毎回再読み込み
    system_prompt = load_system_prompt()
    # .env からモデルと温度を毎回再読み込み（再起動不要で反映）
    current_model, current_temp = get_runtime_config()

    # 送信用コンテキストの構築（全員の共有履歴 直近最大12ターン + 今回の入力）
    request_contents, stack_count = build_request_contents(author_name, user_input)

    async with message.channel.typing():
        try:
            # Gemini API 呼び出し
            response = gemini_client.models.generate_content(
                model=current_model,
                contents=request_contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=current_temp,
                ),
            )
            reply_text = response.text if response.text else "…………（泣き崩れて声が出ないようだ）"

            # 危険な単語の出力検知（LLMの生成テキストに混ざっていた場合）
            if contains_dangerous_word(reply_text):
                print(f"🚨 [Dangerous Word Blocked / Output] User: {author_name} | Blocked text detected.")
                reply_text = WARNING_MESSAGE

            # トークン数の標準出力（ターミナル表示）
            if hasattr(response, "usage_metadata") and response.usage_metadata:
                in_tok = getattr(response.usage_metadata, "prompt_token_count", 0)
                out_tok = getattr(response.usage_metadata, "candidates_token_count", 0)
                total_tok = getattr(response.usage_metadata, "total_token_count", in_tok + out_tok)
                print(f"📊 [Token Usage] User: {author_name} | In: {in_tok} | Out: {out_tok} | Total: {total_tok} | Stack: {stack_count}/{MAX_HISTORY_TURNS} | Temp: {current_temp}")

            # 危険単語がブロックされた場合は履歴を汚染させない
            if reply_text != WARNING_MESSAGE:
                save_shared_turn(author_name, user_input, reply_text)

            # 研究用ログファイル (log.txt) に記録（LLMには非公開）
            append_to_research_log(author_name, user_input, reply_text)

            await send_split_message(message.channel, reply_text, reply_to=message)

        except Exception as e:
            print(f"❌ エラー発生: {e}", file=sys.stderr)
            await message.reply(f"うわあああん！！エラーが出たわよぉぉぉッ！！\n```{e}```")

def main():
    if not DISCORD_BOT_TOKEN:
        print("❌ エラー: DISCORD_BOT_TOKEN が .env に設定されていません。")
        print("💡 .env ファイルを開いてトークンを貼り付けてください。")
        sys.exit(1)

    client.run(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    main()
