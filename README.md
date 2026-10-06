[【自動化システム】承認されたら全SNSへ自動投稿するプログラム（手足）.yaml](https://github.com/user-attachments/files/33019903/SNS.yaml)
[📄 scripts:post_to_sns.py のコード内容 以下がプログラムのコードです。.py](https://github.com/user-attachments/files/33019904/scripts.post_to_sns.py.py)

# Macachette Sports SNS自動化・収益化システム

社会人スポーツイベント「Macachette Sports」の集客〜収益化を、9人のAI社員（プロンプト）と GitHub Actions で回す仕組みです。
目標は月20万円の固定収益（内訳とKPIは [`docs/monetization_plan.md`](docs/monetization_plan.md)）。

## 全体の流れ

```
data/events.csv（申込数・初参加・一人参加）
   ↓ scripts/generate_content.py（AI社員チェーン）
posts/*.md（IG/Threads/X/ストーリーズ/カルーセル/動画台本）  wordpress/ note/ docs/proposals/
   ↓ PR 作成 → validate-post.yml が事前チェック
   ↓ マージ → auto-post.yml が Instagram・Threads へ自動投稿
SNS → プロフィール → 公式LINE（エルメ）→ ボタン4タップ → デジタル参加証
   ↓ タグ集計を data/events.csv へ → 枠埋め投稿（A/B/C パターン）
```

## ディレクトリ

| 場所 | 内容 |
|---|---|
| 直下の `# 〜AI.md` ほか | AI社員9人の人格・役割の原本（＋沢田流の統括方針 `リポジトリ用（スポーツイベント）.md`） |
| `prompts/` | チェーン実行用プロンプト。担当と順序は [`prompts/agents.yml`](prompts/agents.yml)。枠埋めは [`fill_capacity_template.md`](prompts/fill_capacity_template.md) |
| `data/` | `events.csv`（集客の元データ。列の説明は [`data/README.md`](data/README.md)）、`affiliate_links.yml` |
| `scripts/` | 生成・投稿・検証スクリプト |
| `posts/` | SNS投稿データ（マージすると自動投稿） |
| `docs/` | [LINE（エルメ）受付仕様](docs/autosns_setup_guide.md)、[収益化プラン](docs/monetization_plan.md)、[協賛提案書テンプレート](docs/sponsor_proposal_template.md) |
| `videos/` | リール用の動画（追加すると投稿の下書きPRが自動で作られる） |

## コンテンツ生成（scripts/generate_content.py）

```bash
pip install pyyaml
# イベント告知一式
python scripts/generate_content.py --chain sns_set --event 2026-10-17_volleyball_shinjuku
# 枠埋め（充足率から A 安心訴求 / B 顔ぶれ・声 / C 残り枠 を自動判定）
python scripts/generate_content.py --chain fill_capacity --event 2026-10-17_volleyball_shinjuku
# ブログ・note・協賛提案・週次PDCA
python scripts/generate_wordpress_post.py --theme "都内バレーボールコートの抽選確率を上げるコツ"
python scripts/generate_content.py --chain note_hybrid --theme "一人参加で浮かないサークルの選び方"
python scripts/generate_content.py --chain sponsor_proposal --theme "スポーツ飲料ブランド向け サンプリング協賛"
python scripts/generate_content.py --chain weekly_review --metrics reports/metrics_2026-10-06.md
```

- **prompt モード（既定・無料）**: `outputs/prompts/` に1本のプロンプトができるので、Claude や ChatGPT に貼り付けて実行し、結果を表示された保存先に置く
- **api モード（任意）**: `--mode api` で工程ごとに Claude API を呼び、`outputs/drafts/` に保存（`pip install anthropic` と `ANTHROPIC_API_KEY` が必要・従量課金）
- ブログ記事は AI の出力を `wordpress/drafts/` に置いたあと `python scripts/generate_wordpress_post.py --finalize <ファイル>` でアフィリエイトリンク差し込み・PR表記・要確認チェック

## 自動投稿の仕組み

| 流れ | 内容 |
|------|------|
| 1. 投稿データ作成 | `posts/YYYY-MM-DD_種目_場所.md` を作成してPR（承認待ち） |
| 2. 事前チェック | `.github/workflows/validate-post.yml` が PR の時点で検証（API は呼ばない） |
| 3. マージ | `.github/workflows/auto-post.yml` → `scripts/post_to_sns.py` が Instagram と Threads に同時投稿。結果は Actions 実行画面の **Summary** に表で表示 |

- **画像投稿**: フロントマター `image_url` / `image_urls`（公開URL）
- **リール投稿**: フロントマター `video: videos/xxx.mov`。投稿時に 1080x1920（9:16・H.264）へ自動変換し、Instagram へ直接アップロード。Threads には Instagram に投稿された動画のURLを使って投稿
- **動画の自動検出**: `videos/` に動画を追加して main にプッシュすると、`.github/workflows/detect-videos.yml` がリール投稿の下書きPRを動画ごとに作成
- **複数投稿**: 1つのPRに複数の投稿ファイルがあれば、日付順にすべて投稿（1回のマージで5件まで）。1件が失敗しても残りは投稿し、最後にジョブを失敗として通知
- ⚠ **投稿済みのファイルを編集してマージすると、もう一度投稿されます**。投稿後の修正は Instagram・Threads 側で直接行ってください

### 事前チェックの内容（validate-post.yml）
- キャプション・Threads 投稿文・フロントマターに「【要編集】」が残っていないか（他のセクションに残っている場合は警告のみ）
- キャプション2,200字以内、ハッシュタグ30個以内（15個超で警告）、Threads 500字超で警告
- 画像URLが `https://` の公開URLか（`example.com` のサンプルURLはエラー）、カルーセル10枚以内、動画ファイルの存在
- スクリプトの構文と、生成チェーン（prompt モード）がすべて組み立てられるか

ローカルでも同じチェックができます: `python scripts/post_to_sns.py --check posts/xxx.md`

### トークンの期限監視（token-check.yml）
毎週月曜 9:07（JST）に Instagram・Threads のトークンを確認し、無効または残り14日未満ならジョブを失敗させて通知メールを送ります（手動実行も可）。

### 必要な設定（Settings → Secrets and variables → Actions）
| 名前 | 種類 | 内容 |
|------|------|------|
| `META_ACCESS_TOKEN` | Secret | Instagram 投稿用アクセストークン |
| `IG_USER_ID` | Secret | Instagram ビジネス/クリエイターアカウントID |
| `THREADS_ACCESS_TOKEN` | Secret | Threads 投稿用アクセストークン（未設定なら Threads はスキップ） |
| `THREADS_USER_ID` | Secret（任意） | Threads ユーザーID（未設定ならトークンから自動取得） |
| `META_APP_ID` / `META_APP_SECRET` | Secret（任意） | トークン確認（debug_token）をアプリトークンで行う場合 |
| `DEFAULT_IMAGE_URL` | Variable（任意） | 画像の指定が無い投稿に使う画像URL |
| `GRAPH_API_VERSION` | Variable（任意） | Graph API のバージョン（既定 v23.0） |
| `THREADS_TOKEN_EXPIRES_ON` | Variable（任意） | Threads トークンの期限（YYYY-MM-DD）。設定すると期限切れ前に通知 |

動画の自動検出PRを使うには Settings → Actions → General の「Allow GitHub Actions to create and approve pull requests」を有効にしてください。

### エラーが出たとき
Actions のログと Summary に、エラーの原因と対処のヒント（💡）が表示されます。

| よくあるエラー | 対処 |
|---|---|
| アクセストークンが無効または期限切れ（code=190） | トークンを再発行して `META_ACCESS_TOKEN` / `THREADS_ACCESS_TOKEN` を更新 |
| 画像・動画のURLを取得できない（9004） | 画像がログイン不要の公開URL（JPEG）か確認 |
| 画像の縦横比が対応範囲外（36003） | 4:5〜1.91:1 にトリミング |
| 呼び出し回数の上限（4 / 17 / 32 / 613） | 時間をおいて workflow_dispatch で再実行 |
| キャプションに「【要編集】」が残っている | PR で該当箇所を書き換える（事前チェックで検出されます） |

通信エラーや一時的なサーバーエラー（429・5xx）は自動で最大2回再試行します。二重投稿を防ぐため、最後の「公開」処理だけは再試行しません。
