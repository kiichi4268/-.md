#!/usr/bin/env python3
"""AI社員のプロンプトチェーン（prompts/agents.yml）を実行し、1つのテーマから各媒体の素材を作る。

実行方式:
  prompt（既定・無料） … チェーン全体を1本のプロンプトに組み立てて outputs/prompts/ に保存する。
                         Claude や ChatGPT にそのまま貼り付けて使う。
  api                  … 各工程を順番に Claude API へ送り、成果物を outputs/drafts/ に保存する。
                         `pip install anthropic` と ANTHROPIC_API_KEY が必要（従量課金）。

使い方:
  # イベント告知一式（data/events.csv の event_id を指定）
  python scripts/generate_content.py --chain sns_set --event 2026-10-17_volleyball_shinjuku
  # 枠埋め（充足率から A/B/C パターンを自動判定し、テンプレートに数値を差し込む）
  python scripts/generate_content.py --chain fill_capacity --event 2026-10-17_volleyball_shinjuku
  # テーマから記事・note・協賛提案
  python scripts/generate_content.py --chain wordpress_seo --theme "都内バレーボールコートの抽選確率を上げるコツ"
  python scripts/generate_content.py --chain note_hybrid --theme "一人参加で浮かないサークルの選び方"
  python scripts/generate_content.py --chain sponsor_proposal --theme "スポーツ飲料ブランド向け サンプリング協賛"
  # 週次PDCA（数値をまとめたファイルを渡す）
  python scripts/generate_content.py --chain weekly_review --metrics reports/metrics_2026-10-06.md
  # API モード
  python scripts/generate_content.py --chain sns_set --event ... --mode api

必要なパッケージ: pyyaml（prompt モード）、anthropic（api モードのみ）
"""

import argparse
import csv
import datetime as dt
import os
import re
import sys
import unicodedata
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("PyYAML が必要です: pip install pyyaml")

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENTS_FILE = REPO_ROOT / "prompts" / "agents.yml"
EVENTS_FILE = REPO_ROOT / "data" / "events.csv"
OUTPUTS_DIR = REPO_ROOT / "outputs"
WEEKDAYS = "月火水木金土日"
MIN_APPLIED_FOR_RATIO = 5   # これ未満の申込数では初参加率・一人参加率を使わない
DEFAULT_PAST_SOLO_PCT = 80  # これまでのイベント全体の一人参加率（%）
DEFAULT_MODEL = "claude-opus-5-5"
MAX_FIX_ROUNDS = 1          # api モードで品質管理AIが Need Fix を出したときの再作成回数


# ---------------------------------------------------------------- ファイル読み込み

def resolve_repo_file(name: str) -> Path:
    """リポジトリ内のファイルを探す。直下のAI社員ファイルは NFD 名のため NFC で正規化して照合する。"""
    direct = REPO_ROOT / name
    if direct.exists():
        return direct
    target = unicodedata.normalize("NFC", name)
    parent = (REPO_ROOT / name).parent
    for p in parent.iterdir():
        if unicodedata.normalize("NFC", p.name) == unicodedata.normalize("NFC", Path(target).name):
            return p
    raise FileNotFoundError(f"ファイルが見つかりません: {name}")


def read_repo_file(name: str) -> str:
    return resolve_repo_file(name).read_text(encoding="utf-8").strip()


def load_registry() -> dict:
    return yaml.safe_load(AGENTS_FILE.read_text(encoding="utf-8"))


def flatten_steps(steps: list) -> list:
    """[a, {parallel: [b, c]}, d] → [a, b, c, d]"""
    flat = []
    for step in steps:
        if isinstance(step, dict) and "parallel" in step:
            flat.extend(step["parallel"])
        else:
            flat.append(step)
    return flat


# ---------------------------------------------------------------- イベントデータ

def load_event(event_id: str) -> dict:
    with EVENTS_FILE.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["event_id"] == event_id:
                return row
    raise ValueError(f"data/events.csv に event_id「{event_id}」がありません")


def pct(part: int, whole: int):
    return round(part / whole * 100) if whole else None


def event_variables(row: dict, past_solo_pct: int, today: dt.date) -> dict:
    """events.csv の1行から、テンプレートに差し込む変数と枠埋めパターンを計算する。"""
    date = dt.date.fromisoformat(row["date"])
    cap = int(row["capacity_max"])
    applied = int(row["applied"] or 0)
    remaining = max(cap - applied, 0)
    fill_rate = pct(applied, cap)
    enough = applied >= MIN_APPLIED_FOR_RATIO
    unusable = f"（申込{MIN_APPLIED_FOR_RATIO}名未満のため使用不可）"

    if remaining <= cap * 0.3 or remaining <= 5:
        pattern = "C"
    elif not enough or fill_rate < 30:
        pattern = "A"
    else:
        pattern = "B"

    voices = [v.strip() for v in (row.get("voices") or "").split(" / ") if v.strip()]
    return {
        "event_id": row["event_id"],
        "date": f"{date.month}/{date.day}",
        "date_iso": row["date"],
        "weekday": WEEKDAYS[date.weekday()],
        "start": row["start"],
        "end": row["end"],
        "sport": row["sport"],
        "area": row["area"],
        "venue": row["venue"],
        "fee": f"{int(row['fee']):,}",
        "capacity_min": row["capacity_min"],
        "capacity_max": str(cap),
        "applied": str(applied),
        "remaining": str(remaining),
        "fill_rate": f"{fill_rate}%",
        "first_timer_pct": f"{pct(int(row['first_timer'] or 0), applied)}%" if enough else unusable,
        "solo_pct": f"{pct(int(row['solo'] or 0), applied)}%" if enough else unusable,
        "voices": " / ".join(voices) if voices else "（掲載許可済みの声なし。参加者の声は使わない）",
        "days_left": str((date - today).days),
        "past_solo_pct": f"{past_solo_pct}%",
        "pattern": pattern,
    }


def fill_placeholders(text: str, variables: dict) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: variables.get(m.group(1), m.group(0)), text)


# ---------------------------------------------------------------- ブリーフ（入力情報）

PATTERN_NAMES = {"A": "A：安心訴求", "B": "B：顔ぶれ・参加者の声", "C": "C：残り枠カウントダウン"}


def build_brief(args, variables: dict = None) -> str:
    lines = ["## 📥 入力情報（ブリーフ）"]
    if variables:
        v = variables
        lines += [
            f"- イベント: {v['sport']}会（{v['area']}）",
            f"- 日時: {v['date_iso']}（{v['weekday']}） {v['start']}〜{v['end']}",
            f"- 会場: {v['venue']}",
            f"- 参加費: {v['fee']}円",
            f"- 定員: {v['capacity_max']}名（最少催行 {v['capacity_min']}名）",
            f"- 申込数: {v['applied']}名（充足率 {v['fill_rate']}・残り {v['remaining']}枠）",
            f"- 申込者の初参加率: {v['first_timer_pct']}",
            f"- 申込者の一人参加率: {v['solo_pct']}",
            f"- これまでの一人参加率（実績）: {v['past_solo_pct']}",
            f"- 参加者の声（掲載許可済み）: {v['voices']}",
            f"- 開催まで: {v['days_left']}日",
        ]
        if args.chain == "fill_capacity":
            lines.append(f"- **使用パターン（充足率から自動判定）: {PATTERN_NAMES[v['pattern']]}**")
    if args.theme:
        lines.append(f"- テーマ: {args.theme}")
    if args.metrics:
        lines += ["", "### 今週の数値", Path(args.metrics).read_text(encoding="utf-8").strip()]
    if args.note:
        lines.append(f"- 補足: {args.note}")
    lines.append("")
    lines.append("※ 上記に無い数値・参加者の声・会場情報は創作せず「【要編集】」と書くこと。")
    return "\n".join(lines)


# ---------------------------------------------------------------- prompt モード

def build_chain_prompt(registry: dict, chain_name: str, brief: str, variables: dict, deliverable: str) -> str:
    chain = registry["chains"][chain_name]
    agents = registry["agents"]
    headings = registry["headings"]
    steps = flatten_steps(chain["steps"])

    out = [
        f"# 🤖 AI社員チェーン実行プロンプト：{chain_name}",
        "",
        "あなたは以下の AI社員を順番に演じ、各工程の成果物を指定の見出しで出力してください。",
        "前の工程の成果物は、後の工程の入力として使ってください。",
        "最後に、全工程の成果物を「最終成果物」の構成で1つの Markdown にまとめてください。",
        "",
        "---",
        "",
        "## 🧭 共通ルール（全工程に適用）",
        read_repo_file(registry["brand"]["common_rules"]),
        "",
        "---",
        "",
        brief,
        "",
    ]
    for extra in chain.get("extra_prompts", []):
        out += ["---", "", f"## 📎 追加資料: `{extra}`", fill_placeholders(read_repo_file(extra), variables or {}), ""]

    for i, name in enumerate(steps, 1):
        agent = agents[name]
        expected = "、".join(headings.get(k, k) for k in agent["outputs"])
        out += [
            "---",
            "",
            f"## 工程{i}：{agent['label']}",
            f"**この工程で出力する見出し**: {expected}",
            "",
            read_repo_file(agent["prompt"]),
            "",
        ]

    out += [
        "---",
        "",
        "## 📦 最終成果物",
        f"保存先: `{deliverable}`",
        "全工程の出力を、上の工程順にそのまま並べてください（見出し名は変更しないこと）。",
        "品質管理AIが Need Fix と判定した場合は、指摘箇所を修正した版で最終成果物を出力し直してください。",
    ]
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- api モード

def run_api_chain(registry: dict, chain_name: str, brief: str, variables: dict, model: str, effort: str) -> str:
    try:
        import anthropic
    except ImportError:
        sys.exit("api モードには anthropic パッケージが必要です: pip install anthropic")

    client = anthropic.Anthropic()  # ANTHROPIC_API_KEY などの認証情報を環境から読む
    chain = registry["chains"][chain_name]
    agents = registry["agents"]
    headings = registry["headings"]
    common = read_repo_file(registry["brand"]["common_rules"])
    extras = "\n\n".join(fill_placeholders(read_repo_file(p), variables or {}) for p in chain.get("extra_prompts", []))
    steps = flatten_steps(chain["steps"])
    results = {}  # agent 名 → 出力テキスト

    def call(name: str, feedback: str = "") -> str:
        agent = agents[name]
        system = f"{common}\n\n---\n\n{read_repo_file(agent['prompt'])}"
        if extras:
            system += f"\n\n---\n\n## 追加資料\n{extras}"
        context = [brief]
        for prev_name, text in results.items():
            if set(agents[prev_name]["outputs"]) & set(agent["inputs"]) and prev_name != name:
                context.append(f"## {agents[prev_name]['label']} の成果物\n{text}")
        if feedback:
            context.append(f"## 品質管理AIからの差し戻し（必ず反映すること）\n{feedback}")
        expected = "、".join(headings.get(k, k) for k in agent["outputs"])
        context.append(f"上記を踏まえ、あなたの担当分だけを次の見出しで出力してください: {expected}")

        print(f"⏳ {agent['label']} を実行中…", file=sys.stderr)
        with client.beta.messages.stream(
            model=model,
            max_tokens=32000,
            system=system,
            messages=[{"role": "user", "content": "\n\n".join(context)}],
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-07-01"],
            extra_body={"fallbacks": "default", "cache_control": {"type": "ephemeral"}},
        ) as stream:
            message = stream.get_final_message()
        if message.stop_reason == "refusal":
            raise RuntimeError(f"{agent['label']} の生成が拒否されました（入力内容を見直してください）")
        if message.stop_reason == "max_tokens":
            print(f"⚠ {agent['label']} の出力が上限で途切れています", file=sys.stderr)
        return "".join(b.text for b in message.content if b.type == "text").strip()

    for name in steps:
        results[name] = call(name)

    # 品質管理AIが Need Fix を出したら、制作担当（リサーチ・ディレクター・品質管理以外）を作り直す
    if "qa" in results:
        for _ in range(MAX_FIX_ROUNDS):
            if "Need Fix" not in results["qa"]:
                break
            feedback = results["qa"]
            for name in steps:
                if name not in ("researcher", "director", "qa"):
                    results[name] = call(name, feedback)
            results["qa"] = call("qa")

    return "\n\n".join(results[name] for name in steps) + "\n"


def wrap_post_file(body: str, variables: dict) -> str:
    """sns_set / fill_capacity の成果物を posts/*.md の形式（フロントマター付き）にする。"""
    v = variables or {}
    title = f"# {v.get('date_iso', '')} {v.get('sport', '')}会（{v.get('area', '')}）".strip()
    return (
        "---\n"
        "# 画像（公開URLのJPEG）を設定してください。カルーセルは image_urls にカンマ区切り\n"
        "# image_url: 【要編集】\n"
        "platforms: instagram, threads\n"
        "---\n\n"
        f"{title}\n\n{body}"
    )


# ---------------------------------------------------------------- メイン

def slugify(text: str) -> str:
    return re.sub(r"[\s/\\:*?\"<>|]+", "_", text).strip("_")[:40] or "untitled"


def main() -> int:
    parser = argparse.ArgumentParser(description="AI社員チェーンでコンテンツを生成する")
    parser.add_argument("--chain", required=True, help="prompts/agents.yml の chains 名")
    parser.add_argument("--event", help="data/events.csv の event_id")
    parser.add_argument("--theme", help="テーマ（記事・note・協賛提案など）")
    parser.add_argument("--metrics", help="週次の数値をまとめたファイル（weekly_review 用）")
    parser.add_argument("--note", help="補足の指示")
    parser.add_argument("--mode", choices=["prompt", "api"], default=os.environ.get("GENERATE_MODE", "prompt"))
    parser.add_argument("--model", default=os.environ.get("CLAUDE_MODEL", DEFAULT_MODEL))
    parser.add_argument("--effort", default=os.environ.get("CLAUDE_EFFORT", "medium"),
                        choices=["low", "medium", "high", "xhigh", "max"])
    parser.add_argument("--past-solo-pct", type=int, default=DEFAULT_PAST_SOLO_PCT)
    parser.add_argument("--out", help="出力先（省略時は outputs/ 配下）")
    parser.add_argument("--print", action="store_true", help="標準出力にも表示する")
    args = parser.parse_args()

    registry = load_registry()
    if args.chain not in registry["chains"]:
        parser.error(f"未定義のチェーンです: {args.chain}（候補: {', '.join(registry['chains'])}）")
    if args.chain in ("sns_set", "fill_capacity") and not args.event:
        parser.error(f"{args.chain} には --event が必要です")
    if args.chain == "weekly_review" and not args.metrics:
        parser.error("weekly_review には --metrics が必要です")
    if not (args.event or args.theme or args.metrics):
        parser.error("--event / --theme / --metrics のいずれかを指定してください")

    today = dt.date.today()
    variables = event_variables(load_event(args.event), args.past_solo_pct, today) if args.event else None
    brief = build_brief(args, variables)

    slug = slugify(args.event or args.theme or Path(args.metrics).stem)
    deliverable = registry["chains"][args.chain]["deliverable"].format(
        date=variables["date_iso"] if variables else today.isoformat(),
        sport=variables["sport"] if variables else "",
        area=variables["area"] if variables else "",
        slug=slug,
    )

    if args.mode == "prompt":
        text = build_chain_prompt(registry, args.chain, brief, variables, deliverable)
        out = Path(args.out) if args.out else OUTPUTS_DIR / "prompts" / f"{today.isoformat()}_{args.chain}_{slug}.md"
    else:
        text = run_api_chain(registry, args.chain, brief, variables, args.model, args.effort)
        if args.chain in ("sns_set", "fill_capacity"):
            text = wrap_post_file(text, variables)
        out = Path(args.out) if args.out else OUTPUTS_DIR / "drafts" / deliverable

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    if variables and args.chain == "fill_capacity":
        print(f"🎯 枠埋めパターン: {PATTERN_NAMES[variables['pattern']]}（充足率 {variables['fill_rate']}）")
    print(f"✅ {'プロンプト' if args.mode == 'prompt' else '成果物'}を保存しました: {out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}")
    if args.mode == "prompt":
        print(f"   → AIに貼り付けて実行し、結果を `{deliverable}` に保存してください")
    else:
        print(f"   → 内容を確認し、`{deliverable}` に配置してPRを作成してください")
    if args.print:
        print("\n" + text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
