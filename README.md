[【自動化システム】承認されたら全SNSへ自動投稿するプログラム（手足）.yaml](https://github.com/user-attachments/files/33019903/SNS.yaml)
[📄 scripts:post_to_sns.py のコード内容 以下がプログラムのコードです。.py](https://github.com/user-attachments/files/33019904/scripts.post_to_sns.py.py)

## 自動投稿の仕組み

| 流れ | 内容 |
|------|------|
| 1. 投稿データ作成 | `posts/YYYY-MM-DD_種目_場所.md` を作成してPR（承認待ち） |
| 2. マージ | `.github/workflows/auto-post.yml` → `scripts/post_to_sns.py` が Instagram と Threads に同時投稿 |

- **画像投稿**: フロントマター `image_url` / `image_urls`（公開URL）
- **リール投稿**: フロントマター `video: videos/xxx.mov`。投稿時に 1080x1920（9:16・H.264）へ自動変換し、Instagram へ直接アップロード。Threads には Instagram に投稿された動画のURLを使って投稿
- **動画の自動検出**: `videos/` に動画を追加して main にプッシュすると、`.github/workflows/detect-videos.yml` がリール投稿の下書きPRを動画ごとに作成

### 必要な設定（Settings → Secrets and variables → Actions）
| 名前 | 種類 | 内容 |
|------|------|------|
| `META_ACCESS_TOKEN` | Secret | Instagram 投稿用アクセストークン |
| `IG_USER_ID` | Secret | Instagram ビジネス/クリエイターアカウントID |
| `THREADS_ACCESS_TOKEN` | Secret | Threads 投稿用アクセストークン（未設定なら Threads はスキップ） |
| `THREADS_USER_ID` | Secret（任意） | Threads ユーザーID（未設定ならトークンから自動取得） |

動画の自動検出PRを使うには Settings → Actions → General の「Allow GitHub Actions to create and approve pull requests」を有効にしてください。
