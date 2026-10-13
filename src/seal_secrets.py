"""workflow ``seal-secrets`` が呼ぶ: 環境変数の secrets を許可リストだけ封じて ``sealed/secrets.sealed`` に書く (issue #119).

値は標準出力にもログにも出さない. ``BOT_PUBLIC_KEY`` (リポジトリの変数) が公開鍵. 使い方::

    BOT_PUBLIC_KEY=… AZURE_SPEECH_KEY=… AZURE_SPEECH_REGION=… python -m src.seal_secrets sealed/secrets.sealed
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import secrets_box


def main(argv: list[str] | None = None, env: dict[str, str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    env = dict(os.environ) if env is None else env
    if len(argv) != 1:
        print("usage: python -m src.seal_secrets OUT", file=sys.stderr)
        return 2
    public = env.get("BOT_PUBLIC_KEY", "").strip()
    if not public:
        print("BOT_PUBLIC_KEY (the repository variable) is not set", file=sys.stderr)
        return 1
    values = {k: env[k] for k in secrets_box.ALLOWLIST if env.get(k)}
    missing = [k for k in secrets_box.ALLOWLIST if k not in values]
    if missing:
        print(f"not set: {', '.join(missing)}", file=sys.stderr)  # 名前だけ
        return 1
    try:
        sealed = secrets_box.seal(values, public)
    except Exception as e:  # noqa: BLE001
        print(f"could not seal ({type(e).__name__}): is BOT_PUBLIC_KEY the bot's public key?", file=sys.stderr)
        return 1
    out = Path(argv[0])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(sealed + "\n", "ascii")
    print(f"sealed {len(values)} secret(s) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
