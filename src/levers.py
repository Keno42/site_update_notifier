"""レバー (language-learning-audio の docs/LEVERS.md) をチャンネルのトピックで変える.

    [levers]
    late_unhinted_recall = true  # 新出の最後の確認はヒントなし
    pause_multiplier = 1.2       # 答える時間の倍率

1 行の ``levers = { late_unhinted_recall = true, pause_multiplier = 1.2 }`` でもよい.
トピックに ``[levers]`` があれば、config.py の LESSON_EXTRA_ARGS にある同じレバーより
優先する (書かれていないレバーは既定のまま). 変えるのは生成だけで、予定は変わらない.
"""

from __future__ import annotations

from typing import Any

from .topic import TopicError, channel_topic, section

# レバー → generate の引数 (値を取るか)
FLAGS = {
    "late_unhinted_recall": ("--late-unhinted-recall", False),
    "pause_multiplier": ("--pause-multiplier", True),
}
# なくなったレバー: トピックに残っていても読み飛ばして、そう伝える
REMOVED = {"max_same_situation": "同じ状況文の回数の上限はなくなりました"}


def parse(raw: dict) -> list[str]:
    """``[levers]`` の節 → generate の引数."""
    unknown = sorted(set(raw) - set(FLAGS) - set(REMOVED))
    if unknown:
        raise TopicError(f"知らないキー {unknown}（使えるのは {list(FLAGS)}）")
    args: list[str] = []
    late = raw.get("late_unhinted_recall", False)
    if not isinstance(late, bool):
        raise TopicError("late_unhinted_recall は true か false で書いてください")
    if late:
        args.append("--late-unhinted-recall")
    x = raw.get("pause_multiplier")
    if x is not None:
        if isinstance(x, bool) or not isinstance(x, (int, float)) or not 0.3 <= x <= 3:
            raise TopicError("pause_multiplier は 0.3〜3 の数で書いてください")
        args += ["--pause-multiplier", f"{x:g}"]
    return args


def from_topic(channel: Any) -> tuple[list[str] | None, str]:
    """(トピックのレバーの引数. ``[levers]`` がなければ None, 読めなかったときや
    なくなったレバーが残っているときの案内)."""
    try:
        raw = section(channel_topic(channel), "levers")
        if raw is None:
            return None, ""
        gone = [f"{k}（{REMOVED[k]}）" for k in REMOVED if k in raw]
        note = (
            f"チャンネルのトピックの {'、'.join(gone)} は使わないので消してください。"
            if gone
            else ""
        )
        return parse(raw), note
    except TopicError as e:
        return None, (
            f"チャンネルのトピックのレバーの設定を読めませんでした: {e}。"
            "config.py の設定で生成します。"
        )


def apply(extra_args: list[str], levers: list[str] | None) -> list[str]:
    """LESSON_EXTRA_ARGS に、トピックのレバーを重ねる: トピックにレバーの節があれば、
    extra_args のレバーの引数 (と値) を外してトピックのものを足す."""
    if levers is None:
        return list(extra_args)
    takes_value = {flag: v for flag, v in FLAGS.values()}
    out: list[str] = []
    skip = False
    for a in extra_args:
        if skip:
            skip = False
            continue
        flag = a.split("=", 1)[0]
        if flag in takes_value:
            skip = takes_value[flag] and "=" not in a
            continue
        out.append(a)
    return out + levers
