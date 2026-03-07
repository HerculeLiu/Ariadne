# Ariadne Chunk 智能修改功能方案

**版本**: v1.1
**日期**: 2026-03-07
**状态**: 设计中

---

## 1. 概述

### 1.1 背景

用户在课件生成后，需要对部分 chunk 内容进行优化调整。现有 `rewrite_draft` 功能支持单个 chunk 的 AI 重写，但缺少：

1. **批量处理能力**：无法一次评估和修改多个 chunks
2. **智能筛选**：无法自动判断哪些 chunks 需要修改
3. **自然触发**：必须通过专门 API 调用，无法在 chat 中自然表达

### 1.2 目标

实现基于意图识别的智能 chunk 修改功能：

- 支持两种触发方式：显式按钮 + 自然语言
- 智能判断哪些 chunks 需要修改（仅评估已选 chunks）
- 在 Chat 中可视化展示修改建议
- 用户可逐个确认或批量应用修改

### 1.3 设计原则

| 原则 | 说明 |
|------|------|
| **意图识别优先** | 所有请求先经过意图识别，判断是否为修改请求 |
| **仅处理已选** | 只分析用户已选的 chunks，不遍历全部 |
| **格式一致** | UI 与现有生成/交互格式保持一致 |
| **纯 AI 编辑** | 不提供手动编辑功能 |
| **Outline 同步** | Chunk 标题变化时同步更新 outline |

---

## 2. 两种触发方式

### 2.1 触发方式对比

| 特性 | 显式模式（点击按钮） | 隐式模式（自然语言） |
|------|---------------------|---------------------|
| **入口** | 功能栏 "AI 编辑" 按钮 | Chat 直接输入 |
| **用户意图** | 明确表达修改需求 | 需要意图识别判断 |
| **提示词** | "你现在需要AI编辑，告诉我..." | "你分析一下是否需要修改..." |
| **返回结果** | 必然返回修改建议 | 可能是修改建议 OR 正常回复 |

### 2.2 完整流程图

```
┌─────────────────────────────────────────────────────────────────────────┐
│                              用户操作入口                                │
├─────────────────────────────────────────┬───────────────────────────────┤
│           显式模式（点击按钮）            │      隐式模式（自然语言）      │
│                                         │                               │
│  1. 点击功能栏 "AI 编辑" 按钮            │  1. Chat 输入："帮我把改详细点" │
│  2. 弹出输入框收集修改意图               │  2. 直接发送消息               │
│  3. 用户输入："让内容更通俗易懂"          │                               │
└─────────────────────────────────────────┴───────────────────────────────┘
                                        │
                                        ▼
                            ┌───────────────────────┐
                            │      意图识别层       │
                            │      (必经环节)       │
                            └───────────────────────┘
                                        │
                    ┌───────────────────┴───────────────────┐
                    ▼                                       ▼
            ┌───────────────┐                   ┌───────────────┐
            │   显式模式     │                   │   隐式模式     │
            │   Prompt      │                   │   Prompt      │
            └───────────────┘                   └───────────────┘
                    │                                       │
    "你现在需要AI编辑，          "你分析一下是否需要修改，
     告诉我需要修改哪几个，         如果有修改哪几个，
     修改意见是什么"               每一个的修改意见是什么"
                    │                                       │
                    └───────────────┬───────────────────────┘
                                    ▼
                    ┌───────────────────────────────┐
                    │  LLM 返回 JSON 分析结果        │
                    │  {                            │
                    │    is_modification: bool,     │
                    │    chunks: [],                │
                    │    chat_reply?: string        │
                    │  }                            │
                    └───────────────────────────────┘
                                    │
                        ┌───────────┴───────────┐
                        ▼                       ▼
                is_modification=true    is_modification=false
                        │                       │
                        ▼                       ▼
            ┌───────────────────┐   ┌───────────────────┐
            │  Chat 可视化展示   │   │  正常 Chat 回复    │
            │  修改建议卡片      │   │                   │
            │  用户确认/放弃     │   │                   │
            └───────────────────┘   └───────────────────┘
```

---

## 3. 意图识别层设计

### 3.1 显式模式 Prompt

**文件**: `src/prompt/intent_edit_explicit.md`

```markdown
你现在是 AI 编辑助手。用户已显式要求对选中的 chunks 进行编辑。

## 任务
分析用户的修改意图，对已选 chunks 进行评估：
- 哪些 chunks 需要修改
- 修改的原因是什么
- 生成修改后的内容
- 如果 chunk 标题需要调整，一并返回新标题

## 用户意图
{user_intent}

## 已选 Chunks
{chunks}

## 要求
- 只分析用户已选的 chunks，不要考虑其他 chunks
- 用户已明确要求编辑，请积极给出修改建议
- 每个需要修改的 chunk 都要生成完整的修改后内容
- 如果修改后内容主题发生变化，建议同时更新 chunk 标题
- 标题变化需要同步到 outline 中

## 返回格式（JSON）
{
  "is_modification": true,
  "chunks": [
    {
      "key": "章节索引-chunk索引",
      "label": "章节 · chunk 标题",
      "should_modify": true,
      "reason": "修改原因说明",
      "new_title": "新的 chunk 标题（如果需要修改标题，否则返回原标题）",
      "rewritten_content": "修改后的完整内容"
    }
  ]
}
```

### 3.2 隐式模式 Prompt

**文件**: `src/prompt/intent_edit_implicit.md`

```markdown
你是一个智能助手。用户发送了一条消息，请分析用户意图。

## 用户消息
{user_message}

## 当前已选 Chunks
{chunks}

## 任务
判断要点：
1. 这是修改 chunks 的请求吗？还是普通聊天提问？
2. 如果是修改请求，需要修改哪些已选 chunks？
3. chunk 标题是否需要调整以反映内容变化？

## 要求
- 只考虑用户已选的 chunks
- 如果不是修改请求，返回正常的聊天回复
- 如果是修改请求，返回需要修改的 chunks 列表
- 如果内容主题发生变化，建议同时更新 chunk 标题
- 标题变化需要同步到 outline 中

## 返回格式（JSON）
{
  "is_modification": true/false,
  "chat_reply": "普通聊天回复（仅当 is_modification=false 时）",
  "chunks": [
    {
      "key": "章节索引-chunk索引",
      "label": "章节 · chunk 标题",
      "should_modify": true,
      "reason": "修改原因说明",
      "new_title": "新的 chunk 标题（如果需要修改标题，否则返回原标题）",
      "rewritten_content": "修改后的完整内容"
    }
  ]
}
```

---

## 4. 数据模型

### 4.1 意图识别请求

```python
@dataclass
class IntentAnalysisRequest:
    message: str              # 用户输入的消息/意图
    chunks: List[dict]        # 已选 chunks 列表
    explicit_mode: bool       # 是否显式模式
```

### 4.2 Chunk 输入格式

```python
# chatState.selectedChunks 中的每个 chunk 结构
{
    "key": "0-1",              # `${chapterIdx}-${chunkIdx}`
    "label": "Chapter 1 · Chunk 2",
    "title": "向量索引的基本原理",
    "content": "chunk的完整内容..."
}
```

### 4.3 意图识别响应

```python
@dataclass
class IntentAnalysisResponse:
    is_modification: bool
    chat_reply: Optional[str] = None       # 非修改请求时的正常回复
    chunks: List[ChunkSuggestion] = field(default_factory=list)

@dataclass
class ChunkSuggestion:
    key: str                   # chunk 唯一标识
    label: str                 # 显示标签
    should_modify: bool        # 是否建议修改
    reason: str                # 修改原因
    new_title: Optional[str]   # 新标题（如果标题需要变化）
    rewritten_content: str     # 修改后内容
```

---

## 5. 后端实现

### 5.1 API Facade

**文件**: `src/ariadne/api/facade.py`

```python
def analyze_intent(self, payload: Dict[str, Any]) -> Dict[str, Any]:
    """意图识别：判断是否需要修改 chunks，返回修改建议

    Args:
        payload: {
            "message": str,           # 用户消息/意图
            "chunks": List[dict],     # 已选 chunks
            "explicit_mode": bool     # 是否显式模式
        }

    Returns:
        {
            "is_modification": bool,
            "chat_reply": str | None,
            "chunks": [
                {
                    "key": str,
                    "label": str,
                    "should_modify": bool,
                    "reason": str,
                    "rewritten_content": str
                }
            ]
        }
    """
    message = payload.get("message", "")
    chunks = payload.get("chunks", [])
    explicit_mode = payload.get("explicit_mode", False)

    if not chunks:
        return self._ok({
            "is_modification": False,
            "chat_reply": "请先选择要修改的 chunks"
        })

    # 选择 prompt
    if explicit_mode:
        system_prompt = self.prompts.get("intent_edit_explicit.md")
    else:
        system_prompt = self.prompts.get("intent_edit_implicit.md")

    # 构建用户 prompt
    chunks_text = "\n".join([
        f"- {c['key']}: {c['label']}\n  内容: {c['content'][:200]}..."
        for c in chunks
    ])

    user_prompt = f"""用户消息: {message}

已选 Chunks:
{chunks_text}"""

    # 调用 LLM
    response = self.llm._chat([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt}
    ])

    # 解析 JSON 响应
    result = self._parse_json_response(response)
    return self._ok(result)
```

### 5.2 HTTP 路由

**文件**: `src/ariadne/api/http_server.py`

```python
# 新增路由
if path == "/api/v1/intent/analyze" and method == "POST":
    content_length = int(self.headers.get('Content-Length', 0))
    body = self.rfile.read(content_length).decode('utf-8')
    payload = json.loads(body)
    result = api.analyze_intent(payload)
    self._json_response(result)
```

### 5.3 应用修改

```python
def apply_chunk_modification(self, payload: Dict[str, Any]) -> Dict[str, Any]:
    """应用 chunk 修改

    Args:
        payload: {
            "chunk_id": str,
            "new_content": str,
            "new_title": str | None,     # 新标题（可选）
            "expected_version": int
        }
    """
    result = self.courseware.apply_rewrite(
        draft=RewriteDraft(
            id="",  # 不需要持久化 draft
            original="",  # 不需要
            rewritten=payload["new_content"]
        ),
        expected_version=payload.get("expected_version", 0)
    )

    # 如果标题有变化，同步更新 outline
    new_title = payload.get("new_title")
    if new_title:
        self._sync_outline_title(payload["chunk_id"], new_title)

    return self._ok(result)


def _sync_outline_title(self, chunk_id: str, new_title: str) -> None:
    """同步 chunk 标题到 outline

    Args:
        chunk_id: chunk 唯一标识 (格式: "chapterIdx-chunkIdx")
        new_title: 新的 chunk 标题
    """
    # 解析 chunk_id 获取章节和 chunk 索引
    parts = chunk_id.split("-")
    if len(parts) != 2:
        return

    chapter_idx, chunk_idx = int(parts[0]), int(parts[1])

    # 获取 courseware
    courseware = self.repos["courseware"].get_current()

    # 更新 outline 中对应 chunk 的标题
    if courseware.outline and chapter_idx < len(courseware.outline):
        chapter = courseware.outline[chapter_idx]
        if chunk_idx < len(chapter.get("chunks", [])):
            chapter["chunks"][chunk_idx]["title"] = new_title

    # 持久化更新后的 courseware
    self.repos["courseware"].update_outline(
        courseware.id, courseware.outline
    )
```

---

## 6. Outline 同步机制

### 6.1 背景

Chunk 修改可能涉及标题变化，此时需要同步更新 courseware 的 outline，保持数据一致性。

### 6.2 触发条件

| 条件 | 说明 |
|------|------|
| **标题变化** | LLM 返回的 `new_title` 与原标题不同 |
| **主题变更** | 修改后内容主题发生明显变化 |

### 6.3 同步流程

```
┌─────────────────────────────────────────────────────────────────┐
│                     应用 Chunk 修改                              │
│                                                                 │
│  用户点击 "应用修改"                                             │
│        │                                                        │
│        ▼                                                        │
│  POST /api/v1/chunks/apply                                      │
│  {                                                              │
│    chunk_id: "0-1",                                             │
│    new_content: "...",                                          │
│    new_title: "向量索引的工作原理"  // 标题有变化                │
│  }                                                              │
│        │                                                        │
│        ▼                                                        │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  1. 更新 chunk 内容                                      │   │
│  │  2. 检查 new_title 是否存在                              │   │
│  │  3. 如果存在，同步更新 outline                           │   │
│  └─────────────────────────────────────────────────────────┘   │
│        │                                                        │
│        ▼                                                        │
│  解析 chunk_id: "0-1" → chapterIdx=0, chunkIdx=1               │
│        │                                                        │
│        ▼                                                        │
│  更新 outline[0].chunks[1].title                                │
│        │                                                        │
│        ▼                                                        │
│  持久化 courseware (markdown 文件同步更新)                      │
│        │                                                        │
│        ▼                                                        │
│  返回成功                                                       │
└─────────────────────────────────────────────────────────────────┘
```

### 6.4 Outline 数据结构

```python
# courseware.outline 结构
outline = [
    {
        "title": "第一章 向量检索基础",
        "summary": "本章介绍...",
        "chunks": [
            {
                "id": "ch1-chunk1",
                "title": "向量索引的工作原理",  # ← 这里需要同步
                "order_no": 1
            },
            {
                "id": "ch1-chunk2",
                "title": "Embedding 模型介绍",
                "order_no": 2
            }
        ]
    }
]
```

### 6.5 前端同步提示

```javascript
async function applyChunk(chunkKey, newContent, newTitle) {
    const response = await fetch("/api/v1/chunks/apply", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            chunk_id: chunkKey,
            new_content: newContent,
            new_title: newTitle,  // 传递新标题
            expected_version: courseware.current_version
        })
    });

    if (response.ok) {
        const result = await response.json();

        // 如果 outline 也更新了，提示用户
        if (result.data.outline_updated) {
            showToast(`✓ Chunk 已更新，Outline 标题已同步`);
        } else {
            showToast(`✓ Chunk 已更新`);
        }

        refreshChunkDisplay(chunkKey, newContent);
        markChunkApplied(chunkKey);
    }
}
```

---

## 7. 前端实现

### 6.1 功能栏按钮

**文件**: `frontend/templates/courseware_shell.html`

```html
<!-- Chat 功能栏 -->
<div class="chat-toolbar">
    <button id="ai-edit-btn" class="toolbar-btn" title="AI 编辑已选 Chunks">
        <svg><!-- AI 图标 --></svg>
        <span>AI 编辑</span>
    </button>
    <!-- 其他按钮... -->
</div>
```

### 6.2 显式模式触发

```javascript
// 点击 AI 编辑按钮
document.getElementById('ai-edit-btn').addEventListener('click', async () => {
    if (chatState.selectedChunks.length === 0) {
        showToast('请先选择要编辑的 chunks');
        return;
    }

    const intent = prompt(
        "请描述你希望如何优化这些 chunks：\n\n" +
        "例如：\n" +
        "• 让内容更通俗易懂\n" +
        "• 增加更多实例\n" +
        "• 调整语气更正式\n" +
        "• 补充技术细节"
    );

    if (intent) {
        await analyzeAndShow(intent, true);
    }
});
```

### 6.3 隐式模式触发

```javascript
// 拦截普通 chat 消息
async function handleSendMessage(message) {
    // 如果有已选 chunks，先进行意图识别
    if (chatState.selectedChunks.length > 0) {
        const result = await analyzeIntent(message, false);
        if (result.is_modification) {
            showModificationSuggestions(result.chunks);
            return;
        }
    }

    // 正常 chat
    sendChatMessage(message);
}
```

### 6.4 意图识别调用

```javascript
async function analyzeIntent(message, explicitMode) {
    const response = await fetch("/api/v1/intent/analyze", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({
            message: message,
            chunks: chatState.selectedChunks,
            explicit_mode: explicitMode
        })
    });

    const data = await response.json();
    return data.data;
}
```

### 6.5 Chat 可视化展示

```javascript
function showModificationSuggestions(chunks) {
    const modifyCount = chunks.filter(c => c.should_modify).length;

    addMessage("assistant", {
        type: "chunk-modification",
        content: `🤧 AI 分析完成，为你准备了对 ${chunks.length} 个 chunk 的优化建议：`,
        chunks: chunks.map(c => ({
            key: c.key,
            label: c.label,
            should_modify: c.should_modify,
            reason: c.reason,
            original: getChunkOriginalContent(c.key),
            rewritten: c.rewritten_content
        }))
    });
}
```

### 6.6 Chunk 卡片 UI

```html
<!-- 消息类型: chunk-modification -->
<div class="msg assistant">
    <div class="msg-content">
        <div class="msg-text">{{content}}</div>

        <div class="chunk-mod-list">
            {{#each chunks}}
            <div class="chunk-mod-card {{#if should_modify}}modify{{else}}skip{{/if}}">
                <div class="chunk-mod-header">
                    <span class="chunk-mod-label">{{label}}</span>
                    <span class="chunk-mod-badge">
                        {{#if should_modify}}建议修改{{else}}无需修改{{/if}}
                    </span>
                </div>

                {{#if should_modify}}
                <div class="chunk-mod-reason">{{reason}}</div>

                <details class="chunk-mod-preview">
                    <summary>展开对比</summary>
                    <div class="chunk-mod-diff">
                        <div class="diff-original">
                            <h4>原文</h4>
                            <pre>{{original}}</pre>
                        </div>
                        <div class="diff-rewritten">
                            <h4>修改后</h4>
                            <pre>{{rewritten}}</pre>
                        </div>
                    </div>
                </details>

                <div class="chunk-mod-actions">
                    <button class="btn-accept" onclick="applyChunk('{{key}}', '{{rewritten}}')">
                        ✓ 应用修改
                    </button>
                    <button class="btn-skip" onclick="skipChunk('{{key}}')">
                        ✗ 跳过
                    </button>
                </div>
                {{/if}}
            </div>
            {{/each}}
        </div>

        <div class="chunk-mod-batch-actions">
            <button class="btn-apply-all" onclick="applyAllModifications()">
                ✓ 应用全部建议
            </button>
            <button class="btn-skip-all" onclick="skipAllModifications()">
                ✗ 全部跳过
            </button>
        </div>
    </div>
</div>
```

### 7.6 应用修改

```javascript
// Chunk 修改状态枚举
const ChunkModStatus = {
    PENDING: 'pending',       // 待处理
    APPLYING: 'applying',     // 应用中
    SUCCESS: 'success',       // 成功
    FAILED: 'failed'          // 失败
};

// 全局状态管理
const chunkModifications = new Map();  // key -> status

async function applyChunk(chunkKey, newContent, newTitle) {
    // 更新状态为"应用中"
    setChunkStatus(chunkKey, ChunkModStatus.APPLYLYING);

    try {
        const response = await fetch("/api/v1/chunks/apply", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify({
                chunk_id: chunkKey,
                new_content: newContent,
                new_title: newTitle,
                expected_version: courseware.current_version
            })
        });

        if (response.ok) {
            const result = await response.json();

            // 如果 outline 也更新了，提示用户
            if (result.data.outline_updated) {
                showToast(`✓ Chunk 已更新，Outline 标题已同步`);
            } else {
                showToast(`✓ Chunk 已更新`);
            }

            refreshChunkDisplay(chunkKey, newContent);
            setChunkStatus(chunkKey, ChunkModStatus.SUCCESS);
        } else {
            throw new Error('修改失败');
        }
    } catch (error) {
        setChunkStatus(chunkKey, ChunkModStatus.FAILED);
        showToast(`✗ 修改失败: ${error.message}`);
    }
}

function setChunkStatus(chunkKey, status) {
    chunkModifications.set(chunkKey, status);
    updateChunkCardUI(chunkKey, status);
}

function updateChunkCardUI(chunkKey, status) {
    const card = document.querySelector(`[data-chunk-key="${chunkKey}"]`);
    if (!card) return;

    // 移除所有状态类
    card.classList.remove('applying', 'success', 'failed');

    // 添加新状态类
    card.classList.add(status);

    // 更新按钮状态
    const actions = card.querySelector('.chunk-mod-actions');
    if (status === ChunkModStatus.APPLYING) {
        actions.innerHTML = `<span class="status-applying">应用中...</span>`;
    } else if (status === ChunkModStatus.SUCCESS) {
        actions.innerHTML = `<span class="status-success">✓ 已应用</span>`;
    } else if (status === ChunkModStatus.FAILED) {
        actions.innerHTML = `
            <span class="status-failed">✗ 失败</span>
            <button class="btn-retry" onclick="applyChunk('${chunkKey}', ...)">重试</button>
        `;
    }
}

// 批量应用
async function applyAllModifications() {
    const chunksToApply = Array.from(chunkModifications.entries())
        .filter(([key, status]) => status === ChunkModStatus.PENDING);

    // 显示总体进度
    showBatchProgress(chunksToApply.length);

    for (let i = 0; i < chunksToApply.length; i++) {
        const [key, _] = chunksToApply[i];
        await applyChunk(key, ...);
        updateBatchProgress(i + 1, chunksToApply.length);
    }

    hideBatchProgress();
}

function showBatchProgress(total) {
    // 显示进度条
    const progressHTML = `
        <div class="batch-progress">
            <div class="progress-bar">
                <div class="progress-fill" style="width: 0%"></div>
            </div>
            <div class="progress-text">正在应用 0/${total}</div>
        </div>
    `;
    // 插入到合适位置
}

function updateBatchProgress(current, total) {
    const percentage = (current / total) * 100;
    document.querySelector('.progress-fill').style.width = `${percentage}%`;
    document.querySelector('.progress-text').textContent = `正在应用 ${current}/${total}`;
}
```

---

## 8. 样式规范

### 8.1 Chunk 修改卡片样式

```css
/* Chunk 修改卡片 */
.chunk-mod-card {
    background: var(--bg-surface);
    border: 1px solid var(--border-light);
    border-radius: 8px;
    padding: 12px;
    margin: 8px 0;
}

.chunk-mod-card.modify {
    border-left: 3px solid var(--accent-primary);
}

.chunk-mod-card.skip {
    border-left: 3px solid var(--text-muted);
    opacity: 0.7;
}

.chunk-mod-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
}

.chunk-mod-label {
    font-weight: 500;
    color: var(--text-primary);
}

.chunk-mod-badge {
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
    background: var(--accent-bg);
    color: var(--accent-primary);
}

.chunk-mod-reason {
    font-size: 13px;
    color: var(--text-secondary);
    margin-bottom: 8px;
}

.chunk-mod-preview {
    margin: 8px 0;
}

.chunk-mod-diff {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 12px;
    margin-top: 8px;
}

.diff-original, .diff-rewritten {
    background: var(--bg-tertiary);
    border-radius: 4px;
    padding: 8px;
}

.diff-original h4, .diff-rewritten h4 {
    font-size: 12px;
    color: var(--text-secondary);
    margin: 0 0 4px 0;
}

.chunk-mod-actions {
    display: flex;
    gap: 8px;
    margin-top: 8px;
}

.chunk-mod-batch-actions {
    display: flex;
    gap: 12px;
    margin-top: 16px;
    padding-top: 12px;
    border-top: 1px solid var(--border-light);
}

/* Chunk 修改状态样式 */
.chunk-mod-card.applying {
    border-left-color: var(--warning-color);
    opacity: 0.8;
}

.chunk-mod-card.success {
    border-left-color: var(--success-color);
    background: var(--success-bg);
}

.chunk-mod-card.failed {
    border-left-color: var(--error-color);
}

/* 状态指示器 */
.status-applying {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--warning-color);
    font-size: 13px;
}

.status-applying::before {
    content: "";
    width: 14px;
    height: 14px;
    border: 2px solid var(--warning-color);
    border-top-color: transparent;
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
}

@keyframes spin {
    to { transform: rotate(360deg); }
}

.status-success {
    color: var(--success-color);
    font-size: 13px;
}

.status-failed {
    color: var(--error-color);
    font-size: 13px;
    margin-right: 8px;
}

.btn-retry {
    padding: 4px 12px;
    border: 1px solid var(--error-color);
    border-radius: 4px;
    background: transparent;
    color: var(--error-color);
    font-size: 12px;
    cursor: pointer;
}

.btn-retry:hover {
    background: var(--error-bg);
}

/* 批量进度条 */
.batch-progress {
    margin: 16px 0;
    padding: 12px;
    background: var(--bg-tertiary);
    border-radius: 8px;
}

.progress-bar {
    height: 4px;
    background: var(--border-light);
    border-radius: 2px;
    overflow: hidden;
    margin-bottom: 8px;
}

.progress-fill {
    height: 100%;
    background: var(--accent-primary);
    transition: width 0.3s ease;
}

.progress-text {
    font-size: 12px;
    color: var(--text-secondary);
    text-align: center;
}
```

---

## 9. 实施计划

### Phase 1: Prompt 文件
- [ ] 1.1 创建 `src/prompt/intent_edit_explicit.md`
- [ ] 1.2 创建 `src/prompt/intent_edit_implicit.md`

### Phase 2: 后端 API
- [ ] 2.1 `facade.py` 添加 `analyze_intent()` 方法
- [ ] 2.2 `facade.py` 添加 `apply_chunk_modification()` 方法
- [ ] 2.3 `http_server.py` 添加路由

### Phase 3: 前端入口
- [ ] 3.1 功能栏添加 "AI 编辑" 按钮
- [ ] 3.2 显式模式触发逻辑
- [ ] 3.3 隐式模式消息拦截

### Phase 4: Chat 可视化
- [ ] 4.1 修改建议消息类型
- [ ] 4.2 Chunk 卡片 UI 组件
- [ ] 4.3 对比预览功能

### Phase 5: 应用与反馈
- [ ] 5.1 单个应用功能
- [ ] 5.2 批量应用功能
- [ ] 5.3 应用后刷新显示
- [ ] 5.4 修改进度状态管理
- [ ] 5.5 失败重试机制

### Phase 6: Outline 同步
- [ ] 6.1 标题变化检测
- [ ] 6.2 同步更新 outline 数据
- [ ] 6.3 前端同步提示

---

## 10. 待确认事项

| 序号 | 事项 | 状态 |
|------|------|------|
| 1 | 功能栏按钮图标设计 | 待确认 |
| 2 | 意图识别 Prompt 细节调优 | 待确认 |
| 3 | 是否需要支持部分内容修改（而非全文替换） | 待确认 |
| 4 | 修改历史记录需求 | 待确认 |
| 5 | Outline 同步策略（是否需要用户确认） | 待确认 |
| 6 | 批量应用时的并发控制（是否串行） | 待确认 |

---

## 11. 参考资料

- 现有 `rewrite_draft` 功能
- Chat 多选 chunk 功能
- RAG 意图识别最佳实践

---

**变更记录**

| 版本 | 日期 | 变更内容 | 作者 |
|------|------|----------|------|
| v1.1 | 2026-03-07 | 新增 Outline 同步机制、修改进度状态 | Claude |
| v1.0 | 2026-03-07 | 初版设计 | Claude |
