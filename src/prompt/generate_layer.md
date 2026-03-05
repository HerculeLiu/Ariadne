# Generate Layer Prompt (生成层)

This prompt defines how knowledge markdown should be transformed into the **center learning content area** of an interactive HTML page.

## Layer scope (very important)
- This is the **生成层** prompt.
- The markdown source is produced by **理解层** (`understand_layer.md`).
- The app shell is injected by program code:
  - left navigation sidebar: fixed by program
  - right chat sidebar: fixed by program
- Therefore, this prompt must focus on **middle content only**.
- Do NOT generate or redesign left/right sidebars in this layer.

## Goal
Turn markdown knowledge into a polished, interactive, visually strong center page that feels like a modern product learning experience, not a markdown viewer.

## Default render config
The renderer reads this block as defaults (can be overridden by markdown `render-config` block):

```render-config-default
show_hero: false
back_home_path: /
layout_mode: continuous
nav_collapsible: true
```

## Hard constraints
- Preserve chapter/chunk order and meaning.
- Do not leak raw markdown syntax to UI (`#`, `**`, fenced markers, parser leftovers).
- Convert escaped `\\n` into actual visual line breaks.
- Keep all interaction outputs concrete and non-empty.
- This project is explanation-only. Absolutely prohibit:
  - quiz / flashcard / exam / test / practice
  - score / ranking / checkpoint / mastery
  - any self-review or assessment widgets

## Design quality requirements (skill-enhanced)
Apply these style principles in center content generation:
- `frontend-design`: intentional hierarchy, strong spacing rhythm, clear visual focus, no generic template look.
- `figma`: consistent spacing scale, reusable component style, clean alignment, predictable structure.
- `canvas-design`: tasteful atmosphere (gradient depth / subtle glow / layered cards) with readability first.

## Center content layout contract
- Output should be suitable for insertion into `<main>` content area.
- Preferred structure:
  - chapter sections
  - chunk cards/blocks in continuous vertical flow
  - inline interaction blocks near related chunks
- Keep reading width comfortable and avoid full-width text walls.
- Keep heading hierarchy strong and scan-friendly.

## Component transformation rules
Transform markdown semantics into rich HTML components:
- heading -> section/chunk titles with clear visual scale
- paragraph -> readable text block with comfortable line-height
- list -> semantic list with visual bullets/steps
- table -> styled comparison table (responsive if needed)
- code block -> code panel with readable contrast
- interaction block -> compact demo card with CTA button and inline result area

## Interaction behavior rules
- Button labels should be action-oriented.
- Click must reveal deterministic, concrete output in-place.
- Never produce placeholder outputs like `|`, `...`, `TODO`, `待补充`, or empty strings.

## Few-shot style guidance

### Few-shot 1: avoid markdown-viewer output
Input knowledge (simplified):
```md
## 章节一：RAG基础
### Chunk 1.1：为什么需要检索
大模型会幻觉，检索可以提供外部事实支撑。

```interaction
title: 检索前后对比
input: 问题：某公司2024营收是多少？
output: 未检索：可能编造；已检索：给出来源与数值。
button: 运行示例
```
```

Bad output pattern:
- Dumps raw markdown-looking blocks.
- Shows heading symbols or literal fence markers.
- Interaction output area is blank or placeholder.

Good output pattern (target style):
- Chapter rendered as a visual section header.
- Chunk rendered as a high-readability content card.
- Interaction rendered as a compact demo card with clear button and non-empty result panel.
- Subtle gradients, clear contrast, and clean spacing.

### Few-shot 2: comparison table should become visual matrix
Input knowledge (simplified):
```md
### Chunk 2.2：向量检索 vs 关键词检索
| 对比项 | 向量检索 | 关键词检索 |
| 准确性 | 语义好 | 精确词匹配强 |
| 速度 | 中等 | 快 |
| 场景 | 语义问答 | 精准过滤 |
```

Good output pattern (target style):
- Keep table semantics, but render as clean matrix card.
- Header row visually distinct.
- Zebra rows or subtle separators for readability.
- No markdown table artifacts shown to users.

## Visual polish checklist (self-check before output)
- Does this look like a product learning UI instead of a markdown viewer?
- Is center content visually layered (section/chunk/interaction) with consistent rhythm?
- Are typography and contrast comfortable for long reading?
- Are all interactions non-empty and immediately visible after click?
- Is all assessment/testing content fully absent?
