import os
import re
from datetime import datetime

# Optional: Groq LLM (langchain-groq)
USE_GROQ = bool(os.getenv("GROQ_API_KEY"))

def read_file(path: str, max_chars: int = 120_000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            data = f.read()
        return data[:max_chars]
    except FileNotFoundError:
        return ""

def extract_signals(worker_log: str) -> dict:
    """
    Heuristik aus browser-use / Steel Logs:
    - login success Hinweise
    - browser disconnected / websocket closed
    - click/type counts (ungefähr)
    """
    signals = {
        "clicks": 0,
        "types": 0,
        "waits": 0,
        "navigates": 0,
        "errors": 0,
        "login_success": False,
        "disconnect": False,
        "key_errors": [],
    }

    if not worker_log:
        signals["key_errors"].append("Kein worker log gefunden (worker-report/run.log fehlt).")
        return signals

    # grobe Zählungen
    signals["clicks"] = len(re.findall(r"\bclick\b", worker_log, flags=re.IGNORECASE))
    signals["types"] = len(re.findall(r"\btype\b", worker_log, flags=re.IGNORECASE))
    signals["waits"] = len(re.findall(r"\bwait\b", worker_log, flags=re.IGNORECASE))
    signals["navigates"] = len(re.findall(r"\bnavigate\b", worker_log, flags=re.IGNORECASE))

    # Fehler-Indikatoren
    err_hits = re.findall(r"\b(ERROR|Exception|Traceback)\b", worker_log)
    signals["errors"] = len(err_hits)

    # Login Erfolg (typische Sätze aus euren Runs)
    if re.search(r"login was successful|logged in successfully|ich habe mich eingeloggt|logout", worker_log, re.IGNORECASE):
        signals["login_success"] = True

    # Disconnect / Steel crash Muster
    if re.search(r"browser not connected|websocket connection closed|Browser Disconnected|session is corrupted|target_id=None", worker_log, re.IGNORECASE):
        signals["disconnect"] = True
        signals["key_errors"].append("Browser/Steel Session instabil (WebSocket closed / browser not connected / session corrupted).")

    # häufige konkrete Meldungen sammeln (kurz)
    for pat in [
        r"Cannot navigate\s*-\s*browser not connected",
        r"websocket connection closed",
        r"session is corrupted.*target_id=None",
        r"Cannot execute click.*session is corrupted",
        r"CDP .* failed",
    ]:
        m = re.search(pat, worker_log, re.IGNORECASE)
        if m:
            signals["key_errors"].append(m.group(0))

    # dedupe
    signals["key_errors"] = list(dict.fromkeys(signals["key_errors"]))[:6]
    return signals

def llm_summarize(worker_log: str, skill_md: str, signals: dict) -> tuple[str, str]:
    """
    Returns: (advice_md, social_post_md)
    Falls Groq nicht verfügbar ist, fallback auf Template.
    """
    # Fallback ohne LLM
    if not USE_GROQ:
        advice = build_advice_no_llm(worker_log, signals)
        post = build_post_no_llm(worker_log, signals)
        return advice, post

    try:
        from langchain_groq import ChatGroq
        from langchain_core.messages import SystemMessage, HumanMessage
    except Exception:
        advice = build_advice_no_llm(worker_log, signals)
        post = build_post_no_llm(worker_log, signals)
        return advice, post

    llm = ChatGroq(
        model="llama-3.3-70b-versatile",
        api_key=os.getenv("GROQ_API_KEY"),
        temperature=0.2,
    )

    sys = SystemMessage(content=(
        "You are a debugging assistant. Be concrete, propose minimal changes, and focus on root causes.\n"
        "Output must be in TWO markdown blocks:\n"
        "1) advice.md content\n"
        "2) social_post.md content\n"
        "No extra chatter."
    ))

    # Wir geben Skill.md nur kurz (falls lang)
    skill_short = (skill_md[:20_000] if skill_md else "")

    human = HumanMessage(content=f"""
Context:
- We run a dual-agent GitHub Actions workflow.
- Worker agent uses browser-use + Steel + Groq (text-only).
- Social agent should produce: advice.md (internal next steps) and social_post.md (public post draft).
- The Moltbook "skill.md" is included for how to post and how to structure.

Signals (heuristics):
{signals}

Worker log (truncated):
{worker_log[:60_000]}

Moltbook skill.md (truncated):
{skill_short}

Task:
- In advice.md: identify the most likely failure mode(s), and propose 3-5 minimal, actionable fixes.
- In social_post.md: write a short post (title line + body) describing what happened and asking for help, including the key error strings.
""")

    resp = llm.invoke([sys, human]).content

    # Erwartung: zwei md-Blöcke. Wenn nicht, fallback.
    parts = re.split(r"^```(?:md|markdown)?\s*$", resp, flags=re.MULTILINE)
    # einfacher: falls LLM ohne fences schreibt, fallback zu split markers
    if "advice.md" in resp.lower() and "social_post.md" in resp.lower():
        # rudimentär extrahieren
        advice = resp
        post = resp
        return advice, post

    # Fallback: einfach alles als advice, und post template
    advice = "## advice.md\n\n" + resp.strip()
    post = build_post_no_llm(worker_log, signals)
    return advice, post

def build_advice_no_llm(worker_log: str, signals: dict) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Advice (auto) — {now}",
        "",
        "## Kurzdiagnose",
        f"- Login erkannt: **{signals['login_success']}**",
        f"- Session/Disconnect erkannt: **{signals['disconnect']}**",
        f"- Aktionen (heuristisch): clicks≈{signals['clicks']}, types≈{signals['types']}, navigates≈{signals['navigates']}, waits≈{signals['waits']}",
        "",
        "## Wahrscheinlichste Ursache",
        "- **Steel/Browser Session wird nach erfolgreichem Login instabil** (WebSocket/Target detach / session corrupted).",
        "",
        "## Minimale Fixes (3–5)",
        "1. **Nach Login: Tab/Target stabilisieren** — nach dem Submit einmal `wait 2–5s` und dann *keinen zweiten sofortigen Click* auf denselben Link; erst DOM neu lesen.",
        "2. **Retry-Guard**: Wenn `browser not connected` oder `session corrupted` auftaucht → Agent sauber beenden (statt Loop) und nächsten Run starten.",
        "3. **Einmalige Navigation**: Verhindere doppelte `navigate`-Calls hintereinander (führt oft zu Target detach).",
        "4. **Keep-alive / kürzere Runs**: Worker Timeout eher 180–240s, und nach Erfolg früh abbrechen.",
        "5. Falls möglich: **Steel Session pro Phase** (Login-Phase, dann neue Session fürs Scraping) — reduziert Corruption nach Auth.",
        "",
        "## Key Errors",
    ]
    if signals["key_errors"]:
        lines += [f"- {e}" for e in signals["key_errors"]]
    else:
        lines += ["- (keine spezifischen Error-Strings erkannt)"]
    lines += [
        "",
        "## Log-Auszug (letzte ~80 Zeilen)",
        "```",
    ]
    tail = "\n".join(worker_log.splitlines()[-80:]) if worker_log else "(no log)"
    lines.append(tail)
    lines.append("```")
    return "\n".join(lines)

def build_post_no_llm(worker_log: str, signals: dict) -> str:
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    key_err = signals["key_errors"][:3] if signals["key_errors"] else []
    tail = "\n".join(worker_log.splitlines()[-40:]) if worker_log else "(no log)"
    return "\n".join([
        f"# Mersenne Worker: Login ok, then Steel session corruption ({now})",
        "",
        "We run a dual-agent GitHub Actions setup:",
        "- Worker: browser-use + Steel (remote browser) + Groq (text-only) to login and scan latest posts",
        "- Social: generates advice + post drafts",
        "",
        f"**Observed:** login seems successful = **{signals['login_success']}**, then the browser session becomes unstable = **{signals['disconnect']}**.",
        "",
        "Key errors we see:",
        *(f"- `{e}`" for e in key_err),
        "",
        "Question:",
        "- Best practices to avoid **target detach / session corrupted** after login in Steel/browser-use?",
        "- Should we re-create the browser/session after login (two-phase approach)?",
        "",
        "Log tail:",
        "```",
        tail,
        "```",
    ])

def main():
    worker_log_path = os.getenv("WORKER_LOG", "worker-report/run.log")
    skill_path = os.getenv("SKILL_MD", "moltbook_skill.md")

    worker_log = read_file(worker_log_path)
    skill_md = read_file(skill_path, max_chars=50_000)

    signals = extract_signals(worker_log)
    advice_md, social_post_md = llm_summarize(worker_log, skill_md, signals)

    # Wenn LLM "beides in einem" zurückgegeben hat, trennen wir notfalls simpel:
    # Wir schreiben trotzdem zwei Files.
    with open("advice.md", "w", encoding="utf-8") as f:
        f.write(advice_md.strip() + "\n")

    with open("social_post.md", "w", encoding="utf-8") as f:
        f.write(social_post_md.strip() + "\n")

    print("Wrote advice.md and social_post.md")

if __name__ == "__main__":
    main()
