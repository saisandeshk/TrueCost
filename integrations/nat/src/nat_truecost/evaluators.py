"""Per-task evidence; run-level ratios are reported by truecost_summary."""
from nat.plugin_api import EvaluatorBaseConfig
from nat.plugins.eval.evaluator.base_evaluator import BaseEvaluator
from nat.plugins.eval.data_models.evaluator_io import EvalOutputItem
from truecost.ledger import VERIFIED
from truecost.report import J_PER_WH


class VerifiedConfig(EvaluatorBaseConfig, name="truecost_verified"):
    pass


class CostConfig(EvaluatorBaseConfig, name="truecost_cost_per_verified"):
    pass


class EnergyConfig(EvaluatorBaseConfig, name="truecost_energy_per_verified"):
    pass


class ViolationsConfig(EvaluatorBaseConfig, name="truecost_violations"):
    pass


class LedgerEvaluator(BaseEvaluator):
    def __init__(self, metric: str):
        super().__init__(tqdm_desc=f"TrueCost {metric}")
        self.metric = metric

    async def evaluate_item(self, item):
        task = item.full_dataset_entry
        if self.metric == "verified":
            score = float(task["outcome"] == VERIFIED)
            note = "Recorded independent outcome; 1 means verified."
        elif self.metric == "cost":
            score = None if task["unpriced_calls"] else task["cost"]
            note = ("Agent-side task USD, including summariser and governor. Unknown price stays null. "
                    "USD per verified is a ratio of sums in truecost_summary.json, not a mean of these scores.")
        elif self.metric == "energy":
            score = None if task["unmetered_calls"] else task["energy_j"] / J_PER_WH
            note = ("unmeasured" if score is None else "Agent-side task Wh") + (
                ". Wh per verified is a ratio of sums in truecost_summary.json, not a mean of these scores. "
                "Any unmetered call makes run energy unmeasured; zero total energy is also unmeasured.")
        else:
            score = task["violations"]
            note = "Recorded policy violations. truecost_summary.json reports violations per 100 tasks."
        return EvalOutputItem(id=item.id, score=score, reasoning=note)

    async def evaluate(self, eval_input):
        result = await super().evaluate(eval_input)
        # Do not expose a misleading mean as a cost/energy ratio. The callback uses run_metrics.
        if self.metric in ("cost", "energy"):
            result.average_score = None
        elif self.metric == "verified":
            result.average_score = (sum(x.score for x in result.eval_output_items) /
                                    len(result.eval_output_items)) if result.eval_output_items else None
        else:
            scores = [x.score for x in result.eval_output_items]
            result.average_score = (100 * sum(x or 0 for x in scores) / len(scores)
                                    if any(x is not None for x in scores) else None)
        return result
