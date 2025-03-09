import json
import uuid
from github import Github
from openai import OpenAI
from config import config
from .github_utils import get_file_from_repo, get_all_file_paths, create_pull_request

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
    file_paths = get_all_file_paths("src")
    files_content = {}
    for file_path in file_paths:
        file = get_file_from_repo(file_path)
        if file is None:
            return f"GitHubからファイル「{file_path}」の取得に失敗しました。"
        files_content[file_path] = file.decoded_content.decode("utf-8")

    g = Github(PAT)
    branch_name = generate_branch_name()

    try:
        # REPO_NAMEのmainブランチの最新コミットSHAを利用して、FORKED_REPO_NAMEにブランチ作成
        base_repo = g.get_repo(REPO_NAME)
        base_main = base_repo.get_branch("main")
        commit_sha = base_main.commit.sha

        repo = g.get_repo(FORKED_REPO_NAME)
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=commit_sha)
    except Exception as e:
        return f"ブランチの作成に失敗しました: {str(e)}"

    file_descriptions = "\n".join(
        [
            f"### {path}\n```python\n{content}\n```"
            for path, content in files_content.items()
        ]
    )

    prompt = f"""
あなたは優秀なソフトウェア開発者です。以下のファイル群を指示に従って修正してください。

## ファイル群：
{file_descriptions}

## 指示：
{message}

以下のルールを守って、JSONで結果を構造的に返してください：

- 変更または追加が必要なファイルのみを `changes` に含めてください。
- 変更不要なファイルは含めないでください。
- 新規作成が必要なファイルがあれば、それも`changes`に追加してください。

回答は以下のフォーマットを厳密に守ってください（JSON以外のテキストを含めないこと）：

```json
{{
    "pr_title": "プルリクエストの明確で簡潔な日本語タイトル",
    "pr_body": "プルリクエストの変更点や意図を簡潔に日本語で説明",
    "changes": {{
        "ファイル名1": {{
            "commit_message": "1行のコミットメッセージ",
            "updated_code": "修正後または追加するコード全体"
        }},
        "ファイル名2": {{
            "commit_message": "1行のコミットメッセージ",
            "updated_code": "修正後または追加するコード全体"
        }}
    }}
}}
```
"""

    try:
        response = client.chat.completions.create(
            model=GPT_MODEL,
            messages=[{"role": "user", "content": prompt}],
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

    if not changes:
        return "GPTが提示した修正はありません。"

    try:
        for file_name, change in changes.items():
            new_code = change["updated_code"]
            commit_message = change["commit_message"]

            existing_file = get_file_from_repo(file_name)
            if existing_file:
                # 既存ファイルの更新
                repo.update_file(
                    existing_file.path,
                    commit_message,
                    new_code,
                    existing_file.sha,
                    branch=branch_name,
                )
            else:
                # 新規ファイル作成
                repo.create_file(
                    file_name, commit_message, new_code, branch=branch_name
                )

    except Exception as e:
        return f"Commitに失敗しました: {str(e)}"

    # PRの作成
    pr_creation_result = create_pull_request(
        branch_name=branch_name, pr_title=pr_title, pr_body=pr_body
    )

    return (
        f"ブランチ「{branch_name}」に変更をpushしました。\n"
        f"プルリクエスト: {pr_creation_result}"
    )
