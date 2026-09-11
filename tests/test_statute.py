from app.domain.retrieval.statute import article_phrases, cn_to_int, int_to_cn, is_count_query


def test_cn_to_int_converts_compound_numerals():
    assert cn_to_int("三百") == 300
    assert cn_to_int("一千二百六十") == 1260
    assert cn_to_int("十") == 10
    assert cn_to_int("三十") == 30
    assert cn_to_int("一") == 1


def test_int_to_cn_round_trips():
    assert int_to_cn(300) == "三百"
    assert int_to_cn(1260) == "一千二百六十"
    assert int_to_cn(10) == "十"
    assert int_to_cn(30) == "三十"
    assert int_to_cn(11) == "十一"
    assert int_to_cn(310) == "三百一十"
    assert int_to_cn(1050) == "一千零五十"


def test_article_phrases_normalizes_both_numeral_forms():
    assert article_phrases("《民法典》第300条是什么") == ["第三百条", "第300条"]
    assert article_phrases("第三百条是什么") == ["第三百条", "第300条"]
    assert article_phrases("年假有几天") == []


def test_count_query_detection():
    assert is_count_query("《民法典》一共几条？")
    assert is_count_query("总共多少条")
    assert not is_count_query("第三条是什么")
