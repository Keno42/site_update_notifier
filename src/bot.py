from typing import Any
import discord
import asyncio
import aiohttp
import re
import os
import logging
import random
from datetime import datetime
from config.config import CACHE_FILE
from config import config
from .dev import handle_dev_message_sync
from .dev import transcribe_audio
from github import Github
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
import requests
import tempfile
from .audio_utils import split_audio_with_overlap

TOKEN = config.TOKEN
CHANNEL_ID = getattr(config, "CHANNEL_ID", 0)
CHECK_URL = getattr(config, "CHECK_URL", "")
CHECK_INTERVAL = getattr(config, "CHECK_INTERVAL", 86400)
ERROR_INTERVAL = getattr(config, "ERROR_INTERVAL", 86400)
HEALTH_CHECK_GREETING = getattr(config, "HEALTH_CHECK_GREETING", "")
GREETINGS = getattr(config, "GREETINGS", [])
CHATGPT_TOKEN = config.CHATGPT_TOKEN
SYSTEM_PROMPT = config.SYSTEM_PROMPT
GPT_MODEL = config.GPT_MODEL
ERROR_MESSAGE = getattr(config, "ERROR_MESSAGE", "")
SITE_UPDATE_MESSAGE = getattr(config, "SITE_UPDATE_MESSAGE", "{titles_text}")
PAT = getattr(config, "PAT", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)

# Discord setup
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Slack setup
bot_token = getattr(config, "XOXB_TOKEN", "")
app_token = getattr(config, "XAPP_TOKEN", "")

previous_content = None
if CACHE_FILE:
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                previous_content = f.read()
            logging.info("キャッシュファイルから前回の内容を読み込みました。")
        except Exception as e:
            logging.error(f"キャッシュファイルの読み込みに失敗しました: {e}")


def extract_titles(html: str):
    pattern = r'<h3 class="title01">\s*<a href="([^"]+)">([^<]+)</a>\s*</h3>'
    return re.findall(pattern, html)


def update_cache(new_content: str):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)
        logging.info("キャッシュファイルを更新しました。")
    except Exception as e:
        logging.error(f"キャッシュファイルの更新に失敗しました: {e}")


async def fetch_site_content(session, url: str):
    try:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.text()
    except aiohttp.ClientError as e:
        logging.error(f"サイト取得エラー: {e}")
        raise


async def call_chatgpt_with_history(messages):
    url = "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {CHATGPT_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"model": GPT_MODEL, "messages": messages}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as response:
            if response.status == 200:
                result = await response.json()
                answer = result["choices"][0]["message"]["content"].strip()
                return answer
            else:
                error = await response.text()
                logging.error(
                    f"ChatGPT API request failed: {response.status} - {error}"
                )
                return ERROR_MESSAGE


async def typing_loop(channel):
    while True:
        await channel.typing()
        await asyncio.sleep(8)


@client.event
async def on_ready():
    logging.info(f"Logged in as {client.user}")
    if CHECK_URL and CACHE_FILE and CHANNEL_ID:
        client.loop.create_task(check_website())
    else:
        logging.info(
            "CHECK_URLまたはCACHE_FILEまたはCHANNEL_IDが設定されていないため、サイトチェックをスキップします。"
        )


conversation_history = [{"role": "system", "content": SYSTEM_PROMPT}]


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    # Dev mode用のチェック
    if PAT and "Dev mode" in message.content and client.user in message.mentions:
        dev_command = message.content.replace("Dev mode", "").strip()
        typing_task = asyncio.create_task(typing_loop(message.channel))
        reply_text = await asyncio.to_thread(handle_dev_message_sync, dev_command)
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        await message.reply(reply_text)
        return

    # Issue mode用のチェック
    if "Issue mode" in message.content:
        issue_content = message.content.replace("Issue mode", "").strip()
        if not PAT:
            await message.reply("PATが設定されていません。Issueを作成できません。")
            return
        try:
            from .issue_handler import create_issue

            issue_result = await asyncio.to_thread(create_issue, issue_content)
            await message.reply(issue_result)
        except Exception as e:
            logging.error(f"Issue作成中にエラーが発生しました: {e}")
            await message.reply("Issueの作成に失敗しました。")
        return

    # BotへのメンションまたはBotのロールが呼ばれた場合に反応
    bot_mentioned = client.user in message.mentions
    role_mentioned = False
    if message.guild:
        bot_member = message.guild.get_member(client.user.id)
        if bot_member:
            bot_roles = {role.id for role in bot_member.roles}
            role_mentions = {role.id for role in message.role_mentions}
            role_mentioned = bool(bot_roles & role_mentions)
    if bot_mentioned or role_mentioned:
        prompt = (
            message.content.replace(f"<@{client.user.id}>", "")
            .replace(f"<@!{client.user.id}>", "")
            .strip()
        )

        # Check for attached audio in the Discord message (similar to Slack logic)
        audio_files = []
        for attachment in message.attachments:
            if attachment.content_type and attachment.content_type.startswith("audio/"):
                audio_files.append(attachment)

        if not prompt and not audio_files:
            await message.reply("何か質問してにゃ。")
            return
        if prompt.lower() == "check issue":
            try:
                g = Github(PAT)
                repo = g.get_repo(config.REPO_NAME)
                issues = repo.get_issues(state="open")
                issues_list = []
                for issue in issues:
                    issues_list.append(
                        f"Issue#{issue.number}: {issue.title} - URL: {issue.html_url}"
                    )
                reply_text = (
                    "\n".join(issues_list)
                    if issues_list
                    else "現在オープンなIssueはありません。"
                )
                await message.reply(reply_text)
            except Exception as e:
                logging.error(f"Issue取得中にエラー発生: {e}")
                await message.reply("Issueの取得に失敗しました。")
            return
        if message.reference:
            if message.author.bot:
                rounds = (len(conversation_history) - 1) // 2
                if rounds >= 3:
                    return
            conversation_history.append({"role": "user", "content": prompt})
        else:
            conversation_history.clear()
            conversation_history.append({"role": "system", "content": SYSTEM_PROMPT})
            conversation_history.append({"role": "user", "content": prompt})
        typing_task = asyncio.create_task(typing_loop(message.channel))

        if audio_files:
            transcriptions = []
            for audio_file in audio_files:
                try:
                    with tempfile.NamedTemporaryFile(
                        suffix=".m4a", delete=False
                    ) as tmp_file:
                        await attachment.save(tmp_file)
                        tmp_file_path = tmp_file.name

                    def chunk_callback(chunk_path):
                        text = asyncio.run(transcribe_audio(chunk_path, context=prompt))
                        transcriptions.append(text)

                    split_audio_with_overlap(
                        tmp_file_path,
                        output_dir="audio_chunks",
                        chunk_callback=chunk_callback,
                    )

                except Exception as e:
                    logging.error(f"Failed to process audio file: {e}")
                    await message.reply(f"音声ファイルの処理に失敗しました: {str(e)}")

            # If any audio files were found, reply with the final transcription
            if transcriptions:
                final_result = "\n".join(transcriptions)
                reply_text = f"書き起こしが完了しました:\n{final_result}"
        else:
            reply_text = await call_chatgpt_with_history(conversation_history)
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        conversation_history.append({"role": "assistant", "content": reply_text})
        await message.reply(reply_text)
        return
    if GREETINGS and HEALTH_CHECK_GREETING in message.content.lower():
        await message.channel.send(random.choice(GREETINGS))


async def check_website():
    global previous_content
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                content = await fetch_site_content(session, CHECK_URL)
                if previous_content is None:
                    previous_content = content
                    update_cache(content)
                    logging.info("初回チェック完了。キャッシュファイルに保存しました。")
                else:
                    old_list = extract_titles(previous_content)
                    new_list = extract_titles(content)
                    added_entries = [item for item in new_list if item not in old_list]
                    if added_entries:
                        channel = client.get_channel(CHANNEL_ID)
                        if channel:
                            formatted_list = []
                            for url, title in added_entries:
                                formatted_list.append(f"タイトル: {title}\nURL: {url}")
                            titles_text = "\n\n".join(formatted_list)
                            message_to_send = SITE_UPDATE_MESSAGE.format(
                                titles_text=titles_text
                            )
                            await channel.send(message_to_send)
                            logging.info(
                                "更新を検知し、以下の内容で通知を送信しました:"
                            )
                            logging.info(titles_text)
                        else:
                            logging.error("指定したチャンネルが見つかりません。")
                        previous_content = content
                        update_cache(content)
                    else:
                        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        logging.info(
                            f"更新は検知されませんでした。 現在の時刻: {current_time}"
                        )
                await asyncio.sleep(CHECK_INTERVAL)
            except Exception as e:
                logging.error(f"エラーが発生しました: {e}")
                await asyncio.sleep(ERROR_INTERVAL)


if bot_token:
    slack_app = App(token=bot_token)
    logging.info("Slack 初期化")

    @slack_app.event("message")
    def handle_message_events(body: dict[str, Any], logger: logging.Logger) -> None:
        logging.info("メッセージ受信")
        event = body.get("event", {})
        message_text = event.get("text", "")
        channel_id = event.get("channel", "")
        ts = event.get("ts", "")
        files = event.get("files", [])
        for file_info in files:
            if file_info.get("mimetype", "").startswith("audio/"):
                audio_url = file_info.get("url_private_download")
                headers = {"Authorization": f"Bearer {bot_token}"}
                try:
                    response = requests.get(audio_url, headers=headers)
                    response.raise_for_status()
                    with tempfile.NamedTemporaryFile(
                        suffix=".m4a", delete=False
                    ) as tmp_file:
                        tmp_file.write(response.content)
                        tmp_file_path = tmp_file.name

                        transcriptions = []

                        def chunk_callback(chunk_path):
                            # Here we do a synchronous call or wrap the async call
                            # with asyncio.run
                            # so that we can gather each chunk's text
                            # Provide a context if desired
                            text = asyncio.run(
                                transcribe_audio(
                                    chunk_path,
                                    context=message_text,
                                )
                            )
                            transcriptions.append(text)

                        split_audio_with_overlap(
                            tmp_file_path,
                            output_dir="audio_chunks",
                            chunk_callback=chunk_callback,
                        )

                        final_result = "\n".join(transcriptions)
                        logger.info(f"Audio file processed and split: {tmp_file_path}")
                        logger.info(f"Final transcription:\n{final_result}")

                        # Post a Slack reply in the thread where the audio was posted
                        slack_app.client.chat_postMessage(
                            channel=channel_id,
                            text=f"書き起こしが完了しました:\n{final_result}",
                            thread_ts=ts,
                        )

                except Exception as e:
                    logger.error(f"Failed to process audio file: {e}")
        if not files:
            logger.info("No files attached in the Slack message.")


async def start_slack():
    handler = SocketModeHandler(slack_app, app_token)
    logging.info("Slack ログイン")
    await asyncio.to_thread(handler.start)


async def main():
    discord_task = asyncio.create_task(client.start(config.TOKEN))
    if bot_token:
        slack_task = asyncio.create_task(start_slack())
        await asyncio.gather(discord_task, slack_task)
    else:
        await discord_task


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
