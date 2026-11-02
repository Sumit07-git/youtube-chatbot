# TubeBrief

Paste a YouTube link, get a summary. FastAPI backend, plain HTML/CSS/JS frontend,
LangChain + Ollama for the model.

## Run

```bash

ollama pull llama3.2  # chat model
ollama pull nomic-embed-text # embed  model
pip install -r requirements.txt # requirements
uvicorn main:app --reload # load the app
```

Open http://127.0.0.1:8000

## Config (environment variables)

| Variable      | Default                  |
|---------------|--------------------------|
| `LLM_MODEL`   | `llama3.2`               |
| `EMBED_MODEL` | `nomic-embed-text`       |
| `OLLAMA_URL`  | `http://localhost:11434` |

## API

- `POST /api/summarize` with `{"url": "...", "length": "short|standard|detailed"}`
- `POST /api/ask` with `{"video_id": "...", "question": "..."}` (summarize the video first)