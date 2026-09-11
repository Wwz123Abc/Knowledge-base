from evals.summarize_traces import percentile


def test_percentile_handles_empty_and_ordered_values():
    assert percentile([], 0.95) is None
    assert percentile([30, 10, 20], 0.5) == 20
    assert percentile([10, 20, 30, 40], 0.95) == 40
