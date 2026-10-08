# site_update_notifier

Discord（と Slack）の bot。機能:

- **サイト更新の通知**: `CHECK_URL` の記事一覧を `CHECK_INTERVAL` 秒ごとに確認し、新しい記事を `CHANNEL_ID` に投稿する
- **会話**: bot へのメンション（またはロール）に ChatGPT（`GPT_MODEL`）で返事をする。返信を続けると会話が続く
- **音声の書き起こし**: メンションに音声を添付すると Whisper で書き起こす（Slack では音声を投稿したスレッドに返す）。長い音声は約 12 分ずつに分けて処理する（`src/transcribe.py`）
- **語学レッスン**: `/lesson` ほか（以下）

## /lesson（language-learning-audio）

[language-learning-audio](https://github.com/Keno42/language-learning-audio) を
submodule (`external/language-learning-audio`) として取り込み、Discord から語学レッスンを回す。

`/lesson` を実行すると:

1. 振り返りキューから最大 `LESSON_REVIEW_LIMIT` 問を 1 問ずつ出す（問い → 「答えを見る」→
   「言えた / 迷った / 言えなかった」。答えはスポイラーにしない: PC 版 Discord は一度開いた
   スポイラーを、編集で次の問いに変わっても開いたままにするため）。直前のレッスンの新出は上限を
   超えても全部出し、全部に答えるまで次のレッスンは生成しない（答えのない項目は音声レッスン側で
   「言えた」とみなされるため）。振り返らずに生成したいときは `/lesson-auto`。
   続けて場面カードと読みカードを数枚ずつ出す（下の「場面カード」「読みカード」）
2. 答えた結果を `audiolesson report --recalled … --hesitated … --failed …` で送る
3. 次のレッスンを生成し、その問いをキューに足して、音声と transcript をチャンネルに投稿する
   （「Discord 振り返り: 次回 15問（確認待ち 43件）」のように次回の見通しも添える）
4. 生成物を消す（残るのは `learner.json`、振り返りキュー `pending_review.json`、場面カード・読みカードの
   予定 `scene_queue.json` / `reading_queue.json`、フィードバック用のレッスンの記録 `lesson_manifests/` と
   `lesson_feedback.jsonl`）

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

### カード（場面カード・読みカード）

問いのあと、場面カード（`LESSON_SCENE_CARDS` 枚、既定 3）、読みカード（`LESSON_READING_CARDS` 枚、
既定 3）を 1 枚ずつ出す。どちらも「答えを見る」→「言えた / 迷った / 言えなかった」。

- **振り返りの時間は増えない**: 問いの上限をカードの分だけ減らす（既定なら問い 14 + 場面 3 + 読み 3）。
  直前のレッスンの新出の問いは必ず出すので、カードはその残りの分だけ（場面カードが先、問いは最低 1 問
  残す）
- カードは毎回 CLI（`audiolesson scenes` / `audiolesson reading`）の JSON をメモリ上で読むだけで、
  ディスクには書かない。残すのはカード ID ごとの予定（`scene_queue.json` / `reading_queue.json`、
  `src/cards.py`）だけ。読めなかったときはカードなしで振り返る。デッキから消えたカードは出さない
- 出す順: 期限の来たカード（言えなかった → 迷った → 言えた）→ デッキ順の新しいカード。次の確認までの
  日数は振り返りの問いと同じ。結果は音声レッスン側（`learner.json`）には報告しない
- 🔊 は押した本人にだけ mp3 が届く（edge-tts、`src/speech.py`。公開カードの音声は
  `<LESSON_ROOT>/reading-tts/` にキャッシュ）

#### 場面カード（language-learning-audio #129）

旅行の can-do 場面の一場面を台本どおりに練習する（`src/scenes.py`）。

- 日本語の状況（例:「レジで店員に何か聞かれました」）→ 相手の言葉があれば 🔊 でアイスランド語を聞く
  （答えの前は文字では出さない）→ 声に出して答える →「答えを見る」で相手の言葉の文字と意味、答えの例。
  答えの後の 🔊 は答えの例の発音
- 種類: 相手の言葉に答える（respond）、自分から言う（initiate: 挨拶・トイレの場所など）、分からない
  早口を聞き返す（repair）。店員の決まり文句（«Viltu poka?» «Hvað má bjóða þér?»）は答える場面で聞く
- 出すのは、答えに必要な表現をレッスンで習ったカードだけ（`audiolesson scenes --learner`）。
  年末年始のカードは旅程の設定の `season` が合うときだけ
- **準備状況**: `LESSON_READINESS_DAYS` 日ごと（既定 7 日）に、レッスンの投稿へ場面ごとの状況を添える。
  準備OK（その場面のカードが全部出題でき、最後の評価がどれも言えた）・練習中・未学習（まだ 1 枚も
  出題できない）の数を Tier A / B ごとに。前回添えた日は `readiness_reminder.json`

#### 読みカード（language-learning-audio #133）

音声レッスンは綴りを見せないので、看板・店の言葉・地名などを声に出して読む（`src/reading.py`）。

- 書いてあるものを声に出して読む →「答えを見る」で意味・読み方の目安・成り立ち（地名の部品）。
  🔊 は答えを見た後だけ（先に聞くと読む練習にならない）
- デッキの順: 文字と音 → 看板 → 店 → 地名 → 地名の部品

### 旅程のプロフィール（language-learning-audio #132）

旅程の設定は次のどちらかに書く（両方あれば 1 を使う）:

1. `<LESSON_ROOT>/<名前>/trip.toml`（その人だけの設定。サーバーに手で置く）
2. `/lesson` を実行するチャンネルの**トピック**（同じ旅行の人だけがいるチャンネル向け。スレッドなら
   親チャンネルのトピック）。`[trip]` の行から空行までか、1 行の `trip = { … }` で書く:

   ```text
   アイスランド旅行 🇮🇸
   [trip]
   departure = 2030-01-31
   boost = ["A6", "B2"]
   places = ["Ísafjörður"]
   season = "winter-holidays"
   ```

   ```text
   trip = { departure = 2030-01-31, boost = ["A6", "B2"], season = "winter-holidays" }
   ```

キーはどちらも `departure` / `boost` / `places` / `season`（どれも省略できる。空なら Tier A → B の
順）。意味は language-learning-audio の `audiolesson/trip.py` と `docs/TRAVEL-CANDO.md`。

設定があると:

- 生成に `--trip` で渡す: 旅行の can-do 項目（`season` があればその季節の分も）を先に教える
  順番になる。**ペースは変わらない**（言えた / 迷った / 言えなかったで決まるまま）
- `places` の地名も読みカードになる（ID は `own_1`, `own_2` … と番号だけ）。カードは振り返りの
  チャンネルに出る。trip.toml の地名をチャンネルの他の人に見せたくないなら
  `LESSON_READING_OWN_PLACES = False`
- bot は設定の値をどこにも書かない。trip.toml は CLI に渡すだけ。トピックの設定は決まった形の
  TOML に直して一時ファイルにし、使い終わったら消す。レッスンの記録（`manifest.json`）に残るのは
  `trip_sha256`（どの版で生成したか）だけで、引数のパスはトピックなら `channel-topic` と残る。
  エクスポートにも中身は入らない。自分の地名のカードの 🔊 音声も一時ファイルで、送ったら消す
- トピックの `[trip]` を読めないとき（TOML の誤り、知らないキー、型の違い）は、生成の前に
  「チャンネルのトピックの旅程の設定を読めませんでした: …」と出して旅程なしで生成する。
  案内にはキー名と位置だけを出し、値は出さない
- トピックはチャンネルに入れる人全員に見える。**同じ旅行の人だけがいるチャンネル**で使う

### レバー（チャンネルのトピック）

生成の設定のうち「レバー」（language-learning-audio の `docs/LEVERS.md`）は、`/lesson` を実行する
チャンネルのトピックに `[levers]` の節（または 1 行の `levers = { … }`）を書くと変えられる
（`src/levers.py`）。旅程の `[trip]` と同じトピックに並べて書いてよい（節は空行か次の `[…]` で終わる）:

```text
[levers]
late_unhinted_recall = true  # 新出の最後の確認はヒントなし
pause_multiplier = 1.2       # 答える時間の倍率（0.3〜3）
```

- トピックに `[levers]` があれば、`LESSON_EXTRA_ARGS` にある同じレバーより優先する。書かなかった
  レバーは既定（無効）になる。`[levers]` がなければ `LESSON_EXTRA_ARGS` のまま
- 変わるのは次に生成するレッスンだけ。使った値はレッスンの記録（`manifest.json` の引数と
  `plan.json` の `config.levers`）に残るので、フィードバックと突き合わせられる
- 読めないとき（知らないキー、範囲外の値）は「チャンネルのトピックのレバーの設定を読めませんでした: …」
  と出して `config.py` の設定で生成する
- なくなったレバー（`max_same_situation`）が残っていても、ほかのレバーはそのまま使い、消すよう案内する

### 案内

`/lesson` は、いま何をする時間かを短く案内する。

- 振り返りの最初の問いに「これから定着度チェックです（前回までの表現、全 N 問）」と手順を添える。
- レッスンの投稿に、聞き終えたらフィードバックボタン（または `/lesson-feedback`）で記録するよう添える。

### レッスン後のフィードバック（language-learning-audio #128）

レッスンの手応えを直後に 30 秒ほどで記録し、SSH なしで Discord から取り出す（`src/feedback.py`）。
記録するだけで、v1 では `learner.json`・復習・次のレッスンの生成には一切使わない。

- レッスンの投稿に「フィードバック」ボタンが付く（`/lesson-feedback [lesson]` でも開ける）。
  ボタンは custom_id に学習者とレッスンの記録の ID（`lesson-012`、再生成なら `lesson-012.2`）
  を持つので、自動更新で bot が再起動した後でも、同じ番号を再生成した後でも、押した投稿の
  レッスンに紐付く。押せるのはそのレッスンを受けた人だけ
- フォーム（本人にだけ見える）: 今日の新しい表現の一覧（訳つき）と、レッスンの記録から機械が
  気づいたこと（「後半に出てこなかった気がする」「終わり近くに、ヒントなしで言う場面がなかった」。
  language-learning-audio の plan.json の `review_candidates`。当てはまるかだけ答える）を見ながら
  - 練習が足りなかった・覚えていない表現（複数選択、なければ選ばなくてよい）。「覚えた」は聞かない:
    直後の「覚えた」は当てにならず、翌日の振り返りが測る（language-learning-audio の
    docs/LEARNING-DESIGN.md H10）が、「思い出せない」は当てになる。選ばなかった項目は「不満なし」で、
    「十分」ではない。選んだ項目は、送信と同時に迷った扱いで音声レッスンに報告され（早めに、半分の間隔で
    もう一度。降格はしない）、新出の 60% 以上を選んだレッスンは、選んだ負荷が何であっても `load_effective`
    を「重い」として記録する（ペースへの入力にするのは language-learning-audio #218）。
    以前あった「ほとんど出てこなかった・聞こえなかった表現」の欄は外した（一度も使われなかった, #81）
  - 今日のレッスンの量・難しさ: 軽い / ちょうどいい / 重い（これだけ必須）
  - 当てはまること・気になった点（機械が気づいたことのほか、同じ表現がくり返し出すぎた・何を答えれば
    いいか分からない問いがあった・テンポが合わない・その他）
  - メモ（任意）
  - 選択肢の文言は記録に残さない（保存するのは `unheard` `sooner` `load` `friction` などのキー。以前の
    フォームの記録には `usable` がある）ので、言い回しを変えても過去の記録とそのまま比べられる
- `/lesson-feedback-report [lesson]`: 最新のフィードバックの要約を投稿
- `/lesson-feedback-export [lesson]`: フィードバックとレッスンの記録一式を zip で添付
  （Issue や PR、外部での分析用）。learner.json とメモを含むので本人にだけ見える形で返す
- `lesson` は `12`（その番号の最後の記録）、`12.2` や `lesson-012.2`（再生成した記録）、
  `lesson-012`（1 回目）。省略すると最新
- `/lesson` と同じく、`LESSON_CHANNEL_ID` を指定するとそのチャンネルでだけ使える

置き場所は learner.json と同じ `<LESSON_ROOT>/<名前>/`（git の外なので、自動更新の pull で
消えたり上書きされたりしない）:

```text
lesson_feedback.jsonl     追記のみ。1 行 = 1 回分（記録の ID、bot / language-learning-audio の
                          コミット、transcript・script・生成前 learner.json の sha256、回答、メモ）。
                          電源断で途中で切れた行があっても、読めなくなるのはその行だけ
lesson_manifests/lesson-012/
  manifest.json           生成日時、コミット、各ファイルの sha256、生成に使った引数、
                          旅程のプロフィールを使ったならその sha256（中身は残さない）
  lesson-012.plan.json / .script.json / .transcript.md
  learner.before.json     生成前の learner.json（選ばれ方を後から再現するため）
```

記録は生成したときのまま変えない（同じ番号をもう一度生成したら `lesson-012.2` を別に作る）。
この機能より前に生成したレッスンには記録がないので、フィードバックの対象外。

### 週のレポート（/lesson-week）

信号を一画面に並べる。判断（残す・戻す・待つ）は、直近に入れた変更の予測と突き合わせて人が行う
（language-learning-audio の `docs/LEARNING-DESIGN.md` §1「週のたびに」、`src/weekly.py`）。
`/lesson-week [days]`（既定 7 日、最大 60 日）。本人にだけ見える。新しい記録は作らず、すでにある
データを読むだけ:

- **量と長さ**: レッスンごとの新出の数（ペースと並べて）と長さ
- **新出の翌日の確認**: 言えた・迷った・言えなかった・次の振り返り待ち（直前のレッスン）・確認の記録なし
  （古いレッスンで答えが記録されていないもの。音声レッスン側では「言えた」扱い）と、言えなかった・迷った表現
- **言えなかった率**: 1〜2語と3語以上の表現（表現がレッスンの記録に残っている新出だけの目安で、
  数が少ない。3語以上が失敗しやすいかを見る）
- **フィードバック**: 件数、負荷、気になった点、メモ
- **振り返りの所要時間**: `/lesson` の振り返り（問い・場面カード・読みカード）を、1 つ見せてから答えるまで
  の秒数で自動で測る（`review_log.jsonl`、件数と秒だけで、聞かれた中身は入れない）。1 つにつき 2 分を超えた
  分は席を外したとみなして 2 分に切り、その件数も出す。1 回の平均・最長、問い・場面・読みの 1 つあたりの秒、
  途中で終えた回数。「振り返りが長すぎないか」を感覚でなく数字で見る
- **レッスンの中身**（レッスンの記録があるもの、直近 4 回まで）: 練習時間の内訳（単発の想起・相手の言葉が
  あるやり取り・混合復習・新しい文づくり・導入）、復習の項目で 3 回以上出たもの、新出がいちばん長く
  触れられなかった時間（導入後の空白）。「よく知っている表現が何度も出ていないか」「新出が閉じの想起まで
  何分も放っておかれていないか」を、レッスンを実際に聞いた感想と照らして確かめる
- **生成に使った版と引数**: language-learning-audio のコミットと `--auto` などの引数が変わったレッスン
  （どの変更の後で数字が動いたかを見るため。パスは出さない）
- 続けて、場面カードの準備状況（別のメッセージ）

「迷った」はこの学習者はほぼ使わない（すぐ言い切って当たるか外れるか）ので、数字は言えた・言えなかったを
中心に見る。

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
   LESSON_SCENE_CARDS = 3                # 振り返りの場面カードの枚数（その分問いを減らす）。0 で出さない
   LESSON_READING_CARDS = 3              # 振り返りの最後の読みカードの枚数（その分問いを減らす）。0 で出さない
   LESSON_READINESS_DAYS = 7             # 場面ごとの準備状況をレッスンの投稿に添える間隔。0 で添えない
   LESSON_READING_OWN_PLACES = True      # 旅程の設定の地名も読みカードにする（trip.toml の地名を見せたくないなら False）
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
