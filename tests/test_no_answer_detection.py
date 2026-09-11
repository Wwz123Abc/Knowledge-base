from app.core.rag import _is_insufficient_answer


def test_detects_explicit_no_answer_phrases():
    assert _is_insufficient_answer("知识库中没有找到足够依据。") is True
    assert _is_insufficient_answer("资料中未提及公司的餐补标准。") is True
    assert _is_insufficient_answer("知识库中没有找到关于库存的信息，无法回答。") is True
    assert _is_insufficient_answer("知识库未提供设备质保期限。") is True


def test_does_not_mark_grounded_answer_as_insufficient():
    assert _is_insufficient_answer("员工应在十个工作日内提交报销 [1]。") is False
