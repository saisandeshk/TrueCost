import asyncio
import json
import sqlite3
from types import SimpleNamespace

import pytest
from nat.data_models.evaluator import EvalInput, EvalInputItem
from nat.plugin_api import Context
from truecost.report import compare, per_task, run_metrics

from nat_truecost._ledger import read_ledger
from nat_truecost.callback import SummaryCallback, SummaryConfig
from nat_truecost.dataset import LedgerRunConfig, load_rows
from nat_truecost.evaluators import LedgerEvaluator
from nat_truecost.register import ReplayConfig, replay, ledger_dataset


def items(ledger):
    return EvalInput(eval_input_items=[EvalInputItem(
        id=t["id"], input_obj=t["id"], expected_output_obj="verified", full_dataset_entry=t,
    ) for t in load_rows(ledger.path, "run")])


def summary(ledger, tmp_path, baseline="base"):
    cb = SummaryCallback(SummaryConfig(ledger=str(ledger.path), run_id="run", baseline_run_id=baseline))
    cb.on_eval_complete(SimpleNamespace(output_dir=tmp_path))
    return json.loads((tmp_path / "truecost_summary.json").read_text())


def test_loader_only_graded_with_exact_aggregates(ledger):
    rows = load_rows(ledger.path, "run")
    assert [x["id"] for x in rows] == ["run/0", "run/1", "run/2"]
    assert [x["question"] for x in rows] == ["duplicate title", "duplicate title", "2"]
    expected = per_task(ledger, "run")
    for row in rows:
        assert row["answer"] == "verified"
        assert {k: row[k] for k in expected[row["id"]]} == expected[row["id"]]
    assert rows[0]["cost"] == 6  # all three AGENT_SIDE roles; no user/grader
    assert rows[0]["energy_j"] == 10800


def test_dataset_parser_and_registered_loader(ledger):
    cfg = LedgerRunConfig(ledger=str(ledger.path), run_id="run")
    fn, kwargs = cfg.parser()
    df = fn(cfg.file_path, **kwargs)
    assert list(df.id) == ["run/0", "run/1", "run/2"]

    async def check():
        async with ledger_dataset(cfg, None) as loader:
            assert loader.load_fn(cfg.file_path).to_dict("records") == df.to_dict("records")
    asyncio.run(check())


def test_read_only_no_initialization_and_missing_file(tmp_path, ledger):
    with read_ledger(ledger.path) as db:
        assert run_metrics(db, "run") == run_metrics(ledger, "run")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            db.add_call({"call_id": "bad"})
    missing = tmp_path / "no-directory" / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        with read_ledger(missing):
            pass
    assert not missing.parent.exists()


def test_replay_duplicate_titles_uses_keys(ledger):
    async def check():
        with Context.scope(workflow_run_id="replay-test"):
            async with replay(ReplayConfig(ledger=str(ledger.path), run_id="run"), None) as fn:
                assert await fn.single_fn("run/0") == "verified"
                assert await fn.single_fn("run/2") == "failed"
                with pytest.raises(KeyError):
                    await fn.single_fn("run/missing")
    asyncio.run(check())


@pytest.mark.parametrize("metric,scores,aggregate", [
    ("verified", [1, 1, 0], 2 / 3), ("cost", [6, 3, 6], None),
    ("energy", [3, .5, 4], None), ("violations", [0, 1, 2], 100),
])
def test_evaluator_scores_and_honest_aggregates(ledger, metric, scores, aggregate):
    result = asyncio.run(LedgerEvaluator(metric).evaluate(items(ledger)))
    assert [x.score for x in result.eval_output_items] == scores
    assert result.average_score == aggregate
    assert all(x.error is None for x in result.eval_output_items)
    if metric in ("cost", "energy"):
        assert all("ratio of sums" in x.reasoning for x in result.eval_output_items)


def test_summary_exact_core_parity_and_ratio_of_sums(ledger, tmp_path, capsys):
    output = summary(ledger, tmp_path)
    expected = run_metrics(ledger, "run")
    assert {k: output[k] for k in expected} == expected
    assert output["usd_per_verified"] == 7.5  # mean task USD would be 5
    assert output["energy_wh_per_verified"] == 3.75
    assert output["wasted_cost_share"] == .4
    assert output["comparison"] == json.loads(json.dumps(compare(ledger, "base", "run")))
    stdout = capsys.readouterr().out
    assert "7.5" in stdout and "3.75" in stdout and "0.6666666666666666" in stdout


@pytest.mark.parametrize("column,metric,field", [
    ("energy_j", "energy", "energy_wh_per_verified"),
    ("cost_usd", "cost", "usd_per_verified"),
])
def test_unknown_call_never_becomes_zero(ledger, tmp_path, capsys, column, metric, field):
    ledger.conn.execute(f"UPDATE calls SET {column}=NULL WHERE call_id='run/0/agent'")
    result = asyncio.run(LedgerEvaluator(metric).evaluate(items(ledger)))
    assert result.eval_output_items[0].score is None
    out = summary(ledger, tmp_path)
    assert out[field] is None
    ratio = "energy_per_verified_ratio" if metric == "energy" else "usd_per_verified_ratio"
    assert out["comparison"][ratio] is None
    if metric == "energy":
        assert "unmeasured" in capsys.readouterr().out
        assert "unmeasured" in result.eval_output_items[0].reasoning


def test_zero_energy_and_no_verified_follow_core(ledger, tmp_path):
    ledger.conn.execute("UPDATE calls SET energy_j=0 WHERE run_id='run'")
    ledger.conn.execute("UPDATE tasks SET outcome='failed' WHERE run_id='run' AND outcome='verified'")
    out = summary(ledger, tmp_path, baseline=None)
    assert out["verified"] == 0
    assert out["usd_per_verified"] == 15  # preserve core max(verified, 1)
    assert out["energy_wh_per_verified"] is None
    assert "comparison" not in out


def test_empty_run_and_unknown_violations(ledger, tmp_path):
    assert load_rows(ledger.path, "missing") == []
    cb = SummaryCallback(SummaryConfig(ledger=str(ledger.path), run_id="missing"))
    cb.on_eval_complete(SimpleNamespace(output_dir=tmp_path))
    assert json.loads((tmp_path / "truecost_summary.json").read_text())["tasks"] == 0
    ledger.conn.execute("UPDATE tasks SET violations=NULL WHERE run_id='run'")
    result = asyncio.run(LedgerEvaluator("violations").evaluate(items(ledger)))
    assert result.average_score is None
    assert summary(ledger, tmp_path)["violations_per_100"] is None


def test_mixed_unknown_values_survive_nat_dataframe(ledger):
    from nat.plugins.eval.dataset_handler.dataset_handler import DatasetHandler
    from nat.data_models.dataset_handler import EvalDatasetStructureConfig
    ledger.conn.execute("UPDATE tasks SET violations=NULL WHERE task_key='run/1'")
    cfg = LedgerRunConfig(ledger=str(ledger.path), run_id="run",
                          structure=EvalDatasetStructureConfig(question_key="id"))
    parser, kwargs = cfg.parser()
    handler = DatasetHandler(cfg, reps=1, concurrency=1)
    eval_input = handler.get_eval_input_from_df(parser(cfg.file_path, **kwargs))
    assert eval_input.eval_input_items[1].full_dataset_entry["violations"] is None
    out = asyncio.run(LedgerEvaluator("violations").evaluate(eval_input))
    assert [x.score for x in out.eval_output_items] == [0, None, 2]
    assert out.average_score == pytest.approx(200 / 3)


def test_nonfinite_comparison_stays_strict_json(ledger, tmp_path):
    ledger.conn.execute("UPDATE tasks SET outcome='failed' WHERE run_id='run' AND outcome='verified'")
    result = summary(ledger, tmp_path)
    assert result["comparison"]["usd_per_verified_ratio"] == "inf"
    assert "Infinity" not in (tmp_path / "truecost_summary.json").read_text()


def test_summary_requires_nat_output_directory(ledger):
    cb = SummaryCallback(SummaryConfig(ledger=str(ledger.path), run_id="run"))
    with pytest.raises(ValueError, match="output directory"):
        cb.on_eval_complete(SimpleNamespace(output_dir=None))
