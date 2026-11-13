import re


FENCED_BLOCK = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
INLINE_CODE = re.compile(r"`[^`\n]{1,200}`")
HTML_TAG = re.compile(r"</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")

PLACEHOLDER = "__PROTECTED_BLOCK_{idx}__"


class CodeProtector:
    def __init__(self):
        self.blocks: list[str] = []

    def protect(self, text: str) -> str:
        self.blocks = []
        text = self._extract(text, FENCED_BLOCK)
        text = self._extract(text, INLINE_CODE)
        text = self._extract(text, HTML_TAG)
        return text

    def restore(self, text: str) -> str:
        for idx, block in enumerate(self.blocks):
            text = text.replace(PLACEHOLDER.format(idx=idx), block)
        return text

    def integrity_ok(self, text: str) -> bool:
        return all(PLACEHOLDER.format(idx=i) in text for i in range(len(self.blocks)))

    def _extract(self, text: str, pattern: re.Pattern) -> str:
        def repl(match: re.Match) -> str:
            idx = len(self.blocks)
            self.blocks.append(match.group(0))
            return PLACEHOLDER.format(idx=idx)

        return pattern.sub(repl, text)
