# NAT 1.9.0 installed-source verification

Inspected source in `integrations/nat/.venv/lib/python3.12/site-packages/`.
Python 3.12.13; `nvidia-nat`, `nvidia-nat-core`, `nvidia-nat-eval`, and
`nvidia-nat-profiler` are all **1.9.0** (checked through installed metadata).
This inventory can be regenerated with `.venv/bin/python verification/audit_api.py`
from `integrations/nat/`.

| Imported API or method | Installed definition (relative to site-packages) |
|---|---|
| `nat.data_models.dataset_handler.EvalDatasetStructureConfig` | `nat/data_models/dataset_handler.py:58` |
| `nat.data_models.evaluator.EvalInput` | `nat/data_models/evaluator.py:68` |
| `nat.data_models.evaluator.EvalInputItem` | `nat/data_models/evaluator.py:44` |
| `nat.data_models.intermediate_step.IntermediateStep` | `nat/data_models/intermediate_step.py:262` |
| `nat.data_models.intermediate_step.IntermediateStepPayload` | `nat/data_models/intermediate_step.py:149` |
| `nat.data_models.intermediate_step.IntermediateStepType` | `nat/data_models/intermediate_step.py:44` |
| `nat.data_models.intermediate_step.StreamEventData` | `nat/data_models/intermediate_step.py:71` |
| `nat.data_models.intermediate_step.UsageInfo` | `nat/data_models/intermediate_step.py:99` |
| `nat.data_models.invocation_node.InvocationNode` | `nat/data_models/invocation_node.py:19` |
| `nat.data_models.token_usage.TokenUsageBaseModel` | `nat/data_models/token_usage.py:20` |
| `nat.observability.exporter.base_exporter.BaseExporter` | `nat/observability/exporter/base_exporter.py:103` |
| `nat.observability.exporter.base_exporter.IsolatedAttribute` | `nat/observability/exporter/base_exporter.py:40` |
| `nat.plugin_api.Builder` | `nat/builder/builder.py:75` |
| `nat.plugin_api.Context` | `nat/builder/context.py:142` |
| `nat.plugin_api.ContextState` | `nat/builder/context.py:67` |
| `nat.plugin_api.DatasetLoaderInfo` | `nat/builder/dataset_loader.py:24` |
| `nat.plugin_api.EvalBuilder` | `nat/builder/builder.py:796` |
| `nat.plugin_api.EvalDatasetBaseConfig` | `nat/data_models/dataset_handler.py:71` |
| `nat.plugin_api.EvaluatorBaseConfig` | `nat/data_models/evaluator.py:31` |
| `nat.plugin_api.EvaluatorInfo` | `nat/builder/evaluator.py:23` |
| `nat.plugin_api.FunctionBaseConfig` | `nat/data_models/function.py:26` |
| `nat.plugin_api.FunctionInfo` | `nat/builder/function_info.py:293` |
| `nat.plugin_api.TelemetryExporterBaseConfig` | `nat/data_models/telemetry_exporter.py:22` |
| `nat.plugin_api.register_dataset_loader` | `nat/cli/register_workflow.py:557` |
| `nat.plugin_api.register_eval_callback` | `nat/cli/register_workflow.py:815` |
| `nat.plugin_api.register_evaluator` | `nat/cli/register_workflow.py:504` |
| `nat.plugin_api.register_function` | `nat/cli/register_workflow.py:172` |
| `nat.plugin_api.register_telemetry_exporter` | `nat/cli/register_workflow.py:97` |
| `nat.plugins.eval.data_models.evaluator_io.EvalOutputItem` | `nat/plugins/eval/data_models/evaluator_io.py:27` |
| `nat.plugins.eval.dataset_handler.dataset_handler.DatasetHandler` | `nat/plugins/eval/dataset_handler/dataset_handler.py:34` |
| `nat.plugins.eval.evaluator.base_evaluator.BaseEvaluator` | `nat/plugins/eval/evaluator/base_evaluator.py:54` |
| `FunctionInfo.from_fn` | `nat/builder/function_info.py:554` |
| `Context.metadata` | `nat/builder/context.py:161` |
| `Context.workflow_run_id` | `nat/builder/context.py:219` |
| `RequestAttributes.to_dict` | `nat/runtime/user_metadata.py:139` |
| `BaseEvaluator.evaluate` | `nat/plugins/eval/evaluator/base_evaluator.py:76` |
| `BaseExporter.start` | `nat/observability/exporter/base_exporter.py:316` |
| `BaseExporter.stop` | `nat/observability/exporter/base_exporter.py:380` |
| `BaseExporter.create_isolated_instance` | `nat/observability/exporter/base_exporter.py:416` |
| `IsolatedAttribute.__get__` | `nat/observability/exporter/base_exporter.py:85` |
| `Context.scope` | `nat/builder/context.py:415` |
| `ContextState.get` | `nat/builder/context.py:137` |
| `DatasetHandler.get_eval_input_from_df` | `nat/plugins/eval/dataset_handler/dataset_handler.py:92` |
| `Subject.on_next` | `nat/utils/reactive/subject.py:69` |

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

- `nat/builder/context.py`: `acd7f048dd94a980328ee07f4ccf6473f0e1d0548554bea449fdddf75015f988`
- `nat/builder/dataset_loader.py`: `21c3e244a43b645ccb932580445bf0106e1d84357a405aabc11e889d43fc696e`
- `nat/builder/evaluator.py`: `0764e3049aa6e682dda9bc88be8685d7dfb031856939ccbcaf5c4ccbba4798c6`
- `nat/builder/function_info.py`: `76950379c2ce9d8e971c07d472c5598f6983f43d391b5490874ff6585e3c107a`
- `nat/cli/register_workflow.py`: `d6b3238a7049786335a830f8fa46081445cb7ea485c624acedbbb147467fe317`
- `nat/data_models/common.py`: `9dce6872b19528c5894de65c4d79cb2387f4956f8450f728a0aeff79fd702207`
- `nat/data_models/dataset_handler.py`: `ac86fb67f1590eaf807fc0a0fc26dd510e1519630d0f3f41f50d5a8fad956d0a`
- `nat/data_models/evaluate_config.py`: `6ab26afd52afe546a3657bc080ef2c71b010d77d41f30bbb8d92c3150fc09a10`
- `nat/data_models/evaluator.py`: `028d740c8badad6b5c6645af0a92f7477108f315d2665da0dd1d23fd46f829d3`
- `nat/data_models/function.py`: `39ec250b3d79cd4d2329d283e85895631904e7966ff3b177e6344849beee8328`
- `nat/data_models/intermediate_step.py`: `6da6fa44a96add5baacfc30fda35b1ffaabfb32e2579d6350168cff0417328c4`
- `nat/data_models/invocation_node.py`: `3891c4b3188c3163a98ecd7f68a021bc942ac2def0ee53b3e4bc460a3f984bf2`
- `nat/data_models/telemetry_exporter.py`: `768740f17d038a5279e83e25499946c5cc83a537d8a7f90f1cd65786ba17d4ca`
- `nat/data_models/token_usage.py`: `b5fec3bed9a7866f6b5e0decb0749db3a20c67e4b8f0bf477f057e59f3277b04`
- `nat/observability/exporter/base_exporter.py`: `9d0ff8cc32630612cafc49f82579ca608b3fc3c2ba677780553205617370af63`
- `nat/observability/register.py`: `52fb2f14626fb5048ec88c671eee15ca1f690b9e2cd5bfc10175bfeede31cc50`
- `nat/plugin_api/__init__.py`: `8d13033958546a3391f71f7a94ac6728d038683e2318791e8214dc4c6856ce98`
- `nat/plugins/eval/cli/evaluate.py`: `2d262ce3bd500cddaa9e1e18fb6b33e32cb2a9dc4fa31c17c0152eac88a92146`
- `nat/plugins/eval/data_models/evaluator_io.py`: `a0199d6b62e68512740b5356a58207d71e4e6942b60444bb7db8348898a87611`
- `nat/plugins/eval/dataset_handler/dataset_handler.py`: `12d1210fe585d98479d0a74e0642b1982d9c36965cda8e568fe9676641f84b3e`
- `nat/plugins/eval/eval_callbacks.py`: `1fbca4d6bd6ca71afcd3edd53445d03b58d76f269a4d94460202f1ba4e0d4af1`
- `nat/plugins/eval/evaluator/base_evaluator.py`: `89a94137ca8f4c24f83ce01ac3afd1be5dd629d3901ef3c0414cf943b099b2fc`
- `nat/plugins/eval/runtime/evaluate.py`: `63038ca5f4f2e7dacfeeddb6f28afece3367d0bfa9a38b2d3fbfd645bbe319d4`
- `nat/plugins/eval/utils/intermediate_step_adapter.py`: `ad96ef01f9f605a380037d98b60b67477c0c75aa546667c50fb9fedcd35e8c31`
- `nat/runtime/loader.py`: `fc97b08333665b4d182fd7192d3726e19161a9e180693e3d7bed9ac7d6d04d7f`
- `nat/runtime/user_metadata.py`: `1a671948bf75579a8fb73a3bd1ff5ce9d7470ff4dfc6be00c3dbfb459ea310ab`
- `nat/utils/reactive/subject.py`: `8c44348d272f7a49fb0e4a765155b3ffc5db2fd359881215640607587da96a24`
