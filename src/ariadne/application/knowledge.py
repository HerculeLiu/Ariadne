from __future__ import annotations

import html as html_lib
import json
from pathlib import Path
import re

from ariadne.domain.models import Chunk


def markdown_to_chunks(markdown_text: str) -> list[Chunk]:
    """Parse markdown into chunk list.

    Preferred format:
    - ## chapter
    - ### chunk

    Backward compatible fallback:
    - ## chunk
    """
    chunks: list[Chunk] = []
    current_chapter = ""
    current_title = ""
    lines: list[str] = []
    order = 1
    has_h3 = False

    for raw in markdown_text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if current_title:
                chunks.append(Chunk(id=f"mdck_{order}", title=current_title, content="\n".join(lines).strip(), order_no=order))
                order += 1
                lines = []
                current_title = ""
            current_chapter = line[3:].strip()
            continue

        if line.startswith("### "):
            has_h3 = True
            if current_title:
                chunks.append(Chunk(id=f"mdck_{order}", title=current_title, content="\n".join(lines).strip(), order_no=order))
                order += 1
                lines = []
            chunk_name = line[4:].strip()
            current_title = f"{current_chapter} / {chunk_name}" if current_chapter else chunk_name
            continue

        if current_title:
            lines.append(line)

    if current_title:
        chunks.append(Chunk(id=f"mdck_{order}", title=current_title, content="\n".join(lines).strip(), order_no=order))

    if chunks:
        return chunks

    # Fallback for old markdown where ## directly used as chunk.
    title = None
    lines = []
    order = 1
    for raw in markdown_text.splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            if title is not None:
                chunks.append(Chunk(id=f"mdck_{order}", title=title, content="\n".join(lines).strip(), order_no=order))
                order += 1
                lines = []
            title = line[3:].strip()
        else:
            if title is not None:
                lines.append(line)
    if title is not None:
        chunks.append(Chunk(id=f"mdck_{order}", title=title, content="\n".join(lines).strip(), order_no=order))
    return chunks


def chunks_to_markdown(topic: str, chunks: list[Chunk], material_lines: list[str] | None = None) -> str:
    lines: list[str] = [f"# {topic}", ""]
    if material_lines:
        lines.extend(["## 参考资料", *material_lines, ""])

    lines.append("## 章节1：核心讲解")
    lines.append("")
    for chunk in sorted(chunks, key=lambda x: x.order_no):
        lines.extend([f"### Chunk 1.{chunk.order_no}：{chunk.title}", chunk.content.strip(), ""])

    return "\n".join(lines).strip() + "\n"


def _parse_interaction_block(block_lines: list[str]) -> dict[str, str]:
    data: dict[str, str] = {}
    for raw in block_lines:
        line = raw.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip().lower()] = value.strip()
    return data


def _load_generation_layer_defaults() -> dict[str, str]:
    """Load 生成层默认渲染配置 from prompt file (hot-loaded by file read each render)."""
    defaults = {
        "show_hero": "false",
        "back_home_path": "/",
        "layout_mode": "continuous",
        "nav_collapsible": "true",
    }
    prompt_path = Path(__file__).resolve().parents[3] / "src" / "prompt" / "generate_layer.md"
    if not prompt_path.exists():
        return defaults

    lines = prompt_path.read_text(encoding="utf-8").splitlines()
    in_code = False
    code_lang = ""
    code_lines: list[str] = []
    for raw in lines:
        line = raw.strip()
        if line.startswith("```"):
            if not in_code:
                in_code = True
                code_lang = line[3:].strip().lower()
                code_lines = []
                continue
            in_code = False
            if code_lang in {"render-config-default", "render-config", "generate-config"}:
                cfg = _parse_interaction_block(code_lines)
                if cfg:
                    defaults.update(cfg)
            continue
        if in_code:
            code_lines.append(raw)
    return defaults


def _courseware_shell_template_path() -> Path:
    return Path(__file__).resolve().parents[3] / "frontend" / "templates" / "courseware_shell.html"


def _render_courseware_shell_html(*, escaped_title: str, payload_json: str, config_json: str) -> str:
    template_path = _courseware_shell_template_path()
    if not template_path.exists():
        raise FileNotFoundError(f"courseware shell template not found: {template_path}")
    template = template_path.read_text(encoding="utf-8")
    try:
        return template.format(
            escaped_title=escaped_title,
            payload_json=payload_json,
            config_json=config_json,
            initial_content_html=_build_initial_content_html_from_payload_json(payload_json),
            initial_nav_html=_build_initial_nav_html_from_payload_json(payload_json),
        )
    except KeyError as exc:
        raise ValueError(f"invalid placeholder in courseware shell template: {exc}") from exc


def _build_initial_content_html_from_payload_json(payload_json: str) -> str:
    try:
        payload = json.loads(payload_json)
    except Exception:  # noqa: BLE001
        return ""
    chapters = payload.get("chapters") if isinstance(payload, dict) else []
    if not isinstance(chapters, list):
        return ""

    parts: list[str] = []
    for ci, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            continue
        chapter_title = str(chapter.get("title", "") or "").strip()
        chunks = chapter.get("chunks")
        if not isinstance(chunks, list):
            chunks = []
        chunk_parts: list[str] = []
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            title = str(chunk.get("title", "") or "").strip()
            paragraphs = chunk.get("paragraphs") if isinstance(chunk.get("paragraphs"), list) else []
            bullets = chunk.get("bullets") if isinstance(chunk.get("bullets"), list) else []
            if not title and not paragraphs and not bullets:
                continue
            body: list[str] = []
            if title:
                body.append(f"<h2>{html_lib.escape(title)}</h2>")
            for p in paragraphs:
                txt = str(p).strip()
                if txt:
                    body.append(f"<p>{html_lib.escape(txt)}</p>")
            bullet_items = [f"<li>{html_lib.escape(str(x).strip())}</li>" for x in bullets if str(x).strip()]
            if bullet_items:
                body.append(f"<ul>{''.join(bullet_items)}</ul>")
            chunk_parts.append(f"<article class='chunk-card' id='chunk-{ci}-ssr'>{''.join(body)}</article>")
        if not chunk_parts and not chapter_title:
            continue
        chapter_head = (
            f"<div class='chapter-head'><h2 class='chapter-title'>{html_lib.escape(chapter_title)}</h2></div>"
            if chapter_title
            else ""
        )
        parts.append(f"<section class='chapter-block' id='chapter-{ci}'>{chapter_head}{''.join(chunk_parts)}</section>")
    return "".join(parts)


def _build_initial_nav_html_from_payload_json(payload_json: str) -> str:
    try:
        payload = json.loads(payload_json)
    except Exception:  # noqa: BLE001
        return ""
    chapters = payload.get("chapters") if isinstance(payload, dict) else []
    if not isinstance(chapters, list):
        return ""

    groups: list[str] = []
    for ci, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            continue
        chapter_title = str(chapter.get("title", "") or "").strip() or f"Section {ci + 1}"
        chunks = chapter.get("chunks")
        if not isinstance(chunks, list):
            chunks = []
        chunk_buttons: list[str] = []
        for ki, chunk in enumerate(chunks):
            if not isinstance(chunk, dict):
                continue
            chunk_title = str(chunk.get("title", "") or "").strip() or f"Chunk {ci + 1}.{ki + 1}"
            chunk_buttons.append(
                f"<button class='chunk-nav-item' type='button' data-chapter='{ci}' data-chunk='{ki}'>{html_lib.escape(chunk_title)}</button>"
            )
        groups.append(
            "<div class='chapter-group'>"
            f"<button class='chapter-item{' active' if ci == 0 else ''}' type='button' data-chapter='{ci}'>{html_lib.escape(chapter_title)}</button>"
            f"<div class='chunk-nav-list'>{''.join(chunk_buttons)}</div>"
            "</div>"
        )
    return "".join(groups)


def markdown_to_html(markdown_text: str) -> str:
    lines = markdown_text.splitlines()
    title = "Ariadne Knowledge"
    chapters: list[dict] = []
    render_config: dict[str, str] = _load_generation_layer_defaults()
    current_chapter: dict | None = None
    current_chunk: dict | None = None
    in_code = False
    code_lang = ""
    code_lines: list[str] = []
    table_rows: list[list[str]] = []

    def ensure_chapter() -> dict:
        nonlocal current_chapter
        if current_chapter is None:
            current_chapter = {"title": "默认章节", "chunks": []}
            chapters.append(current_chapter)
        return current_chapter

    def ensure_chunk() -> dict:
        nonlocal current_chunk
        chapter = ensure_chapter()
        if current_chunk is None:
            current_chunk = {"title": "核心内容", "paragraphs": [], "bullets": [], "tables": [], "interactions": [], "codes": []}
            chapter["chunks"].append(current_chunk)
        return current_chunk

    def flush_table() -> None:
        nonlocal table_rows
        if not table_rows:
            return
        chunk = ensure_chunk()
        header = table_rows[0]
        body = table_rows[1:] if len(table_rows) > 1 else []
        chunk["tables"].append({"header": header, "rows": body})
        table_rows = []

    for raw in lines:
        line = raw.rstrip()
        stripped = line.strip()

        if stripped.startswith("```"):
            if not in_code:
                in_code = True
                code_lang = stripped[3:].strip().lower()
                code_lines = []
                continue
            in_code = False
            chunk = ensure_chunk()
            if code_lang in {"render-config", "html-config", "render"}:
                for k, v in _parse_interaction_block(code_lines).items():
                    render_config[k] = v
            elif code_lang == "interaction":
                chunk["interactions"].append(_parse_interaction_block(code_lines))
            else:
                chunk["codes"].append("\n".join(code_lines).strip())
            continue

        if in_code:
            code_lines.append(line)
            continue

        if stripped.startswith("|") and stripped.endswith("|"):
            cols = [x.strip() for x in stripped.strip("|").split("|")]
            if all(set(c) <= {"-", ":", " "} for c in cols):
                continue
            table_rows.append(cols)
            continue
        flush_table()

        if not stripped:
            continue

        if stripped.startswith("# "):
            title = stripped[2:].strip() or title
            continue

        if stripped.startswith("## "):
            current_chapter = {"title": stripped[3:].strip(), "chunks": []}
            chapters.append(current_chapter)
            current_chunk = None
            continue

        if stripped.startswith("### "):
            chapter = ensure_chapter()
            current_chunk = {
                "title": stripped[4:].strip(),
                "paragraphs": [],
                "bullets": [],
                "tables": [],
                "interactions": [],
                "codes": [],
            }
            chapter["chunks"].append(current_chunk)
            continue

        chunk = ensure_chunk()
        if stripped.startswith("#### "):
            chunk["paragraphs"].append(stripped[5:].strip())
        elif stripped.startswith("- "):
            chunk["bullets"].append(stripped[2:].strip())
        elif re.match(r"^\d+\.\s+", stripped):
            chunk["bullets"].append(re.sub(r"^\d+\.\s+", "", stripped))
        else:
            chunk["paragraphs"].append(stripped)

    flush_table()

    if not chapters:
        clean = markdown_text.strip()
        chapters = [
            {
                "title": "",
                "chunks": [
                    {
                        "title": "",
                        "paragraphs": [clean] if clean else [],
                        "bullets": [],
                        "tables": [],
                        "interactions": [],
                        "codes": [],
                    }
                ],
            }
        ]

    payload = {"title": title, "chapters": chapters}
    payload_json = json.dumps(payload, ensure_ascii=False)
    config_json = json.dumps(render_config, ensure_ascii=False)

    escaped_title = html_lib.escape(title)
    return _render_courseware_shell_html(
        escaped_title=escaped_title,
        payload_json=payload_json,
        config_json=config_json,
    )


class KnowledgeDocStore:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, courseware_id: str, markdown_text: str) -> str:
        path = self.base_dir / f"{courseware_id}.md"
        path.write_text(markdown_text, encoding="utf-8")
        return str(path)

    def load(self, courseware_id: str) -> str:
        path = self.base_dir / f"{courseware_id}.md"
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")
