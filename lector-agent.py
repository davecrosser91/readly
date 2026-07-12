#!/usr/bin/env python3
"""Lector Agent — headless worker for the outbox.

Arbeitet automatisierbare Aktionen ab (v0.2a: enrich_vocab — Vokabel-Erklärungen
nachziehen). unclear_mark-Aktionen bleiben bewusst liegen: Dort soll ein
*interaktiver* Agent (Claude-Session via /lector-Skill, ChatGPT) erst mit David
klären, was zu tun ist.

Engines laufen über Subscriptions, kein API-Key:
  --engine claude  →  claude -p   (Anthropic-Abo, Default)
  --engine codex   →  codex exec  (ChatGPT-Abo)

Usage:
  python3 lector-agent.py            # Daemon, pollt alle 10s
  python3 lector-agent.py --once     # eine Runde, dann Exit (für cron/Test)
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8123"
LANG_NAMES = {"es": "Spanisch", "en": "Englisch"}


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read().decode())


def run_engine(engine, prompt):
    if engine == "claude":
        cmd = ["claude", "-p", "--output-format", "text", "--strict-mcp-config"]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=300)
    elif engine == "codex":
        cmd = ["codex", "exec", "--skip-git-repo-check", prompt]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    else:
        raise ValueError("unknown engine " + engine)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip()[:300])
    return proc.stdout.strip()


def enrich_vocab(engine, action, context):
    p = action["payload"]
    book = context.get("book") or {}
    lang = LANG_NAMES.get(book.get("language"), book.get("language", ""))
    passage = context.get("passage") or {}
    around = "\n".join(passage.get("before", [])[-1:] +
                       [passage.get("containing", "")] +
                       passage.get("after", [])[:1])
    prompt = (
        'Erkläre kompakt für einen deutschen Lerner (Niveau B1-B2) das %s-Wort/'
        'die Wendung "%s".\nSatzkontext: "%s"\nUmgebung im Buch "%s":\n%s\n\n'
        "Format (Markdown, max ~90 Wörter):\n"
        "**Bedeutung hier:** <deutsch, im Kontext>\n"
        "**Synonyme & Nuancen:** <2-3 in der Zielsprache, je kurz abgegrenzt>\n"
        "**Beispiel:** <neuer Beispielsatz in der Zielsprache>"
        % (lang, p["word"], p.get("sentence", ""), book.get("title", ""), around[:1200]))
    explanation = run_engine(engine, prompt)
    http("POST", "/api/agent/complete",
         {"action_id": action["id"], "agent": "lector-agent/" + engine,
          "result": {"explanation": explanation}})
    print("[agent] enriched vocab: %s" % p["word"])


def run_once(engine):
    handled = 0
    while True:
        nxt = http("GET", "/api/agent/next?types=enrich_vocab")
        action = nxt.get("action")
        if not action:
            return handled
        if not http("POST", "/api/agent/claim",
                    {"action_id": action["id"], "agent": "lector-agent/" + engine}).get("ok"):
            continue
        try:
            enrich_vocab(engine, action, nxt.get("context", {}))
            handled += 1
        except Exception as e:
            print("[agent] failed:", e, file=sys.stderr)
            http("POST", "/api/agent/complete",
                 {"action_id": action["id"], "status": "failed",
                  "agent": "lector-agent/" + engine, "result": {"error": str(e)}})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=["claude", "codex"], default="claude")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    if args.once:
        n = run_once(args.engine)
        print("[agent] done, %d action(s) handled" % n)
        return
    print("[agent] polling %s (engine: %s)" % (BASE, args.engine))
    while True:
        try:
            run_once(args.engine)
        except Exception as e:
            print("[agent] error:", e, file=sys.stderr)
        time.sleep(10)


if __name__ == "__main__":
    main()
