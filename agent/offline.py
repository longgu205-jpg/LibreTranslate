import logging
import time

logger = logging.getLogger("agent.offline")

TERMINOLOGY = [
    ("視頻", "影片"),
    ("數據", "資料"),
    ("網絡", "網路"),
    ("服務器", "伺服器"),
    ("代碼", "程式碼"),
    ("默認", "預設"),
    ("內存", "記憶體"),
    ("硬盤", "硬碟"),
    ("屏幕", "螢幕"),
    ("鼠標", "滑鼠"),
    ("打印機", "印表機"),
    ("優化", "最佳化"),
    ("質量", "品質"),
    ("界面", "介面"),
    ("信息", "訊息"),
    ("人工智能", "人工智慧"),
    ("軟件", "軟體"),
]


def apply_terminology(text: str) -> str:
    for mainland, taiwan in TERMINOLOGY:
        text = text.replace(mainland, taiwan)
    return text


class OfflineEngine:
    def __init__(self):
        self._converter_tw = None
        self._converter_s = None

    def _ensure_converters(self):
        if self._converter_tw is None:
            import opencc

            self._converter_tw = opencc.OpenCC("s2twp")
            self._converter_s = opencc.OpenCC("tw2s")

    def available(self) -> bool:
        try:
            from argostranslate import translate
        except ImportError:
            return False
        codes = {lang.code for lang in translate.get_installed_languages()}
        return "en" in codes and "zh" in codes

    def _argos_translate(self, text: str, from_code: str, to_code: str) -> str | None:
        from argostranslate import translate

        installed = translate.get_installed_languages()
        src = next((lang for lang in installed if lang.code == from_code), None)
        tgt = next((lang for lang in installed if lang.code == to_code), None)
        if src is None or tgt is None:
            return None
        return src.get_translation(tgt).translate(text)

    def translate(self, text: str, source_lang: str, target_lang: str) -> str | None:
        if not text.strip():
            return None
        started = time.perf_counter()
        try:
            if target_lang == "zh-TW":
                raw = self._argos_translate(text, "en", "zh")
                if raw is None:
                    logger.warning("Offline en->zh model not available")
                    return None
                self._ensure_converters()
                result = apply_terminology(self._converter_tw.convert(raw))
                logger.info(
                    "Offline en->zh-TW done: %d chars in %.0f ms",
                    len(text), (time.perf_counter() - started) * 1000,
                )
                return result

            if target_lang == "en" and source_lang == "zh":
                self._ensure_converters()
                simplified = self._converter_s.convert(text)
                result = self._argos_translate(simplified, "zh", "en")
                if result is not None:
                    logger.info(
                        "Offline zh->en done: %d chars in %.0f ms",
                        len(text), (time.perf_counter() - started) * 1000,
                    )
                return result
        except Exception:
            logger.exception("Offline translation failed")
            return None
        return None
