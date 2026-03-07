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
```json
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

请只返回 JSON，不要有其他内容。
