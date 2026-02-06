import os
import asyncio
import smtplib
import subprocess
import sys
from email.message import EmailMessage
from dataclasses import dataclass, asdict
from typing import Tuple, Optional

# --- Auto-Install (GitHub Actions freundlich) ---
try:
    from langchain_groq import ChatGroq
except ImportError:
    print("[core] Installing langchain-groq...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "langchain-groq"])
    from langchain_groq import ChatGroq

try:
    from browser_use import Agent, Browser
except ImportError:
    print("[core] Installing browser-use...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "browser-use"])
    from browser_use import Agent, Browser


# ----------------------------
# LLM Adapter (provider shim)
# ----------------------------
class GroqAdapter:
    def __init__(self, llm):
        self.llm = llm
        # browser-use erwartet oft OpenAI-like Attributes
        self.provider = "openai"
        self.model_name = getattr(llm, "model", "llama-3.3-70b-versatile")
        self.model = self.model_name

    async def ainvoke(self, *args, **kwargs):
        return await self.llm.ainvoke(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.llm, name)


# ----------------------------
# Telemetrie
# ----------------------------
@dataclass
class Telemetry:
    navigates: int = 0
    clicks: int = 0
    types: int = 0
    waits: int = 0
    scrolls: int = 0
    errors: int = 0
    login_claimed: bool = False

def analyze_history(history) -> Telemetry:
    t = Telemetry()
    # browser-use history entries variieren je nach Version – wir parsen robust per str()
    for step in getattr(history, "history", []) or []:
        try:
            content = ""
            if hasattr(step, "model_output") and step.model_output:
                content = str(step.model_output)
            elif hasattr(step, "result") and step.result:
                content = str(step.result)

            low = content.lower()

            # Actions (heuristics)
            if "navigate" in low:
                t.navigates += 1
            if '"click"' in low or "'click'" in low or "clicked" in low:
                t.clicks += 1
            if '"type"' in low or "'type'" in low or "typed" in low:
                t.types += 1
            if "wait" in low or "waited" in low:
                t.waits += 1
            if "scroll" in low:
                t.scrolls += 1

            # Login claim heuristics (aus deinen Logs typisch)
            if "logged in successfully" in low or "login was successful" in low or "eingeloggt" in low:
                t.login_claimed = True

        except Exception:
            pass

        # Fehler
        try:
            if getattr(step, "error", None):
                t.errors += 1
        except Exception:
            pass

    return t


# ----------------------------
# Mail
# ----------------------------
def send_mail(subject: str, body: str):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.getenv("EMAIL_USER")
    msg["To"] = os.getenv("EMAIL_RECEIVER")
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(os.getenv("EMAIL_USER"), os.getenv("EMAIL_APP_PASSWORD"))
        smtp.send_message(msg)


def fmt_telemetry(t: Telemetry) -> str:
    return (
        "TELEMETRIE:\n"
        f"- Navigates: {t.navigates}\n"
        f"- Clicks: {t.clicks}\n"
        f"- Types: {t.types}\n"
        f"- Waits: {t.waits}\n"
        f"- Scrolls: {t.scrolls}\n"
        f"- Errors: {t.errors}\n"
        f"- Login claimed: {t.login_claimed}\n"
    )


# ----------------------------
# Core Run (mit Fixes)
# ----------------------------
def build_task() -> str:
    url = os.getenv("TARGET_URL")
    user = os.getenv("TARGET_USER")
    pw = os.getenv("TARGET_PW")

    # Wichtig: wir sagen explizit "NAVIGATE" als erste Aktion.
    # Das reduziert die Chance auf "0 actions produced" drastisch.
    return f"""
ROLE: Du bist ein aggressiver Browser-Automations-Bot. Du MUSST Aktionen ausführen.

HARTE REGELN:
- Beginne IMMER mit: NAVIGATE zu der Ziel-URL.
- Du darfst nicht nur beobachten. Du musst klicken oder tippen.
- Vision ist AUS. Nutze nur DOM/Text.

ZIEL:
1) NAVIGATE zu {url}.
2) Warte bis geladen.
3) Finde Login über:
   - Text: "Log in", "Sign in", "Anmelden"
   - oder Link mit href enthält "/login"
   - oder Profil/User Icon (oben rechts)
4) Klicke Login.
5) Fülle User "{user}" und Passwort "{pw}".
6) Klicke Submit/Login.
7) Bestätige Login (suche "Logout" / "My Profile" / Username).
8) Danach: Finde "Today's Posts" / "Recent Posts" / "Latest Posts" und liste relevante Titel der letzten 4 Wochen.

OUTPUT:
- Liefere konkrete Aktionen (click/type/navigate/wait).
- Keine langen Erklärungen.
""".strip()


async def run_once() -> Tuple[str, Telemetry, Optional[str]]:
    # LLM
    real_llm = ChatGroq(
        model="llama-3.3-70b-versatile",
        api_key=os.getenv("GROQ_API_KEY"),
        temperature=0.35,
    )
    llm = GroqAdapter(real_llm)

    # Steel Browser
    steel_key = os.getenv("STEEL_API_KEY")
    browser = Browser(cdp_url=f"wss://connect.steel.dev?apiKey={steel_key}")

    task = build_task()

    agent = Agent(
        task=task,
        llm=llm,
        browser=browser,
        use_vision=False,  # critical for Groq/Llama in deinem Setup
    )

    history = await agent.run()

    telemetry = analyze_history(history)

    # Ergebnis extrahieren
    result = None
    try:
        result = history.final_result()
    except Exception:
        result = None

    if not result:
        # fallback: letzten Step dumpen
        try:
            last = history.history[-1]
            result = getattr(last, "result", None) or getattr(last, "model_output", None) or str(last)
        except Exception:
            result = "Kein Ergebnistext."

    # optional: wenn browser-use irgendeinen Fehlertext hatte, sammeln
    err_text = None
    try:
        # manche Versionen haben history.errors o.ä. – wir bleiben defensiv
        if telemetry.errors > 0:
            err_text = "Agent hatte Fehler (siehe GitHub Action Logs / Steel Logs)."
    except Exception:
        pass

    return str(result), telemetry, err_text


async def main():
    # --- No-Op Guard + Retries ---
    # Wenn 0 actions, wird der Run als "schlafend" gewertet und erneut versucht.
    max_attempts = int(os.getenv("WORKER_MAX_ATTEMPTS", "3"))

    last_result = ""
    last_tel = Telemetry()
    last_err = None

    for attempt in range(1, max_attempts + 1):
        try:
            result, tel, err = await run_once()
            last_result, last_tel, last_err = result, tel, err

            # NO-OP DETECTOR: der kritische Fix
            no_actions = (tel.clicks == 0 and tel.types == 0 and tel.navigates == 0)
            if no_actions:
                # direkt retry
                print(f"[core] Attempt {attempt}: NO ACTIONS produced. Retrying...")
                if attempt < max_attempts:
                    continue

            # Wenn wir zumindest navigated haben, akzeptieren wir den Run (auch wenn Login evtl. nicht klappt)
            break

        except Exception as e:
            last_err = f"CRASH: {e}"
            print(f"[core] Attempt {attempt} crashed: {e}")
            if attempt < max_attempts:
                continue

    # --- Mail Report ---
    # Betreff kurz & benchmarkfähig (dein Wunsch)
    status = "✅" if (last_tel.login_claimed or last_tel.types >= 2) else "⚠️"
    if last_tel.clicks == 0 and last_tel.types == 0:
        status = "❌"

    subject = (
        f"{status} Worker: nav={last_tel.navigates} "
        f"clicks={last_tel.clicks} types={last_tel.types} "
        f"err={last_tel.errors} login={last_tel.login_claimed}"
    )

    body = fmt_telemetry(last_tel) + "\nERGEBNIS:\n" + (last_result or "") + "\n"
    if last_err:
        body += "\nHINWEIS:\n" + last_err + "\n"

    send_mail(subject, body)


if __name__ == "__main__":
    asyncio.run(main())
