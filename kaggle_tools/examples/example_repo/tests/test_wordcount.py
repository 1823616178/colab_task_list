"""词频统计的验收测试。"""

from textstats import top_words, word_frequencies


def test_counts_repeated_words():
    assert word_frequencies("a b a") == {"a": 2, "b": 1}


def test_is_case_insensitive():
    assert word_frequencies("The the THE") == {"the": 3}


def test_strips_punctuation():
    assert word_frequencies("hello, world! hello.") == {"hello": 2, "world": 1}


def test_empty_text_gives_empty_result():
    assert word_frequencies("") == {}


def test_top_words_orders_by_descending_count():
    assert top_words("a a a b b c") == [("a", 3), ("b", 2), ("c", 1)]


def test_top_words_respects_limit():
    assert top_words("a a a b b c", limit=2) == [("a", 3), ("b", 2)]
