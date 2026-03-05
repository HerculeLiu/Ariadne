# Ariadne MVP (PI-1 + PI-2)

This project is an agent-style learning platform MVP.
Generated courseware and QA/rewrite behavior are driven by prompts in `src/prompt/*.md`.
Naming convention:
- 理解层: generate markdown knowledge (`understand_layer.md`)
- 生成层: render interactive html (`generate_layer.md`)
Knowledge workflow: collect materials -> generate local markdown knowledge doc -> render HTML from markdown.

## Hard Constraint
- This project is explanation-only.
- Never generate or render any review/exam/test/practice content.
- Forbidden content types: quiz, flashcard, exercise, score, checkpoint, mastery test.

## Start project

```bash
./start.sh
```

Open:
- Frontend: `http://127.0.0.1:1557/`
- Health API: `http://127.0.0.1:1557/api/v1/health/live`

## Run tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Model selection (single field)

Edit `.env` and set only this switch field:

- `MODEL_PROVIDER=mock | glm | deepseek`

Then configure key for the selected provider:

- If `MODEL_PROVIDER=glm`, set `GLM_API_KEY`
- If `MODEL_PROVIDER=deepseek`, set `DEEPSEEK_API_KEY`

## Key env fields
- `MODEL_PROVIDER`: provider switch
- `MODEL_GLM`, `MODEL_DEEPSEEK`, `MODEL_MOCK`: model names
- `GLM_API_KEY`, `DEEPSEEK_API_KEY`: provider keys
- `GLM_API_BASE`, `GLM_CHAT_PATH`: glm endpoint config
- `DEEPSEEK_API_BASE`, `DEEPSEEK_CHAT_PATH`: deepseek endpoint config
- `PROMPT_DIR`: prompt markdown directory, default `src/prompt`
- `KNOWLEDGE_DOC_DIR`: local markdown storage directory

## Prompt files
- `src/prompt/understand_layer.md` (理解层)
- `src/prompt/generate_layer.md` (生成层)
- `src/prompt/generate_courseware.md` (legacy alias, backward compatible)
- `src/prompt/chunk_qa.md`
- `src/prompt/rewrite_chunk.md`

## Implemented phases
- PI-1 core loop: topic -> generate -> chunk ask -> asset upload -> export html
- PI-2 enhancements: chunk state, append accept/reject, chat sessions, rewrite draft/apply/undo,
  retrieval settings, profile local-only mode, logs and performance metrics
- Content sync rule: content edits update markdown and regenerate HTML; style/layout-only changes can stay page-level.

## Main PI-2 APIs
- `PUT /api/v1/retrieval/settings`
- `GET /api/v1/retrieval/settings`
- `PUT /api/v1/profiles/current`
- `GET /api/v1/profiles/current`
- `POST /api/v1/chat/sessions`
- `POST /api/v1/chat/messages`
- `GET /api/v1/chat/sessions`
- `POST /api/v1/chunks/{id}/append`
- `PATCH /api/v1/chunks/{id}/state`
- `GET /api/v1/coursewares/{id}/markdown`
- `PUT /api/v1/coursewares/{id}/markdown`
- `POST /api/v1/pages/{id}/rewrite-draft`
- `POST /api/v1/pages/{id}/apply-draft`
- `POST /api/v1/pages/{id}/undo`
- `GET /api/v1/logs/events`
- `GET /api/v1/metrics/performance`
