ZH_TW_TEMPLATE = """請把以下內容翻譯成繁體中文（台灣習慣用語）。

規則：
1. 理解整段語意後重寫，句子順暢自然，嚴禁逐字直翻與大陸式翻譯腔。
2. 採用台灣常見技術詞彙（例如：軟體、資料、程式、專案、伺服器）。
3. 保留專有名詞、程式碼、數字、單位、網址與佔位符（例如 __PROTECTED_BLOCK_0__），一個都不能改動或刪除。
4. 人名、地名、產品名稱若無台灣通用譯名，請保留英文原文。
5. 不要補充或刪減原文沒有的資訊。
6. 只輸出譯文，不要加任何說明。

原文：
{source_text}"""

EN_TEMPLATE = """Translate the following content into English.

Rules:
1. Rewrite naturally after understanding the full meaning; avoid word-for-word translationese.
2. Keep proper nouns, code, numbers, units, URLs and placeholders (e.g. __PROTECTED_BLOCK_0__) untouched.
3. Do not add or remove information that is not in the source.
4. Output only the translation, with no explanations.

Source text:
{source_text}"""

TEMPLATES = {
    "zh-TW": ZH_TW_TEMPLATE,
    "en": EN_TEMPLATE,
}

PROMPT_VERSION = "v1"


def build_prompt(target_lang: str, source_text: str) -> str:
    template = TEMPLATES.get(target_lang)
    if template is None:
        raise ValueError(f"Unsupported target language: {target_lang}")
    return template.format(source_text=source_text)
