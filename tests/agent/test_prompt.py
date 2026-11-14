import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from agent.prompt import build_prompt


def test_build_prompt_zh_tw():
    prompt = build_prompt("zh-TW", "Hello world")
    assert "繁體中文" in prompt
    assert "台灣" in prompt
    assert "Hello world" in prompt


def test_build_prompt_en():
    prompt = build_prompt("en", "你好世界")
    assert "English" in prompt
    assert "你好世界" in prompt


def test_build_prompt_rejects_unknown_lang():
    with pytest.raises(ValueError):
        build_prompt("fr", "Bonjour")
