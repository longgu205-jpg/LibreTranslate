import logging
from contextlib import asynccontextmanager
from pathlib import Path

import fitz
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from agent.cache import TranslationCache
from agent.config import settings
from agent.llm_client import LLMClient, LLMError
from agent.offline import OfflineEngine
from agent.pdf_format import find_broken_pages, splice_replacement_pages, translate_pdf_format
from agent.pdf_translator import parse_page_range, translate_pdf
from agent.translator import TranslatorService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.llm_client = LLMClient()
    app.state.cache = TranslationCache()
    app.state.offline = OfflineEngine()
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


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "provider": settings.llm_provider,
        "model": settings.llm_model,
        "api_key_configured": bool(settings.llm_api_key),
        "offline_available": app.state.offline.available(),
        "cache_entries": app.state.cache.count(),
    }


@app.post("/translate/pdf")
def translate_pdf_endpoint(
    file: UploadFile = File(...),
    pages: str = Form(""),
    target_lang: str = Form("zh-TW"),
    mode: str = Form("compare"),
):
    if target_lang not in ("zh-TW", "en"):
        raise HTTPException(status_code=400, detail="target_lang must be zh-TW or en")
    if mode not in ("compare", "translation_only", "format"):
        raise HTTPException(status_code=400, detail="mode must be compare, translation_only or format")
    if not settings.llm_api_key:
        raise HTTPException(
            status_code=503,
            detail="LLM_API_KEY is not configured. Set it in the .env file.",
        )

    data = file.file.read()
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise HTTPException(status_code=400, detail="不是有效的 PDF 檔案")
    if doc.needs_pass:
        raise HTTPException(status_code=400, detail="PDF 已加密，請先解除密碼再上傳")
    if doc.page_count == 0:
        raise HTTPException(status_code=400, detail="PDF 沒有任何頁面")

    service = TranslatorService(app.state.llm_client, app.state.cache, app.state.offline)
    try:
        all_text = " ".join(doc[i].get_text() for i in range(min(doc.page_count, 3)))
        source_lang = detect_source_lang(all_text)
        if mode == "format":
            page_numbers = parse_page_range(pages or None, doc.page_count)
            pdf_bytes = translate_pdf_format(data, page_numbers, source_lang, target_lang, service)
            out_doc = fitz.open(stream=pdf_bytes, filetype="pdf")
            broken = find_broken_pages(doc, out_doc, page_numbers)
            if broken:
                broken_sources = [page_numbers[i] for i in broken]
                logger.warning(
                    "Format mode lost content on source pages %s; falling back to comparison mode for those pages",
                    broken_sources,
                )
                comp_pdf = translate_pdf(
                    doc, ",".join(map(str, broken_sources)), source_lang, target_lang, service, mode="compare",
                )
                comp_doc = fitz.open(stream=comp_pdf, filetype="pdf")
                splice_replacement_pages(out_doc, comp_doc, broken)
                pdf_bytes = out_doc.tobytes(deflate=True, garbage=3)
        else:
            pdf_bytes = translate_pdf(
                doc, pages or None, source_lang, target_lang, service, mode=mode,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LLMError as exc:
        logger.error("PDF translation failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    finally:
        doc.close()

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="translated.pdf"'},
    )


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

    service = TranslatorService(app.state.llm_client, app.state.cache, app.state.offline)
    try:
        result = service.translate(request.text, source_lang, request.target_lang)
    except LLMError as exc:
        logger.error("Translation failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    return TranslateResponse(**result)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
