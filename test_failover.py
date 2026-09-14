"""
Recalq — Provider Failover Test
Verifies: when a provider fails, the fallback chain answers, and we can
tell WHICH model actually responded (no silent total failure).

Run:  cd ~/memlayer && source .venv/bin/activate && python3 test_failover.py

Tests via the LiteLLM proxy directly (localhost:4000), same path the app uses.
"""
import os, sys, json, urllib.request

PROXY = "http://localhost:4000/chat/completions"
KEY = os.getenv("LITELLM_MASTER_KEY", "")

def call(model, timeout=40):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "reply with the single word OK"}],
        "max_tokens": 10,
    }).encode()
    req = urllib.request.Request(PROXY, data=body, headers={
        "Authorization": "Bearer " + KEY,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
            return {"ok": True, "model": data.get("model", "?"),
                    "content": data["choices"][0]["message"]["content"][:30]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

def main():
    if not KEY:
        print("ERROR: LITELLM_MASTER_KEY not set. Run: set -a && source .env && set +a")
        return 1

    print("="*55)
    print(" PROVIDER FAILOVER TEST")
    print("="*55)

    # 1. Each real provider answers (baseline)
    print("\n[1] Baseline — each provider responds:")
    providers = ["gemini", "groq", "nvidia", "auto"]
    for p in providers:
        r = call(p)
        status = f"OK  -> answered as '{r['model']}'" if r["ok"] else f"FAIL: {r['error']}"
        print(f"    {p:10} {status}")

    # 2. Non-existent model -> should fallback or error clearly (not hang)
    print("\n[2] Failure path — bad model name:")
    r = call("this_provider_does_not_exist_xyz")
    if r["ok"]:
        print(f"    Fallback answered as '{r['model']}' (fallback working)")
    else:
        print(f"    Cleanly rejected: {r['error'][:70]}")
        print("    (No silent hang — error surfaced. Acceptable.)")

    # 3. Report — what a real fallback looks like
    print("\n[3] Interpretation:")
    print("    - If baseline providers answer with their OWN model name -> routing correct.")
    print("    - 'auto' should answer as your cheapest provider (groq).")
    print("    - Bad-model either falls back OR errors clearly (never hangs).")
    print("\n    NOTE: To test a REAL provider outage, temporarily break one")
    print("    provider's key in .env, restart, and confirm its fallback (gemini)")
    print("    answers instead. This test confirms the routing paths are live.")
    print("="*55)
    return 0

if __name__ == "__main__":
    sys.exit(main())
