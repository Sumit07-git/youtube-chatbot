"""TubeBrief: a YouTube video summarizer (FastAPI + LangChain + Ollama)."""

import json
import os
import re
from collections import OrderedDict
from pathlib import Path
from typing import Literal
from urllib.parse import quote
from urllib.request import Request, urlopen

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_community.vectorstores import FAISS
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import (
    RunnableLambda,
    RunnableParallel,
    RunnablePassthrough,
)
from langchain_ollama import ChatOllama, OllamaEmbeddings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)

BASE_DIR = Path(__file__).parent
LLM_MODEL = os.getenv("LLM_MODEL", "llama3.2")
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

DIRECT_LIMIT = 9_000  
NOTES_LIMIT = 10_000   
MAX_CACHE = 20         

llm = ChatOllama(
    model=LLM_MODEL, temperature=0.2, num_ctx=8192, base_url=OLLAMA_URL
)
embeddings = OllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_URL)
parser = StrOutputParser()


LENGTH_SPECS = {
    "short": "Overview: 2-3 sentences. Key points: 3 bullets. Takeaways: 1-2 bullets.",
    "standard": "Overview: one short paragraph. Key points: 5-7 bullets. Takeaways: 2-3 bullets.",
    "detailed": "Overview: two paragraphs. Key points: 8-12 bullets, each with a concrete detail. Takeaways: 3-5 bullets.",
}

map_prompt = PromptTemplate.from_template(
    """You are taking notes on one part of a YouTube video transcript.
Write at most 6 concise bullet points covering the concrete facts, ideas and
examples in this part. Use only the text below. No introduction.

Transcript part:
{text}

Notes:"""
)

condense_prompt = PromptTemplate.from_template(
    """Merge the notes below into at most 8 bullet points. Keep the order and
keep concrete facts. No introduction.

Notes:
{text}

Merged notes:"""
)

reduce_prompt = PromptTemplate.from_template(
    """You are summarizing the YouTube video titled "{title}".
Below are the transcript or notes taken from it, in order. Use only this
information; do not add anything that is not in it.

Write the summary in exactly this markdown format:

## Overview
(text)

## Key points
- (bullet)

## Takeaways
- (bullet)

Size: {spec}

Material:
{notes}"""
)

qa_prompt = PromptTemplate(
    template="""You are a helpful assistant.
Answer ONLY from the provided transcript context.
If the context is insufficient, just say you don't know.

{context}
Question: {question}
""",
    input_variables=["context", "question"],
)

map_chain = map_prompt | llm | parser
condense_chain = condense_prompt | llm | parser
reduce_chain = reduce_prompt | llm | parser

map_splitter = RecursiveCharacterTextSplitter(chunk_size=4000, chunk_overlap=200)
notes_splitter = RecursiveCharacterTextSplitter(chunk_size=6000, chunk_overlap=0)
rag_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)

CACHE: "OrderedDict[str, dict]" = OrderedDict()


def cache_get(video_id: str):
    entry = CACHE.get(video_id)
    if entry:
        CACHE.move_to_end(video_id)
    return entry


def cache_put(video_id: str, entry: dict):
    CACHE[video_id] = entry
    CACHE.move_to_end(video_id)
    while len(CACHE) > MAX_CACHE:
        CACHE.popitem(last=False)


_ID_RE = re.compile(r"(?:v=|youtu\.be/|embed/|shorts/|live/)([A-Za-z0-9_-]{11})")


def extract_video_id(text: str):
    text = text.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    match = _ID_RE.search(text)
    return match.group(1) if match else None


def fetch_meta(video_id: str):
    """Title and channel name via YouTube's public oEmbed endpoint."""
    watch_url = quote(f"https://www.youtube.com/watch?v={video_id}", safe="")
    url = f"https://www.youtube.com/oembed?url={watch_url}&format=json"
    try:
        req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(req, timeout=6) as resp:
            data = json.load(resp)
        return data.get("title") or "YouTube video", data.get("author_name") or ""
    except Exception:
        return "YouTube video", ""


def fetch_transcript(video_id: str):
    """Returns (clean_text, duration_in_seconds). Prefers English captions,
    falls back to the first available language translated to English."""
    api = YouTubeTranscriptApi()
    try:
        available = api.list(video_id)
        try:
            transcript = available.find_transcript(["en", "en-US", "en-GB"])
        except NoTranscriptFound:
            transcript = next(iter(available))
            if not transcript.language_code.startswith("en") and transcript.is_translatable:
                transcript = transcript.translate("en")
        fetched = transcript.fetch()
    except TranscriptsDisabled:
        raise HTTPException(422, "This video has captions turned off, so there is nothing to summarize.")
    except (NoTranscriptFound, StopIteration):
        raise HTTPException(422, "No captions were found for this video.")
    except VideoUnavailable:
        raise HTTPException(404, "This video is unavailable. Check the link and try again.")
    except Exception as exc:  # network issues, IP blocks, etc.
        raise HTTPException(502, f"Could not fetch the transcript from YouTube: {exc}")

    snippets = list(fetched)
    if not snippets:
        raise HTTPException(422, "The transcript for this video is empty.")

    text = " ".join(s.text for s in snippets)
    text = re.sub(r"\[[^\]]*\]", " ", text)     
    text = re.sub(r"\s+", " ", text).strip()
    last = snippets[-1]
    duration = int(last.start + last.duration)
    return text, duration


def condense(notes: list) -> str:
    joined = "\n\n".join(n.strip() for n in notes)
    for _ in range(3):
        if len(joined) <= NOTES_LIMIT:
            break
        parts = notes_splitter.split_text(joined)
        merged = condense_chain.batch(
            [{"text": p} for p in parts], config={"max_concurrency": 2}
        )
        joined = "\n\n".join(m.strip() for m in merged)
    return joined


def summarize_text(transcript: str, title: str, length: str) -> str:
    if len(transcript) <= DIRECT_LIMIT:
        material = transcript
    else:
        parts = map_splitter.split_text(transcript)
        notes = map_chain.batch(
            [{"text": p} for p in parts], config={"max_concurrency": 2}
        )
        material = condense(notes)
    return reduce_chain.invoke(
        {"title": title, "notes": material, "spec": LENGTH_SPECS[length]}
    ).strip()


def raise_llm_error(exc: Exception):
    msg = str(exc).lower()
    if "connect" in msg or "refused" in msg:
        raise HTTPException(
            503, f"Can't reach Ollama at {OLLAMA_URL}. Start it with `ollama serve` and try again."
        )
    if "model" in msg and "not found" in msg:
        raise HTTPException(
            503, f"An Ollama model is missing. Run `ollama pull {LLM_MODEL}` and `ollama pull {EMBED_MODEL}`."
        )
    raise HTTPException(500, f"The language model failed: {exc}")


app = FastAPI(title="TubeBrief")


class SummarizeRequest(BaseModel):
    url: str = Field(min_length=1, max_length=300)
    length: Literal["short", "standard", "detailed"] = "standard"


class AskRequest(BaseModel):
    video_id: str = Field(min_length=11, max_length=11)
    question: str = Field(min_length=1, max_length=500)

@app.post("/api/summarize")
def summarize(req: SummarizeRequest):
    video_id = extract_video_id(req.url)
    if not video_id:
        raise HTTPException(400, "That doesn't look like a YouTube link.")

    entry = cache_get(video_id)
    if not entry:
        transcript, duration = fetch_transcript(video_id)
        title, channel = fetch_meta(video_id)
        entry = {
            "transcript": transcript,
            "duration": duration,
            "title": title,
            "channel": channel,
            "summaries": {},
        }
        cache_put(video_id, entry)

    if req.length not in entry["summaries"]:
        try:
            entry["summaries"][req.length] = summarize_text(
                entry["transcript"], entry["title"], req.length
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise_llm_error(exc)

    return {
        "video_id": video_id,
        "title": entry["title"],
        "channel": entry["channel"],
        "duration_seconds": entry["duration"],
        "word_count": len(entry["transcript"].split()),
        "summary": entry["summaries"][req.length],
    }


@app.post("/api/ask")
def ask(req: AskRequest):
    entry = cache_get(req.video_id)
    if not entry:
        raise HTTPException(404, "Summarize the video first, then ask questions about it.")

    try:
        if "store" not in entry:
            chunks = rag_splitter.create_documents([entry["transcript"]])
            entry["store"] = FAISS.from_documents(chunks, embeddings)

        retriever = entry["store"].as_retriever(
            search_type="similarity", search_kwargs={"k": 4}
        )
        parallel_chain = RunnableParallel(
            {
                "context": retriever
                | RunnableLambda(lambda docs: "\n\n".join(d.page_content for d in docs)),
                "question": RunnablePassthrough(),
            }
        )
        chain = parallel_chain | qa_prompt | llm | parser
        answer = chain.invoke(req.question.strip())
    except Exception as exc:
        raise_llm_error(exc)

    return {"answer": answer.strip()}

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE_DIR / "static" / "index.html")