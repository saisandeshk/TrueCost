"""Finished TrueCost runs as offline NAT evaluation datasets."""
from pathlib import Path

import pandas as pd
from nat.plugin_api import EvalDatasetBaseConfig
from truecost.report import per_task

from ._ledger import read_ledger


class LedgerRunConfig(EvalDatasetBaseConfig, name="truecost_ledger_run"):
    ledger: str
    run_id: str

    def parser(self):
        from functools import partial
        return partial(load_dataframe, self), {}


def load_rows(ledger: str | Path, run_id: str) -> list[dict]:
    with read_ledger(ledger) as db:
        tasks = per_task(db, run_id)
    return [dict(task, id=key, question=task["title"] or task["task_id"] or key,
                 answer="verified") for key, task in sorted(tasks.items())]


def load_dataframe(config: LedgerRunConfig, file_path=None, **kwargs) -> pd.DataFrame:
    # NAT's loader protocol passes file_path even though the ledger is our data source.
    return pd.DataFrame(load_rows(config.ledger, config.run_id), dtype=object)
