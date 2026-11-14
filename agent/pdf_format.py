import logging
import re

import fitz

logger = logging.getLogger("agent.pdf_format")

_FORMULA_PLACEHOLDER = re.compile(r"\{\{v(\d+)\}\}")

_layout_model = None


def _get_layout_model():
    global _layout_model
    if _layout_model is None:
        from pdf2zh.doclayout import DocLayoutModel

        _layout_model = DocLayoutModel.load_available()
    return _layout_model


def protect_formula_placeholders(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}

    def _sub(match):
        key = f"__PDFPH_{match.group(1)}__"
        mapping[key] = match.group(0)
        return key

    return _FORMULA_PLACEHOLDER.sub(_sub, text), mapping


def restore_formula_placeholders(text: str, mapping: dict[str, str]) -> str:
    for key, original in mapping.items():
        text = text.replace(key, original)
    return text


def translate_pdf_format(pdf_bytes: bytes, page_numbers: list[int], source_lang: str, target_lang: str, service) -> bytes:
    import pdf2zh.converter
    from pdf2zh.high_level import translate_stream
    from pdf2zh.translator import OpenAITranslator

    class HybridTranslator(OpenAITranslator):
        name = "openai"

        def __init__(self, lang_in, lang_out, model, envs=None, prompt=None, ignore_cache=False):
            from agent.config import settings

            super().__init__(
                lang_in,
                lang_out,
                model or settings.llm_model,
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key or "unused",
                envs=envs,
                prompt=prompt,
                ignore_cache=ignore_cache,
            )

        def do_translate(self, text: str) -> str:
            prepared, mapping = protect_formula_placeholders(text)
            try:
                result = service.translate(prepared, source_lang, target_lang)
                translated = result["translated_text"]
                if any(key not in translated for key in mapping):
                    raise ValueError("placeholder lost in translation")
            except Exception as exc:
                logger.warning("Format-mode fragment failed (%s); keeping original text", exc)
                return text
            return restore_formula_placeholders(translated, mapping)

    pdf2zh.converter.OpenAITranslator = HybridTranslator

    pages_param = [n - 1 for n in page_numbers] if page_numbers else None
    mono, _dual = translate_stream(
        pdf_bytes,
        pages=pages_param,
        lang_in=source_lang,
        lang_out=target_lang,
        service="openai",
        thread=2,
        ignore_cache=True,
        model=_get_layout_model(),
    )
    out_doc = fitz.open(stream=mono, filetype="pdf")
    if page_numbers and out_doc.page_count > len(page_numbers):
        out_doc.select([n - 1 for n in page_numbers])
    return out_doc.tobytes(deflate=True, garbage=3)


def page_integrity_ok(src_page, out_page) -> bool:
    if len(out_page.get_images(full=True)) < len(src_page.get_images(full=True)):
        return False
    src_drawings = len(src_page.get_drawings())
    if src_drawings > 0 and len(out_page.get_drawings()) < src_drawings * 0.8:
        return False
    if src_page.get_text().strip() and not out_page.get_text().strip():
        return False
    return True


def find_broken_pages(doc, out_doc, page_numbers: list[int]) -> list[int]:
    broken = []
    for i, page_number in enumerate(page_numbers):
        if i >= out_doc.page_count or not page_integrity_ok(doc[page_number - 1], out_doc[i]):
            broken.append(i)
    return broken


def splice_replacement_pages(out_doc, comp_doc, broken_indexes: list[int]):
    for k, i in enumerate(broken_indexes):
        out_doc.delete_page(i)
        out_doc.insert_pdf(comp_doc, from_page=k, to_page=k, start_at=i)
