#!/usr/bin/env python3
"""posts/ 内の投稿データ（Markdown）を読み込み、Instagram と Threads へ自動投稿する。

対応する投稿の種類:
  - 画像1枚 / カルーセル（2〜10枚）  … フロントマターの image_url / image_urls
  - リール（動画）                     … フロントマターの video（リポジトリ内のパス）または video_url（公開URL）

必要な環境変数:
  META_ACCESS_TOKEN   Instagram投稿権限を持つアクセストークン（長期トークン推奨）
  IG_USER_ID          Instagram ビジネス/クリエイターアカウントのID

任意の環境変数:
  THREADS_ACCESS_TOKEN  Threads 投稿用アクセストークン。設定されていれば Threads にも同時投稿する
  THREADS_USER_ID       Threads ユーザーID（未指定ならトークンから自動取得）
  GRAPH_API_VERSION     Graph API のバージョン（既定: v23.0）
  THREADS_API_VERSION   Threads API のバージョン（既定: v1.0）
  POST_FILE             投稿するファイルを明示指定（改行区切りで複数可。未指定なら posts/ 内の最新ファイル）
  DEFAULT_IMAGE_URL     投稿ファイルに画像・動画の指定が無い場合に使う画像URL
  DRY_RUN               "true" の場合、APIを呼ばずに投稿内容のみ表示（動画の変換は行う）
  GITHUB_STEP_SUMMARY   GitHub Actions 上では投稿結果の表をジョブのサマリーに書き出す

検証だけ行う（PR 時のチェック用。API・動画変換・ネットワークを使わない）:
  python scripts/post_to_sns.py --check posts/a.md posts/b.md

投稿ファイルの形式（例: posts/2025-08-16_バドミントン_イベントレポート.md）:
  ---
  video: videos/0817.mov          # リール投稿（リポジトリ内の動画ファイル）
  # video_url: https://...mp4     # 公開URLの動画を使う場合（Threads にもそのまま使われる）
  # image_url: https://example.com/image.jpg
  # image_urls: https://example.com/1.jpg, https://example.com/2.jpg
  # platforms: instagram, threads  # 投稿先（既定: 両方。Threads はトークンがある場合のみ）
  # reel_fit: pad                 # pad=9:16にぼかし背景で収める（既定） / none=変換しない
  ---
  ### 📝 Instagram キャプション案
  （この見出しから次の ### 見出しまでが Instagram のキャプションになる）

  ### 🧵 Threads 投稿文
  （この見出しから次の ### 見出しまでが Threads の本文になる。無ければキャプションから自動生成）
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = REPO_ROOT / "posts"
GRAPH_BASE = "https://graph.facebook.com"
RUPLOAD_BASE = "https://rupload.facebook.com/ig-api-upload"
THREADS_BASE = "https://graph.threads.net"
CAPTION_MAX_LEN = 2200
THREADS_MAX_LEN = 500
HASHTAG_MAX = 30  # Instagram の上限
PLACEHOLDER = "【要編集】"  # 自動生成された下書きの未編集箇所
CAPTION_HEADING = re.compile(r"^#{2,4}\s*.*Instagram\s*キャプション.*$", re.MULTILINE)
THREADS_HEADING = re.compile(r"^#{2,4}\s*.*Threads\s*投稿.*$", re.MULTILINE)
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
        value = re.sub(r"\s+#.*$", "", value.strip())  # 行末コメントを除去
        meta[key.strip()] = value.strip().strip("'\"")
    body = text[end + 4:].lstrip("\n")
    return meta, body


def clean_text(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines() if not ln.strip().startswith("```")]  # コードフェンスは除去
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def extract_section(body: str, heading: re.Pattern) -> str:
    """見出し配下（次の ###以上の見出しまで）を抜き出す。見出しが無ければ空文字。"""
    m = heading.search(body)
    if not m:
        return ""
    rest = body[m.end():]
    nxt = re.search(r"^#{1,3}\s", rest, re.MULTILINE)
    return clean_text(rest[: nxt.start()] if nxt else rest)


def remove_section(body: str, heading: re.Pattern) -> str:
    """見出しとその配下（次の ###以上の見出しまで）を取り除く。"""
    m = heading.search(body)
    if not m:
        return body
    nxt = re.search(r"^#{1,3}\s", body[m.end():], re.MULTILINE)
    return body[: m.start()] + (body[m.end() + nxt.start():] if nxt else "")


def extract_caption(body: str) -> str:
    """「Instagram キャプション」見出し配下を抜き出す。見出しが無ければ本文全体を使う。"""
    return extract_section(body, CAPTION_HEADING) or clean_text(body)


def build_threads_text(body: str, caption: str) -> str:
    """Threads 用の本文。専用セクションが無ければキャプションからハッシュタグ行を除いて作る。"""
    text = extract_section(body, THREADS_HEADING)
    if not text:
        lines = [ln for ln in caption.splitlines()
                 if not ln.strip().startswith("【ハッシュタグ】") and not re.fullmatch(r"(#[^\s#]+\s*)+", ln.strip())]
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if len(text) > THREADS_MAX_LEN:
        text = text[: THREADS_MAX_LEN - 1].rstrip() + "…"
    return text


def validate_caption(caption: str) -> None:
    if not caption:
        raise ValueError("キャプションが空です")
    if PLACEHOLDER in caption:
        raise ValueError(f"キャプションに未編集の「{PLACEHOLDER}」が残っています")
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
        raise ValueError("画像・動画の指定がありません（フロントマターの video / video_url / image_url / image_urls、"
                         "または DEFAULT_IMAGE_URL を設定してください）")
    if len(urls) > 10:
        raise ValueError("カルーセル投稿の画像は最大10枚です")
    return urls


def get_platforms(meta: dict) -> list:
    raw = meta.get("platforms") or "instagram, threads"
    platforms = [p.strip().lower() for p in raw.split(",") if p.strip()]
    unknown = set(platforms) - {"instagram", "threads"}
    if unknown:
        raise ValueError(f"未対応の投稿先です: {', '.join(sorted(unknown))}")
    return platforms


# ---------------------------------------------------------------- 動画の変換

def _ffprobe(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout
    return json.loads(out)


def prepare_reel_video(src: Path, out_dir: Path, fit: str = "pad") -> Path:
    """Reels / Threads の仕様（H.264・横幅1920px以下・9:16推奨）に合わせて MP4 へ変換する。

    横長動画はぼかし背景付きで 1080x1920 に収める。HDR（HLG/PQ）はSDRへトーンマップする。
    """
    if fit == "none":
        return src
    if not shutil.which("ffmpeg"):
        raise RuntimeError("動画の変換に ffmpeg が必要です（reel_fit: none で変換を無効化できます）")

    info = _ffprobe(src)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    duration = float(info["format"].get("duration", 0))
    if duration and not 3 <= duration <= 900:
        raise ValueError(f"リール動画は3秒〜15分にしてください（{duration:.1f}秒）")

    tonemap = ""
    if video.get("color_transfer") in ("arib-std-b67", "smpte2084"):
        tonemap = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
                   "tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,")
    graph = (
        f"[0:v]{tonemap}format=yuv420p,split=2[a][b];"
        "[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
        "boxblur=40:3,eq=brightness=-0.08[bg];"
        "[b]scale=1080:1920:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2[fg];"
        "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]"
    )
    dst = out_dir / f"{src.stem}_reel.mp4"
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", str(src), "-filter_complex", graph, "-map", "[v]"]
    if has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-profile:v", "high",
            "-r", "30", "-g", "60", "-movflags", "+faststart", str(dst)]
    print(f"🎞  動画を変換中: {src.name} → {dst.name}（1080x1920 / H.264）")
    subprocess.run(cmd, check=True)
    return dst


# ---------------------------------------------------------------- API クライアント

# Graph API / Threads API のエラーコード → 対処のヒント
ERROR_HINTS = {
    190: "アクセストークンが無効または期限切れです。トークンを再発行して Secrets を更新してください",
    102: "セッションが無効です。トークンを再発行してください",
    10: "権限が不足しています。アプリの権限（instagram_content_publish 等）を確認してください",
    200: "権限が不足しています。アプリの権限とアカウントの連携を確認してください",
    4: "API の呼び出し回数の上限に達しました。時間をおいて再実行してください",
    17: "API の呼び出し回数の上限に達しました。時間をおいて再実行してください",
    32: "API の呼び出し回数の上限に達しました。時間をおいて再実行してください",
    613: "API の呼び出し回数の上限に達しました。時間をおいて再実行してください",
    9004: "画像・動画のURLを Meta が取得できませんでした。公開URL（ログイン不要・JPEG）か確認してください",
    9007: "メディアの準備ができていません。少し待ってから再実行してください",
    36003: "画像の縦横比が対応範囲外です（4:5〜1.91:1）。画像をトリミングしてください",
    36001: "画像の解像度が対応範囲外です。画像サイズを確認してください",
}
RETRY_STATUS = {429, 500, 502, 503, 504}


def describe_api_error(code: int, detail: str) -> str:
    try:
        err = json.loads(detail).get("error", {})
    except (json.JSONDecodeError, AttributeError):
        return detail
    parts = [err.get("message", detail)]
    sub = err.get("error_subcode")
    hint = ERROR_HINTS.get(err.get("code")) or ERROR_HINTS.get(sub)
    if err.get("error_user_msg"):
        parts.append(err["error_user_msg"])
    if hint:
        parts.append(f"💡 {hint}")
    ids = f"code={err.get('code')}" + (f", subcode={sub}" if sub else "")
    return f"{' / '.join(parts)} ({ids})"


def http_request(method: str, url: str, params: dict = None, data: bytes = None, headers: dict = None,
                 timeout: int = 60, retries: int = 2) -> dict:
    """API を呼ぶ。429・5xx・通信エラーは retries 回まで指数バックオフで再試行する。"""
    params = params or {}
    if method == "GET":
        req_url, body = f"{url}?{urllib.parse.urlencode(params)}", None
    elif data is not None:
        req_url, body = url, data
    else:
        req_url, body = url, urllib.parse.urlencode(params).encode()
    path = urllib.parse.urlparse(url).path
    for attempt in range(retries + 1):
        req = urllib.request.Request(req_url, data=body, method=method)
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            if e.code in RETRY_STATUS and attempt < retries:
                wait = 2 ** (attempt + 1)
                print(f"↻ API エラー ({e.code}) {method} {path}。{wait}秒後に再試行します", file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f"API エラー ({e.code}) {method} {path}: {describe_api_error(e.code, detail)}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                wait = 2 ** (attempt + 1)
                print(f"↻ 通信エラー {method} {path}: {e}。{wait}秒後に再試行します", file=sys.stderr)
                time.sleep(wait)
                continue
            raise RuntimeError(f"通信エラー {method} {path}: {e}") from None


class GraphClient:
    """Instagram Graph API（Instagram API with Facebook Login）"""

    def __init__(self, token: str, ig_user_id: str, version: str):
        self.token = token
        self.ig_user_id = ig_user_id
        self.version = version
        self.base = f"{GRAPH_BASE}/{version}"

    def _request(self, method: str, path: str, params: dict, retries: int = 2) -> dict:
        return http_request(method, f"{self.base}/{path}", {**params, "access_token": self.token}, retries=retries)

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
        # 公開は再試行しない（応答だけ失われた場合に二重投稿になるため）
        return self._request("POST", f"{self.ig_user_id}/media_publish", {"creation_id": creation_id}, retries=0)["id"]

    def media_url(self, media_id: str) -> str:
        return self._request("GET", media_id, {"fields": "media_url"}).get("media_url", "")

    def permalink(self, media_id: str) -> str:
        try:
            return self._request("GET", media_id, {"fields": "permalink"}).get("permalink", "")
        except RuntimeError:
            return ""

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

    def post_reel(self, caption: str, video_file: Path = None, video_url: str = None) -> str:
        """リール投稿。公開URLが無い場合はローカルファイルを resumable upload で直接アップロードする。"""
        params = {"media_type": "REELS", "caption": caption, "share_to_feed": "true"}
        if video_url:
            container = self.create_container(video_url=video_url, **params)
        else:
            container = self.create_container(upload_type="resumable", **params)
            data = video_file.read_bytes()
            print(f"⬆  動画をアップロード中（{len(data) / 1024 / 1024:.1f}MB）")
            res = http_request(
                "POST", f"{RUPLOAD_BASE}/{self.version}/{container}", data=data, timeout=600,
                headers={"Authorization": f"OAuth {self.token}", "offset": "0", "file_size": str(len(data))},
            )
            if not res.get("success"):
                raise RuntimeError(f"動画のアップロードに失敗しました: {res}")
        self.wait_until_ready(container, timeout_sec=900)
        return self.publish(container)


class ThreadsClient:
    """Threads API（graph.threads.net）"""

    def __init__(self, token: str, user_id: str, version: str):
        self.token = token
        self.base = f"{THREADS_BASE}/{version}"
        self.user_id = user_id or self._request("GET", "me", {"fields": "id"})["id"]

    def _request(self, method: str, path: str, params: dict, retries: int = 2) -> dict:
        return http_request(method, f"{self.base}/{path}", {**params, "access_token": self.token}, retries=retries)

    def create_container(self, **params) -> str:
        return self._request("POST", f"{self.user_id}/threads", params)["id"]

    def wait_until_ready(self, container_id: str, timeout_sec: int = 300) -> None:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            res = self._request("GET", container_id, {"fields": "status,error_message"})
            status = res.get("status")
            if status == "FINISHED":
                return
            if status in ("ERROR", "EXPIRED"):
                raise RuntimeError(f"Threads コンテナの処理に失敗しました: {container_id} "
                                   f"({status} {res.get('error_message', '')})")
            time.sleep(5)
        raise TimeoutError(f"Threads コンテナの処理がタイムアウトしました: {container_id}")

    def publish(self, creation_id: str) -> str:
        # 公開は再試行しない（応答だけ失われた場合に二重投稿になるため）
        return self._request("POST", f"{self.user_id}/threads_publish", {"creation_id": creation_id}, retries=0)["id"]

    def permalink(self, media_id: str) -> str:
        try:
            return self._request("GET", media_id, {"fields": "permalink"}).get("permalink", "")
        except RuntimeError:
            return ""

    def post(self, text: str, image_urls: list = None, video_url: str = None) -> str:
        if video_url:
            container = self.create_container(media_type="VIDEO", video_url=video_url, text=text)
        elif image_urls and len(image_urls) == 1:
            container = self.create_container(media_type="IMAGE", image_url=image_urls[0], text=text)
        elif image_urls:
            children = []
            for url in image_urls:
                child = self.create_container(media_type="IMAGE", image_url=url, is_carousel_item="true")
                self.wait_until_ready(child)
                children.append(child)
            container = self.create_container(media_type="CAROUSEL", children=",".join(children), text=text)
        else:
            container = self.create_container(media_type="TEXT", text=text)
        self.wait_until_ready(container, timeout_sec=900 if video_url else 300)
        return self.publish(container)


# ---------------------------------------------------------------- 検証

def check_post(post_file: Path) -> tuple:
    """投稿ファイルを API を使わずに検証する。(errors, warnings) を返す。"""
    errors, warnings = [], []
    meta, body = parse_front_matter(post_file.read_text(encoding="utf-8"))
    caption = extract_caption(body)
    try:
        validate_caption(caption)
    except ValueError as e:
        errors.append(str(e))
    threads_raw = extract_section(body, THREADS_HEADING)
    if PLACEHOLDER in threads_raw:
        errors.append(f"Threads 投稿文に未編集の「{PLACEHOLDER}」が残っています")
    if len(threads_raw) > THREADS_MAX_LEN:
        warnings.append(f"Threads 投稿文が{len(threads_raw)}文字です（{THREADS_MAX_LEN}文字で切り詰められます）")
    for key, value in meta.items():
        if PLACEHOLDER in value:
            errors.append(f"フロントマター `{key}` が未編集です（「{PLACEHOLDER}」）")
    try:
        get_platforms(meta)
    except ValueError as e:
        errors.append(str(e))

    if meta.get("video"):
        if not (REPO_ROOT / meta["video"]).is_file():
            errors.append(f"動画ファイルが見つかりません: {meta['video']}")
    elif not meta.get("video_url"):
        raw = meta.get("image_urls") or meta.get("image_url") or ""
        urls = [u.strip() for u in raw.split(",") if u.strip()]
        if not urls:
            if os.environ.get("DEFAULT_IMAGE_URL"):
                warnings.append("画像の指定が無いため DEFAULT_IMAGE_URL の画像で投稿されます")
            else:
                errors.append("画像・動画の指定がありません（image_url / image_urls / video / video_url）")
        if len(urls) > 10:
            errors.append("カルーセル投稿の画像は最大10枚です")
        for u in urls:
            if not u.startswith("https://"):
                errors.append(f"画像URLは https:// で始まる公開URLにしてください: {u}")
            elif urllib.parse.urlparse(u).hostname in ("example.com", "www.example.com"):
                errors.append(f"画像URLがサンプル（example.com）のままです: {u}")

    if PLACEHOLDER in remove_section(remove_section(body, CAPTION_HEADING), THREADS_HEADING):
        warnings.append(f"本文の他のセクション（企画意図・台本など）に「{PLACEHOLDER}」が残っています（投稿はされません）")
    return errors, warnings


def run_checks(files: list) -> int:
    failed = 0
    rows = []
    for f in files:
        errors, warnings = check_post(f)
        status = "❌" if errors else ("⚠️" if warnings else "✅")
        print(f"{status} {f.relative_to(REPO_ROOT) if f.is_relative_to(REPO_ROOT) else f}")
        for e in errors:
            print(f"   ❌ {e}")
            print(f"::error file={f.relative_to(REPO_ROOT) if f.is_relative_to(REPO_ROOT) else f}::{e}")
        for w in warnings:
            print(f"   ⚠ {w}")
        rows.append((f.name, status, "<br>".join(errors + warnings) or "問題なし"))
        failed += bool(errors)
    write_summary("## 🔎 投稿ファイルの事前チェック", ["ファイル", "結果", "内容"], rows)
    return 1 if failed else 0


def write_summary(title: str, header: list, rows: list) -> None:
    """GitHub Actions のジョブサマリーに表を書き出す（ローカル実行では何もしない）。"""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", "<br>")  # noqa: E731
    lines = [title, "", "| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(esc(c) for c in row) + " |" for row in rows]
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n\n")


# ---------------------------------------------------------------- 投稿

def post_one(post_file: Path, dry_run: bool) -> list:
    """1ファイルを投稿し、結果の行 [(投稿先, 結果, ID/リンク)] を返す。失敗時は例外に結果を添える。"""
    print(f"📄 投稿ファイル: {post_file}")
    meta, body = parse_front_matter(post_file.read_text(encoding="utf-8"))
    caption = extract_caption(body)
    validate_caption(caption)
    threads_text = build_threads_text(body, caption)
    platforms = get_platforms(meta)

    video_path = REPO_ROOT / meta["video"] if meta.get("video") else None
    video_url = meta.get("video_url")
    is_reel = bool(video_path or video_url)
    image_urls = [] if is_reel else get_image_urls(meta)
    if video_path and not video_path.is_file():
        raise FileNotFoundError(f"動画ファイルが見つかりません: {meta['video']}")

    print(f"📣 投稿先: {', '.join(platforms)}")
    if is_reel:
        print(f"🎬 リール動画: {video_url or meta['video']}")
    else:
        print(f"🖼  画像: {', '.join(image_urls)}")
    print("📝 Instagram キャプション:\n" + "-" * 40 + f"\n{caption}\n" + "-" * 40)
    if "threads" in platforms:
        print(f"🧵 Threads 本文（{len(threads_text)}文字）:\n" + "-" * 40 + f"\n{threads_text}\n" + "-" * 40)

    results = []
    with tempfile.TemporaryDirectory() as tmp:
        upload_file = None
        if video_path and not video_url:
            upload_file = prepare_reel_video(video_path, Path(tmp), meta.get("reel_fit", "pad"))

        if dry_run:
            print("DRY_RUN のため投稿は行いません")
            return [(p, "🧪 dry run", "") for p in platforms]

        ig = None
        ig_media_id = None
        if "instagram" in platforms:
            token = os.environ.get("META_ACCESS_TOKEN")
            ig_user_id = os.environ.get("IG_USER_ID")
            if not token or not ig_user_id:
                raise RuntimeError("META_ACCESS_TOKEN と IG_USER_ID を設定してください")
            ig = GraphClient(token, ig_user_id, os.environ.get("GRAPH_API_VERSION", "v23.0"))
            try:
                if is_reel:
                    ig_media_id = ig.post_reel(caption, video_file=upload_file, video_url=video_url)
                else:
                    ig_media_id = ig.post(image_urls, caption)
            except Exception as e:
                results.append(("instagram", "❌ 失敗", str(e)))
                raise PostError(str(e), results) from None
            link = ig.permalink(ig_media_id)
            print(f"✅ Instagram へ{'リールを' if is_reel else ''}投稿しました (media_id: {ig_media_id}) {link}")
            results.append(("instagram", "✅ 投稿", link or ig_media_id))

        if "threads" in platforms:
            threads_token = os.environ.get("THREADS_ACCESS_TOKEN")
            if not threads_token:
                print("⚠ THREADS_ACCESS_TOKEN が未設定のため Threads への投稿はスキップします", file=sys.stderr)
                results.append(("threads", "⏭ スキップ", "THREADS_ACCESS_TOKEN 未設定"))
            else:
                try:
                    th_video_url = video_url
                    if is_reel and not th_video_url:
                        # Threads は公開URLの動画しか受け付けないため、Instagram に投稿した動画のURLを使う
                        th_video_url = ig.media_url(ig_media_id) if ig_media_id else ""
                        if not th_video_url:
                            raise RuntimeError("Threads 用の動画URLがありません（video_url を指定するか、"
                                               "Instagram にも同時投稿してください）")
                    threads = ThreadsClient(threads_token, os.environ.get("THREADS_USER_ID", ""),
                                            os.environ.get("THREADS_API_VERSION", "v1.0"))
                    th_id = threads.post(threads_text, image_urls=image_urls, video_url=th_video_url)
                    link = threads.permalink(th_id)
                    print(f"✅ Threads へ投稿しました (id: {th_id}) {link}")
                    results.append(("threads", "✅ 投稿", link or th_id))
                except Exception as e:  # noqa: BLE001  Instagram 側の成功は維持しつつ失敗を通知する
                    print(f"❌ Threads への投稿に失敗しました: {e}", file=sys.stderr)
                    results.append(("threads", "❌ 失敗", str(e)))
                    raise PostError(str(e), results) from None
    return results


class PostError(RuntimeError):
    def __init__(self, message: str, results: list):
        super().__init__(message)
        self.results = results


# ---------------------------------------------------------------- メイン

def main() -> int:
    parser = argparse.ArgumentParser(description="posts/ の投稿を Instagram・Threads へ投稿する")
    parser.add_argument("files", nargs="*", help="投稿（または --check で検証）するMarkdownファイル")
    parser.add_argument("--file", default=os.environ.get("POST_FILE") or None,
                        help="投稿するMarkdownファイル（改行区切りで複数可）")
    parser.add_argument("--dry-run", action="store_true", default=os.environ.get("DRY_RUN", "").lower() == "true",
                        help="APIを呼ばずに投稿内容を表示する")
    parser.add_argument("--check", action="store_true", help="API・動画変換を使わずに投稿ファイルを検証するだけ")
    args = parser.parse_args()

    names = list(args.files) or [ln.strip() for ln in (args.file or "").splitlines() if ln.strip()]
    files = [Path(n) if Path(n).is_absolute() else REPO_ROOT / n for n in names] or [find_latest_post(POSTS_DIR)]

    if args.check:
        return run_checks(files)

    rows, failed = [], False
    for i, post_file in enumerate(files):
        if i:
            print()
        try:
            results = post_one(post_file, args.dry_run)
        except PostError as e:
            results, failed = e.results, True
            print(f"❌ {post_file.name}: {e}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001  1件の失敗で残りの投稿を止めない
            results, failed = [("-", "❌ 失敗", str(e))], True
            print(f"❌ {post_file.name}: {e}", file=sys.stderr)
        rows += [(post_file.name, *r) for r in results]
    write_summary("## 📣 SNS 自動投稿の結果" + ("（DRY RUN）" if args.dry_run else ""),
                  ["ファイル", "投稿先", "結果", "ID / リンク / エラー"], rows)
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
