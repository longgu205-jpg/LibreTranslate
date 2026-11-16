import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

import fitz
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from agent.cache import TranslationCache
from agent.config import settings
from agent.llm_client import LLMClient, LLMError
from agent.live import LiveCaptionEngine, pyaudio
from agent.offline import OfflineEngine
from agent.pdf_format import find_broken_pages, splice_replacement_pages, translate_pdf_format
from agent.pdf_translator import parse_page_range, translate_pdf
from agent.stt import ALLOWED_AUDIO_EXTENSIONS, STTError, TranscriptionClient
from agent.translator import TranslatorService

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.llm_client = LLMClient()
    app.state.backup_client = (
        LLMClient(
            api_key=settings.backup_api_key,
            model=settings.backup_model,
            base_url=settings.backup_base_url,
        )
        if settings.backup_api_key
        else None
    )
    app.state.cache = TranslationCache()
    app.state.offline = OfflineEngine()
    app.state.stt_client = TranscriptionClient() if settings.stt_api_key else None
    yield
    engine = getattr(app.state, "live_engine", None)
    if engine is not None and engine.running:
        engine.stop()
    app.state.llm_client.close()
    if app.state.backup_client is not None:
        app.state.backup_client.close()
    if app.state.stt_client is not None:
        app.state.stt_client.close()


app = FastAPI(title="中英翻譯_Formosalab watergu 製", version="0.1.0", lifespan=lifespan)


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
        "backup_configured": app.state.backup_client is not None,
        "backup_model": settings.backup_model if app.state.backup_client else "",
        "stt_available": app.state.stt_client is not None,
        "offline_available": app.state.offline.available(),
        "cache_entries": app.state.cache.count(),
    }


def make_service() -> TranslatorService:
    return TranslatorService(
        app.state.llm_client, app.state.cache, app.state.offline, app.state.backup_client,
    )


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

    service = make_service()
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

    service = make_service()
    try:
        result = service.translate(request.text, source_lang, request.target_lang)
    except LLMError as exc:
        logger.error("Translation failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    return TranslateResponse(**result)


@app.get("/live/window")
def live_window():
    return FileResponse(Path(__file__).parent / "static" / "live.html")


@app.post("/live/start")
def live_start():
    """開始即時字幕：擷取系統聲音 → VAD → Whisper → 翻譯。"""
    if not settings.live_enabled:
        raise HTTPException(status_code=403, detail="即時字幕功能已停用（LIVE_ENABLED=0）")
    if pyaudio is None:
        raise HTTPException(status_code=503, detail="此功能需要 Windows 與 PyAudioWPatch 套件")
    if app.state.stt_client is None:
        raise HTTPException(status_code=503, detail="尚未設定語音辨識金鎖（.env 的 STT_API_KEY 或 LLM_API_KEY）")

    engine = getattr(app.state, "live_engine", None)
    if engine is not None and engine.running:
        return {"status": "already_running"}

    engine = LiveCaptionEngine(app.state.stt_client, make_service())
    try:
        engine.start()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"無法啟動即時字幕：{exc}")
    app.state.live_engine = engine
    return {"status": "started"}


@app.post("/live/stop")
def live_stop():
    engine = getattr(app.state, "live_engine", None)
    if engine is None or not engine.running:
        return {"status": "not_running"}
    engine.stop()
    return {"status": "stopped"}


@app.get("/live/status")
def live_status():
    engine = getattr(app.state, "live_engine", None)
    if engine is None:
        return {"running": False, "captions": [], "error": None}
    return {
        "running": engine.running,
        "captions": engine.store.latest(8),
        "error": engine.last_error,
    }


@app.post("/transcribe")
def transcribe_endpoint(
    file: UploadFile = File(...),
    language: str = Form(""),
):
    """語音檔轉文字（測試用）：上傳音訊 → Whisper 辨識，不翻譯。"""
    if not settings.voice_enabled:
        raise HTTPException(status_code=403, detail="語音功能已停用（VOICE_ENABLED=0）")
    if app.state.stt_client is None:
        raise HTTPException(
            status_code=503,
            detail="尚未設定語音辨識金鎖（.env 的 STT_API_KEY 或 LLM_API_KEY）",
        )

    filename = file.filename or "audio.webm"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的音訊格式 {ext or filename}，可用：{', '.join(sorted(ALLOWED_AUDIO_EXTENSIONS))}",
        )

    data = file.file.read()
    max_bytes = settings.stt_max_mb * 1024 * 1024
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="音訊檔案是空的")
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"音訊檔案超過 {settings.stt_max_mb} MB 上限")

    started = time.perf_counter()
    try:
        text = app.state.stt_client.transcribe(
            data, filename=filename, language=language or None,
        )
    except STTError as exc:
        logger.error("Transcription failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    elapsed_ms = round((time.perf_counter() - started) * 1000)
    return {
        "text": text,
        "model": app.state.stt_client.model,
        "elapsed_ms": elapsed_ms,
    }


@app.post("/translate/voice")
def translate_voice_endpoint(
    file: UploadFile = File(...),
    source_lang: str = Form("auto"),
    target_lang: str = Form("zh-TW"),
):
    """語音翻譯：上傳音訊 → Whisper 辨識 → 現有翻譯管線（快取 → 雲端 → 離線）。"""
    if not settings.voice_enabled:
        raise HTTPException(status_code=403, detail="語音功能已停用（VOICE_ENABLED=0）")
    if source_lang not in ("auto", "en", "zh"):
        raise HTTPException(status_code=400, detail="source_lang must be auto, en or zh")
    if target_lang not in ("zh-TW", "en"):
        raise HTTPException(status_code=400, detail="target_lang must be zh-TW or en")
    if app.state.stt_client is None:
        raise HTTPException(
            status_code=503,
            detail="尚未設定語音辨識金鎖（.env 的 STT_API_KEY 或 LLM_API_KEY）",
        )
    if not settings.llm_api_key:
        raise HTTPException(
            status_code=503,
            detail="LLM_API_KEY is not configured. Set it in the .env file.",
        )

    filename = file.filename or "audio.webm"
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"不支援的音訊格式 {ext or filename}，可用：{', '.join(sorted(ALLOWED_AUDIO_EXTENSIONS))}",
        )

    data = file.file.read()
    max_bytes = settings.stt_max_mb * 1024 * 1024
    if len(data) == 0:
        raise HTTPException(status_code=400, detail="音訊檔案是空的")
    if len(data) > max_bytes:
        raise HTTPException(status_code=413, detail=f"音訊檔案超過 {settings.stt_max_mb} MB 上限")

    stt_started = time.perf_counter()
    try:
        transcript = app.state.stt_client.transcribe(
            data, filename=filename, language=source_lang if source_lang != "auto" else None,
        )
    except STTError as exc:
        logger.error("Transcription failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))
    stt_elapsed_ms = round((time.perf_counter() - stt_started) * 1000)

    if not transcript:
        raise HTTPException(status_code=422, detail="語音辨識結果是空的，請確認音訊內容有說話")

    detected = source_lang
    if detected == "auto":
        detected = detect_source_lang(transcript)

    if detected == target_lang:
        return {
            "transcript": transcript,
            "translated_text": transcript,
            "source": "passthrough",
            "model": settings.llm_model,
            "source_lang": detected,
            "target_lang": target_lang,
            "elapsed_ms": 0,
            "stt_elapsed_ms": stt_elapsed_ms,
        }

    service = make_service()
    try:
        result = service.translate(transcript, detected, target_lang)
    except LLMError as exc:
        logger.error("Voice translation failed: %s", exc)
        raise HTTPException(status_code=502, detail=str(exc))

    return {
        "transcript": transcript,
        "translated_text": result["translated_text"],
        "source": result["source"],
        "model": result["model"],
        "source_lang": result["source_lang"],
        "target_lang": result["target_lang"],
        "elapsed_ms": result["elapsed_ms"],
        "stt_elapsed_ms": stt_elapsed_ms,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.host, port=settings.port)
