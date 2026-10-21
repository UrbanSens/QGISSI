"""Every QGISSI-Bench checker must give full marks to the scripted reference solutions."""
import pytest

from qgissi.bench.tasks import TASKS


@pytest.mark.parametrize("task", TASKS, ids=[t.id for t in TASKS])
def test_oracle_solves_task(task, fixture_dir, tmp_path):
    from qgissi.bench.runner import BenchConfig, make_target, run_task
    record = run_task(task, make_target("oracle"), BenchConfig(max_steps=12), fixture_dir, str(tmp_path / task.id))
    assert record["status"] == "completed", record
    assert record["score"] == 1.0, record


def test_failing_answer_scores_zero(fixture_dir, tmp_path):
    from qgissi.bench.runner import BenchConfig, make_target, run_task
    from qgissi.bench.oracle import SCRIPTS
    from qgissi.bench.tasks import TASKS_BY_ID
    SCRIPTS["_wrong"] = [lambda r: "ANSWER: 999"]
    task = TASKS_BY_ID["inspect_count_schools"]
    target = make_target("oracle")
    target.provider.prepare = lambda task_id: type(target.provider).prepare(target.provider, "_wrong")
    record = run_task(task, target, BenchConfig(max_steps=3), fixture_dir, str(tmp_path / "wrong"))
    assert record["score"] == 0.0 and record["got"] == 999
