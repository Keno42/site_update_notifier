# site_update_notifier

## /lesson（language-learning-audio）

[language-learning-audio](https://github.com/Keno42/language-learning-audio) を
submodule (`external/language-learning-audio`) として取り込み、Discord から語学レッスンを回す。

`/lesson` を実行すると:

1. 前回のレッスンがあれば、その項目を 1 問ずつ振り返る（問い → 答えはスポイラー →
   「言えた / 迷った / 言えなかった」）。「振り返らずに生成」でスキップもできる
2. 振り返りの結果で `audiolesson report --failed …` を実行する
3. 次のレッスンを生成し、音声と transcript をチャンネルに投稿する
4. 生成物を消す（残るのは `learner.json` と次回の振り返り用 `pending_review.json` だけ）

`/lesson-auto` は振り返りも自己申告もせず、生成と投稿だけをする（自己申告が面倒な人向け）。
language-learning-audio の auto モード（`generate --auto`）で動き、報告がなければ
「できた」とみなしてペースが上がっていく。auto モードは learner.json に残るので、
その後 `/lesson` で振り返って `--failed` を報告すれば、その分ペースは落ちる。

「迷った」は今のところ言えた扱い。音声がアップロード上限を超えるときは ffmpeg で
ビットレートを落として送る。

### サーバーでの初回セットアップ

1. submodule を取得し、依存を入れる

   ```sh
   git submodule update --init
   pip install -r requirements.txt   # edge-tts が増えた
   which ffmpeg                       # mp3 化と再圧縮に必要
   ```

   `update_and_restart.sh` にも `git submodule update --init` と
   `pip install -r requirements.txt` を足しておく（pull で submodule の指す
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
   LESSON_KEEP_CACHE = False             # True で TTS キャッシュを残す（生成が速くなる）
   LESSON_UPLOAD_LIMIT_MB = 20
   LESSON_REVIEW_LIMIT = 0               # 振り返りの最大問数。0 なら全部（超える分は新出項目を優先）
   ```

3. `LESSON_ROOT` を作って bot の実行ユーザーに書き込み権限を付け、`learner.json` を
   バックアップ対象にする

4. これまで PC で生成していたなら、`out/<名前>/learner.json` を
   `<LESSON_ROOT>/<名前>/learner.json` にコピーすると続きから始まる
   （前回分の振り返りはない状態で始まる）

5. Discord の Developer Portal: bot を `applications.commands` スコープ付きで招待し直す
   （まだなら）。レッスン用チャンネルでファイル添付の権限があることを確認する

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
