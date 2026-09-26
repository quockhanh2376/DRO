"""Deterministic candidate ranking and rewrite decision rules."""

from __future__ import annotations

from app.models.benchmark import BenchmarkResult, DecisionResult, PendingCandidateState


def rank_candidates(candidates: list[BenchmarkResult]) -> list[BenchmarkResult]:
    """Rank healthy candidates by average, then median, then jitter (ascending)."""
    return sorted((candidate for candidate in candidates if candidate.healthy and candidate.average_ms is not None),
                  key=lambda result: (result.average_ms, result.median_ms, result.jitter_ms))


class DecisionEngine:
    def decide(self, current_ip: str | None, current: BenchmarkResult | None,
               candidates: list[BenchmarkResult], pending: PendingCandidateState | None = None,
               switch_threshold_ms: float = 50, switch_threshold_percent: float = 5,
               required_consecutive_wins: int = 2, manual_lock_ip: str | None = None,
               immediate_switch_if_current_unhealthy: bool = True) -> DecisionResult:
        pending = pending or PendingCandidateState()
        ranked = rank_candidates(candidates)
        current_result = next((result for result in candidates if result.ip == current_ip), current)
        winner = ranked[0] if ranked else None
        if manual_lock_ip is not None:
            return DecisionResult(action="LOCKED", current_ip=current_ip, candidate_ip=manual_lock_ip,
                                  reason="Manual lock is active; automatic rewrite is disabled.")
        if current_ip and (current_result is None or not current_result.healthy):
            alternative = next((candidate for candidate in ranked if candidate.ip != current_ip), None)
            if immediate_switch_if_current_unhealthy and alternative:
                return DecisionResult(action="FAILOVER", current_ip=current_ip, candidate_ip=alternative.ip,
                                      reason="Current IP is unhealthy; switching to the best healthy alternative.",
                                      required_wins=required_consecutive_wins)
            return DecisionResult(action="KEEP", current_ip=current_ip,
                                  reason="Current IP is unhealthy and no healthy alternative is available.")
        if winner is None:
            return DecisionResult(action="KEEP", current_ip=current_ip, reason="No healthy candidates are available.")
        if winner.ip == current_ip:
            return DecisionResult(action="KEEP", current_ip=current_ip, candidate_ip=winner.ip,
                                  reason="Current IP is already the best candidate.")
        if current_result is None or current_result.average_ms is None or winner.average_ms is None:
            return DecisionResult(action="KEEP", current_ip=current_ip, candidate_ip=winner.ip,
                                  reason="Current IP has no healthy benchmark to compare.")
        improvement_ms = current_result.average_ms - winner.average_ms
        improvement_percent = improvement_ms / current_result.average_ms * 100 if current_result.average_ms > 0 else 0
        if improvement_ms < switch_threshold_ms and improvement_percent < switch_threshold_percent:
            return DecisionResult(action="KEEP", current_ip=current_ip, candidate_ip=winner.ip,
                                  reason="Candidate improvement is below both switch thresholds.",
                                  improvement_ms=improvement_ms, improvement_percent=improvement_percent)
        wins = pending.consecutive_wins + 1 if pending.candidate_ip == winner.ip else 1
        action = "UPDATE" if wins >= required_consecutive_wins else "HOLD"
        return DecisionResult(action=action, current_ip=current_ip, candidate_ip=winner.ip,
                              reason="Candidate met the improvement threshold; consecutive win confirmed." if action == "UPDATE"
                              else "Candidate met the improvement threshold; awaiting another consecutive win.",
                              wins=wins, required_wins=required_consecutive_wins,
                              improvement_ms=improvement_ms, improvement_percent=improvement_percent)
