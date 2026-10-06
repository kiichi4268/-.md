# data/ — 集客コンテンツの元データ

`events.csv` は「残り◯枠」「初参加◯割・一人参加◯%」などを投稿に差し込むための**唯一の元データ**です。
ここに無い数値は投稿に書きません（品質管理AIが創作を差し戻します）。

## events.csv の列

| 列 | 内容 | 例 |
|---|---|---|
| `event_id` | `日付_種目_地域`（英数字） | `2026-10-10_badminton_nakano` |
| `date` / `start` / `end` | 開催日・開始・終了 | `2026-10-10` / `18:30` / `20:30` |
| `sport` / `area` / `venue` | 種目・地域・会場（公開してよい表記） | `バドミントン` / `中野` / `中野区内の体育館` |
| `fee` | 参加費（円） | `1500` |
| `capacity_min` / `capacity_max` | 最少催行人数・定員 | `20` / `40` |
| `applied` | 現在の申込数（エルメのフォーム回答数から転記） | `14` |
| `first_timer` | 申込者のうち初参加の人数 | `6` |
| `solo` | 申込者のうち一人参加の人数 | `11` |
| `voices` | 参加者の声（**掲載許可を得たもののみ**。複数は ` / ` 区切り） | `一人でも気づいたら輪に入れてた（20代・初参加）` |
| `status` | `open` / `closed` / `canceled` | `open` |
| `note` | メモ（投稿には使わない） | |

## 投稿で使う計算値

`prompts/fill_capacity_template.md` の差し込み変数は次の式で計算します（`scripts/generate_content.py` で自動計算予定）。

| 変数 | 計算式 |
|---|---|
| `{{remaining}}` | `capacity_max - applied` |
| `{{fill_rate}}` | `applied / capacity_max`（%） |
| `{{first_timer_pct}}` | `first_timer / applied`（%。申込5名未満なら使わない） |
| `{{solo_pct}}` | `solo / applied`（%。申込5名未満なら使わない） |
| `{{days_left}}` | 開催日までの日数 |

## 更新のタイミング
- 申込が入るたびに `applied` / `first_timer` / `solo` を更新（エルメの回答フォームに「参加は初めてですか？」「お一人での参加ですか？」の設問を入れておく）
- フェーズ2以降、Google スプレッドシートからの自動取り込みも検討します
