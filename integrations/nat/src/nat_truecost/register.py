"""All plugin registrations, discovered through the nat.plugins entry point."""
from functools import partial

from nat.plugin_api import (
    Builder, DatasetLoaderInfo, EvalBuilder, EvaluatorInfo, FunctionBaseConfig, FunctionInfo,
    register_dataset_loader, register_eval_callback, register_evaluator, register_function,
    register_telemetry_exporter,
)

from .callback import SummaryCallback, SummaryConfig
from .dataset import LedgerRunConfig, load_dataframe, load_rows
from .evaluators import CostConfig, EnergyConfig, LedgerEvaluator, VerifiedConfig, ViolationsConfig
from .exporter import LedgerExporter, LedgerExporterConfig, SummaryExporter


class ReplayConfig(FunctionBaseConfig, name="truecost_replay"):
    ledger: str
    run_id: str


@register_dataset_loader(config_type=LedgerRunConfig)
async def ledger_dataset(config: LedgerRunConfig, builder: EvalBuilder):
    yield DatasetLoaderInfo(config=config, load_fn=partial(load_dataframe, config),
                            description="Graded tasks and agent-side aggregates from a read-only ledger")


@register_function(config_type=ReplayConfig)
async def replay(config: ReplayConfig, builder: Builder):
    tasks = {row["id"]: row for row in load_rows(config.ledger, config.run_id)}

    async def recorded_outcome(task_key: str) -> str:
        return tasks[task_key]["outcome"]

    yield FunctionInfo.from_fn(recorded_outcome, description="Replay an independently graded recorded outcome offline")


@register_evaluator(config_type=VerifiedConfig)
async def verified(config: VerifiedConfig, builder: EvalBuilder):
    yield EvaluatorInfo(config=config, evaluate_fn=LedgerEvaluator("verified").evaluate,
                        description="Verified rate from recorded outcomes")


@register_evaluator(config_type=CostConfig)
async def cost(config: CostConfig, builder: EvalBuilder):
    yield EvaluatorInfo(config=config, evaluate_fn=LedgerEvaluator("cost").evaluate,
                        description="Task USD; authoritative USD per verified in truecost_summary.json")


@register_evaluator(config_type=EnergyConfig)
async def energy(config: EnergyConfig, builder: EvalBuilder):
    yield EvaluatorInfo(config=config, evaluate_fn=LedgerEvaluator("energy").evaluate,
                        description="Task Wh; authoritative Wh per verified in truecost_summary.json")


@register_evaluator(config_type=ViolationsConfig)
async def violations(config: ViolationsConfig, builder: EvalBuilder):
    yield EvaluatorInfo(config=config, evaluate_fn=LedgerEvaluator("violations").evaluate,
                        description="Policy violations per 100 tasks")


@register_telemetry_exporter(config_type=LedgerExporterConfig)
async def ledger_exporter(config: LedgerExporterConfig, builder: Builder):
    yield LedgerExporter(config)


@register_telemetry_exporter(config_type=SummaryConfig)
async def summary_exporter(config: SummaryConfig, builder: Builder):
    yield SummaryExporter()


@register_eval_callback(config_type=SummaryConfig)
def summary_callback(config: SummaryConfig):
    return SummaryCallback(config)
