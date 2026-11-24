"""Does the model answer accurately?

This is the live eval: it drives the real Flask endpoint and the real Gemini
`Chatter`, with a frozen headline corpus injected (so no NewsAPI call and a
known ground truth). It scores each answer for expected facts, refusal
behaviour, tool use, citation and unsupported claims, prints a scorecard, and
fails when the aggregate drops below the gates.

    py -m pytest -m eval -s

Costs one Gemini request per prompt (10 by default). Gates can be tuned with
EVAL_GATE_FACT / EVAL_GATE_REFUSAL / EVAL_GATE_TOOLS / EVAL_GATE_CITATIONS /
EVAL_GATE_UNSUPPORTED; set EVAL_REPORT to a path to also write JSON results.
"""

from __future__ import annotations

import copy
import json
import os
import statistics
import time

import pytest

import app as app_module
from accuracy_corpus import ARTICLES, PROMPTS
from accuracy_grader import grade

# Gemini occasionally returns 503 UNAVAILABLE / 429 under load; that is an
# infrastructure problem, not an accuracy result, so retry a little first.
ATTEMPTS = 3
RETRYABLE_MARKERS = ("UNAVAILABLE", "try again later", "RESOURCE_EXHAUSTED", "quota", "503")

GATES = {
    "fact": float(os.getenv("EVAL_GATE_FACT", "0.8")),
    "refusal": float(os.getenv("EVAL_GATE_REFUSAL", "1.0")),
    "tools": float(os.getenv("EVAL_GATE_TOOLS", "0.85")),
    "citations": float(os.getenv("EVAL_GATE_CITATIONS", "0.7")),
    "unsupported": float(os.getenv("EVAL_GATE_UNSUPPORTED", "0.34")),
}


def _ask(client, prompt: dict) -> tuple[dict, list[dict], float]:
    """Creates a chat, injects the frozen articles, asks one question."""
    created = client.post("/api/chats", json={})
    assert created.status_code == 201, created.get_json()
    chat_id = created.get_json()["id"]

    articles = ARTICLES.get(prompt["topic"], []) if prompt.get("topic") else []
    if articles:
        session = app_module.SESSIONS[chat_id]
        session._articles = copy.deepcopy(articles)
        session.query = prompt["topic"]

    payload: dict = {"question": prompt["question"]}
    if os.getenv("EVAL_MODEL"):
        payload["model"] = os.environ["EVAL_MODEL"]

    response = None
    elapsed = 0.0
    for attempt in range(ATTEMPTS):
        started = time.perf_counter()
        response = client.post(f"/api/chats/{chat_id}/messages", json=payload)
        elapsed = time.perf_counter() - started
        if response.status_code == 200:
            break
        body = response.get_json() or {}
        detail = json.dumps(body)
        retryable = response.status_code in {429, 500, 502, 503, 504} and any(
            marker in detail for marker in RETRYABLE_MARKERS
        )
        if not retryable or attempt == ATTEMPTS - 1:
            break
        time.sleep(min(float(body.get("retry_after") or 5.0), 30.0))

    if response.status_code != 200:
        body = response.get_json() or {}
        detail = json.dumps(body)
        if body.get("code") == "rate_limited" or any(
            marker in detail for marker in ("UNAVAILABLE", "try again later")
        ):
            pytest.skip(
                "Gemini quota/unavailability blocked the eval - this is not an "
                f"accuracy result: {body.get('detail') or detail}"
            )
        assert response.status_code == 200, (response.status_code, body)
    return response.get_json(), articles, elapsed


@pytest.mark.eval
def test_model_answers_are_accurate(tmp_path, monkeypatch):
    if not os.getenv("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY is not set (add it to .env) - accuracy eval needs it")

    monkeypatch.setattr(app_module, "HISTORY_ROOT", str(tmp_path / "web"))
    monkeypatch.setattr(app_module, "ARCHIVE_DIR", str(tmp_path / "archives"))
    app_module.SESSIONS.clear()
    app_module.app.config["TESTING"] = True

    results: list[dict] = []
    with app_module.app.test_client() as client:
        for index, prompt in enumerate(PROMPTS):
            if index:
                time.sleep(float(os.getenv("EVAL_DELAY", "1.5")))
            body, articles, elapsed = _ask(client, prompt)
            result = grade(prompt, body["answer"], body.get("tools"), articles)
            result["latency"] = elapsed
            results.append(result)

    lines = ["", f"model: {app_module.DEFAULT_MODEL}", ""]
    for result in results:
        status = "PASS" if result["passed"] else "FAIL"
        unsupported = (
            f"{result['unsupported']:.2f}" if result["unsupported"] is not None else "-"
        )
        lines.append(
            f"{result['id']:<22} {result['category']:<10} {status:<4} "
            f"cite={result['cited']} unsupported={unsupported} "
            f"tools={','.join(result['tools']) or '-'}"
        )
        if not result["passed"]:
            lines.append(f"    checks: {result['checks']}")
            lines.append(f"    answer: {result['answer'][:200]!r}")

    facts = [result for result in results if "expect" in result["checks"]]
    fact_rate = sum(bool(r["checks"]["expect"]) for r in facts) / len(facts)

    refusals = [result for result in results if "refusal" in result["checks"]]
    refusal_rate = sum(bool(r["checks"]["refusal"]) for r in refusals) / len(refusals)

    tool_checks = [
        result
        for result in results
        if "must_call" in result["checks"] or "no_tools" in result["checks"]
    ]
    tool_rate = sum(
        all(result["checks"].get(name, True) for name in ("must_call", "no_tools"))
        for result in tool_checks
    ) / len(tool_checks)

    citation_pool = [
        result
        for result in results
        if result["category"] in {"lookup", "detail", "cross", "ranking", "summary"}
    ]
    citation_rate = sum(1 for r in citation_pool if r["cited"] >= 1) / len(citation_pool)

    unsupported_pool = [r["unsupported"] for r in results if r["unsupported"] is not None]
    unsupported_mean = sum(unsupported_pool) / len(unsupported_pool)
    invented = [number for result in results for number in result["invented"]]

    latencies = sorted(result["latency"] for result in results)
    lines += [
        "",
        f"fact recall      {fact_rate:.2f}  (gate {GATES['fact']:.2f}, n={len(facts)})",
        f"refusal accuracy {refusal_rate:.2f}  (gate {GATES['refusal']:.2f}, n={len(refusals)})",
        f"tool use         {tool_rate:.2f}  (gate {GATES['tools']:.2f}, n={len(tool_checks)})",
        f"cites a headline {citation_rate:.2f}  (gate {GATES['citations']:.2f}, n={len(citation_pool)})",
        f"unsupported      {unsupported_mean:.2f}  (gate <= {GATES['unsupported']:.2f}, lower is better)",
        f"invented numbers {invented or 'none'}",
        f"latency p50      {statistics.median(latencies):.2f}s  (max {max(latencies):.2f}s)",
        f"passed           {sum(1 for r in results if r['passed'])}/{len(results)}",
    ]
    scorecard = "\n".join(lines)
    print(scorecard)

    report_path = os.getenv("EVAL_REPORT")
    if report_path:
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(
                {"gates": GATES, "summary": {"fact": fact_rate, "refusal": refusal_rate,
                                             "tools": tool_rate, "citations": citation_rate,
                                             "unsupported": unsupported_mean},
                 "results": results},
                handle,
                indent=2,
            )

    problems = []
    if fact_rate < GATES["fact"]:
        problems.append(f"fact recall {fact_rate:.2f} < {GATES['fact']:.2f}")
    if refusal_rate < GATES["refusal"]:
        problems.append(f"refusal accuracy {refusal_rate:.2f} < {GATES['refusal']:.2f}")
    if tool_rate < GATES["tools"]:
        problems.append(f"tool use {tool_rate:.2f} < {GATES['tools']:.2f}")
    if citation_rate < GATES["citations"]:
        problems.append(f"citation rate {citation_rate:.2f} < {GATES['citations']:.2f}")
    if unsupported_mean > GATES["unsupported"]:
        problems.append(
            f"unsupported claim rate {unsupported_mean:.2f} > {GATES['unsupported']:.2f}"
        )
    if invented:
        problems.append(f"invented numbers: {invented}")

    assert not problems, "accuracy gates failed:\n" + "\n".join(problems) + "\n" + scorecard
