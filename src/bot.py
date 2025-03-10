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
from .dev import handle_dev_message
from github import Github

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

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

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


async def fetch_site_content(session, url: str):
    try:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.text()
    except aiohttp.ClientError as e:
        logging.error(f"サイト取得エラー: {e}")
        raise


def update_cache(new_content: str):
    try:
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(new_content)
        logging.info("キャッシュファイルを更新しました。")
    except Exception as e:
        logging.error(f"キャッシュファイルの更新に失敗しました: {e}")


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

# システムプロンプトにリファクタリング制限の注意事項を追加
conversation_history = [{
    "role": "system",
    "content": SYSTEM_PROMPT + "\n指示がない限り、関連しない箇所のリファクタリングはしないこと、極力コメントをつけないこと、lintingルールに抵触してmergeできなくなることを避けること"
}]


@client.event
async def on_message(message):
    if message.author == client.user:
        return

    # ブランチ削除コマンドの検出
    branch_deletion_pattern = r"ブランチ削除して:\s*(\S+)"
    if client.user in message.mentions and re.search(branch_deletion_pattern, message.content):
        match = re.search(branch_deletion_pattern, message.content)
        branch_name = match.group(1)
        if not (PAT and getattr(config, "FORKED_REPO_NAME", "")):
            await message.reply("環境変数が正しく設定されていません。")
            return
        try:
            g = Github(PAT)
            forked_repo = g.get_repo(getattr(config, "FORKED_REPO_NAME", ""))
            ref = forked_repo.get_git_ref(f"heads/{branch_name}")
            ref.delete()
            await message.reply(f"ブランチ『{branch_name}』を削除しました。")
        except Exception as e:
            await message.reply(f"ブランチ削除に失敗しました: {e}")
        return

    if PAT and "Dev mode" in message.content and client.user in message.mentions:
        dev_command = message.content.replace("Dev mode", "").strip()
        typing_task = asyncio.create_task(typing_loop(message.channel))
        reply_text = await handle_dev_message(dev_command)
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass
        await message.reply(reply_text)
        return

    if client.user in message.mentions:
        prompt = (
            message.content.replace(f"<@{client.user.id}>", "")
            .replace(f"<@!{client.user.id}>", "")
            .strip()
        )
        if not prompt:
            await message.reply("何か質問してにゃ。")
            return
        if message.reference:
            if message.author.bot:
                rounds = (len(conversation_history) - 1) // 2
                if rounds >= 3:
                    return
            conversation_history.append({"role": "user", "content": prompt})
        else:
            conversation_history.clear()
            conversation_history.append({"role": "system", "content": SYSTEM_PROMPT + "\n指示がない限り、関連しない箇所のリファクタリングはしないこと、極力コメントをつけないこと、lintingルールに抵触してmergeできなくなることを避けること"})
            conversation_history.append({"role": "user", "content": prompt})
        typing_task = asyncio.create_task(typing_loop(message.channel))
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


client.run(TOKEN)
