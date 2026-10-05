#!/usr/bin/env python3
"""videos/ 内の動画を自動検出し、まだ投稿データが無い動画ごとにリール投稿の下書きを posts/ に作成する。

- 既存の posts/*.md のフロントマター `video:` で参照済みの動画はスキップする
- 下書きには「【要編集】」が入っており、編集せずにマージしても post_to_sns.py が投稿を拒否する
- 作成したファイルは標準出力と GITHUB_OUTPUT（created=改行区切り）に出力する

使い方:
  python scripts/create_reel_posts.py            # 下書きを作成
  python scripts/create_reel_posts.py --dry-run  # 作成予定の一覧だけ表示
"""

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from post_to_sns import PLACEHOLDER, parse_front_matter  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
VIDEOS_DIR = REPO_ROOT / "videos"
POSTS_DIR = REPO_ROOT / "posts"
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}

TEMPLATE = """---
# videos/ の動画から自動生成されたリール投稿の下書きです。
# 「{ph}」をすべて書き換えてからマージしてください（残っていると投稿されません）。
video: {video}
platforms: instagram, threads
---

# 🎬 {date} リール投稿（{name}）

## 動画情報
- ファイル: `{video}`
- 長さ: {duration}
- 撮影日時: {shot}

## 企画意図・ターゲット訴求ポイント
- **ターゲット**: 20〜30代の社会人、一人参加に不安がある初心者
- **訴求ポイント**: {ph}
- **ゴール**: プロフィールリンクから公式LINE登録・参加予約フォームへ

### 📝 Instagram キャプション案
【タイトル・フック】
{ph}

【本文】
{ph}

【申込方法】
1️⃣ プロフィール（@macachette_sports）のリンクをタップ
2️⃣ フォームに入力して完了！
公式LINEに登録すると、次回のイベント情報もいち早く届きます📩

【ハッシュタグ】
#社会人サークル #東京スポーツサークル #一人参加OK #初心者歓迎 #運動不足解消 {ph}

### 🧵 Threads 投稿文
{ph}
"""


def referenced_videos() -> set:
    refs = set()
    for p in POSTS_DIR.glob("*.md"):
        meta, _ = parse_front_matter(p.read_text(encoding="utf-8"))
        if meta.get("video"):
            refs.add(Path(meta["video"]).as_posix())
    return refs


def probe(path: Path) -> dict:
    if not shutil.which("ffprobe"):
        return {}
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-of", "json", str(path)],
                             check=True, capture_output=True, text=True).stdout
        return json.loads(out).get("format", {})
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return {}


def shot_datetime(fmt: dict):
    raw = (fmt.get("tags") or {}).get("creation_time")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(dt.timezone(dt.timedelta(hours=9)))
    except ValueError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="videos/ の動画からリール投稿の下書きを作成する")
    parser.add_argument("--dry-run", action="store_true", help="ファイルを作成せず一覧だけ表示する")
    args = parser.parse_args()

    videos = sorted(p for p in VIDEOS_DIR.glob("*") if p.suffix.lower() in VIDEO_EXTS) if VIDEOS_DIR.is_dir() else []
    refs = referenced_videos()
    created = []
    for video in videos:
        rel = video.relative_to(REPO_ROOT).as_posix()
        if rel in refs:
            print(f"⏭  投稿データ作成済み: {rel}")
            continue

        fmt = probe(video)
        shot = shot_datetime(fmt)
        date = (shot or dt.datetime.now(dt.timezone(dt.timedelta(hours=9)))).strftime("%Y-%m-%d")
        name = re.sub(r"[^\w\-]+", "_", video.stem).strip("_") or "video"
        post = POSTS_DIR / f"{date}_リール_{name}.md"
        duration = f"{float(fmt['duration']):.1f}秒" if fmt.get("duration") else "不明"

        print(f"🆕 {rel} → {post.relative_to(REPO_ROOT).as_posix()}")
        if not args.dry_run:
            post.write_text(TEMPLATE.format(
                ph=PLACEHOLDER, video=rel, date=date, name=video.name, duration=duration,
                shot=shot.strftime("%Y-%m-%d %H:%M (JST)") if shot else "不明",
            ), encoding="utf-8")
        created.append(post.relative_to(REPO_ROOT).as_posix())

    if not created:
        print("新しい動画はありません")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("created<<EOF\n" + "\n".join(created) + "\nEOF\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
