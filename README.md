# site_update_notifier

## /lesson（language-learning-audio）

[language-learning-audio](https://github.com/Keno42/language-learning-audio) を
submodule (`external/language-learning-audio`) として取り込み、Discord から語学レッスンを回す。

`/lesson` を実行すると:

1. 振り返りキューから最大 `LESSON_REVIEW_LIMIT` 問を 1 問ずつ出す（問い → 「答えを見る」→
   「言えた / 迷った / 言えなかった」。答えはスポイラーにしない: PC 版 Discord は一度開いた
   スポイラーを、編集で次の問いに変わっても開いたままにするため）。直前のレッスンの新出は上限を
   超えても全部出し、全部に答えるまで次のレッスンは生成しない（答えのない項目は音声レッスン側で
   「言えた」とみなされるため）。振り返らずに生成したいときは `/lesson-auto`
2. 答えた結果を `audiolesson report --recalled … --hesitated … --failed …` で送る
3. 次のレッスンを生成し、その問いをキューに足して、音声と transcript をチャンネルに投稿する
   （「Discord 振り返り: 次回 15問（確認待ち 43件）」のように次回の見通しも添える）
4. 生成物を消す（残るのは `learner.json` と振り返りキュー `pending_review.json` だけ）

### 振り返りキュー

音声レッスンの間隔反復（`learner.json`）とは別に、「いつ Discord で自己申告を求めるか」を
`pending_review.json` で管理する（`src/review_queue.py`）。

- 単位は**問い**。«Ég vil fara heim.» のように複数の項目を一度に言う問いは 1 件で、その結果は
  含まれる項目すべてに当てはめる（どちらで詰まったかは分けられないため）。同じ項目が別の問いに
  入ることもある

- 問いは原則として消えない（例外は下の、答えないまま次のレッスンに進んだ新出）。上限で出せなかった
  問い、時間切れで答えなかった問いは未回答（unseen）のまま次回に回る。答えた問いは、その問いの
  状態と次の期限だけが変わる
- 新出項目の問いはすぐ（同じ日の次の `/lesson` でも）出す。復習した項目の問いは、その項目が
  まだキューにないときだけ入る（翌日から）
- 出す順: 新出の未回答（新しいレッスンのものから）→ 期限の来た「言えなかった」→「迷った」→
  その他の未回答 →「言えた」→（上限に余りがあれば）期限前のもの。同じ順位では期限の早いもの、
  最後に答えたのが古いものから。答えのない項目は音声レッスン側で「言えた」とみなされるので、
  前回の新出は必ず先に確かめる。期限を 7 日以上過ぎた問いは「言えなかった」と同じ順位に上がる
- 直前より前のレッスンの新出で、答えないまま次のレッスンに進んだもの（`/lesson-auto` を挟んだ
  ときなど）は、次の `/lesson` の開始時にキューから外す（音声レッスン側では「言えた」扱い済み）。
  その項目が後の復習でまた出れば、通常の問いとして入り直す
- 次の確認まで: 言えなかった 1 日 / 迷った 1→3→7 日 / 言えた 1→3→7→14→30 日
  （同じ結果が続くたびに伸びる。`INTERVALS` で変えられる）
- 結果はすべて音声レッスン側にも伝わる: 「言えなかった」は `report --failed`（段階が下がり、
  翌日に復習、次のレッスンで 1 秒長く待つ）、「迷った」は `--hesitated`（復習の間隔が半分に
  縮む）、「言えた」は `--recalled`（予定どおり。確認済みとして数える）。答えのない項目は
  音声レッスン側で「言えた」とみなされる
- 報告は出題元のレッスンごとに `report --lesson <出題元>` で送る。最新のレッスンが「報告済み」に
  なる（手動モードのペースに効く）のは、そのレッスンの問いに答えたときだけ
- 報告が失敗しても答えは失われない。届いていない報告はキューに残り、次の `/lesson` の最初に
  送り直す（届いた報告は二度送らない）
- 旧形式の `pending_review.json`（`{"lesson", "questions"}`）は最初に読んだときに変換する。
  変換した問いはすべて未回答・当日期限で、元のファイルは `pending_review.json.v1.bak` に残る

`/lesson-auto` は振り返りも自己申告もせず、生成と投稿だけをする（自己申告が面倒な人向け）。
language-learning-audio の auto モード（`generate --auto`）で動き、報告がなければ
「できた」とみなしてペースが上がっていく。auto モードは learner.json に残るので、
その後 `/lesson` で振り返って `--failed` を報告すれば、その分ペースは落ちる。

生成中はチャンネルに「生成中…（経過 3:15）音声合成 120/450」のような 1 通を出し、
15 秒おきに書き換える。初回はすべての文を音声合成するので時間がかかる（キャッシュを
残せば 2 回目以降は新しい文だけ）。

音声がアップロード上限を超えるときは ffmpeg でビットレートを落として送る。

### サーバーでの初回セットアップ

1. submodule を取得し、依存を入れる

   ```sh
   git submodule update --init
   .venv/bin/pip install -r requirements.txt   # bot を動かしている venv に入れる（edge-tts が増えた）
   which ffmpeg                                 # mp3 化と再圧縮に必要
   ```

   レッスン生成は bot を動かしている Python（`sys.executable`）で実行されるので、
   edge-tts は必ずその venv に入れる。システムの `pip` は Debian / Raspberry Pi OS では
   `externally-managed-environment` で拒否される。venv がまだなければ
   `python3 -m venv .venv` で作り、bot を `.venv/bin/python -m src.bot` で起動する。

   `update_and_restart.sh` にも `git submodule update --init` と
   `.venv/bin/pip install -r requirements.txt` を足しておく（pull で submodule の指す
   コミットが変わったときに追従するため）。

2. `config/config.py` に追記する（`LESSON_ROOT` と `LESSON_USERS` がなければ `/lesson` は無効）

   ```python
   LESSON_ROOT = "/srv/lessons"          # 永続ディレクトリ。<LESSON_ROOT>/<名前>/learner.json
   LESSON_USERS = {123456789012345678: "yuki"}  # Discord ユーザー ID → 学習者名（この人だけ使える）
   LESSON_CHANNEL_ID = 0                 # 0 ならどのチャンネルでも。指定するとそこでだけ
   LESSON_GUILD_ID = 0                   # サーバー ID を入れるとコマンドがすぐ出る（0 だとグローバル同期で反映に時間がかかる）
   LESSON_CURRICULUM = "curricula/is-en" # 以下はパスも含め submodule からの相対
   LESSON_KNOWN = "ja"
   LESSON_PROFILE = "profiles/edge-is-ja.toml"
   LESSON_MINUTES = 30
   LESSON_EXTRA_ARGS = []                # 例: ["--auto"]
   LESSON_KEEP_CACHE = False             # True で TTS キャッシュを <LESSON_ROOT>/tts-cache に残し、全員で共有する。同じ文が何度も出るので 2 回目以降・2 人目以降の生成がずっと速くなる（Raspberry Pi では True 推奨）
   LESSON_TIMEOUT_MIN = 60               # 生成がこれ以上かかったら止めてエラーにする
   LESSON_UPLOAD_LIMIT_MB = 20
   LESSON_REVIEW_LIMIT = 20              # 1 回の振り返りの最大問数（出せなかった分は次回へ）。0 なら期限の来ている問いすべて
   ```

3. `LESSON_ROOT` を作って bot の実行ユーザーに書き込み権限を付け、`learner.json` を
   バックアップ対象にする

4. これまで PC で生成していたなら、`out/<名前>/learner.json` を
   `<LESSON_ROOT>/<名前>/learner.json` にコピーすると続きから始まる
   （振り返りキューは空から始まる）

5. Discord の Developer Portal: bot を `applications.commands` スコープ付きで招待し直す
   （まだなら）。レッスン用チャンネルでファイル添付の権限があることを確認する

### /version

いつ起動し、どのコミットで動いているかを本人にだけ表示する（`src/version.py`）。自動更新が
効いたかの確認用。

```
起動: 2026-09-29 02:41:30 +0900（稼働 2時間15分）
**bot** `0afcac7`（2026-09-28 17:03）Bump language-learning-audio to #126: …
**language-learning-audio** `b429c96`（2026-09-29 02:37）#29 «Ég á …» family: … (#127)
```

- 値は起動時に一度だけ読む。pull しても再起動するまでは、ディスク上ではなく今動いている版を答える
- language-learning-audio は submodule が実際に指しているコミット（`git submodule update`
  を忘れていると、bot のコミットが新しくてもこちらは古いまま、と分かる）
- 日時はサーバーのローカル時刻。git で読めないときは「不明」
- `/lesson` と同じく `LESSON_ROOT` / `LESSON_USERS` があるときだけ登録される

### language-learning-audio を更新する

submodule は特定のコミットを指す。新しい版を使うときは:

```sh
git submodule update --remote external/language-learning-audio
git add external/language-learning-audio && git commit -m "Bump language-learning-audio"
```

### テスト

```sh
python -m unittest discover -s tests -t .
```

submodule が取得済みなら、stub 音声で 生成 → 投稿 → 振り返り → report → 次の生成 まで通す。
