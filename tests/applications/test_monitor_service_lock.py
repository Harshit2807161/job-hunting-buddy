from jhb import config
from jhb.applications import monitor
from tests.applications.test_monitor import setup, failure, runner, repo


def test_repair_cannot_mutate_code_while_approved_service_owns_its_lane(setup):
    failure(setup)
    calls = []
    with monitor._lock(config.ROOT / "private" / "approved-worker.lock") as owned:
        assert owned
        result = monitor.once(run=runner(calls), inspect_repository=repo)
    assert result["state"] == "approved_worker_busy" and calls == []
    assert not (monitor.directory() / "repair-pending.json").exists()


def test_all_repair_and_validation_operations_hold_both_worker_lanes(setup):
    failure(setup)
    calls = []
    base = runner(calls)
    def run(*args, **kwargs):
        with monitor._lock(config.ROOT / "private" / "approved-worker.lock") as owned:
            assert not owned
        return base(*args, **kwargs)
    assert monitor.once(run=run, inspect_repository=repo)["state"] == "validated"
    assert len(calls) == 4
