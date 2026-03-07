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
```json
{
  "is_modification": true|false,
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

请只返回 JSON，不要有其他内容。
