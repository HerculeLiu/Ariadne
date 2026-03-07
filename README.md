# Ariadne

Ariadne 是一个本地优先的学习内容生成与讲解系统。

系统围绕一条主链工作：

1. 上传文件并解析
2. 基于资料生成课件结构与讲解内容
3. 渲染为固定壳层 HTML 页面
4. 在页面内进行聊天、AI 编辑、chunk 删除与版本回滚

## 当前能力

- 课件生成：`topic + 文件资料 -> outline -> explain content -> HTML shell`
- 混合检索聊天：
  - 检索 `courseware chunks`
  - 检索 `source fragments`
  - `selected chunks` 只做软优先级，不做强绑定
- AI 编辑：
  - 对已选 chunk 做改写建议
  - 支持应用修改与撤销
- Chunk 删除：
  - 删除前自动创建 snapshot
  - 删除后重排章节号、块号、顺序号
- 本地文件持久化：
  - `courseware`
  - `chunk`
  - `page`
  - `chat session`
  - `chat message`
  - `asset`

## 启动

```bash
./start.sh
```

启动后访问：

- 前端首页：`http://127.0.0.1:1557/`
- 健康检查：`http://127.0.0.1:1557/api/v1/health/live`

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 模型配置

在 `.env` 中设置：

- `MODEL_PROVIDER=mock | glm | deepseek`

并配置对应 provider 的 key：

- `GLM_API_KEY`
- `DEEPSEEK_API_KEY`

常用环境变量：

- `MODEL_PROVIDER`
- `MODEL_GLM`
- `MODEL_DEEPSEEK`
- `MODEL_MOCK`
- `GLM_API_BASE`
- `GLM_CHAT_PATH`
- `DEEPSEEK_API_BASE`
- `DEEPSEEK_CHAT_PATH`
- `PROMPT_DIR`
- `COURSEWARE_STORAGE_DIR`
- `STORAGE_INDEX_DIR`

## Prompt 分层

当前 prompt 结构是三层：

1. `outline_layer.md`
   - 只负责生成章节和 chunk 结构
2. `explain_layer.md`
   - 只负责单个 chunk 的讲解内容
3. `generate_layer.md`
   - 提供 HTML 渲染配置默认值

其他 prompt：

- `chat_general.md`
- `chunk_qa.md`
- `rewrite_chunk.md`
- `intent_edit_implicit.md`
- `intent_edit_explicit.md`

## Chat / RAG 架构

聊天阶段不是只查上传文件。

当前会并行使用两类检索语料：

1. `courseware chunks`
   - 解决“第 1 章第 2 个 chunk 讲了什么”这类问题
2. `source fragments`
   - 解决“原始资料里怎么写的”这类问题

检索增强包括：

- query rewrite
- keyword retrieval
- vector retrieval
- courseware chunk soft boost

`selected chunks` 的语义是：

- 提高相关 chunk 的命中优先级
- 但不强制把回答锁定在 selected 内容上

## 本地存储结构

当前持久化以文件系统为主，不使用数据库。

关键目录：

```text
storage/
  assets/
    <asset_id>/
      meta.json
      fragments.json
      source.*
  coursewares/
    <courseware_id>/
      meta.json
      outline.json
      markdown.md
      html.html
      chunks/
      pages/
      chats/
      snapshots/
  indexes/
    assets.json
    coursewares.json
    chat_sessions.json
```

详细方案见：

- [本地文件持久化方案_v1.0](docs/本地文件持久化方案_v1.0.md)

## 关键接口

### 课件

- `POST /api/v1/coursewares/generate`
- `GET /api/v1/coursewares/{id}`
- `GET /api/v1/coursewares/{id}/progress`
- `GET /api/v1/coursewares/{id}/markdown`
- `PUT /api/v1/coursewares/{id}/markdown`
- `POST /api/v1/coursewares/{id}/undo`
- `GET /api/v1/coursewares/{id}/html`

### 聊天

- `POST /api/v1/chat/sessions`
- `GET /api/v1/chat/sessions`
- `POST /api/v1/chat/messages`

### Chunk

- `PATCH /api/v1/chunks/{id}/state`
- `POST /api/v1/chunks/{id}/delete`
- `POST /api/v1/chunks/{id}/apply`

### Rewrite

- `POST /api/v1/pages/{id}/rewrite-draft`
- `POST /api/v1/pages/{id}/apply-draft`

## 补充文档

- [RAG技术框架方案_v1.0](docs/RAG技术框架方案_v1.0.md)
- [RAG实施工作报告_v1.0](docs/RAG实施工作报告_v1.0.md)
- [Chat检索与Chunk编辑说明_v1.0](docs/Chat检索与Chunk编辑说明_v1.0.md)

## 当前实现边界

- 课件聊天已经支持 `courseware chunk + source fragment` 混合检索
- `selected chunk` 已是软优先级
- 删除 chunk 后前端做局部更新，并会重新同步后端元数据
- 大规模检索权重调优仍然可以继续迭代
