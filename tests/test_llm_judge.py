from evals.llm_judge import parse_judge_score


def test_parse_llm_judge_json_and_clamp_score():
    assert parse_judge_score('{"score": 0.82}') == 0.82
    assert parse_judge_score('{"score": 1.5}') == 1.0


def test_parse_llm_judge_text_fallback():
    assert parse_judge_score("忠实度分数：0.67") == 0.67
