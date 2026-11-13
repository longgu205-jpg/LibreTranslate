import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from agent.config import settings
from agent.llm_client import LLMClient, LLMError
from agent.translator import TranslatorService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.llm_client = LLMClient()
    yield
    app.state.llm_client.close()


app = FastAPI(title="Hybrid Translation Agent", version="0.1.0", lifespan=lifespan)


class TranslateRequest(BaseModel):
    text: str = Field(min_length=1, max_length=50000)
    source_lang: str = Field(default="auto", pattern="^(auto|en|zh)$")
    target_lang: str = Field(default="zh-TW", pattern="^(zh-TW|en)$")


class TranslateResponse(BaseModel):
    translated_text: str
    source: str
    model: str
    source_lang: str
    target_lang: str
    elapsed_ms: int


def detect_source_lang(text: str) -> str:
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return "zh" if cjk > len(text) * 0.1 else "en"


@app.get("/health")
def health():
    return {
        "status": "ok",
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "api_key_configured": bool(settings.llm_api_key),
    }


@app.post("/translate", response_model=TranslateResponse)
def translate(request: TranslateRequest):
    source_lang = request.source_lang
    if source_lang == "auto":
        source_lang = detect_source_lang(request.text)

    if source_lang == request.target_lang:
        return TranslateResponse(
            translated_text=request.text,
            source="passthrough",
            model=settings.llm_model,
            source_lang=source_lang,
            target_lang=request.target_lang,
            elapsed_ms=0,
        )

    if not settings.llm_api_key:
        raise HTTPException(
            status_code=503,
            detail="LLM_API_KEY is not configured. Set it in the .env file.",
        )

    service = TranslatorService(app.state.llm_client)
    try:
        result = service.translate(request.text, source_lang, request.target_lang)
    except LLMError as exc:
        logger.error("Translation failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    return TranslateResponse(**result)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
