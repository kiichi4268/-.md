#!/usr/bin/env python3
"""Sports Backstage（WordPress）向けのSEO記事を作る。

2つの使い方:
  1) 生成: テーマから wordpress_seo チェーンを実行する（prompt モード既定 / api モード可）
       python scripts/generate_wordpress_post.py --theme "都内バレーボールコートの抽選確率を上げるコツ"
       python scripts/generate_wordpress_post.py --theme "..." --mode api   # 生成後に自動で仕上げまで行う

  2) 仕上げ（--finalize）: AIの出力（### 📰 記事メタ情報 / ### 📝 記事本文）を WordPress 貼り付け用に整える
       python scripts/generate_wordpress_post.py --finalize wordpress/drafts/2026-10-06_xxx.md
     - 本文中の {{AFF:キー}} を data/affiliate_links.yml のリンクに置き換える
     - アフィリエイトリンクがあれば冒頭に PR 表記を入れる
     - 【要確認】【要編集】の残り、リンク数（3つまで）、未登録キーをチェックする

仕上げた Markdown は WordPress のブロックエディタに貼り付けると見出し・表がブロックに変換されます。
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
AFFILIATE_FILE = REPO_ROOT / "data" / "affiliate_links.yml"
PLACEHOLDER = "【要編集】"
MAX_AFF_LINKS = 3
PR_NOTICE = "※本記事はアフィリエイト広告（PR）を含みます。"
AFF_PATTERN = re.compile(r"\{\{AFF:(\w+)\}\}")


def load_links() -> dict:
    return yaml.safe_load(AFFILIATE_FILE.read_text(encoding="utf-8"))["links"]


def section(text: str, heading: str) -> str:
    m = re.search(rf"^###\s*{re.escape(heading)}.*$", text, re.MULTILINE)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^###\s", rest, re.MULTILINE)
    return (rest[: nxt.start()] if nxt else rest).strip()


def finalize(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    meta = section(text, "📰 記事メタ情報")
    body = section(text, "📝 記事本文")
    if not body:
        print("❌ 「### 📝 記事本文」が見つかりません", file=sys.stderr)
        return 1

    links = load_links()
    problems = []
    used = AFF_PATTERN.findall(body)
    if len(used) > MAX_AFF_LINKS:
        problems.append(f"アフィリエイトリンクが{len(used)}か所あります（{MAX_AFF_LINKS}か所まで）")

    def replace(m):
        key = m.group(1)
        link = links.get(key)
        if not link:
            problems.append(f"未登録のキーです: {key}（data/affiliate_links.yml に追加してください）")
            return m.group(0)
        if not link["url"] or PLACEHOLDER in link["url"]:
            problems.append(f"リンク未設定のキーです: {key}（data/affiliate_links.yml の url を設定してください）")
            return m.group(0)
        return f"[{link['label']}]({link['url']})"

    body = AFF_PATTERN.sub(replace, body)
    if used:
        body = f"{PR_NOTICE}\n\n{body}"

    for marker in ("【要確認", PLACEHOLDER):
        n = body.count(marker) + meta.count(marker)
        if n:
            problems.append(f"「{marker}…」が{n}か所残っています（公式情報で確認して書き換えてください）")

    out = path.with_name(path.stem + "_final.md")
    out.write_text(f"<!--\n{meta}\n-->\n\n{body}\n", encoding="utf-8")
    print(f"✅ WordPress 貼り付け用に仕上げました: {out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}")
    print("   （先頭のコメント部分はメタ情報です。タイトル・スラッグ・抜粋欄に転記し、本文には貼らないでください）")
    if problems:
        print("⚠ 公開前に対応が必要な項目:")
        for p in problems:
            print(f"  - {p}")
        return 2
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Sports Backstage 向けSEO記事の生成・仕上げ")
    parser.add_argument("--theme", help="記事のテーマ")
    parser.add_argument("--mode", choices=["prompt", "api"], default="prompt")
    parser.add_argument("--finalize", help="AIの出力ファイルを WordPress 貼り付け用に仕上げる")
    args = parser.parse_args()

    if args.finalize:
        return finalize(Path(args.finalize))
    if not args.theme:
        parser.error("--theme か --finalize を指定してください")

    keys = ", ".join(f"{k}（{v['label']}）" for k, v in load_links().items())
    out = REPO_ROOT / "wordpress" / "drafts" / f"{re.sub(r'[^\w-]+', '_', args.theme)[:40]}.md"
    cmd = [sys.executable, str(REPO_ROOT / "scripts" / "generate_content.py"),
           "--chain", "wordpress_seo", "--theme", args.theme, "--mode", args.mode,
           "--note", f"使えるアフィリエイトキー: {keys}"]
    if args.mode == "api":
        cmd += ["--out", str(out)]
    result = subprocess.run(cmd)
    if result.returncode != 0:
        return result.returncode
    if args.mode == "api":
        return finalize(out)
    print("   → AIの出力を wordpress/drafts/ に保存したら、--finalize で仕上げてください")
    return 0


if __name__ == "__main__":
    sys.exit(main())
