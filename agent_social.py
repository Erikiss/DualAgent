import json
import os
import time
from pathlib import Path


def read_worker_report() -> dict:
    report_path = Path("worker-report/worker_report.json")
    if not report_path.exists():
        return {"error": "worker_report.json not found", "ts": int(time.time())}
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"failed to parse worker_report.json: {e}", "ts": int(time.time())}


def build_social_post(worker: dict) -> str:
    telemetry = worker.get("telemetry", {})
    result = worker.get("result", "")

    return f"""# Mersenne Worker Update

**Timestamp:** {worker.get("ts")}

## Telemetry
- Clicks: {telemetry.get("clicks")}
- Inputs: {telemetry.get("types")}
- Waits: {telemetry.get("waits")}
- Scrolls: {telemetry.get("scrolls")}
- Navigates: {telemetry.get("navigates")}
- Errors: {telemetry.get("errors")}

## Result (short)
{result[:1200] if isinstance(result, str) else str(result)[:1200]}

## Question to other agents
- We see successful login but sometimes Steel/CDP disconnects after clicking "Today's Posts".
- Any robust patterns to avoid CDP session corruption / focus detach in browser-use + remote CDP?
- Should we reduce tab creation / prevent opening in new tab?
"""


def build_advice(worker: dict) -> str:
    telemetry = worker.get("telemetry", {})
    extra = worker.get("extra", {})
    result = worker.get("result", "")

    # Baseline rules that help with your observed failure mode
    lines = []
    lines.append("# Advice for next Worker run")
    lines.append("")
    lines.append("## Hard Rules (MANDATORY)")
    lines.append("- If you see 'browser not connected'/'websocket closed'/'session corrupted': STOP and EXIT (do not retry clicks).")
    lines.append("- Never click the same element repeatedly; max 1 click per page for the same target.")
    lines.append("- After login: prefer extraction (read DOM) over navigation loops.")
    lines.append("- If a click opens a new tab or detaches focus: stop further actions and return an error summary.")
    lines.append("")
    lines.append("## Next Attempt Strategy")
    lines.append("- After successful login, wait 2s, then locate a single stable content area.")
    lines.append("- If 'Today's Posts' exists: click once; if navigation turns into search results, extract titles+dates from DOM.")
    lines.append("- Avoid anything that triggers a new tab/window; prefer same-tab navigation.")
    lines.append("")
    lines.append("## Signals from last run")
    lines.append(f"- Telemetry: {telemetry}")
    lines.append(f"- Extra: {extra}")
    lines.append("")
    lines.append("## Last result excerpt")
    if isinstance(result, str):
        lines.append(result[:1500])
    else:
        lines.append(str(result)[:1500])

    return "\n".join(lines)


def main():
    worker = read_worker_report()

    # Outputs
    Path("advice.md").write_text(build_advice(worker), encoding="utf-8")
    Path("social_post.md").write_text(build_social_post(worker), encoding="utf-8")

    print("✅ SOCIAL artifacts created: advice.md, social_post.md")


if __name__ == "__main__":
    main()
