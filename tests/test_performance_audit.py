from scripts.performance_audit import run_target


def test_deterministic_performance_audit_counts_work_without_spawning_processes():
    metrics = run_target("audit.example", requested_candidates=2,
                         mode="deterministic", requested_runs=10)

    assert metrics.candidate_count == 2
    assert metrics.runs_per_candidate == 10
    assert metrics.sample_count == metrics.valid_samples == 20
    assert metrics.failed_samples == 0
    assert metrics.subprocess_count == 0
    assert metrics.command_invocations == 20
    assert metrics.db_inserts == 24
    assert metrics.db_updates == 1
    assert metrics.db_deletes == 0
    assert metrics.db_commits == 1
    assert metrics.summarize_calls == 2
    assert metrics.summarize_ms >= 0
