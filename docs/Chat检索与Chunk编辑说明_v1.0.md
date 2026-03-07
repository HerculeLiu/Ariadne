# Chat 检索与 Chunk 编辑说明 v1.0

## 1. 目标

本说明用于记录 Ariadne 当前聊天与 chunk 编辑的实际行为，方便后续回归和继续迭代。

覆盖范围：

1. chat 检索链
2. `selected chunk` 的语义
3. chunk 删除
4. 相关本地持久化

---

## 2. Chat 检索架构

### 2.1 当前检索语料

聊天阶段会并行使用两类语料：

1. `courseware chunks`
2. `source fragments`

其中：

- `courseware chunks` 用来回答“课件里第几个 chunk 在讲什么”
- `source fragments` 用来回答“原始资料里怎么写的”

### 2.2 请求结构

前端聊天请求核心字段：

```json
{
  "session_id": "cs_xxx",
  "message": "用户问题",
  "selected_chunk_ids": ["ck_xxx", "ck_yyy"],
  "asset_ids": ["as_xxx"],
  "selected_context": "仅在部分 selected chunk 还没有 chunkId 时作为 fallback hint"
}
```

### 2.3 selected chunk 的语义

`selected chunk` 不是强绑定上下文。

当前设计是：

- `selected_chunk_ids` 进入检索排序，作为 soft boost
- 不会强制把回答限制在 selected 的几个块里
- 如果用户选错了 chunk，系统仍然可以命中其他更相关的课件内容

这意味着：

1. 不选 chunk，也能问课件里某个 chunk 的内容
2. 选了 chunk，只是让这些 chunk 更容易排到前面

### 2.4 query rewrite

聊天检索前会做 retrieval-only query rewrite。

主要作用：

1. 把短问题补上 topic / chunk scope
2. 提取关键词
3. 中文问句去尾，如：
   - `古诗讲的是什么` -> `古诗`
   - `核心职责是什么` -> `核心职责`

### 2.5 检索流程

```text
用户问题
  -> query rewrite
  -> courseware chunk retrieval
  -> source fragment retrieval
  -> 合并为 combined_rag_context
  -> LLM.chat_reply()
```

最终上下文按两段组织：

1. `【课件相关内容】`
2. `【原始资料】`

---

## 3. Courseware Chunk Retrieval

### 3.1 命中方式

当前支持：

1. 章节号 / chunk 号直达匹配
   - `1.2`
   - `第1章`
   - `第2个chunk`
2. chunk 标题关键词匹配
3. chunk 正文关键词匹配
4. selected chunk soft boost

### 3.2 典型问题

例如：

- `第1章第2个chunk讲了什么`
- `古诗讲的是什么`
- `核心职责是什么`

这些问题现在都不需要先手动选中 chunk。

---

## 4. Chunk 删除

### 4.1 行为

当前删除是两步确认：

1. 第一次点击 `x`
   - 按钮变成红色 `确认删除`
2. 第二次点击
   - 才真正删除
3. 点击其他地方
   - 取消确认态

### 4.2 删除后的后端行为

删除 chunk 时：

1. 创建 snapshot
2. 删除目标 chunk
3. 重排：
   - `order_no`
   - `chapter_no`
   - `chunk_no`
4. 重新生成 markdown / html
5. 更新课件版本号

### 4.3 删除后的前端行为

前端不会整页刷新。

当前逻辑：

1. 本地先移除 chunk
2. 自动选择下一个可展示 chunk
3. 更新本地版本号
4. 调 `loadCoursewareVersion()` 重新同步后端元数据

同步优先级：

1. 优先按 `chunk.id` 对齐
2. 位置匹配只做兜底

---

## 5. 跳转与引用

### 5.1 已选 chunk 浮窗

聊天输入框上方的 selected chunk 条目现在支持点击跳转：

- 点击条目 -> 跳到对应 chunk
- 连续模式下会滚动到对应位置
- 单卡模式下会切到对应 chunk

### 5.2 聊天消息中的引用卡片

用户消息中的 chunk 引用卡片同样支持跳转。

匹配优先级：

1. `chunkId`
2. 位置 key

---

## 6. 本地持久化

聊天与 chunk 编辑相关的关键数据都走文件持久化。

### 6.1 Chat message

当前 `ChatMessage` 会持久化：

- `content`
- `selected_context`
- `selected_chunk_ids`
- `asset_ids`
- `sources`

### 6.2 Courseware chunk

每个 chunk 单独落盘：

```text
storage/coursewares/<courseware_id>/chunks/<chunk_id>.json
```

### 6.3 Chat session

每个 session 单独落盘：

```text
storage/coursewares/<courseware_id>/chats/<session_id>.json
```

---

## 7. 当前已知边界

1. chunk 检索排序仍然可以继续调优
   - 尤其是中文短词、标题精确匹配、子串匹配权重
2. `selected_context` 目前仍保留为 fallback hint
   - 当 selected chunk 还没有拿到稳定 `chunkId` 时使用
3. 删除 chunk 后，前端依然依赖一次后端元数据重同步
   - 当前已稳定，但仍建议继续做浏览器回归

---

## 8. 建议回归场景

1. 不选 chunk，直接问：
   - `古诗讲的是什么`
2. 选错 chunk，再问另一个主题
3. 删除中间 chunk
4. 删除某章最后一个 chunk
5. 删除后再点击历史消息里的引用卡片跳转

---

## 9. 结论

当前 chat 架构已经从：

- `source-only retrieval + selected_context 强绑定`

切换为：

- `courseware chunk retrieval + source fragment retrieval + selected soft boost`

这版已经能支撑：

1. 不选 chunk 直接问课件内容
2. 保留上传资料检索
3. 选中 chunk 只是提高优先级，而不是强制绑定话题
