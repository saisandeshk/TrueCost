import json

from truecost.collectors import claude_code


def _line(**d):
    return json.dumps({"cwd": "/proj", "sessionId": "s1", "timestamp": "2026-10-21T05:00:00Z", **d})


def test_prompts_vs_harness_turns(tmp_path):
    """Human and SDK (claude -p) prompts start tasks; compaction summaries and task notifications do not."""
    rows = [
        _line(type="user", turnOrigin="human", message={"role": "user", "content": "fix the bug"}),
        _line(type="assistant", message={"id": "m1", "model": "claude-opus-5-5", "content": [],
                                         "usage": {"input_tokens": 10, "output_tokens": 5}}),
        _line(type="user", isCompactSummary=True, message={"role": "user", "content": "This session is being continued"}),
        _line(type="user", turnOrigin="task_notification", message={"role": "user", "content": "background run finished"}),
        _line(type="user", turnOrigin="sdk", message={"role": "user", "content": "solve the exercise"}),
        _line(type="user", message={"role": "user", "content": "a prompt from an older log"}),
    ]
    p = tmp_path / "s1.jsonl"
    p.write_text("\n".join(rows) + "\n")
    calls, tasks = claude_code.parse_transcript(p, ["/proj"])
    assert [t["title"] for t in tasks] == ["fix the bug", "solve the exercise", "a prompt from an older log"]
    assert len(calls) == 1
