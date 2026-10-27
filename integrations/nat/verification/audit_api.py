"""Record installed-source evidence for every NAT import used by the plugin."""
import ast
import hashlib
import importlib
from importlib.metadata import version
import inspect
from pathlib import Path
import sysconfig

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SITE = Path(sysconfig.get_paths()["purelib"])
imports = set()
for file in [*(ROOT / "src/nat_truecost").glob("*.py"), *(ROOT / "tests").glob("*.py")]:
    for node in ast.walk(ast.parse(file.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("nat."):
            imports.update((node.module, name.name) for name in node.names)
rows = []
for module, name in sorted(imports):
    obj = getattr(importlib.import_module(module), name)
    source = Path(inspect.getsourcefile(obj))
    lines, start = inspect.getsourcelines(obj)
    rows.append(f"| `{module}.{name}` | `{source.relative_to(SITE)}:{start}` |")

# Additional methods used directly, including the inherited exporter lifecycle.
from nat.plugin_api import FunctionInfo, Context, ContextState
from nat.observability.exporter.base_exporter import BaseExporter, IsolatedAttribute
from nat.runtime.user_metadata import RequestAttributes
from nat.plugins.eval.evaluator.base_evaluator import BaseEvaluator
from nat.plugins.eval.dataset_handler.dataset_handler import DatasetHandler
from nat.utils.reactive.subject import Subject
methods = [FunctionInfo.from_fn, Context.metadata.fget, Context.workflow_run_id.fget,
           RequestAttributes.to_dict, BaseEvaluator.evaluate, BaseExporter.start,
           BaseExporter.stop, BaseExporter.create_isolated_instance, IsolatedAttribute.__get__, Context.scope, ContextState.get,
           DatasetHandler.get_eval_input_from_df, Subject.on_next]
for obj in methods:
    obj = inspect.unwrap(obj)
    source = Path(inspect.getsourcefile(obj))
    _, start = inspect.getsourcelines(obj)
    rows.append(f"| `{obj.__qualname__}` | `{source.relative_to(SITE)}:{start}` |")

files = {
    "nat/plugin_api/__init__.py", "nat/cli/register_workflow.py", "nat/runtime/loader.py",
    "nat/data_models/common.py", "nat/data_models/dataset_handler.py", "nat/data_models/evaluator.py",
    "nat/data_models/function.py", "nat/data_models/intermediate_step.py", "nat/data_models/token_usage.py",
    "nat/data_models/telemetry_exporter.py", "nat/data_models/invocation_node.py", "nat/utils/reactive/subject.py", "nat/builder/context.py", "nat/runtime/user_metadata.py",
    "nat/builder/dataset_loader.py", "nat/builder/evaluator.py", "nat/builder/function_info.py",
    "nat/observability/exporter/base_exporter.py", "nat/observability/register.py",
    "nat/plugins/eval/evaluator/base_evaluator.py", "nat/plugins/eval/data_models/evaluator_io.py",
    "nat/plugins/eval/cli/evaluate.py", "nat/plugins/eval/eval_callbacks.py",
    "nat/plugins/eval/runtime/evaluate.py", "nat/plugins/eval/dataset_handler/dataset_handler.py",
    "nat/plugins/eval/utils/intermediate_step_adapter.py", "nat/data_models/evaluate_config.py",
}
hashes = {file: hashlib.sha256((SITE / file).read_bytes()).hexdigest() for file in sorted(files)}
text = """# NAT 1.9.0 installed-source verification

Inspected source in `integrations/nat/.venv/lib/python3.12/site-packages/`.
Python 3.12.13; `nvidia-nat`, `nvidia-nat-core`, `nvidia-nat-eval`, and
`nvidia-nat-profiler` are all **1.9.0** (checked through installed metadata).
This inventory can be regenerated with `.venv/bin/python verification/audit_api.py`
from `integrations/nat/`.

| Imported API or method | Installed definition (relative to site-packages) |
|---|---|
""" + "\n".join(rows) + """

## Contracts checked against source

- `nat/plugin_api/__init__.py` exports all decorators, config bases, Builder /
  EvalBuilder, Context, DatasetLoaderInfo, EvaluatorInfo, and FunctionInfo used here.
  The exceptions below are absent from its `__all__` and imported from their
  actual defining modules. No guessed or compatibility `nat.eval.*` imports.
- `nat/cli/register_workflow.py`: dataset/function/evaluator/telemetry decorators
  wrap async generator factories in async context managers. Callback registration
  accepts a **TelemetryExporterBaseConfig** type and a synchronous factory.
- `nat/runtime/loader.py`: loads module entry points in group `nat.plugins` for
  decorator side effects. `nat_truecost.register` is a module entry point.
- `nat/data_models/common.py`: config subclasses declare `name=...`; YAML uses
  `_type` to select them. Separate names register each of the four evaluators.
- `DatasetLoaderInfo(config, load_fn, description)` and
  `EvaluatorInfo(config=..., evaluate_fn=..., description=...)` match constructors.
  `FunctionInfo.from_fn` accepts the annotated async replay callable.
- `nat/plugins/eval/dataset_handler/dataset_handler.py`: the CLI calls
  `dataset_config.parser()` and passes `file_path` to its returned function.
  The plugin implements that protocol as well as its registered load_fn.
  NAT builds full_dataset_entry from the complete dataframe row; structure's
  question_key selects workflow input. The demo uses `id` for unambiguous replay.
- `BaseEvaluator.evaluate_item` returns EvalOutputItem(id, score, reasoning).
  Its default evaluate averages numeric scores, rounds to two decimals, and
  ignores nulls. The subclass keeps ratio aggregates null, and computes full
  precision verified rate / violations per 100. Authoritative ratios use the
  existing core run_metrics in the callback, with unknown handling intact.
- `nat/plugins/eval/cli/evaluate.py:_build_eval_callback_manager` discovers
  callbacks from general.telemetry.tracing, by exporter config type.
  `EvalResult.output_dir` is the actual configured output path. Its callback
  manager invokes on_eval_complete(result); the summary uses that path.
- BaseExporter subscribes synchronously to IntermediateStep events on start;
  stop unsubscribes; workflow sessions make isolated exporter copies.
  The exporter subclasses it and uses IsolatedAttribute for paired-start state.
- IntermediateStep payload exposes event_type, UUID, event_timestamp,
  span_event_timestamp, metadata, data, usage_info; LLM_START / LLM_END are enum
  values. UsageInfo.token_usage provides prompt_tokens, completion_tokens,
  cached_tokens, reasoning_tokens. StreamEventData has payload; TraceMetadata
  has provided_metadata and allows extra fields. Model/task/header lookup uses
  those dictionaries without assuming undocumented fixed metadata keys.
- Context wraps the current context state; workflow_run_id provides the trace
  fallback, and metadata.to_dict() exposes request headers for gateway skipping.
- `nat/plugins/eval/utils/intermediate_step_adapter.py` unconditionally imports
  langchain_core.agents.AgentAction. The pinned eval/profiler install omitted
  langchain-core, producing ModuleNotFoundError on the first real CLI run.
  This package declares langchain-core explicitly; it needs no LLM provider.

## Exceptions to imports from nat.plugin_api

BaseEvaluator and EvalOutputItem live in `nat.plugins.eval`; BaseExporter and
IsolatedAttribute live in `nat.observability.exporter.base_exporter`;
IntermediateStepType lives in `nat.data_models.intermediate_step`. These APIs
are not exported by nat.plugin_api in 1.9.0. Tests additionally use the installed
evaluation/event models, Context.scope, dataset handler, and event Subject to
exercise the actual contracts.

## Source SHA-256

""" + "\n".join(f"- `{file}`: `{digest}`" for file, digest in hashes.items()) + "\n"
for package in ("nvidia-nat", "nvidia-nat-core", "nvidia-nat-eval", "nvidia-nat-profiler"):
    assert version(package) == "1.9.0"
(HERE / "NAT_API.md").write_text(text)
print(f"Verified {len(imports)} imported NAT symbols and {len(methods)} directly used methods.")
