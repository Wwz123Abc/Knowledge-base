from langchain_core.documents import Document

from app.domain.chat.helpers import _excerpt, citation

# Shaped like the real production chunk that produced a useless citation: it opens with the
# stray "。" left by the splitter, spends its first 200 characters on unrelated leave rules, and
# only then gets to the line that answers "how many days is Spring Festival".
LEAVE_RULES = (
    "。\n公司对员工提供的假期相关证明材料有疑问的，有权进行核实及复查，员工需配合公司的核实及复查。\n"
    "年假以0.5天为最小单位，事假、病假等假期以2H为最小单位。\n"
    "婚假、护理假、流产假、产假、工伤假、丧假等假期必须连休。\n"
    "休产假、流产假、护理假的员工申请提前返岗，公司同意其申请后，予以发放返岗补贴。\n"
    "请假审批权限：\n假期类别\n法定假\n"
    "法定假包括元旦1天，春节4天，清明节1天，劳动节2天，端午节1天，中秋节1天，国庆节3天。\n"
    "最终法定假放假天数参照当年国务院发布的通知。\n"
    "年假的计算方式：当年度应休年假天数=(当年度剩余日历天数÷365天)×职工本人全年应当享受的年休假天数。\n"
    "试用期员工不享受年假，转正后按剩余日历天数折算。\n"
) * 2


def test_excerpt_shows_the_passage_that_answers_the_question_not_the_chunk_head():
    excerpt = _excerpt(LEAVE_RULES, query="春节放几天假")
    assert "春节4天" in excerpt
    assert not excerpt.lstrip("…").startswith("。")
    assert len(excerpt) <= 225


def test_excerpt_follows_the_question_to_a_different_part_of_the_same_chunk():
    excerpt = _excerpt(LEAVE_RULES, query="试用期员工能不能休年假")
    assert "试用期员工不享受年假" in excerpt


def test_excerpt_without_a_query_is_the_cleaned_head():
    excerpt = _excerpt(LEAVE_RULES)
    assert excerpt.startswith("公司对员工提供的假期相关证明材料")
    assert excerpt.endswith(("。", "…", "\n"))


def test_short_chunks_are_returned_whole_and_citation_passes_the_query_through():
    short = "。员工每个自然年度享有5天带薪年假。"
    assert _excerpt(short, query="年假") == "员工每个自然年度享有5天带薪年假。"
    document = Document(page_content=LEAVE_RULES, metadata={"title": "考勤规定", "chunk_id": "c1"})
    assert "春节4天" in citation(document, "春节放几天假").excerpt
