import pytest

from src.learning.policy_store import CreditConflict, PolicyStore, PolicyStoreError, StrategyStatus

TENANT = "tenant-a"


def _strategy(store, *, tenant=TENANT):
    return store.propose_strategy(
        tenant_id=tenant,
        kind="precedent_probe", pattern="{material} direct precedent common protocol",
        source_job_id="source-job", source_task_family="garnet-electrolytes",
    )


def _credit(store, strategy_id, index, success=True, family=None, tenant=TENANT):
    application = store.record_application(
        tenant_id=tenant, strategy_id=strategy_id, target_job_id=f"target-{index}",
        target_task_family=family or f"family-{index}",
        rendered_query=f"query {index}", idempotency_key=f"application-{index}",
    )
    store.record_outcome(
        tenant_id=tenant, application_id=application,
        evaluator_kind="known_answer", success=success,
        false_gap_avoided=success, valid_finding_delta=1.0 if success else 0.0,
        calls=2, tokens=0, evidence_ref=f"challenge-set:item-{index}",
    )
    return application


def test_strategy_is_not_recalled_before_delayed_external_credit(tmp_path):
    store = PolicyStore(tmp_path / "policy.db")
    strategy = _strategy(store)
    assert store.recall_active(tenant_id=TENANT, target_task_family="new") == []
    _credit(store, strategy, 1)
    _credit(store, strategy, 2)
    assert store.refresh_statuses(
        tenant_id=TENANT, min_evaluations=3
    )[0].status == StrategyStatus.CANDIDATE
    _credit(store, strategy, 3)
    scores = store.refresh_statuses(
        tenant_id=TENANT, min_evaluations=3, activation_lower_bound=0.2
    )
    assert scores[0].status == StrategyStatus.ACTIVE
    assert store.recall_active(
        tenant_id=TENANT, target_task_family="new"
    )[0].strategy_id == strategy


def test_model_self_rating_cannot_create_credit(tmp_path):
    store = PolicyStore(tmp_path / "policy.db")
    strategy = _strategy(store)
    application = store.record_application(
        tenant_id=TENANT, strategy_id=strategy, target_job_id="target",
        target_task_family="other",
        rendered_query="query", idempotency_key="application",
    )
    with pytest.raises(PolicyStoreError, match="credit must come"):
        store.record_outcome(
            tenant_id=TENANT, application_id=application,
            evaluator_kind="model_self", success=True,
            false_gap_avoided=True, valid_finding_delta=1, calls=1, tokens=1,
            evidence_ref="model said so",
        )


def test_same_family_outcomes_do_not_promote_cross_task_strategy(tmp_path):
    store = PolicyStore(tmp_path / "policy.db")
    strategy = _strategy(store)
    for index in range(5):
        _credit(store, strategy, index, family="garnet-electrolytes")
    score = store.refresh_statuses(tenant_id=TENANT, min_evaluations=3)[0]
    assert score.evaluations == 0
    assert score.status == StrategyStatus.CANDIDATE


def test_outcome_is_idempotent_but_conflicting_rewrite_fails(tmp_path):
    store = PolicyStore(tmp_path / "policy.db")
    strategy = _strategy(store)
    application = _credit(store, strategy, 1)
    store.record_outcome(
        tenant_id=TENANT, application_id=application,
        evaluator_kind="known_answer", success=True,
        false_gap_avoided=True, valid_finding_delta=1.0, calls=2, tokens=0,
        evidence_ref="challenge-set:item-1",
    )
    with pytest.raises(CreditConflict):
        store.record_outcome(
            tenant_id=TENANT, application_id=application,
            evaluator_kind="known_answer", success=False,
            false_gap_avoided=False, valid_finding_delta=0.0, calls=2, tokens=0,
            evidence_ref="challenge-set:item-1",
        )


def test_active_strategy_not_recalled_into_source_family(tmp_path):
    store = PolicyStore(tmp_path / "policy.db")
    strategy = _strategy(store)
    for index in range(3):
        _credit(store, strategy, index)
    store.refresh_statuses(
        tenant_id=TENANT, min_evaluations=3, activation_lower_bound=0.2
    )
    assert store.recall_active(
        tenant_id=TENANT, target_task_family="garnet-electrolytes"
    ) == []


def test_tenant_boundaries_cover_lookup_credit_and_recall(tmp_path):
    store = PolicyStore(tmp_path / "policy.db")
    strategy = _strategy(store)
    with pytest.raises(PolicyStoreError, match="unknown strategy"):
        store.record_application(
            tenant_id="tenant-b", strategy_id=strategy, target_job_id="target",
            target_task_family="other", rendered_query="query",
            idempotency_key="cross-tenant-application",
        )
    for index in range(3):
        _credit(store, strategy, index)
    store.refresh_statuses(
        tenant_id=TENANT, min_evaluations=3, activation_lower_bound=0.2
    )
    assert store.recall_active(
        tenant_id="tenant-b", target_task_family="other"
    ) == []
    assert store.audit_snapshot(tenant_id="tenant-b")["credited_outcomes"] == 0


def test_uncredited_replay_and_candidate_exploration_are_distinct(tmp_path):
    store = PolicyStore(tmp_path / "policy.db")
    strategy = _strategy(store)
    replay = store.recall_uncredited(
        tenant_id=TENANT, target_task_family="new-family",
    )
    assert [item.strategy_id for item in replay] == [strategy]
    candidate = store.recall_candidate_for_evaluation(
        tenant_id=TENANT, target_task_family="new-family",
    )
    assert candidate.strategy_id == strategy

    store.record_application(
        tenant_id=TENANT, strategy_id=strategy, target_job_id="target",
        target_task_family="new-family", rendered_query="query",
        idempotency_key="candidate-evaluation",
    )
    assert store.recall_candidate_for_evaluation(
        tenant_id=TENANT, target_task_family="new-family",
    ) is None
    assert store.recall_candidate_for_evaluation(
        tenant_id=TENANT, target_task_family="another-family",
    ).strategy_id == strategy


def test_ordered_intervention_is_immutable_and_replayable(tmp_path):
    store = PolicyStore(tmp_path / "policy.db")
    strategy = _strategy(store)
    kwargs = dict(
        tenant_id=TENANT, run_id="run", method_id="memory-method",
        sequence_index=1, challenge_id="challenge", order_sha256="a" * 64,
        memory_mode="delayed_external_credit", strategy_ids=[strategy],
    )
    first = store.bind_sequence_intervention(**kwargs)
    second = store.bind_sequence_intervention(**kwargs)
    assert first == second
    assert store.sequence_intervention(
        tenant_id=TENANT, run_id="run", method_id="memory-method",
        sequence_index=1,
    ) == first
    with pytest.raises(CreditConflict):
        store.bind_sequence_intervention(**{**kwargs, "challenge_id": "changed"})
