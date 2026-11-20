import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest

from agent.offline import OfflineEngine, rule_based_split_sentences

pytest.importorskip("argostranslate")


def test_rule_split_english():
    text = "Hello world. How are you? I am fine!"
    assert rule_based_split_sentences(None, text) == [
        "Hello world.",
        "How are you?",
        "I am fine!",
    ]


def test_rule_split_chinese():
    text = "這是第一句。這是第二句！這是第三句？"
    assert rule_based_split_sentences(None, text) == [
        "這是第一句。",
        "這是第二句！",
        "這是第三句？",
    ]


def test_rule_split_no_terminator_returns_whole():
    text = "one sentence without ending"
    assert rule_based_split_sentences(None, text) == [text]


def test_rule_split_multiple_spaces():
    text = "First.  Second.   Third."
    assert rule_based_split_sentences(None, text) == ["First.", "Second.", "Third."]


def test_engine_init_patches_argos_sbd():
    OfflineEngine()
    from argostranslate import sbd as argos_sbd
    from argostranslate.translate import MiniSBDSentencizer

    assert argos_sbd.MiniSBDSentencizer.split_sentences is rule_based_split_sentences
    assert MiniSBDSentencizer.split_sentences is rule_based_split_sentences


def test_patched_sentencizer_needs_no_network():
    OfflineEngine()
    from argostranslate.translate import MiniSBDSentencizer

    class FakePkg:
        from_code = "en"

    sentencizer = MiniSBDSentencizer.__new__(MiniSBDSentencizer)
    result = sentencizer.split_sentences("One. Two. Three.")
    assert result == ["One.", "Two.", "Three."]
