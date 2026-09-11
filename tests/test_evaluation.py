from evals.dataset import build_dataset, build_mechanical_dataset
from evals.metrics import aggregate, score_case
from evals.run_eval import _write_report


def test_dataset_contains_one_hundred_cases():
    cases = build_dataset()

    assert len(cases) == 100
    assert len({case.id for case in cases}) == 100


def test_mechanical_dataset_contains_real_domain_and_unanswerable_cases():
    cases = build_mechanical_dataset()

    assert len(cases) == 64
    assert len({case.id for case in cases}) == 64
    assert sum(case.answerable for case in cases) == 54
    assert sum(not case.answerable for case in cases) == 10


def test_evaluation_metrics_score_grounded_answer():
    case = build_dataset()[0]
    response = {
        "answer": "员工每年有5天年假。[1]",
        "insufficient_context": False,
        "citations": [
            {
                "title": "sample_handbook",
                "document_id": "doc-1",
                "chunk_id": "chunk-1",
                "excerpt": "员工每个自然年度享有5天带薪年假。",
            }
        ],
    }

    scores = score_case(case, response)
    summary = aggregate([scores])

    assert summary["recall_at_k"] == 1.0
    assert summary["mrr"] == 1.0
    assert summary["first_citation_accuracy"] == 1.0
    assert summary["answer_correctness"] == 1.0
    assert summary["citation_accuracy"] == 1.0


def test_first_citation_accuracy_follows_the_first_reference_in_answer():
    case = build_dataset()[0]
    response = {
        "answer": "员工每年有5天年假。[2]",
        "insufficient_context": False,
        "citations": [
            {"title": "无关资料", "document_id": "d0", "chunk_id": "c0", "excerpt": ""},
            {
                "title": "sample_handbook",
                "document_id": "d1",
                "chunk_id": "c1",
                "excerpt": "员工每年有5天年假。",
            },
        ],
    }

    assert score_case(case, response)["first_citation_accuracy"] == 1.0


def test_evaluation_checkpoint_excludes_failed_cases_from_summary(tmp_path):
    output = tmp_path / "evaluation.json"
    report = _write_report(
        output,
        "http://localhost:8000",
        "mechanical",
        [
            {"scores": {"recall_at_k": 1.0}},
            {"scores": None, "error": "timeout"},
        ],
    )

    assert output.exists()
    assert report["completed_count"] == 1
    assert report["error_count"] == 1
    assert report["summary"]["recall_at_k"] == 1.0
