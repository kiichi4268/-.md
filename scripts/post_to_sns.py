#!/usr/bin/env python3
"""posts/ 内の最新Markdown投稿データを読み込み、Meta Graph API で Instagram へ自動投稿する。

必要な環境変数:
  META_ACCESS_TOKEN   Instagram投稿権限を持つアクセストークン（長期トークン推奨）
  IG_USER_ID          Instagram ビジネス/クリエイターアカウントのID

任意の環境変数:
  GRAPH_API_VERSION   Graph API のバージョン（既定: v23.0）
  POST_FILE           投稿するファイルを明示指定（未指定なら posts/ 内の最新ファイル）
  DEFAULT_IMAGE_URL   投稿ファイルに画像URLが無い場合に使う画像URL
  DRY_RUN             "true" の場合、APIを呼ばずに投稿内容のみ表示

投稿ファイルの形式（例: posts/2026-10-10_バドミントン_中野.md）:
  ---
  image_url: https://example.com/image.jpg
  # または image_urls: https://example.com/1.jpg, https://example.com/2.jpg
  ---
  ### 📝 Instagram キャプション案
  （この見出しから次の ### 見出しまでがキャプションになる）
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

POSTS_DIR = Path(__file__).resolve().parent.parent / "posts"
GRAPH_BASE = "https://graph.facebook.com"
CAPTION_MAX_LEN = 2200
HASHTAG_MAX = 30  # Instagram の上限
CAPTION_HEADING = re.compile(r"^#{2,4}\s*.*Instagram\s*キャプション.*$", re.MULTILINE)
DATE_PREFIX = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def find_latest_post(posts_dir: Path) -> Path:
    """ファイル名先頭の日付（YYYY-MM-DD）が最新のものを選ぶ。日付が無ければ更新日時で比較。"""
    files = [p for p in posts_dir.glob("*.md") if p.name.lower() != "readme.md"]
    if not files:
        raise FileNotFoundError(f"{posts_dir} に投稿用のMarkdownファイルがありません")

    def sort_key(p: Path):
        m = DATE_PREFIX.match(p.name)
        return (m.group(1) if m else "", p.stat().st_mtime)

    return max(files, key=sort_key)


def parse_front_matter(text: str):
    """先頭の --- で囲まれた key: value 形式のメタデータを読み取る。"""
    meta = {}
    if not text.startswith("---"):
        return meta, text
    end = text.find("\n---", 3)
    if end == -1:
        return meta, text
    for line in text[3:end].splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip().strip("'\"")
    body = text[end + 4:].lstrip("\n")
    return meta, body


def extract_caption(body: str) -> str:
    """「Instagram キャプション」見出し配下を抜き出す。見出しが無ければ本文全体を使う。"""
    m = CAPTION_HEADING.search(body)
    if m:
        rest = body[m.end():]
        nxt = re.search(r"^#{1,3}\s", rest, re.MULTILINE)
        section = rest[: nxt.start()] if nxt else rest
    else:
        section = body

    lines = []
    for line in section.splitlines():
        if line.strip().startswith("```"):
            continue  # コードフェンスは除去
        lines.append(line.rstrip())
    caption = "\n".join(lines).strip()
    caption = re.sub(r"\n{3,}", "\n\n", caption)
    return caption


def validate_caption(caption: str) -> None:
    if not caption:
        raise ValueError("キャプションが空です")
    if len(caption) > CAPTION_MAX_LEN:
        raise ValueError(f"キャプションが{CAPTION_MAX_LEN}文字を超えています（{len(caption)}文字）")
    hashtags = re.findall(r"#[^\s#]+", caption)
    if len(hashtags) > HASHTAG_MAX:
        raise ValueError(f"ハッシュタグが{HASHTAG_MAX}個を超えています（{len(hashtags)}個）")
    if len(hashtags) > 15:
        print(f"⚠ ハッシュタグが{len(hashtags)}個あります（運用ルールは最大10〜15個）", file=sys.stderr)


def get_image_urls(meta: dict) -> list:
    raw = meta.get("image_urls") or meta.get("image_url") or os.environ.get("DEFAULT_IMAGE_URL", "")
    urls = [u.strip() for u in raw.split(",") if u.strip()]
    if not urls:
        raise ValueError("画像URLがありません（フロントマターの image_url / image_urls、または DEFAULT_IMAGE_URL を設定してください）")
    if len(urls) > 10:
        raise ValueError("カルーセル投稿の画像は最大10枚です")
    return urls


class GraphClient:
    def __init__(self, token: str, ig_user_id: str, version: str):
        self.token = token
        self.ig_user_id = ig_user_id
        self.base = f"{GRAPH_BASE}/{version}"

    def _request(self, method: str, path: str, params: dict) -> dict:
        params = {**params, "access_token": self.token}
        data = urllib.parse.urlencode(params).encode()
        url = f"{self.base}/{path}"
        if method == "GET":
            req = urllib.request.Request(f"{url}?{data.decode()}", method="GET")
        else:
            req = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            raise RuntimeError(f"Graph API エラー ({e.code}) {method} {path}: {detail}") from None

    def create_container(self, **params) -> str:
        return self._request("POST", f"{self.ig_user_id}/media", params)["id"]

    def wait_until_ready(self, container_id: str, timeout_sec: int = 300) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            status = self._request("GET", container_id, {"fields": "status_code"}).get("status_code")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"メディアコンテナの処理に失敗しました: {container_id} ({status})")
            time.sleep(5)
        raise TimeoutError(f"メディアコンテナの処理がタイムアウトしました: {container_id}")

    def publish(self, creation_id: str) -> str:
        return self._request("POST", f"{self.ig_user_id}/media_publish", {"creation_id": creation_id})["id"]

    def post(self, image_urls: list, caption: str) -> str:
        if len(image_urls) == 1:
            container = self.create_container(image_url=image_urls[0], caption=caption)
        else:
            children = []
            for url in image_urls:
                child = self.create_container(image_url=url, is_carousel_item="true")
                self.wait_until_ready(child)
                children.append(child)
            container = self.create_container(media_type="CAROUSEL", children=",".join(children), caption=caption)
        self.wait_until_ready(container)
        return self.publish(container)


def main() -> int:
    parser = argparse.ArgumentParser(description="posts/ の最新投稿を Instagram へ投稿する")
    parser.add_argument("--file", default=os.environ.get("POST_FILE") or None, help="投稿するMarkdownファイル")
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN", "").lower() == "true",
                        help="APIを呼ばずに投稿内容を表示する")
    args = parser.parse_args()

    post_file = Path(args.file) if args.file else find_latest_post(POSTS_DIR)
    print(f"📄 投稿ファイル: {post_file}")

    meta, body = parse_front_matter(post_file.read_text(encoding="utf-8"))
    caption = extract_caption(body)
    validate_caption(caption)
    image_urls = get_image_urls(meta)

    print(f"🖼  画像: {', '.join(image_urls)}")
    print("📝 キャプション:\n" + "-" * 40 + f"\n{caption}\n" + "-" * 40)

    if args.dry_run:
        print("DRY_RUN のため投稿は行いません")
        return 0

    token = os.environ.get("META_ACCESS_TOKEN")
    ig_user_id = os.environ.get("IG_USER_ID")
    if not token or not ig_user_id:
        print("META_ACCESS_TOKEN と IG_USER_ID を設定してください", file=sys.stderr)
        return 1

    client = GraphClient(token, ig_user_id, os.environ.get("GRAPH_API_VERSION", "v23.0"))
    media_id = client.post(image_urls, caption)
    print(f"✅ Instagram へ投稿しました (media_id: {media_id})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
