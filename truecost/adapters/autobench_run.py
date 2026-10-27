"""Run Zapier's AutomationBench through the TrueCost gateway, with every model call tied to its task.

Runs inside AutomationBench's own environment (imports nothing from truecost). Run-level headers
(X-TrueCost-Run, X-TrueCost-Role) go through the benchmark's own --headers flag; this wrapper adds
X-TrueCost-Task per rollout: the task name is read from the rollout state and passed as an extra header
on that rollout's requests. AutomationBench itself is not modified.

Usage (from the AutomationBench checkout):
  uv run python autobench_run.py --model strong --base-url http://127.0.0.1:30020/v1 --api-key local \
      --domains finance --headers X-TrueCost-Run=fin-base --export-json out.json
"""

from __future__ import annotations

import sys


def install() -> None:
    """Tag every model call with its task: verifiers posts the request itself and forwards the
    `extra_headers` keyword, so the task name (from the rollout state) is added there."""
    from automationbench import clients
    from automationbench.scripts import eval as ab_eval

    orig_get = clients.RetryingOpenAIChatCompletionsClient.get_native_response

    async def get_native_response(self, *args, **kwargs):
        state = kwargs.get("state")
        name = ab_eval._task_name(state) if isinstance(state, dict) else None
        if name:
            kwargs["extra_headers"] = {**(kwargs.get("extra_headers") or {}), "X-TrueCost-Task": name}
        return await orig_get(self, *args, **kwargs)

    clients.RetryingOpenAIChatCompletionsClient.get_native_response = get_native_response


def main() -> int:
    install()
    from automationbench.scripts import eval as ab_eval

    sys.argv = ["auto-bench", *sys.argv[1:]]
    return ab_eval.main() or 0


if __name__ == "__main__":
    sys.exit(main())
