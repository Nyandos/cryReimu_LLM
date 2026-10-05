import os
import sys
import json
from pathlib import Path
import random
from datetime import datetime, time, timezone, timedelta
import asyncio
import re
import tempfile
import httpx
import discord
from discord import app_commands
from discord.ext import commands, tasks
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

def get_runtime_config() -> dict:
    """メッセージ受信や処理ごとに .env を再読込し、最新のコンフィグを返す（ホットスワップ対応）"""
    load_dotenv(dotenv_path=ENV_FILE, override=True)
    gemini_model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    try:
        gemini_temp = float(os.getenv("GEMINI_TEMPERATURE", "1.2"))
    except ValueError:
        gemini_temp = 1.2

    voicevox_host = os.getenv("VOICEVOX_HOST", "http://127.0.0.1:50021").rstrip("/")
    voicevox_fallback_host = os.getenv("VOICEVOX_FALLBACK_HOST", "http://127.0.0.1:50021").rstrip("/")
    try:
        voicevox_speaker = int(os.getenv("VOICEVOX_SPEAKER_ID", "76"))
    except ValueError:
        voicevox_speaker = 76

    try:
        voicevox_speed = float(os.getenv("VOICEVOX_SPEED", "1.15"))
    except ValueError:
        voicevox_speed = 1.15

    try:
        voicevox_pitch = float(os.getenv("VOICEVOX_PITCH", "0.0"))
    except ValueError:
        voicevox_pitch = 0.0

    try:
        voicevox_intonation = float(os.getenv("VOICEVOX_INTONATION", "1.2"))
    except ValueError:
        voicevox_intonation = 1.2

    try:
        voicevox_timeout = float(os.getenv("VOICEVOX_TIMEOUT", "120.0"))
    except ValueError:
        voicevox_timeout = 120.0

    try:
        voicevox_max_chars = int(os.getenv("VOICEVOX_MAX_CHARS", "60"))
    except ValueError:
        voicevox_max_chars = 60

    voicevox_overflow_suffix = os.getenv("VOICEVOX_OVERFLOW_SUFFIX", "……あああああああああ！！")

    try:
        vc_idle_timeout = int(os.getenv("VC_IDLE_TIMEOUT_SECONDS", "900"))
    except ValueError:
        vc_idle_timeout = 900

    return {
        "gemini_model": gemini_model,
        "gemini_temperature": gemini_temp,
        "voicevox_host": voicevox_host,
        "voicevox_fallback_host": voicevox_fallback_host,
        "voicevox_speaker_id": voicevox_speaker,
        "voicevox_speed": voicevox_speed,
        "voicevox_pitch": voicevox_pitch,
        "voicevox_intonation": voicevox_intonation,
        "voicevox_timeout": voicevox_timeout,
        "voicevox_max_chars": voicevox_max_chars,
        "voicevox_overflow_suffix": voicevox_overflow_suffix,
        "vc_idle_timeout_seconds": vc_idle_timeout,
    }

PROMPT_FILE = Path(__file__).parent / "systemPrompt.txt"
FALLBACK_PROMPT_FILE = Path(__file__).parent / "prompt.txt"

def load_system_prompt() -> str:
    """systemPrompt.txt (または prompt.txt) からシステムプロンプトを読み込む（ホットスワップ対応）。"""
    for file_path in [PROMPT_FILE, FALLBACK_PROMPT_FILE]:
        if file_path.exists():
            try:
                content = file_path.read_text(encoding="utf-8").strip()
                if content:
                    return content
            except Exception as e:
                print(f"⚠️ {file_path.name} の読み込みに失敗しました: {e}", file=sys.stderr)
    return "あなたはギャン泣きしている博麗霊夢です。"

EXIT_VOICE_FILE = Path(__file__).parent / "exitVoice.txt"

def load_exit_voice_text() -> str:
    """exitVoice.txt から退出時の発狂セリフを読み込む（ホットスワップ対応）。
    ファイルが存在しない場合は .env の EXIT_VOICE_TEXT またはデフォルトセリフを使用する。
    """
    if EXIT_VOICE_FILE.exists():
        try:
            content = EXIT_VOICE_FILE.read_text(encoding="utf-8").strip()
            if content:
                return content
        except Exception as e:
            print(f"⚠️ exitVoice.txt の読み込みに失敗しました: {e}", file=sys.stderr)
    return os.getenv("EXIT_VOICE_TEXT", "うわあああああん！！誰も構ってくれないいいいいいッ！！賽銭もくれないし放置するなんて酷すぎるわよおおおおおッ！！もう帰るわよおおおおおおッ！！！！")

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
client = commands.Bot(command_prefix="!", intents=intents)

# ==================== VOICEVOX 音声合成 & 再生キュー ====================

def clean_text_for_tts(text: str, max_chars: int = 60, overflow_suffix: str = "……あああああああああ！！") -> str:
    """TTS用にテキストからメンションや記号などをクリーンアップし、長すぎる場合は適切に切り詰めて叫び声を差し込む"""
    text = re.sub(r'<@!?[0-9]+>', '', text)
    text = re.sub(r'<#[0-9]+>', '', text)
    text = re.sub(r'<a?:[a-zA-Z0-9_]+:[0-9]+>', '', text)
    text = re.sub(r'https?://\S+', '', text)
    text = re.sub(r'```.*?```', '', text, flags=re.DOTALL)
    text = re.sub(r'[*_~`#]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()

    if max_chars > 0 and len(text) > max_chars:
        suffix_len = len(overflow_suffix)
        target_len = max(10, max_chars - suffix_len)
        cut_text = text[:target_len]

        last_punct = max(
            cut_text.rfind('！'), cut_text.rfind('!'),
            cut_text.rfind('。'), cut_text.rfind('？'),
            cut_text.rfind('?'), cut_text.rfind('、'),
            cut_text.rfind(',')
        )
        if last_punct > target_len // 2:
            cut_text = cut_text[:last_punct + 1]

        cut_text = cut_text.rstrip('！!。、,?？ ')
        text = f"{cut_text}{overflow_suffix}"
    return text

async def synthesize_voice(text: str, host: str, fallback_host: str | None, speaker_id: int, speed: float, pitch: float, intonation: float, timeout: float = 120.0, max_chars: int = 60, overflow_suffix: str = "……あああああああああ！！") -> bytes | None:
    """VOICEVOX APIを叩いてWAV音声バイナリを生成する（メインPC優先 + サブPC自動フォールバック）"""
    cleaned = clean_text_for_tts(text, max_chars=max_chars, overflow_suffix=overflow_suffix)
    if not cleaned:
        return None

    # 試行するホストリスト（メインPC GPU -> サブPC CPU）
    hosts_to_try = [host]
    if fallback_host and fallback_host != host:
        hosts_to_try.append(fallback_host)

    for target_host in hosts_to_try:
        is_primary = (target_host == host)
        host_label = "メインPC(GPU)" if is_primary else "サブPC(CPU)"
        # メインPCが落ちていても素早くフォールバックできるように接続タイムアウトを2秒に設定
        connect_timeout = 2.0 if is_primary and fallback_host else timeout
        client_timeout = httpx.Timeout(timeout, connect=connect_timeout)

        try:
            print(f"🎙️ [VOICEVOX: {host_label}] 音声合成開始 (文字数: {len(cleaned)}, Speaker: {speaker_id}): '{cleaned[:30]}...'")
            async with httpx.AsyncClient(timeout=client_timeout) as http_client:
                query_resp = await http_client.post(
                    f"{target_host}/audio_query",
                    params={"speaker": speaker_id, "text": cleaned}
                )
                if query_resp.status_code != 200:
                    print(f"⚠️ [VOICEVOX: {host_label}] audio_query 失敗 (ステータス: {query_resp.status_code})", file=sys.stderr)
                    continue

                query_data = query_resp.json()
                query_data["speedScale"] = speed
                query_data["pitchScale"] = pitch
                query_data["intonationScale"] = intonation

                synth_resp = await http_client.post(
                    f"{target_host}/synthesis",
                    params={"speaker": speaker_id},
                    json=query_data
                )
                if synth_resp.status_code != 200:
                    print(f"⚠️ [VOICEVOX: {host_label}] synthesis 失敗 (ステータス: {synth_resp.status_code})", file=sys.stderr)
                    continue

                print(f"✅ [VOICEVOX: {host_label}] 音声合成完了 (WAV: {len(synth_resp.content)} bytes)")
                return synth_resp.content

        except (httpx.ConnectError, httpx.ConnectTimeout):
            if is_primary and fallback_host:
                print("⚠️ [VOICEVOX] メインPC(GPU)未接続のため、サブPC(CPU)に自動フォールバックします...")
                continue
            else:
                print(f"⚠️ [VOICEVOX: {host_label}] 接続エラー", file=sys.stderr)
        except Exception as e:
            print(f"⚠️ [VOICEVOX: {host_label}] 音声合成エラー: ({type(e).__name__}) {e}", file=sys.stderr)
            if is_primary and fallback_host:
                print("⚠️ [VOICEVOX] サブPC(CPU)に自動フォールバックします...")
                continue

    return None

# サーバーごとのボイス再生キューとワーカータスク
voice_queues: dict[int, asyncio.Queue] = {}
voice_worker_tasks: dict[int, asyncio.Task] = {}

async def voice_worker(guild: discord.Guild):
    """ギルドごとのボイス再生キューを順次処理するワーカー"""
    queue = voice_queues[guild.id]
    while True:
        try:
            wav_bytes = await queue.get()
            voice_client: discord.VoiceClient = guild.voice_client
            if not voice_client or not voice_client.is_connected():
                queue.task_done()
                continue

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                temp_filename = tf.name
                tf.write(wav_bytes)

            try:
                done_event = asyncio.Event()

                def after_playing(err):
                    if err:
                        print(f"⚠️ [Voice Playback Error]: {err}", file=sys.stderr)
                    try:
                        os.unlink(temp_filename)
                    except OSError:
                        pass
                    client.loop.call_soon_threadsafe(done_event.set)

                source = discord.FFmpegPCMAudio(temp_filename)
                voice_client.play(source, after=after_playing)
                await done_event.wait()
            except Exception as e:
                print(f"⚠️ [Voice Play Error]: {e}", file=sys.stderr)
                try:
                    os.unlink(temp_filename)
                except OSError:
                    pass

            queue.task_done()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"⚠️ [Voice Worker Exception]: {e}", file=sys.stderr)
            await asyncio.sleep(1)

async def queue_speech(guild: discord.Guild, text: str):
    """テキストをVOICEVOXで合成し、VCの再生キューに追加する"""
    voice_client: discord.VoiceClient = guild.voice_client
    if not voice_client or not voice_client.is_connected():
        return

    cfg = get_runtime_config()
    wav = await synthesize_voice(
        text=text,
        host=cfg["voicevox_host"],
        fallback_host=cfg.get("voicevox_fallback_host"),
        speaker_id=cfg["voicevox_speaker_id"],
        speed=cfg["voicevox_speed"],
        pitch=cfg["voicevox_pitch"],
        intonation=cfg["voicevox_intonation"],
        timeout=cfg["voicevox_timeout"],
        max_chars=cfg["voicevox_max_chars"],
        overflow_suffix=cfg.get("voicevox_overflow_suffix", "……あああああああああ！！"),
    )
    if not wav:
        return

    if guild.id not in voice_queues:
        voice_queues[guild.id] = asyncio.Queue()
    if guild.id not in voice_worker_tasks or voice_worker_tasks[guild.id].done():
        voice_worker_tasks[guild.id] = client.loop.create_task(voice_worker(guild))

    await voice_queues[guild.id].put(wav)

async def play_exit_and_disconnect(voice_client: discord.VoiceClient, exit_text: str):
    """退出音声を再生してからVCを切断する"""
    guild = voice_client.guild
    cfg = get_runtime_config()

    try:
        wav = await synthesize_voice(
            text=exit_text,
            host=cfg["voicevox_host"],
            fallback_host=cfg.get("voicevox_fallback_host"),
            speaker_id=cfg["voicevox_speaker_id"],
            speed=cfg["voicevox_speed"],
            pitch=cfg["voicevox_pitch"],
            intonation=cfg["voicevox_intonation"],
            timeout=cfg["voicevox_timeout"],
            max_chars=cfg["voicevox_max_chars"],
            overflow_suffix=cfg.get("voicevox_overflow_suffix", "……あああああああああ！！"),
        )
        if wav:
            # 再生待ちのキューをクリア
            if guild.id in voice_queues:
                while not voice_queues[guild.id].empty():
                    voice_queues[guild.id].get_nowait()

            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                temp_filename = tf.name
                tf.write(wav)

            done_event = asyncio.Event()

            def after_exit(err):
                if err:
                    print(f"⚠️ [Exit Audio Error]: {err}", file=sys.stderr)
                try:
                    os.unlink(temp_filename)
                except OSError:
                    pass
                client.loop.call_soon_threadsafe(done_event.set)

            if voice_client.is_playing():
                voice_client.stop()
            voice_client.play(discord.FFmpegPCMAudio(temp_filename), after=after_exit)
            try:
                await asyncio.wait_for(done_event.wait(), timeout=15.0)
            except asyncio.TimeoutError:
                pass
    except Exception as e:
        print(f"⚠️ [Exit Playback Exception]: {e}", file=sys.stderr)
    finally:
        try:
            await voice_client.disconnect(force=True)
            print(f"👋 [VC Disconnected] サーバー '{guild.name}' のVCから切断しました。")
        except Exception as e:
            print(f"⚠️ [VC Disconnect Error]: {e}", file=sys.stderr)

        last_action_times.pop(guild.id, None)
        if guild.id in voice_worker_tasks:
            voice_worker_tasks[guild.id].cancel()
            del voice_worker_tasks[guild.id]
        if guild.id in voice_queues:
            del voice_queues[guild.id]

# ==================== VC無操作タイムアウト監視 ====================

# サーバーごとの最終アクション時刻とテキストチャンネル記録
last_action_times: dict[int, datetime] = {}
last_text_channels: dict[int, discord.TextChannel] = {}

def update_action(guild_id: int, channel: discord.TextChannel | None = None):
    """VC無操作タイマーをリセットする"""
    last_action_times[guild_id] = datetime.now(timezone.utc)
    if channel:
        last_text_channels[guild_id] = channel

@tasks.loop(seconds=15)
async def vc_idle_check_task():
    """設定された無操作時間（デフォルト15分）が経過したら発狂してVCから退出する"""
    now = datetime.now(timezone.utc)
    cfg = get_runtime_config()
    timeout_sec = cfg["vc_idle_timeout_seconds"]

    for voice_client in list(client.voice_clients):
        if not voice_client.is_connected():
            continue
        guild = voice_client.guild
        last_time = last_action_times.get(guild.id)
        if not last_time:
            last_action_times[guild.id] = now
            continue

        elapsed = (now - last_time).total_seconds()
        if elapsed >= timeout_sec:
            print(f"⏰ [VC Timeout] サーバー '{guild.name}' で {int(elapsed)} 秒間無操作のため退出します。")
            exit_text = load_exit_voice_text()
            last_chan = last_text_channels.get(guild.id)
            if last_chan:
                try:
                    await last_chan.send(f"😭 **【通話退出】**\n{exit_text}")
                except Exception:
                    pass
            await play_exit_and_disconnect(voice_client, exit_text)

@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """BotがVCから切断された際の後処理"""
    if member.id == client.user.id and before.channel and not after.channel:
        guild_id = member.guild.id
        last_action_times.pop(guild_id, None)
        if guild_id in voice_worker_tasks:
            voice_worker_tasks[guild_id].cancel()
            del voice_worker_tasks[guild_id]
        if guild_id in voice_queues:
            del voice_queues[guild_id]

# ==================== 日直機能 ====================

async def pick_and_announce_duty(channel: discord.TextChannel) -> bool:
    """日直ロールのユーザーから2名を抽選して指定チャンネルでメンションする"""
    guild = channel.guild
    role = discord.utils.get(guild.roles, name=DUTY_ROLE_NAME)
    if not role:
        print(f"⚠️ [日直機能] サーバー '{guild.name}' にロール '{DUTY_ROLE_NAME}' が見つかりませんでした。")
        return False

    members = [m for m in role.members if not m.bot]

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

    count = min(2, len(members))
    selected = random.sample(members, count)
    mentions = " ".join(m.mention for m in selected)

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

@tasks.loop(time=time(hour=0, minute=0, tzinfo=JST))
async def daily_duty_task():
    """毎日 日本時間 0:00 (JST / UTC+9) に実行する定期タスク"""
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

# ==================== スラッシュコマンド ====================

@client.tree.command(name="spawnreimu", description="ギャン泣き霊夢をあなたが現在いるボイスチャンネルに呼び出します")
async def spawnreimu(interaction: discord.Interaction):
    """ユーザーがいるボイスチャンネルに参加する"""
    if not interaction.guild:
        await interaction.response.send_message("DMではボイスチャンネルに参加できないわよぉぉぉッ！！", ephemeral=True)
        return

    # ユーザーがVCにいるか確認
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            "うわああああん！！あんたボイスチャンネルに入ってないじゃないのよぉぉぉッ！！\nどこに行けばいいかわからないわよおおおッ！！",
            ephemeral=True
        )
        return

    user_channel = interaction.user.voice.channel
    voice_client = interaction.guild.voice_client

    # 既にVCに参加している場合のチェック
    if voice_client and voice_client.is_connected():
        if voice_client.channel.id != user_channel.id:
            await interaction.response.send_message(
                f"今別のチャンネル（`{voice_client.channel.name}`）でギャン泣きしてる最中なのよおおおッ！！\nあっちこっち同時に呼ばないでええええッ！！"
            )
            return
        else:
            await interaction.response.send_message(
                f"もう `{user_channel.name}` にいるでしょおおおおおッ！！これ以上どこに行けっていうのよおおおッ！！"
            )
            return

    # VCに接続
    try:
        await user_channel.connect()
        text_channel = interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None
        update_action(interaction.guild_id, text_channel)
        greet_text = f"ひぐっ……ううぅ……{user_channel.name} に来たわよ……。賽銭もくれないのに呼び出さないでよおおおおッ！！"
        await interaction.response.send_message(f"😭 **【VC参加】** `{user_channel.name}` に接続したわよ！\n{greet_text}")
        asyncio.create_task(queue_speech(interaction.guild, greet_text))
    except Exception as e:
        print(f"❌ [VC Connect Error]: {e}", file=sys.stderr)
        await interaction.response.send_message(f"うわあああん！！VCに接続できなかったわよぉぉぉッ！！\n```{e}```")

@client.tree.command(name="despawnreimu", description="ギャン泣き霊夢をボイスチャンネルから退出させます")
async def despawnreimu(interaction: discord.Interaction):
    """ボイスチャンネルから手動で退出させる"""
    if not interaction.guild:
        return
    voice_client = interaction.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        await interaction.response.send_message("そもそもボイスチャンネルにいないわよおおおッ！！", ephemeral=True)
        return

    exit_text = load_exit_voice_text()
    await interaction.response.send_message(f"😭 **【通話退出】**\n{exit_text}")
    await play_exit_and_disconnect(voice_client, exit_text)

# ==================== イベントハンドラ ====================

@client.event
async def on_ready():
    initial_cfg = get_runtime_config()
    print(f"✅ ログイン完了: {client.user.name} ({client.user.id})")
    print(f"📡 使用モデル: {initial_cfg['gemini_model']}")
    print(f"🌡️ 生成温度 (temperature): {initial_cfg['gemini_temperature']}")
    print(f"🔊 VOICEVOX: {initial_cfg['voicevox_host']} (Speaker: {initial_cfg['voicevox_speaker_id']}, Speed: {initial_cfg['voicevox_speed']})")
    print(f"⏱️ VC放置タイムアウト: {initial_cfg['vc_idle_timeout_seconds']}秒 ({initial_cfg['vc_idle_timeout_seconds'] // 60}分)")
    print(f"📝 プロンプトファイル: {PROMPT_FILE.resolve()}")
    print(f"⏰ 日直通知: 毎日 00:00 JST / チャンネル ID: {DUTY_CHANNEL_ID}")
    print("--------------------------------------------------")

    # スラッシュコマンド同期
    try:
        synced = await client.tree.sync()
        print(f"🔄 スラッシュコマンド同期完了: {len(synced)} 個のコマンド登録済み")
    except Exception as e:
        print(f"⚠️ スラッシュコマンド同期エラー: {e}")

    # 定期タスクの開始
    if not daily_duty_task.is_running():
        daily_duty_task.start()
    if not vc_idle_check_task.is_running():
        vc_idle_check_task.start()

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
MAX_HISTORY_TURNS = 12
shared_history: list[dict[str, str]] = []

def build_request_contents(author_name: str, current_user_input: str) -> tuple[list[types.Content], int]:
    """共有履歴から Gemini 送信用 contents を構築する"""
    recent_turns = shared_history[-(MAX_HISTORY_TURNS - 1):] if MAX_HISTORY_TURNS > 1 else []

    contents = []
    for turn in recent_turns:
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=turn["user"])]))
        contents.append(types.Content(role="model", parts=[types.Part.from_text(text=turn["model"])]))

    formatted_user_input = f"{author_name}: {current_user_input}"
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=formatted_user_input)]))
    stack_count = len(recent_turns) + 1
    return contents, stack_count

def save_shared_turn(author_name: str, user_text: str, model_text: str):
    """1ターン分の会話を共有スタックに保存し、最大12ターンを超えた古い履歴を削除する"""
    formatted_user_input = f"{author_name}: {user_text}"
    shared_history.append({"author": author_name, "user": formatted_user_input, "model": model_text})
    if len(shared_history) > MAX_HISTORY_TURNS:
        del shared_history[:len(shared_history) - MAX_HISTORY_TURNS]

@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    # 日直の手動抽選コマンド (!日直)
    if message.content.strip() in ["!日直", "!nichoku"]:
        if not await is_duty_server(message.guild):
            return

        if message.guild:
            update_action(message.guild.id, message.channel)

        if is_duty_done_today():
            state = load_duty_state()
            m_names = state.get("member_names", [])
            if not m_names:
                m_ids = state.get("member_ids", [])
                for mid in m_ids:
                    user = client.get_user(mid)
                    m_names.append(user.display_name if user else f"ID:{mid}")

            names_str = "、".join(f"`{name}`" for name in m_names) if m_names else "（記録なし）"
            await message.reply(
                f"今日の日直はもう決まってるわよぉぉぉッ！！再抽選なんてさせないからね！！\n【今日の日直】 {names_str}",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none()
            )
            return

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

    user_input = message.content
    if client.user:
        user_input = user_input.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()

    author_name = message.author.display_name

    # 危険な単語の入力検知
    if contains_dangerous_word(user_input):
        print(f"🚨 [Dangerous Word Blocked / Input] User: {author_name} | Text: {user_input}")
        await message.reply(WARNING_MESSAGE)
        return

    # 履歴リセットコマンドの処理
    if user_input.lower() in ["!reset", "/reset", "リセット", "忘れて"]:
        shared_history.clear()
        print(f"🧹 [Reset] 全員の共有会話履歴をクリアしました (実行者: {author_name})")
        reply_reset = "うわあああん！！みんなとのこと全部忘れてやったわよぉぉぉッ！！誰よあんたらああああ！！"
        await message.reply(reply_reset)
        if message.guild:
            update_action(message.guild.id, message.channel)
            if message.guild.voice_client and message.guild.voice_client.is_connected():
                await queue_speech(message.guild, reply_reset)
        return

    if not user_input:
        user_input = "（無言で見つめている）"

    if not gemini_client:
        await message.reply("⚠️ GEMINI_API_KEY が設定されていません。.env を確認してください。")
        return

    # プロンプトとコンフィグのホットスワップ再読み込み
    system_prompt = load_system_prompt()
    current_cfg = get_runtime_config()
    current_model = current_cfg["gemini_model"]
    current_temp = current_cfg["gemini_temperature"]

    request_contents, stack_count = build_request_contents(author_name, user_input)

    async with message.channel.typing():
        try:
            response = gemini_client.models.generate_content(
                model=current_model,
                contents=request_contents,
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=current_temp,
                ),
            )
            reply_text = response.text if response.text else "…………（泣き崩れて声が出ないようだ）"

            if contains_dangerous_word(reply_text):
                print(f"🚨 [Dangerous Word Blocked / Output] User: {author_name} | Blocked text detected.")
                reply_text = WARNING_MESSAGE

            if hasattr(response, "usage_metadata") and response.usage_metadata:
                in_tok = getattr(response.usage_metadata, "prompt_token_count", 0)
                out_tok = getattr(response.usage_metadata, "candidates_token_count", 0)
                total_tok = getattr(response.usage_metadata, "total_token_count", in_tok + out_tok)
                print(f"📊 [Token Usage] User: {author_name} | In: {in_tok} | Out: {out_tok} | Total: {total_tok} | Stack: {stack_count}/{MAX_HISTORY_TURNS} | Temp: {current_temp}")

            if reply_text != WARNING_MESSAGE:
                save_shared_turn(author_name, user_input, reply_text)

            append_to_research_log(author_name, user_input, reply_text)

        except Exception as e:
            print(f"❌ エラー発生: {e}", file=sys.stderr)
            await message.reply(f"うわあああん！！エラーが出たわよぉぉぉッ！！\n```{e}```")
            return

    # テキスト送信 (typing表示終了後に送信)
    await send_split_message(message.channel, reply_text, reply_to=message)

    # VC無操作タイマー更新 & VC接続中ならバックグラウンドで音声合成して再生
    if message.guild:
        update_action(message.guild.id, message.channel)
        if message.guild.voice_client and message.guild.voice_client.is_connected():
            asyncio.create_task(queue_speech(message.guild, reply_text))

def main():
    if not DISCORD_BOT_TOKEN:
        print("❌ エラー: DISCORD_BOT_TOKEN が .env に設定されていません。")
        print("💡 .env ファイルを開いてトークンを貼り付けてください。")
        sys.exit(1)

    client.run(DISCORD_BOT_TOKEN)

if __name__ == "__main__":
    main()
