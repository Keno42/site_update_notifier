"""リポジトリの Actions secrets を、SSH なしで bot に渡す (issue #119): CI で封をし、bot の機械で開ける.

bot の機械は cron で ``main`` を pull して再起動するだけで、GitHub Actions の secrets は workflow の実行中にしか読めない.
値は ``main`` を通るしかないので、bot の機械だけが持つ鍵で暗号化する: bot は X25519 の鍵対を作り (秘密鍵は機械の外に出さない)、
公開鍵だけを GitHub の変数 (``BOT_PUBLIC_KEY``) に置く. workflow ``seal-secrets`` が許可リストの secrets を libsodium の sealed box
(GitHub 自身の secrets と同じ構成) で封じて ``sealed/secrets.sealed`` を PR にし、bot が起動時に開く.
開いた値はメモリにだけ置く (``SECRETS``): 生成のサブプロセスの env にだけ渡し、``os.environ`` にも、ログにも、Discord にも出さない.
"""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path

# 封じて渡してよい名前 (許可リスト). 増やすには workflow と合わせた、レビューされた変更が要る
ALLOWLIST = ("AZURE_SPEECH_KEY", "AZURE_SPEECH_REGION")
KEY_FILE = ".bot_secret_key"  # LESSON_ROOT の下. git の外. mode 600
SEALED_PATH = Path(__file__).resolve().parent.parent / "sealed" / "secrets.sealed"

# 開けた値. 起動時に ``load`` が入れる. ここ以外には置かない
SECRETS: dict[str, str] = {}


def _nacl():
    from nacl.public import PrivateKey, PublicKey, SealedBox

    return PrivateKey, PublicKey, SealedBox


def load_or_create_key(root: Path):
    """bot の秘密鍵 (なければ作る). ファイルは mode 600 で作り、中身は秘密鍵の base64 だけ."""
    PrivateKey, _, _ = _nacl()
    path = Path(root) / KEY_FILE
    try:
        return PrivateKey(base64.b64decode(path.read_text("ascii").strip()))
    except (OSError, ValueError):
        pass
    key = PrivateKey.generate()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(base64.b64encode(bytes(key)).decode("ascii"))
    os.chmod(path, 0o600)
    return key


def public_key_b64(key) -> str:
    return base64.b64encode(bytes(key.public_key)).decode("ascii")


def seal(values: dict[str, str], public_b64: str) -> str:
    """許可リストの値を JSON 1 つにして、公開鍵宛ての sealed box で封じる (base64)."""
    _, PublicKey, SealedBox = _nacl()
    chosen = {k: values[k] for k in ALLOWLIST if values.get(k)}
    box = SealedBox(PublicKey(base64.b64decode(public_b64)))
    return base64.b64encode(box.encrypt(json.dumps(chosen, sort_keys=True).encode("utf-8"))).decode("ascii")


def open_sealed(text: str, key) -> dict[str, str]:
    """封じた文字列を秘密鍵で開く. 許可リストの名前の値だけ返す. 開けなければ ValueError."""
    _, _, SealedBox = _nacl()
    try:
        raw = SealedBox(key).decrypt(base64.b64decode(text.strip()))
        data = json.loads(raw.decode("utf-8"))
    except Exception as e:  # noqa: BLE001  暗号・base64・JSON のどれでも「開けない」
        raise ValueError(type(e).__name__) from None
    if not isinstance(data, dict):
        raise ValueError("not an object")
    return {k: str(v) for k, v in data.items() if k in ALLOWLIST and v}


def load(root: Path, sealed: Path | None = None) -> dict[str, str]:
    """起動時: 鍵を用意し (なければ作る)、封じたファイルがあれば開いて ``SECRETS`` に入れる. 開けなければ警告を 1 行だけ出して無視する
    (鍵を作り直した直後など)."""
    SECRETS.clear()
    try:
        key = load_or_create_key(root)
    except ImportError:
        logging.warning("PyNaCl が入っていないので、封じた secrets は開けません")
        return SECRETS
    path = sealed or SEALED_PATH
    if not path.exists():
        return SECRETS
    try:
        SECRETS.update(open_sealed(path.read_text("ascii"), key))
    except (OSError, ValueError) as e:
        logging.warning("sealed/secrets.sealed を開けませんでした (%s): 鍵を作り直したか、ファイルが壊れています", e)
    return SECRETS


def environment() -> dict[str, str]:
    """生成のサブプロセスにだけ足す env (``os.environ`` には入れない)."""
    return dict(SECRETS)


def status() -> str:
    """/version に出す一行: 値は出さず、あるかどうかだけ."""
    return "secrets: azure " + ("✓" if all(SECRETS.get(k) for k in ALLOWLIST) else "–")
