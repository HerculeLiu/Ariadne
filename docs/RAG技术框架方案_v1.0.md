# Ariadne RAG 技术框架方案

**版本**: v1.0
**日期**: 2025-03-06
**状态**: 初版设计

---

## 1. 概述

### 1.1 背景

Ariadne 现有的课件生成流程为"普通模式"：用户输入 topic + keywords，LLM 直接生成大纲和内容。这种方式生成的质量完全依赖 LLM 的内置知识。

**RAG 模式**的目标是在生成过程中引入用户提供的参考资料（文件），通过检索增强生成提高内容的准确性和针对性。

### 1.2 目标

- 支持用户上传文件作为课件生成的参考资料
- 基于用户文件生成更准确、更有据可依的课件内容
- 保持与现有"普通模式"的兼容性（无文件时自动降级）

### 1.3 设计原则

| 原则 | 说明 |
|------|------|
| **渐进增强** | 普通模式为基础，RAG 为增强 |
| **可选启用** | 用户选择是否上传文件 |
| **透明降级** | 无文件时自动使用普通流程 |
| **API优先** | 使用成熟 API 而非自建模型 |

---

## 2. 流程对比

### 2.1 普通模式（现有）

```
用户输入: topic + keywords
    │
    ▼
LLM 生成大纲
    │
    ▼
LLM 生成每个 chunk
    │
    ▼
输出课件
```

### 2.2 RAG 模式（新增）

```
用户输入: topic + keywords + files
    │
    ▼
┌─────────────────────────────────────────┐
│  1. 文件解析与切分                        │
│     - 解析 PDF/MD/TXT/DOCX               │
│     - 递归切分为文本片段                   │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│  2. 向量化                                │
│     - 调用 GLM Embedding-3 API           │
│     - 存储到 ChromaDB 向量库              │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│  3. LLM 生成大纲                          │
│     - 与普通模式相同                      │
└─────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────┐
│  4. Chunk 生成 + RAG                     │
│     对每个 chunk:                        │
│       a) 构造查询: 章节标题 + chunk 标题  │
│       b) 向量检索 top-3 相关片段          │
│       c) 将片段注入 prompt               │
│       d) LLM 基于资料生成内容             │
└─────────────────────────────────────────┘
    │
    ▼
输出课件
```

---

## 3. 技术选型

### 3.1 技术栈

| 组件 | 选型 | 说明 |
|------|------|------|
| **向量数据库** | ChromaDB | 轻量级、易集成、本地持久化 |
| **Embedding** | GLM Embedding-3 API | 第三代文本向量化模型 |
| **Rerank** | GLM Rerank API | 二次重排序（后续优化） |
| **文本切分** | 递归切分 | 段落 > 句子 > 固定长度 |
| **文件解析** | 现有 Asset 系统 | 支持 PDF/MD/TXT/DOCX |

### 3.2 GLM API 规格

#### Embedding-3
| 项目 | 值 |
|------|-----|
| 端点 | `https://open.bigmodel.cn/api/paas/v4/embeddings` |
| 向量维度 | 256-2048（可调） |
| 上下文窗口 | 8K tokens |
| 价格 | 0.5元/百万tokens |

#### Rerank
| 项目 | 值 |
|------|-----|
| 端点 | `https://open.bigmodel.cn/api/paas/v4/rerank` |
| 候选文档数 | 最多 128 条 |
| 单文档长度 | 最大 4096 字符 |

---

## 4. 核心模块设计

### 4.1 GLM API 客户端

```python
class GLMEmbeddingClient:
    """GLM Embedding-3 API 客户端"""

    def encode(self, texts: List[str]) -> List[List[float]]:
        """批量编码文本为向量"""
        pass

class GLMRerankClient:
    """GLM Rerank API 客户端（可选）"""

    def rerank(self, query: str, documents: List[str], top_n: int) -> List[RerankResult]:
        """重排序文档"""
        pass
```

### 4.2 文本切分器

```python
class RecursiveTextSplitter:
    """递归文本切分器"""

    参数:
        max_length: int = 800      # 最大片段长度
        overlap: int = 100         # 重叠长度
        paragraph_sep: str = "\n\n" # 段落分隔符
        sentence_sep: str = "。\n！？\n"  # 句子分隔符

    切分优先级:
        1. 按段落切分
        2. 段落过长按句子切分
        3. 句子过长按字符切分
        4. 添加 overlap 保持连续性
```

### 4.3 向量存储

```python
class VectorStore:
    """基于 ChromaDB 的向量存储"""

    def add_fragments(self, fragments: List[DocumentFragment]) -> None:
        """添加文档片段"""
        pass

    def search(self, query: str, top_k: int = 3) -> List[DocumentFragment]:
        """向量检索"""
        pass

    def clear_asset(self, asset_id: str) -> None:
        """清除指定文件的所有片段"""
        pass
```

### 4.4 数据模型

```python
@dataclass
class DocumentFragment:
    """文档片段"""
    id: str                    # 片段唯一ID
    asset_id: str              # 来源文件ID
    text: str                  # 片段文本
    embedding: List[float]     # 向量表示
    order_no: int              # 顺序号

@dataclass
class RerankResult:
    """重排序结果"""
    index: int
    relevance_score: float
    document: str
```

### 4.5 生成请求扩展

```python
@dataclass
class GenerationRequest:
    """课件生成请求（扩展）"""
    topic: str
    keywords: List[str]
    asset_ids: List[str] = field(default_factory=list)  # 新增：关联文件ID
```

---

## 5. 切分算法详细设计

### 5.1 递归切分流程

```
输入: 长文本
  │
  ├─ 1. 按段落分割 (\n\n)
  │   └─ 每个段落检查长度
  │       ├─ 长度 ≤ max_length: 保留
  │       └─ 长度 > max_length: 进入步骤2
  │
  ├─ 2. 按句子分割 (。！？\n)
  │   └─ 每个句子检查长度
  │       ├─ 长度 ≤ max_length: 保留
  │       └─ 长度 > max_length: 进入步骤3
  │
  ├─ 3. 按字符分割
  │   └─ 按 max_length 切分
  │
  └─ 4. 添加 overlap
      └─ 相邻片段共享 overlap 长度的文本
```

### 5.2 示例

```
原文:
"第一段内容。第二段内容。第三段内容。

第四段是一个很长的段落，包含很多内容，超过了设定的最大长度限制...

第五段内容。"

切分后 (max_length=500, overlap=100):
  frag_1: "第一段内容。第二段内容。第三段内容。\n\n第四段是一个很长的段落..."
  frag_2: "...很长的段落，包含很多内容... [overlap] ...第五段内容。"
```

---

## 6. 检索策略

### 6.1 查询构造

```python
# Chunk 生成时的查询构造
query = f"{chapter_title} {chunk_title}"

# 例如:
chapter_title = "核心概念"
chunk_title = "什么是机器学习"
query = "核心概念 什么是机器学习"
```

### 6.2 检索流程

```
1. 向量检索
   - 用查询文本获取向量
   - 在 ChromaDB 中做相似度搜索
   - 返回 top-3 候选片段

2. [可选] Rerank 重排序
   - 调用 GLM Rerank API
   - 对 top-3 候选做精细排序
   - 返回最终结果
```

### 6.3 Prompt 注入

```python
# 原 prompt
user_prompt = f"topic={topic}\nchunk_title={chunk_title}\n请生成内容"

# RAG prompt
fragments_text = "\n\n".join([f"参考资料{i+1}: {f.text}" for i, f in retrieved_fragments])
user_prompt = f"""topic={topic}
chunk_title={chunk_title}

{fragments_text}

请基于以上参考资料，生成该chunk的内容。如果资料不足，可以基于通用知识补充。
"""
```

---

## 7. 集成流程

### 7.1 模式判断

```python
def generate(topic: str, keywords: List[str], asset_ids: List[str] = []):
    if not asset_ids:
        # 普通模式
        return _generate_normal(topic, keywords)
    else:
        # RAG 模式
        return _generate_with_rag(topic, keywords, asset_ids)
```

### 7.2 RAG 生成流程

```python
def _generate_with_rag(topic, keywords, asset_ids):
    # 1. 解析文件、切分、向量化
    fragments = _prepare_fragments(asset_ids)

    # 2. 生成大纲（与普通模式相同）
    outline = llm.generate_outline_markdown(topic, keywords)

    # 3. 生成每个 chunk（带检索）
    for chapter in outline:
        for chunk in chapter.chunks:
            # 检索相关片段
            query = f"{chapter.title} {chunk.title}"
            relevant = vector_store.search(query, top_k=3)

            # 生成内容
            chunk.content = llm.generate_chunk_content(
                topic=topic,
                chapter_title=chapter.title,
                chunk_title=chunk.title,
                context_fragments=relevant
            )
```

---

## 8. 实施计划

### Phase 1: GLM API 客户端
- [ ] 1.1 Embedding-3 客户端封装
- [ ] 1.2 Rerank 客户端封装（可选）

### Phase 2: 文本处理
- [ ] 2.1 递归切分器实现
- [ ] 2.2 文件解析增强（确认现有能力）

### Phase 3: 向量存储
- [ ] 3.1 ChromaDB 集成
- [ ] 3.2 向量化流程

### Phase 4: 生成流程集成
- [ ] 4.1 请求模型扩展
- [ ] 4.2 模式判断逻辑
- [ ] 4.3 检索逻辑实现
- [ ] 4.4 Prompt 增强

### Phase 5: 优化（可选）
- [ ] 5.1 Rerank 二次排序
- [ ] 5.2 检索缓存
- [ ] 5.3 性能优化

---

## 9. 待确认事项

| 序号 | 事项 | 状态 |
|------|------|------|
| 1 | ChromaDB 存储路径 | 待确认 |
| 2 | 文件大小限制 | 待确认 |
| 3 | 单个课件关联文件数量上限 | 待确认 |
| 4 | Embedding 向量维度选择 | 待确认（建议1024） |
| 5 | 切分参数调优（max_length, overlap） | 待确认 |

---

## 10. 参考资料

- [GLM Embedding-3 文档](https://docs.bigmodel.cn/cn/guide/models/embedding/embedding-3)
- [GLM Rerank API 文档](https://open.bigmodel.cn/dev/api/knowlage-manage/rerank)
- [ChromaDB 官方文档](https://docs.trychroma.com/)
- [智谱AI 开放平台](https://open.bigmodel.cn/)

---

**变更记录**

| 版本 | 日期 | 变更内容 | 作者 |
|------|------|----------|------|
| v1.0 | 2025-03-06 | 初版设计 | Claude |
