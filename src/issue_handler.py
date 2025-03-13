import logging
from github import Github
from config import config


def create_issue(content: str) -> str:
    PAT = getattr(config, "PAT", "")
    REPO_NAME = getattr(config, "REPO_NAME", "")
    if not PAT:
        return "PATが設定されていません。Issueを作成できません。"
    try:
        g = Github(PAT)
        repo = g.get_repo(REPO_NAME)
        title = "Discord Issue"
        issue = repo.create_issue(title=title, body=content)
        return f"Issueが作成されました: {issue.html_url}"
    except Exception as e:
        logging.error(f"Issue作成中にエラー: {e}")
        return f"Issueの作成に失敗しました: {e}"
