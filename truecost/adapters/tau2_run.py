"""Run τ²-bench through the TrueCost gateway, with every model call tied to its task and role.

Runs inside τ²'s own environment (it imports nothing from truecost). Works with τ² (0.2) and τ³ (1.0, the same
package with a new runner). It adds headers to each call:
X-TrueCost-Run, X-TrueCost-Task (the τ² task id, plus "#t<trial>" for repeat trials) and
X-TrueCost-Role: "agent" for the agent under test, "simulator" for the simulated customer, "judge" for
the natural-language-assertion grader. The grader defaults to GPT-4.1 in τ²; here it is served by the
gateway's judge tier instead (a local model), so no external key is needed. τ² itself is not modified.

Usage:
  python tau2_run.py --gateway http://127.0.0.1:30020/v1 --run retail-base-s300 -- \
      run --domain retail --agent-llm openai/strong --user-llm openai/sim ...
"""

from __future__ import annotations

import argparse
import re
import sys
import threading


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gateway", required=True, help="gateway base URL, e.g. http://127.0.0.1:30020/v1")
    ap.add_argument("--run", required=True, help="run id written to the ledger")
    ap.add_argument("--judge-model", default="openai/judge")
    args, rest = ap.parse_known_args()
    if rest and rest[0] == "--":
        rest = rest[1:]

    tls = threading.local()

    import tau2.runner.batch as batch

    if hasattr(batch, "run_unit"):  # τ² 0.2: one call per (task, trial) in a worker thread
        orig_run_unit = batch.run_unit

        def run_unit(ctx, task, trial, seed, *a, **k):
            tls.task = task.id if not trial else f"{task.id}#t{trial}"
            try:
                return orig_run_unit(ctx, task, trial, seed, *a, **k)
            finally:
                tls.task = None

        batch.run_unit = run_unit
    else:  # τ³ 1.0: run_single_task(config, task, ...) per task in a worker thread (single trial: the task id)
        orig_single = batch.run_single_task

        def run_single_task(config, task, *a, **k):
            tls.task = task.id
            try:
                return orig_single(config, task, *a, **k)
            finally:
                tls.task = None

        batch.run_single_task = run_single_task

    def tagged(fn, role):
        def wrapper(*a, **k):
            headers = dict(k.pop("extra_headers", None) or {})
            headers["X-TrueCost-Run"] = args.run
            headers["X-TrueCost-Role"] = role
            task = getattr(tls, "task", None)
            if task:
                headers["X-TrueCost-Task"] = str(task)
            k["extra_headers"] = headers
            return fn(*a, **k)
        return wrapper

    import tau2.agent.llm_agent as llm_agent
    import tau2.evaluator.evaluator_nl_assertions as nl
    import tau2.user.user_simulator as user_simulator

    llm_agent.generate = tagged(llm_agent.generate, "agent")
    user_simulator.generate = tagged(user_simulator.generate, "simulator")

    nl.DEFAULT_LLM_NL_ASSERTIONS = args.judge_model
    nl.DEFAULT_LLM_NL_ASSERTIONS_ARGS = {
        "temperature": 0.0, "api_base": args.gateway, "api_key": "local",
        "extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    judge_generate = tagged(nl.generate, "judge")

    def judge(*a, **k):  # strip markdown code fences so the judge's JSON parses
        m = judge_generate(*a, **k)
        if m.content:
            m.content = re.sub(r"^```(?:json)?\s*|\s*```$", "", m.content.strip())
        return m

    nl.generate = judge

    from tau2.cli import main as tau2_main

    sys.argv = ["tau2", *rest]
    return tau2_main()


if __name__ == "__main__":
    sys.exit(main())
