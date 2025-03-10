import json
import uuid
from github import Github
from openai import OpenAI
from config import config
from .github_utils import get_file_from_repo, get_all_file_paths,
    create_pull_request
from github.GithubException import GithubException

PAT = getattr(config, "PAT", "")
CHATGPT_TOKEN = config.CHATGPT_TOKEN
REPO_NAME = getattr(config, "REPO_NAME", "")
FORKED_REPO_NAME = getattr(config, "FORKED_REPO_NAME", "")
GPT_MODEL = config.GPT_MODEL


def generate_branch_name(prefix="auto-fix-"):
    unique_id = uuid.uuid4().hex[:8]  # UUIDから8文字取得
    return f"{prefix}{unique_id}"


client = OpenAI(api_key=CHATGPT_TOKEN)


async def handle_dev_message(message: str) -> str:
    if not (PAT and CHATGPT_TOKEN and REPO_NAME and FORKED_REPO_NAME):
        return "環境変数が設定されていません。"

    g = Github(PAT)
    branch_name = generate_branch_name()

    try:
        # REPO_NAMEのmainブランチの最新コミットSHAを利用して、
        # FORKED_REPO_NAMEにブランチ作成
        base_repo = g.get_repo(REPO_NAME)
        base_main = base_repo.get_branch("main")
        commit_sha = base_main.commit.sha

        forked_repo = g.get_repo(FORKED_REPO_NAME)
        forked_repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=commit_sha)
    except Exception as e:
        return f"ブランチの作成に失敗しました: {str(e)}"

    files_content = {}
    file_paths = get_all_file_paths("src", branch=branch_name)
    for file_path in file_paths:
        file = get_file_from_repo(file_path, branch=branch_name)
        if file is None:
            continue
        files_content[file_path] = file.decoded_content.decode("utf-8")

    file_descriptions = "\n".join([
        f"### {path}\n```python\n{content}\n```"
        for path, content in files_content.items()
    ])

    system_message = (
        "あなたは優秀なソフトウェア開発者です。与えられたファイル群を指示に従って"
        "修正してください。\n\n"
        "以下のルールを守って、JSONで結果を構造的に返してください：\n"
        "- lintを考慮して１行88文字を超えないように適宜改行してください。\n"
        "- JSON以外のテキストは含まないでください。\n"
        "- 変更または追加が必要なファイルのみを `changes` に含めてください。\n"
        "- 変更不要なファイルは含めないでください。\n"
        "- 新規作成が必要なファイルがあれば、それも`changes`に追加してください。\n\n"
        "```json\n"
        "{\n"
        "    \"pr_title\": \"プルリクエストの明確で簡潔な日本語タイトル\",\n"
        "    \"pr_body\": \"プルリクエストの変更点や意図を簡潔に日本語で説明\",\n"
        "    \"changes\": {\n"
        "        \"ファイル名1\": {\n"
        "            \"commit_message\": \"1行のコミットメッセージ\",\n"
        "            \"updated_code\": \"修正後または追加するコード全体\"\n"
        "        },\n"
        "        \"ファイル名2\": {\n"
        "            \"commit_message\": \"1行のコミットメッセージ\",\n"
        "            \"updated_code\": \"修正後または追加するコード全体\"\n"
        "        }\n"
        "    }\n"
        "}\n"
        "```\n"
    )
    user_message = (
        "## ファイル群：\n" f"{file_descriptions}\n\n"
        "## 指示：\n" f"{message}\n"
    )

    try:
        response = client.chat.completions.create(
            model=GPT_MODEL,
            messages=[
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_message},
            ],
            response_format={"type": "json_object"},
        )
        if response.choices[0].message.content is None:
            return "構造解析に失敗しました。"

        result = json.loads(response.choices[0].message.content)
    except Exception as e:
        return f"GPTによる修正案の取得に失敗しました: {str(e)}"

    # PRの情報取得
    pr_title = result.get("pr_title", "自動生成PR")
    pr_body = result.get("pr_body", "")
    changes = result.get("changes", {})

    if not isinstance(changes, dict):
        return "GPTが提示した修正の形式が正しくありません（changesの型異常）。"

    if not changes:
        return "GPTが提示した修正はありません。"

    for file_name, change in changes.items():
        if not isinstance(change, dict):
            return (
                f"ファイル『{file_name}』の変更内容が正しくありません（型が異常です）。"
            )
        commit_message = change.get("commit_message")
        new_code = change.get("updated_code")
        if not commit_message:
            return f"ファイル『{file_name}』の変更に必須フィールドが不足しています。"

        existing_file = get_file_from_repo(file_name, branch=branch_name)

        try:
            if new_code == "":
                if existing_file:
                    forked_repo.delete_file(
                        existing_file.path,
                        commit_message,
                        existing_file.sha,
                        branch=branch_name,
                    )
                else:
                    pass
            else:
                if existing_file:
                    forked_repo.update_file(
                        existing_file.path,
                        commit_message,
                        new_code,
                        existing_file.sha,
                        branch=branch_name,
                    )
                else:
                    forked_repo.create_file(
                        file_name, commit_message, new_code, branch=branch_name
                    )
        except GithubException as e:
            return (
                f"ファイル『{file_name}』のGitHub操作に失敗しました: "
                f"{e.data.get('message', str(e))}"
            )
        except Exception as e:
            return f"ファイル『{file_name}』の予期せぬエラー: {str(e)}"

    pr_creation_result = create_pull_request(
        branch_name=branch_name, pr_title=pr_title, pr_body=pr_body
    )

    return (
        f"ブランチ『{branch_name}』に変更をpushしました。\n"
        f"プルリクエスト: {pr_creation_result}"
    )


def handle_dev_message_sync(message: str) -> str:
    import asyncio
    return asyncio.run(handle_dev_message(message))
