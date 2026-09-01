"""将指定格式的 Markdown 文件转换为 Anki .apkg 牌组。"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import shutil
import sqlite3
import tempfile
import threading
import traceback
import zipfile
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import quote, unquote

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk


APP_TITLE = "Anki ↔ Markdown 往返工具"
MODEL_ID = 2_026_082_901
ROUNDTRIP_MANIFEST = ".anki-roundtrip.json"
FIELD_SEPARATOR = "\x1f"
INVALID_WINDOWS_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

CARD_SEPARATOR_RE = re.compile(r"(?m)^\s*---\s*$")
CARD_ID_RE = re.compile(
    r"(?mi)^\s*<!--\s*anki-id\s*:\s*([a-z0-9._:-]+)\s*-->\s*$"
)
CARD_GUID_RE = re.compile(
    r"(?mi)^\s*<!--\s*anki-guid\s*:\s*([^\s]+)\s*-->\s*$"
)
CARD_MODEL_ID_RE = re.compile(
    r"(?mi)^\s*<!--\s*anki-model-id\s*:\s*(\d+)\s*-->\s*$"
)
CARD_TAGS_RE = re.compile(
    r"(?mi)^\s*<!--\s*anki-tags\s*:\s*([^\s]+)\s*-->\s*$"
)
CARD_RAW_HTML_RE = re.compile(
    r"(?mi)^\s*<!--\s*anki-raw-html\s*:\s*(?:1|true|yes)\s*-->\s*$"
)
CARD_RE = re.compile(
    r"(?ms)^\s*###\s+Front\s*$\s*(.*?)"
    r"^\s*###\s+Back\s*$\s*(.*?)"
    r"(?:^\s*###\s+OriginalMaterial\s*$\s*(.*?))?\s*$"
)
IMAGE_RE = re.compile(
    r"!\[[^\]\r\n]*\]\(\s*<?/?media/([^)>\r\n]+?)>?\s*\)",
    re.IGNORECASE,
)
HTML_IMAGE_RE = re.compile(
    r"(?is)(<img\b[^>]*?\bsrc\s*=\s*)([\"'])(?:/?media/)(.*?)(\2)([^>]*>)"
)
ANY_HTML_IMAGE_RE = re.compile(
    r"(?is)(<img\b[^>]*?\bsrc\s*=\s*)([\"'])(.*?)(\2)([^>]*>)"
)
MERMAID_RE = re.compile(
    r"(?ms)^[ \t]*```[ \t]*mermaid[ \t]*\n(.*?)^[ \t]*```[ \t]*$"
)

CARD_CSS = r"""
.card {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
  background: #f9f9f9;
  color: #333;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  font-size: 20px;
  line-height: 1.65;
  text-align: left;
}
.apple-card {
  box-sizing: border-box;
  max-width: 850px;
  margin: 20px auto;
  padding: 30px;
  border: 1px solid #e0e0e0;
  border-radius: 12px;
  background: #fff;
  box-shadow: 0 2px 10px rgba(0, 0, 0, 0.04);
}
.front-side {
  color: #222;
  font-size: 24px;
  font-weight: 600;
  text-align: center;
}
.back-side {
  color: #333;
  font-size: 20px;
  font-weight: 400;
  text-align: left;
}
mark {
  padding: 0.08em 0.22em;
  border-radius: 0.22em;
  background: linear-gradient(transparent 12%, #ffe66d 12%, #ffe66d 90%, transparent 90%);
  color: inherit;
}
img {
  display: block;
  max-width: 90%;
  height: auto;
  margin: 18px auto;
  border-radius: 8px;
}
table {
  width: 100%;
  margin: 18px auto;
  border-collapse: collapse;
  text-align: left;
  font-size: 0.88em;
}
th, td { padding: 8px 10px; border: 1px solid #bbb; }
th { background: #f3f0e8; }
ul, ol { text-align: left; }
pre, code { font-family: Consolas, "SFMono-Regular", monospace; }
pre {
  overflow-x: auto;
  padding: 14px;
  border-radius: 8px;
  background: #f3f3f3;
  text-align: left;
  white-space: pre-wrap;
}
.mermaid { margin: 20px auto; text-align: center; }
hr#answer { margin: 26px 0; border: 0; border-top: 2px solid #e2ded5; }
.nightMode.card { background: #121212; color: #e0e0e0; }
.nightMode .apple-card { background: #1e1e1e; border-color: #333; box-shadow: none; }
.nightMode .front-side, .nightMode .back-side { color: #e0e0e0; }
.nightMode mark { background: #9a7b00; }
.nightMode th { background: #333; }
.nightMode th, .nightMode td { border-color: #666; }
.nightMode pre { background: #181818; }
"""

FRONT_TEMPLATE = r"""<div class="apple-card">
  <div class="front-side">{{Front}}</div>
</div>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
  mermaid.initialize({ startOnLoad: true, theme: 'default' });
</script>
"""
BACK_TEMPLATE = r"""<div class="apple-card">
  <div class="front-side">{{Front}}</div>
  <hr id=answer>
  <div class="back-side">{{Back}}</div>
</div>
<script type="module">
  import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.esm.min.mjs';
  mermaid.initialize({ startOnLoad: true, theme: 'default' });
</script>
"""


@dataclass(frozen=True)
class ParsedCard:
    front: str
    back: str
    original_material: str
    explicit_id: str | None
    explicit_guid: str | None
    model_id: int | None
    tags: tuple[str, ...]
    raw_html: bool


def stable_deck_id(deck_name: str) -> int:
    """按牌组名称生成稳定且合法的 Anki 牌组 ID。"""
    value = int.from_bytes(hashlib.sha256(deck_name.encode("utf-8")).digest()[:4], "big")
    return (value & 0x7FFF_FFFF) or 1


def parse_cards(
    markdown_text: str,
) -> tuple[list[ParsedCard], list[str]]:
    """解析卡片，并返回卡片列表和格式警告。"""
    normalized = markdown_text.replace("\r\n", "\n").replace("\r", "\n")
    cards: list[ParsedCard] = []
    warnings: list[str] = []

    for block_number, block in enumerate(CARD_SEPARATOR_RE.split(normalized), start=1):
        if not block.strip():
            continue
        card_ids = CARD_ID_RE.findall(block)
        card_guids = CARD_GUID_RE.findall(block)
        model_ids = CARD_MODEL_ID_RE.findall(block)
        encoded_tags = CARD_TAGS_RE.findall(block)
        raw_html = CARD_RAW_HTML_RE.search(block) is not None
        cleaned_block = CARD_ID_RE.sub("", block)
        cleaned_block = CARD_GUID_RE.sub("", cleaned_block)
        cleaned_block = CARD_MODEL_ID_RE.sub("", cleaned_block)
        cleaned_block = CARD_TAGS_RE.sub("", cleaned_block)
        cleaned_block = CARD_RAW_HTML_RE.sub("", cleaned_block)
        match = CARD_RE.fullmatch(cleaned_block)
        if not match:
            warnings.append(
                f"第 {block_number} 个内容块格式不正确，已跳过（需要依次包含 ### Front 和 ### Back）。"
            )
            continue
        front, back = (part.strip() for part in match.groups()[:2])
        original_material = (match.group(3) or "").strip()
        if (not front or not back) and not card_guids:
            warnings.append(f"第 {block_number} 张卡片正面或背面为空，已跳过。")
            continue
        if not front or not back:
            warnings.append(
                f"第 {block_number} 张往返卡片的 Front 或 Back 为空；"
                "因含精确 anki-guid，仍将保留。"
            )
        if len(card_ids) > 1:
            warnings.append(
                f"第 {block_number} 张卡片包含多个 anki-id，将使用第一个：{card_ids[0]}"
            )
        if len(card_guids) > 1:
            warnings.append(
                f"第 {block_number} 张卡片包含多个 anki-guid，将使用第一个。"
            )
        if len(model_ids) > 1:
            warnings.append(
                f"第 {block_number} 张卡片包含多个 anki-model-id，将使用第一个。"
            )

        explicit_guid = unquote(card_guids[0]) if card_guids else None
        tags: tuple[str, ...] = ()
        if encoded_tags:
            try:
                parsed_tags = json.loads(unquote(encoded_tags[0]))
                if not isinstance(parsed_tags, list) or not all(
                    isinstance(tag, str) for tag in parsed_tags
                ):
                    raise ValueError("标签必须是字符串列表")
                tags = tuple(parsed_tags)
            except (json.JSONDecodeError, ValueError) as exc:
                warnings.append(
                    f"第 {block_number} 张卡片的 anki-tags 无效，已忽略：{exc}"
                )

        cards.append(
            ParsedCard(
                front=front,
                back=back,
                original_material=original_material,
                explicit_id=card_ids[0] if card_ids else None,
                explicit_guid=explicit_guid,
                model_id=int(model_ids[0]) if model_ids else None,
                tags=tags,
                raw_html=raw_html,
            )
        )

    return cards, warnings


def _convert_mermaid(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        code = match.group(1).strip("\n")
        return f'<div class="mermaid">\n{code}\n</div>'

    return MERMAID_RE.sub(replace, text)


def _convert_images(
    text: str,
    media_dir: Path,
    media_files: dict[str, Path],
    warn: Callable[[str], None],
    media_key_prefix: str = "",
) -> str:
    media_root = media_dir.resolve()

    def resolve_reference(raw_reference: str) -> str | None:
        raw_reference = html.unescape(raw_reference.strip())
        # 支持 Markdown 常见的尖括号路径；题目要求的简单路径无需额外处理。
        decoded = unquote(raw_reference).replace("\\", "/")
        relative = PurePosixPath(decoded)
        filename = relative.name

        if not filename or relative.is_absolute() or ".." in relative.parts:
            warn(f"不安全或无效的图片路径已跳过：media/{raw_reference}")
            return None

        physical_path = (media_dir.joinpath(*relative.parts)).resolve()
        try:
            physical_path.relative_to(media_root)
        except ValueError:
            warn(f"图片路径超出 media 文件夹，已跳过：media/{raw_reference}")
            return None

        if not physical_path.is_file():
            warn(f"图片找不到，未打包：{physical_path}")
            return ""

        # Anki 的媒体目录是扁平结构。若不同章节存在同名图片，给后者添加
        # 稳定的路径摘要，避免互相覆盖。
        target_name = filename
        previous = media_files.get(target_name)
        if previous is not None and previous != physical_path:
            # 反向导出的不同章节可能各自保存了同一份媒体副本；内容一致时
            # 继续复用原媒体名，避免无意义地重命名。
            if previous.read_bytes() != physical_path.read_bytes():
                media_key = f"{media_key_prefix}/media/{relative.as_posix()}".casefold()
                digest = hashlib.sha256(media_key.encode("utf-8")).hexdigest()[:10]
                target_name = f"{digest}_{filename}"
                collision_number = 2
                while (
                    target_name in media_files
                    and media_files[target_name] != physical_path
                ):
                    target_name = f"{digest}_{collision_number}_{filename}"
                    collision_number += 1
                warn(f"检测到同名图片 {filename}，打包时重命名为 {target_name}")

        media_files[target_name] = physical_path
        return target_name

    def replace_markdown(match: re.Match[str]) -> str:
        target_name = resolve_reference(match.group(1))
        if target_name is None:
            return match.group(0)
        if not target_name:
            return ""
        return f'<img src="{html.escape(target_name, quote=True)}">'

    def replace_html(match: re.Match[str]) -> str:
        target_name = resolve_reference(match.group(3))
        if target_name is None:
            return match.group(0)
        if not target_name:
            return ""
        escaped_name = html.escape(target_name, quote=True)
        return f"{match.group(1)}{match.group(2)}{escaped_name}{match.group(4)}{match.group(5)}"

    converted = IMAGE_RE.sub(replace_markdown, text)
    return HTML_IMAGE_RE.sub(replace_html, converted)


def markdown_to_html(text: str) -> str:
    """把 Markdown 转为 HTML，同时保护 Anki MathJax 定界符。"""
    try:
        import markdown
    except ImportError as exc:
        raise RuntimeError(
            "缺少 Markdown 库。请先运行：python -m pip install -r requirements.txt"
        ) from exc

    math_tokens = {
        r"\(": "ANKI_MATH_INLINE_OPEN_7F3A",
        r"\)": "ANKI_MATH_INLINE_CLOSE_7F3A",
        r"\[": "ANKI_MATH_BLOCK_OPEN_7F3A",
        r"\]": "ANKI_MATH_BLOCK_CLOSE_7F3A",
    }
    protected = text
    for delimiter, token in math_tokens.items():
        protected = protected.replace(delimiter, token)

    rendered = markdown.markdown(
        protected,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )
    for delimiter, token in math_tokens.items():
        rendered = rendered.replace(token, delimiter)
    return rendered


def _find_markdown_files(folder: Path) -> list[Path]:
    """递归查找牌组文件夹中的 Markdown，忽略隐藏目录与 media 目录。"""
    files: list[Path] = []
    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".md", ".markdown"}:
            continue
        relative_parts = path.relative_to(folder).parts[:-1]
        if any(part.lower() == "media" or part.startswith(".") for part in relative_parts):
            continue
        files.append(path)
    return sorted(files, key=lambda item: item.relative_to(folder).as_posix().casefold())


def _load_roundtrip_manifest(source_root: Path) -> dict[str, object] | None:
    manifest_path = source_root / ROUNDTRIP_MANIFEST
    if not manifest_path.is_file():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取往返元数据 {manifest_path}：{exc}") from exc
    if manifest.get("format") != "markdown-to-anki-roundtrip-v1":
        raise ValueError(f"不支持的往返元数据格式：{manifest.get('format')!r}")
    if not isinstance(manifest.get("models"), dict) or not isinstance(
        manifest.get("files"), dict
    ):
        raise ValueError("往返元数据缺少 models 或 files。")
    return manifest


def _model_from_manifest(genanki: object, data: dict[str, object]) -> object:
    try:
        model_id = int(data["id"])
        name = str(data["name"])
        field_names = data["fields"]
        templates = data["templates"]
        if not isinstance(field_names, list) or not all(
            isinstance(item, str) for item in field_names
        ):
            raise TypeError("fields 必须是字符串列表")
        if not isinstance(templates, list):
            raise TypeError("templates 必须是列表")
        normalized_templates = []
        for template in templates:
            if not isinstance(template, dict):
                raise TypeError("template 必须是对象")
            normalized_templates.append(
                {
                    "name": str(template["name"]),
                    "qfmt": str(template["qfmt"]),
                    "afmt": str(template["afmt"]),
                }
            )
        return genanki.Model(
            model_id,
            name,
            fields=[{"name": item} for item in field_names],
            templates=normalized_templates,
            css=str(data.get("css", "")),
            model_type=int(data.get("model_type", 0)),
            latex_pre=str(data.get("latex_pre", "")),
            latex_post=str(data.get("latex_post", "")),
            sort_field_index=int(data.get("sort_field_index", 0)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"往返元数据中的模型定义无效：{exc}") from exc


def _safe_component(value: str, fallback: str) -> str:
    cleaned = INVALID_WINDOWS_CHARS_RE.sub("_", value).strip().rstrip(". ")
    if cleaned in {"", ".", ".."}:
        cleaned = fallback
    if cleaned.split(".", 1)[0].upper() in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }:
        cleaned = f"_{cleaned}"
    return cleaned


def _safe_deck_component(value: str, fallback: str) -> str:
    """生成目录名，并把 Ch.1 规范化为自然排序友好的 Ch.01。"""
    chapter_match = re.fullmatch(r"(?i)ch\.(\d+)", value.strip())
    if chapter_match:
        return f"Ch.{int(chapter_match.group(1)):02d}"
    return _safe_component(value, fallback)


def _encoded(value: str) -> str:
    return quote(value, safe="")


def _model_manifest(model: dict[str, object]) -> dict[str, object]:
    fields = model.get("flds", [])
    templates = model.get("tmpls", [])
    return {
        "id": int(model["id"]),
        "name": str(model.get("name", f"Model {model['id']}")),
        "fields": [str(field["name"]) for field in fields],
        "templates": [
            {
                "name": str(template.get("name", "Card")),
                "qfmt": str(template.get("qfmt", "")),
                "afmt": str(template.get("afmt", "")),
            }
            for template in templates
        ],
        "css": str(model.get("css", "")),
        "model_type": int(model.get("type", 0)),
        "latex_pre": str(model.get("latexPre", "")),
        "latex_post": str(model.get("latexPost", "")),
        "sort_field_index": int(model.get("sortf", 0)),
    }


def _deck_relative_markdown(
    deck_name: str,
    deck_id: int,
    common_root: str | None,
    occupied: set[str],
) -> Path:
    parts = deck_name.split("::")
    if common_root is not None and parts and parts[0] == common_root:
        parts = parts[1:]
    safe_parts = [
        _safe_deck_component(part, f"deck_{deck_id}")
        for part in parts
        if part.strip()
    ]
    candidate = Path(*safe_parts, "cards.md") if safe_parts else Path("cards.md")
    key = candidate.as_posix().casefold()
    if key in occupied:
        if safe_parts:
            safe_parts[-1] = f"{safe_parts[-1]}_{deck_id}"
            candidate = Path(*safe_parts, "cards.md")
        else:
            candidate = Path(f"cards_{deck_id}.md")
        key = candidate.as_posix().casefold()
    occupied.add(key)
    return candidate


def build_anki_package(
    source_path: Path,
    output_path: Path | None = None,
    log: Callable[[str], None] = print,
) -> Path:
    """读取一个 Markdown 或整个牌组文件夹，并写出 .apkg 文件。"""
    try:
        import genanki
    except ImportError as exc:
        raise RuntimeError(
            "缺少 genanki 库。请先运行：python -m pip install -r requirements.txt"
        ) from exc

    source_path = source_path.expanduser().resolve()
    if source_path.is_file():
        if source_path.suffix.lower() not in {".md", ".markdown"}:
            raise ValueError("请选择 .md、.markdown 文件或牌组文件夹。")
        source_root = source_path.parent
        markdown_files = [source_path]
        root_deck_name = source_path.stem
        folder_mode = False
        default_output = source_path.with_suffix(".apkg")
    elif source_path.is_dir():
        source_root = source_path
        markdown_files = _find_markdown_files(source_path)
        root_deck_name = source_path.name
        folder_mode = True
        default_output = source_path / f"{source_path.name}.apkg"
        if not markdown_files:
            raise ValueError("所选文件夹及其子文件夹中没有 Markdown 文件。")
    else:
        raise FileNotFoundError(f"所选路径不存在：{source_path}")

    roundtrip_manifest = _load_roundtrip_manifest(source_root) if folder_mode else None
    log(f"牌组名称：{root_deck_name}")
    log(f"发现 {len(markdown_files)} 个 Markdown 文件。")
    default_model = genanki.Model(
        MODEL_ID,
        "Markdown 转 Anki 模板",
        fields=[{"name": "Front"}, {"name": "Back"}],
        templates=[
            {
                "name": "问答卡",
                "qfmt": FRONT_TEMPLATE,
                "afmt": BACK_TEMPLATE,
            }
        ],
        css=CARD_CSS,
    )
    roundtrip_models: dict[int, object] = {}
    if roundtrip_manifest is not None:
        for raw_id, raw_model in roundtrip_manifest["models"].items():
            if not isinstance(raw_model, dict):
                raise ValueError(f"模型 {raw_id} 的往返元数据无效。")
            model = _model_from_manifest(genanki, raw_model)
            roundtrip_models[int(raw_id)] = model
        log(
            f"已读取往返元数据：{len(roundtrip_models)} 个原始笔记类型，"
            "将保留原始 GUID、模型 ID 与牌组 ID。"
        )
    decks: dict[str, object] = {}
    if roundtrip_manifest is None:
        decks[root_deck_name] = genanki.Deck(
            stable_deck_id(root_deck_name), root_deck_name
        )
    media_files: dict[str, Path] = {}
    warning_cache: set[str] = set()
    identity_counts: dict[str, int] = {}
    total_cards = 0
    valid_files = 0

    def warn_once(message: str) -> None:
        if message not in warning_cache:
            warning_cache.add(message)
            log(f"警告：{message}")

    if roundtrip_manifest is not None:
        extra_media = roundtrip_manifest.get("unreferenced_media_files", [])
        if not isinstance(extra_media, list):
            raise ValueError("往返元数据中的 unreferenced_media_files 必须是列表。")
        extra_media_dir = source_root / "unreferenced_media"
        for raw_name in extra_media:
            name = str(raw_name)
            if Path(name).name != name or name in {"", ".", ".."}:
                warn_once(f"忽略不安全的未引用媒体名：{name!r}")
                continue
            physical_path = extra_media_dir / name
            if not physical_path.is_file():
                warn_once(f"未引用媒体找不到：{physical_path}")
                continue
            media_files[name] = physical_path
        if extra_media:
            log(f"已载入 {len(media_files)} 个未引用媒体文件，以便完整往返。")

    for markdown_path in markdown_files:
        relative_source = markdown_path.relative_to(source_root)
        source_key = relative_source.as_posix()
        log(f"正在读取：{source_key}")
        try:
            text = markdown_path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            warn_once(f"无法读取 {source_key}，已跳过：{exc}")
            continue

        cards, warnings = parse_cards(text)
        for warning in warnings:
            warn_once(f"{source_key}：{warning}")
        if not cards:
            warn_once(f"{source_key} 中没有格式正确且内容完整的卡片，已跳过。")
            continue

        valid_files += 1
        file_metadata = None
        if roundtrip_manifest is not None:
            candidate = roundtrip_manifest["files"].get(source_key)
            if isinstance(candidate, dict):
                file_metadata = candidate
            else:
                warn_once(
                    f"{source_key} 不在往返元数据中，将按普通 Markdown 规则生成牌组。"
                )

        if file_metadata is not None:
            try:
                deck_name = str(file_metadata["deck_name"])
                deck_id = int(file_metadata["deck_id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{source_key} 的牌组元数据无效：{exc}") from exc
        else:
            relative_parent = relative_source.parent
            if folder_mode and relative_parent != Path("."):
                deck_name = "::".join((root_deck_name, *relative_parent.parts))
            else:
                deck_name = root_deck_name
            deck_id = stable_deck_id(deck_name)
        if deck_name not in decks:
            description = ""
            if file_metadata is not None:
                description = str(file_metadata.get("description", ""))
            decks[deck_name] = genanki.Deck(deck_id, deck_name, description=description)
        deck = decks[deck_name]

        media_dir = markdown_path.parent / "media"
        if not media_dir.is_dir() and ("media/" in text or "/media/" in text):
            warn_once(f"media 文件夹不存在：{media_dir}")

        for card_number, card in enumerate(cards, start=1):
            original_front = card.front
            front = _convert_images(
                card.front, media_dir, media_files, warn_once, source_key
            )
            back = _convert_images(
                card.back, media_dir, media_files, warn_once, source_key
            )
            original_material = _convert_images(
                card.original_material,
                media_dir,
                media_files,
                warn_once,
                source_key,
            )
            if card.raw_html:
                front_html = front
                back_html = back
                original_material_html = original_material
            else:
                front_html = markdown_to_html(_convert_mermaid(front))
                back_html = markdown_to_html(_convert_mermaid(back))
                original_material_html = markdown_to_html(
                    _convert_mermaid(original_material)
                )

            if card.explicit_guid:
                guid = card.explicit_guid
                guid_parts: tuple[str, ...] | None = None
            elif card.explicit_id:
                guid_parts = (
                    "markdown-to-anki-v1",
                    f"explicit:{card.explicit_id.casefold()}",
                )
            elif not folder_mode:
                # 与本工具旧版的单文件 GUID 算法保持兼容，避免升级后产生重复卡。
                guid_parts = (root_deck_name, original_front.strip())
            else:
                normalized_front = " ".join(original_front.split())
                guid_parts = (
                    "markdown-to-anki-v1",
                    f"source:{source_key.casefold()}\nfront:{normalized_front}",
                )
            identity = (
                f"exact-guid:{guid}" if guid_parts is None else "\0".join(guid_parts)
            )
            occurrence = identity_counts.get(identity, 0) + 1
            identity_counts[identity] = occurrence
            if occurrence > 1:
                if guid_parts is None:
                    raise ValueError(
                        f"检测到重复的精确 anki-guid（{source_key} 第 {card_number} 张）：{guid!r}"
                    )
                else:
                    warn_once(
                        f"检测到重复卡片标识（{source_key} 第 {card_number} 张），"
                        "已为重复项分配独立但稳定的 ID。建议添加唯一 anki-id。"
                    )

            if guid_parts is not None:
                guid = genanki.guid_for(
                    *guid_parts,
                    *(() if occurrence == 1 else (f"duplicate:{occurrence}",)),
                )

            if card.model_id is not None:
                note_model = roundtrip_models.get(card.model_id)
                if note_model is None:
                    raise ValueError(
                        f"{source_key} 第 {card_number} 张卡片引用了未知模型 ID："
                        f"{card.model_id}"
                    )
            else:
                note_model = default_model

            field_values = {
                "Front": front_html,
                "Back": back_html,
                "OriginalMaterial": original_material_html,
            }
            fields = [field_values.get(field["name"], "") for field in note_model.fields]
            if card.original_material and not any(
                field["name"] == "OriginalMaterial" for field in note_model.fields
            ):
                warn_once(
                    f"{source_key} 第 {card_number} 张卡片含 OriginalMaterial，"
                    "但目标模型没有该字段，内容已忽略。"
                )

            note = genanki.Note(
                model=note_model,
                fields=fields,
                tags=list(card.tags),
                guid=guid,
            )
            deck.add_note(note)
            total_cards += 1
        log(f"已从 {source_key} 读取 {len(cards)} 张卡片 → {deck_name}")

    if total_cards == 0:
        raise ValueError("没有找到格式正确且内容完整的卡片。")
    log(f"成功读取 {valid_files} 个文件，共 {total_cards} 张卡片。")

    final_path = (output_path or default_output).resolve()
    final_path.parent.mkdir(parents=True, exist_ok=True)
    package = genanki.Package(list(decks.values()))
    log(f"正在打包 {len(media_files)} 个图片文件……")
    # genanki 以物理文件名作为 Anki 媒体名，因此先复制到临时目录，
    # 以便安全处理不同章节中的同名图片。
    with tempfile.TemporaryDirectory(prefix="markdown_anki_") as temp_dir:
        staged_media: list[str] = []
        for target_name, physical_path in media_files.items():
            staged_path = Path(temp_dir) / target_name
            shutil.copy2(physical_path, staged_path)
            staged_media.append(str(staged_path))
        package.media_files = staged_media
        package.write_to_file(str(final_path))
    log(f"生成完毕，保存在：{final_path}")
    return final_path


def export_anki_package(
    package_path: Path,
    output_folder: Path | None = None,
    log: Callable[[str], None] = print,
) -> Path:
    """导出牌组、卡片、精确 GUID、模型/牌组元数据及媒体。"""
    package_path = package_path.expanduser().resolve()
    if not package_path.is_file() or package_path.suffix.lower() != ".apkg":
        raise ValueError("请选择有效的 .apkg 文件。")

    final_folder = (output_folder or package_path.with_suffix("")).expanduser().resolve()
    if final_folder.exists():
        raise FileExistsError(
            f"输出文件夹已存在：{final_folder}\n请改用新的输出路径，以免覆盖已有修改。"
        )
    final_folder.parent.mkdir(parents=True, exist_ok=True)
    working_folder = Path(
        tempfile.mkdtemp(prefix=f".{final_folder.name}_export_", dir=final_folder.parent)
    )

    try:
        with zipfile.ZipFile(package_path) as archive:
            database_member = next(
                (
                    name
                    for name in ("collection.anki2", "collection.anki21")
                    if name in archive.namelist()
                ),
                None,
            )
            if database_member is None:
                if "collection.anki21b" in archive.namelist():
                    raise ValueError(
                        "此包使用压缩版 collection.anki21b，当前导出器尚不支持；"
                        "请先用 Anki 导出为兼容旧版的 .apkg。"
                    )
                raise ValueError("包内找不到 collection.anki2/collection.anki21。")

            database_info = archive.getinfo(database_member)
            if database_info.file_size > 2 * 1024 * 1024 * 1024:
                raise ValueError("牌组数据库超过 2 GiB，已停止处理。")

            database_path = working_folder / ".collection.sqlite"
            with archive.open(database_info) as source, database_path.open("wb") as target:
                shutil.copyfileobj(source, target)

            connection = sqlite3.connect(database_path)
            try:
                raw_decks, raw_models = connection.execute(
                    "SELECT decks, models FROM col LIMIT 1"
                ).fetchone()
                decks = json.loads(raw_decks)
                models = json.loads(raw_models)
                rows = connection.execute(
                    """
                    SELECT n.id, n.guid, n.mid, n.flds, n.tags,
                           (SELECT c.did FROM cards c WHERE c.nid = n.id
                            ORDER BY c.ord, c.id LIMIT 1) AS did
                    FROM notes n
                    WHERE EXISTS (SELECT 1 FROM cards c WHERE c.nid = n.id)
                    ORDER BY did, n.id
                    """
                ).fetchall()
                orphan_count = connection.execute(
                    "SELECT COUNT(*) FROM notes n WHERE NOT EXISTS "
                    "(SELECT 1 FROM cards c WHERE c.nid = n.id)"
                ).fetchone()[0]
            finally:
                connection.close()
                database_path.unlink(missing_ok=True)

            if not rows:
                raise ValueError("包内没有可导出的卡片。")
            visible_deck_count = len(decks) - (1 if "1" in decks else 0)
            log(f"发现 {len(rows)} 张笔记卡片、{visible_deck_count} 个牌组。")
            if orphan_count:
                log(f"提示：跳过 {orphan_count} 条没有卡片的孤立笔记。")

            if "media" in archive.namelist():
                media_index = json.loads(archive.read("media").decode("utf-8"))
            else:
                media_index = {}
            if not isinstance(media_index, dict):
                raise ValueError("包内 media 索引格式无效。")
            archive_names = set(archive.namelist())
            media_by_name: dict[str, str] = {}
            for member, original_name in media_index.items():
                if not isinstance(member, str) or not isinstance(original_name, str):
                    continue
                if member not in archive_names:
                    log(f"警告：媒体索引指向不存在的包内文件：{member}")
                    continue
                if original_name in media_by_name:
                    log(f"警告：媒体名称重复，将使用最后一项：{original_name}")
                media_by_name[original_name] = member

            safe_media_names: dict[str, str] = {}
            occupied_media_names: set[str] = set()
            for number, original_name in enumerate(media_by_name, start=1):
                safe_name = _safe_component(original_name, f"media_{number}")
                candidate = safe_name
                suffix_number = 2
                while candidate.casefold() in occupied_media_names:
                    stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
                    candidate = f"{stem}_{suffix_number}{suffix}"
                    suffix_number += 1
                occupied_media_names.add(candidate.casefold())
                safe_media_names[original_name] = candidate

            deck_ids = {int(row[5]) for row in rows}
            deck_names = {
                deck_id: str(decks.get(str(deck_id), {}).get("name", f"Deck {deck_id}"))
                for deck_id in deck_ids
            }
            roots = {name.split("::", 1)[0] for name in deck_names.values()}
            common_root = next(iter(roots)) if len(roots) == 1 else None
            occupied_markdown: set[str] = set()
            deck_files = {
                deck_id: _deck_relative_markdown(
                    deck_names[deck_id], deck_id, common_root, occupied_markdown
                )
                for deck_id in sorted(
                    deck_ids, key=lambda item: deck_names[item].casefold()
                )
            }

            cards_by_deck: dict[int, list[str]] = defaultdict(list)
            media_by_deck: dict[int, set[str]] = defaultdict(set)
            used_model_ids: set[int] = set()

            def rewrite_images(value: str, deck_id: int) -> str:
                def replace(match: re.Match[str]) -> str:
                    raw_src = html.unescape(unquote(match.group(3).strip()))
                    normalized = raw_src.replace("\\", "/")
                    if normalized.startswith("./"):
                        normalized = normalized[2:]
                    if normalized.startswith("media/"):
                        normalized = normalized[6:]
                    if normalized.startswith("/media/"):
                        normalized = normalized[7:]
                    if normalized not in media_by_name:
                        return match.group(0)
                    media_by_deck[deck_id].add(normalized)
                    target_name = safe_media_names[normalized]
                    target_src = f"media/{quote(target_name, safe='')}"
                    return (
                        f"{match.group(1)}{match.group(2)}{target_src}"
                        f"{match.group(4)}{match.group(5)}"
                    )

                return ANY_HTML_IMAGE_RE.sub(replace, value)

            for note_id, guid, model_id, raw_fields, raw_tags, deck_id in rows:
                model_id = int(model_id)
                deck_id = int(deck_id)
                model = models.get(str(model_id))
                if not isinstance(model, dict):
                    raise ValueError(f"笔记 {note_id} 引用了不存在的模型 {model_id}。")
                field_names = [str(item["name"]) for item in model.get("flds", [])]
                field_values = str(raw_fields).split(FIELD_SEPARATOR)
                values = dict(zip(field_names, field_values))
                if "Front" not in values or "Back" not in values:
                    raise ValueError(
                        f"模型 {model.get('name', model_id)} 不含 Front/Back 字段，"
                        "无法转成当前 Markdown 格式。"
                    )
                front = rewrite_images(values["Front"], deck_id).strip()
                back = rewrite_images(values["Back"], deck_id).strip()
                original_material = rewrite_images(
                    values.get("OriginalMaterial", ""), deck_id
                ).strip()
                if not front or not back:
                    log(
                        f"提示：笔记 {note_id} 的 Front 或 Back 为空；"
                        "仍按原样导出并以精确 GUID 保留。"
                    )

                tags = [item for item in str(raw_tags).strip().split() if item]
                block = [
                    f"<!-- anki-guid: {_encoded(str(guid))} -->",
                    f"<!-- anki-model-id: {model_id} -->",
                    "<!-- anki-raw-html: 1 -->",
                ]
                if tags:
                    encoded_tags = _encoded(
                        json.dumps(tags, ensure_ascii=False, separators=(",", ":"))
                    )
                    block.append(f"<!-- anki-tags: {encoded_tags} -->")
                block.extend(["### Front", front, "", "### Back", back])
                if original_material:
                    block.extend(["", "### OriginalMaterial", original_material])
                block.extend(["", "---", ""])
                cards_by_deck[deck_id].append("\n".join(block))
                used_model_ids.add(model_id)

            manifest_files: dict[str, object] = {}
            exported_cards = 0
            for deck_id, relative_markdown in deck_files.items():
                blocks = cards_by_deck.get(deck_id, [])
                if not blocks:
                    continue
                markdown_path = working_folder / relative_markdown
                markdown_path.parent.mkdir(parents=True, exist_ok=True)
                markdown_path.write_text("".join(blocks), encoding="utf-8")
                exported_cards += len(blocks)
                deck_data = decks.get(str(deck_id), {})
                manifest_files[relative_markdown.as_posix()] = {
                    "deck_id": deck_id,
                    "deck_name": deck_names[deck_id],
                    "description": str(deck_data.get("desc", "")),
                }

                target_media_dir = markdown_path.parent / "media"
                for original_name in sorted(
                    media_by_deck.get(deck_id, set()), key=str.casefold
                ):
                    target_media_dir.mkdir(parents=True, exist_ok=True)
                    member = media_by_name[original_name]
                    target_path = target_media_dir / safe_media_names[original_name]
                    with archive.open(member) as source, target_path.open("wb") as target:
                        shutil.copyfileobj(source, target)

            referenced_media = (
                set().union(*media_by_deck.values()) if media_by_deck else set()
            )
            unreferenced_media = set(media_by_name) - referenced_media
            if unreferenced_media:
                unused_dir = working_folder / "unreferenced_media"
                unused_dir.mkdir(parents=True, exist_ok=True)
                for original_name in sorted(unreferenced_media, key=str.casefold):
                    target_path = unused_dir / safe_media_names[original_name]
                    with archive.open(media_by_name[original_name]) as source, target_path.open(
                        "wb"
                    ) as target:
                        shutil.copyfileobj(source, target)

            manifest = {
                "format": "markdown-to-anki-roundtrip-v1",
                "source_package": package_path.name,
                "root_deck_name": common_root,
                "models": {
                    str(model_id): _model_manifest(models[str(model_id)])
                    for model_id in sorted(used_model_ids)
                },
                "files": manifest_files,
                "exported_note_count": exported_cards,
                "source_orphan_note_count": orphan_count,
                "unreferenced_media_count": len(unreferenced_media),
                "unreferenced_media_files": [
                    safe_media_names[name]
                    for name in sorted(unreferenced_media, key=str.casefold)
                ],
            }
            (working_folder / ROUNDTRIP_MANIFEST).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        working_folder.replace(final_folder)
        log(
            f"已导出 {exported_cards} 张卡片、"
            f"{len(referenced_media)} 个被引用媒体文件。"
        )
        if unreferenced_media:
            log(f"另保存 {len(unreferenced_media)} 个未引用媒体到 unreferenced_media。")
        log(f"导出完成：{final_folder}")
        return final_folder
    except Exception:
        shutil.rmtree(working_folder, ignore_errors=True)
        raise


class AnkiMarkdownApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("860x650")
        self.minsize(720, 540)
        self.selected_source: Path | None = None
        self.selected_package: Path | None = None

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        forward = ttk.LabelFrame(self, text="Markdown → Anki", padding=12)
        forward.grid(row=0, column=0, padx=16, pady=(16, 8), sticky="ew")
        forward.columnconfigure(2, weight=1)

        self.select_markdown_button = ttk.Button(
            forward, text="选择 Markdown 文件", command=self.select_markdown_file
        )
        self.select_markdown_button.grid(
            row=0, column=0, padx=(0, 8), sticky="w"
        )
        self.select_folder_button = ttk.Button(
            forward, text="选择牌组文件夹", command=self.select_folder
        )
        self.select_folder_button.grid(
            row=0, column=1, padx=(0, 10), sticky="w"
        )
        self.source_var = tk.StringVar(value="尚未选择 Markdown 或牌组文件夹")
        ttk.Entry(forward, textvariable=self.source_var, state="readonly").grid(
            row=0, column=2, sticky="ew"
        )
        self.generate_button = ttk.Button(
            forward,
            text="生成 .apkg 文件",
            command=self.start_generation,
            state="disabled",
        )
        self.generate_button.grid(
            row=1, column=0, columnspan=3, pady=(10, 0), sticky="ew"
        )

        reverse = ttk.LabelFrame(self, text="Anki → Markdown", padding=12)
        reverse.grid(row=1, column=0, padx=16, pady=(0, 8), sticky="ew")
        reverse.columnconfigure(1, weight=1)
        self.select_package_button = ttk.Button(
            reverse, text="选择 .apkg 文件", command=self.select_package
        )
        self.select_package_button.grid(row=0, column=0, padx=(0, 10), sticky="w")
        self.package_var = tk.StringVar(value="尚未选择 .apkg 文件")
        ttk.Entry(reverse, textvariable=self.package_var, state="readonly").grid(
            row=0, column=1, sticky="ew"
        )
        self.export_button = ttk.Button(
            reverse,
            text="导出 Markdown 与媒体",
            command=self.start_export,
            state="disabled",
        )
        self.export_button.grid(
            row=1, column=0, columnspan=2, pady=(10, 0), sticky="ew"
        )

        console_frame = ttk.LabelFrame(self, text="运行日志", padding=8)
        console_frame.grid(row=2, column=0, padx=16, pady=(0, 16), sticky="nsew")
        console_frame.columnconfigure(0, weight=1)
        console_frame.rowconfigure(0, weight=1)
        self.console = scrolledtext.ScrolledText(
            console_frame,
            wrap="word",
            state="disabled",
            font=("Consolas", 10),
        )
        self.console.grid(row=0, column=0, sticky="nsew")
        self.write_log("请选择转换方向和源文件。反向导出会保留 GUID、模型、牌组与媒体。")

    def write_log(self, message: str) -> None:
        if threading.current_thread() is not threading.main_thread():
            self.after(0, self.write_log, message)
            return
        self.console.configure(state="normal")
        self.console.insert("end", message.rstrip() + "\n")
        self.console.see("end")
        self.console.configure(state="disabled")

    def select_markdown_file(self) -> None:
        chosen = filedialog.askopenfilename(
            title="选择 Markdown 文件",
            filetypes=[("Markdown 文件", "*.md *.markdown"), ("所有文件", "*.*")],
        )
        if chosen:
            self.selected_source = Path(chosen)
            self.source_var.set(str(self.selected_source))
            self.generate_button.configure(state="normal")
            self.write_log(f"正向源：{self.selected_source}")

    def select_folder(self) -> None:
        chosen = filedialog.askdirectory(title="选择牌组文件夹")
        if chosen:
            self.selected_source = Path(chosen)
            self.source_var.set(str(self.selected_source))
            self.generate_button.configure(state="normal")
            self.write_log(f"正向源文件夹：{self.selected_source}")

    def select_package(self) -> None:
        chosen = filedialog.askopenfilename(
            title="选择 Anki 包",
            filetypes=[("Anki 包", "*.apkg"), ("所有文件", "*.*")],
        )
        if chosen:
            self.selected_package = Path(chosen)
            self.package_var.set(str(self.selected_package))
            self.export_button.configure(state="normal")
            self.write_log(f"反向源：{self.selected_package}")

    def _set_busy(self, busy: bool, active: str = "") -> None:
        if busy:
            self.select_markdown_button.configure(state="disabled")
            self.select_folder_button.configure(state="disabled")
            self.select_package_button.configure(state="disabled")
            self.generate_button.configure(
                state="disabled", text="正在生成……" if active == "build" else "生成 .apkg 文件"
            )
            self.export_button.configure(
                state="disabled", text="正在导出……" if active == "export" else "导出 Markdown 与媒体"
            )
            return
        self.select_markdown_button.configure(state="normal")
        self.select_folder_button.configure(state="normal")
        self.select_package_button.configure(state="normal")
        self.generate_button.configure(
            state="normal" if self.selected_source else "disabled",
            text="生成 .apkg 文件",
        )
        self.export_button.configure(
            state="normal" if self.selected_package else "disabled",
            text="导出 Markdown 与媒体",
        )

    def start_generation(self) -> None:
        if self.selected_source is None:
            messagebox.showwarning(APP_TITLE, "请先选择 Markdown 文件或牌组文件夹。")
            return
        self._set_busy(True, "build")
        self.write_log("开始生成 Anki 牌组……")
        threading.Thread(target=self._run_generation, daemon=True).start()

    def _run_generation(self) -> None:
        try:
            assert self.selected_source is not None
            output = build_anki_package(self.selected_source, log=self.write_log)
        except Exception as exc:
            self.write_log(f"生成失败：{exc}")
            self.write_log(traceback.format_exc())
            self.after(0, messagebox.showerror, APP_TITLE, f"生成失败：\n{exc}")
        else:
            self.after(0, messagebox.showinfo, APP_TITLE, f"生成成功！\n\n{output}")
        finally:
            self.after(0, self._set_busy, False)

    def start_export(self) -> None:
        if self.selected_package is None:
            messagebox.showwarning(APP_TITLE, "请先选择 .apkg 文件。")
            return
        self._set_busy(True, "export")
        self.write_log("开始反向导出 Markdown 与媒体……")
        threading.Thread(target=self._run_export, daemon=True).start()

    def _run_export(self) -> None:
        try:
            assert self.selected_package is not None
            output = export_anki_package(self.selected_package, log=self.write_log)
        except Exception as exc:
            self.write_log(f"导出失败：{exc}")
            self.write_log(traceback.format_exc())
            self.after(0, messagebox.showerror, APP_TITLE, f"导出失败：\n{exc}")
        else:
            self.after(0, messagebox.showinfo, APP_TITLE, f"导出成功！\n\n{output}")
        finally:
            self.after(0, self._set_busy, False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Anki 与 Markdown 双向往返工具")
    subparsers = parser.add_subparsers(dest="command")
    build_parser = subparsers.add_parser("build", help="Markdown/文件夹 → .apkg")
    build_parser.add_argument("source", type=Path)
    build_parser.add_argument("-o", "--output", type=Path)
    export_parser = subparsers.add_parser("export", help=".apkg → Markdown/媒体文件夹")
    export_parser.add_argument("package", type=Path)
    export_parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()

    if args.command == "build":
        build_anki_package(args.source, args.output)
    elif args.command == "export":
        export_anki_package(args.package, args.output)
    else:
        AnkiMarkdownApp().mainloop()


if __name__ == "__main__":
    main()
