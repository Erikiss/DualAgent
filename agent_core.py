import os
import asyncio
import json
import smtplib
import time
from pathlib import Path
from email.message import EmailMessage

# --- Auto-Install für GitHub Actions ---
try:
    from langchain_groq import ChatGroq
except ImportError:
    import sys, subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "langchain-groq"])
    from langchain_groq import ChatGroq

from browser_use import Agent, Browser


# -----------------------------
# Adapter: browser-use erwartet provider/model Attribute
# -----------------------------
class GroqAdapter:
    def __init__(self, llm):
        self.llm = llm
        self.provider = "openai"
        self.model = "llama-3.3-70b-versatile"
        self.model_name = self.model

    async def ainvoke(self, *args, **kwargs):
        return await self.llm.ainvoke(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.llm, name)


# -----------------------------
# Advice loader
# -----------------------------
def load_advice() -> str:
    advice_path = os.getenv("ADVICE_FILE", "").strip()
    if advice_path and os.path.exists(advice_path):
        try:
            with open(advice_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception:
            return ""
    return ""


# -----------------------------
# Telemetry from history
# -----------------------------
def analyze_history(history):
    stats = {"clicks": 0, "types": 0, "scrolls": 0, "waits": 0, "navigates": 0, "errors": 0}

    for step in getattr(history, "history", []):
        if getattr(step, "error", None):
            stats["errors"] += 1

        # Heuristik: model_output als string nach Keywords
        try:
            content = str(getattr(step, "model_output", "")).lower()
            if "click" in content:
                stats["clicks"] += 1
            if "type" in content or '"input"' in content or "fill" in content:
                stats["types"] += 1
            if "scroll" in content:
                stats["scrolls"] += 1
            if "wait" in content:
                stats["waits"] += 1
            if "navigate" in content or "goto" in content:
                stats["navigates"] += 1
        except Exception:
            pass

    report = (
        f"📊 TELEMETRIE\n"
        f"- Navigates: {stats['navigates']}\n"
        f"- Waits: {stats['waits']}\n"
        f"- Scrolls: {stats['scrolls']}\n"
        f"- Clicks: {stats['clicks']}\n"
        f"- Inputs: {stats['types']}\n"
        f"- Errors: {stats['errors']}\n"
    )
    return stats, report


# -----------------------------
# Write artifacts for Social job
# -----------------------------
def write_worker_report(result_text: str, tele_report: str, stats: dict, extra: dict | None = None):
    out_dir = Path("worker-report")
    out_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "ts": int(time.time()),
        "result": result_text,
        "telemetry_text": tele_report,
        "telemetry": stats,
        "extra": extra or {},
    }

    (out_dir / "worker_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / "result.txt").write_text(str(result_text), encoding="utf-8")
    (out_dir / "telemetry.txt").write_text(str(tele_report), encoding="utf-8")


# -----------------------------
# Email
# -----------------------------
def send_mail(result_text: str, tele_report: str, stats: dict):
    # Status-Emoji
    if stats.get("types", 0) >= 2:
        icon = "🚀"
    elif stats.get("clicks", 0) > 0:
        icon = "✅"
    else:
        icon = "⚠️"

    subject = f"{icon} CORE | {stats.get('clicks',0)} Clicks, {stats.get('types',0)} Inputs, {stats.get('errors',0)} Errors"

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("EMAIL_USER")
    msg["To"] = os.getenv("EMAIL_RECEIVER")
    msg.set_content(f"{tele_report}\n\n📝 RESULT:\n{result_text}")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_APP_PASSWORD"))
        smtp.send_message(msg)


# -----------------------------
# Core run
# -----------------------------
async def run_core():
    advice = load_advice()

    real_llm = ChatGroq(
        model="llama-3.3-70b-versatile",
        api_key=os.getenv("GROQ_API_KEY"),
        temperature=0.35,
    )
    llm = GroqAdapter(real_llm)

    browser = Browser(cdp_url=f"wss://connect.steel.dev?apiKey={os.getenv('STEEL_API_KEY')}")

    base_task = f"""
ROLE: Robuster Web-Automation-Agent. Du MUSST handeln (klicken/tippen).
WICHTIG:
- Vision ist AUS. Nutze nur DOM/Text/Attribute.
- Klicke denselben Link maximal 1x pro Seite (keine Loops).
- Nach jedem Click: WAIT 2 Sekunden.
- Bei Browser-Fehlern ("browser not connected", "websocket closed", "session corrupted"): STOP und gib den Fehler zurück.

ZIEL:
1) Login auf {os.getenv('TARGET_URL')}
2) Danach: finde Berichte/Posts der letzten 4 Wochen (Titel/Links als Liste)
3) Wenn keine: "Keine neuen Daten gefunden."

LOGIN-STRATEGIE (A -> B -> C):
A) Suche nach Text: "Log in", "Login", "Sign in", "Anmelden" und klicke.
B) Wenn nicht: suche Links mit href enthält "login" oder "signin" und klicke.
C) Wenn nicht: suche Header-Icons/Menu mit aria-label/title/class enthält "user/account/profile/login", öffne und klicke Login.

FORMULAR:
- Username/Email Feld finden (type=text/email oder name/id enthält user/email/login)
- Password Feld finden (type=password oder name/id enthält pass)
- Type Username "{os.getenv('TARGET_USER')}"
- Type Password "{os.getenv('TARGET_PW')}"
- Click Submit/Login (type=submit oder Text)
- WAIT 5 Sekunden

ERFOLGSPRÜFUNG:
- Suche nach "Logout/Sign out/Abmelden" oder User-Profil.
- Wenn nicht gefunden: gib "Login fehlgeschlagen" + beobachtete Hinweise zurück.

DANN:
- Navigiere NICHT wild. Extrahiere Inhalte. Wenn du "Today's Posts" findest: einmal klicken, dann extrahieren.
"""

    task = f"SYSTEM POLICY (FOLLOW STRICTLY):\n{advice}\n\n{base_task}" if advice else base_task

    agent = Agent(task=task, llm=llm, browser=browser, use_vision=False)

    timeout_sec = int(os.getenv("WORKER_TIMEOUT_SEC", "240"))
    history = await asyncio.wait_for(agent.run(), timeout=timeout_sec)

    stats, tele_report = analyze_history(history)
    result = history.final_result() or "Kein Ergebnistext."

    return result, tele_report, stats


async def main():
    try:
        result, tele_report, stats = await run_core()

        write_worker_report(str(result), str(tele_report), stats)
        send_mail(str(result), tele_report, stats)

        print("✅ CORE done")

    except asyncio.TimeoutError:
        stats = {"clicks": 0, "types": 0, "scrolls": 0, "waits": 0, "navigates": 0, "errors": 1}
        msg = "ABBRUCH: Timeout (vermutlich Loop oder Browser disconnected)."
        write_worker_report(msg, "Status: Timeout", stats, extra={"reason": "timeout"})
        send_mail(msg, "Status: Timeout", stats)
        raise

    except Exception as e:
        emsg = str(e).lower()
        markers = ["browser not connected", "websocket closed", "session corrupted", "target_id=none", "cdp"]
        extra = {"reason": "steel_disconnect"} if any(m in emsg for m in markers) else {"reason": "crash"}

        stats = {"clicks": 0, "types": 0, "scrolls": 0, "waits": 0, "navigates": 0, "errors": 1}
        write_worker_report(f"System-Crash: {e}", "Status: Crash", stats, extra=extra)
        send_mail(f"System-Crash: {e}", "Status: Crash", stats)
        raise


if __name__ == "__main__":
    asyncio.run(main())
