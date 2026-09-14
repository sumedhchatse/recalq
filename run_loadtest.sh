#!/bin/bash
# Recalq — run a staged load test and print a clean summary
# Runs escalating concurrency levels so you see WHERE it starts to strain.
set -e

HOST="${1:-http://localhost:5000}"
cd "$HOME/memlayer"

echo "═══════════════════════════════════════════════════════════"
echo " RECALQ LOAD TEST — target: $HOST"
echo "═══════════════════════════════════════════════════════════"
echo ""

# Capture baseline memory
echo "Baseline memory:"
free -h | grep Mem
echo ""

# Staged runs: escalate concurrency to find the strain point
for USERS in 20 50 100; do
    echo "───────────────────────────────────────────────────────────"
    echo " STAGE: $USERS concurrent users (ramp 10/sec, 90 seconds)"
    echo "───────────────────────────────────────────────────────────"

    locust -f loadtest.py --headless \
        -u "$USERS" -r 10 -t 90s \
        --host "$HOST" \
        --csv="results_${USERS}u" \
        --only-summary 2>/dev/null || echo "(run completed)"

    echo ""
    echo "Memory during ${USERS}-user load:"
    free -h | grep Mem
    echo ""
    sleep 5
done

echo "═══════════════════════════════════════════════════════════"
echo " RESULTS SUMMARY"
echo "═══════════════════════════════════════════════════════════"
for USERS in 20 50 100; do
    F="results_${USERS}u_stats.csv"
    if [ -f "$F" ]; then
        echo ""
        echo "── ${USERS} concurrent users ──"
        # Print the aggregated row (last line) with key columns
        python3 -c "
import csv
with open('$F') as f:
    rows = list(csv.DictReader(f))
agg = [r for r in rows if r.get('Name') == 'Aggregated']
if agg:
    r = agg[0]
    print(f\"  Requests:     {r.get('Request Count','?')}\")
    print(f\"  Failures:     {r.get('Failure Count','?')}\")
    print(f\"  Median (p50): {r.get('Median Response Time','?')} ms\")
    print(f\"  p95:          {r.get('95%','?')} ms\")
    print(f\"  p99:          {r.get('99%','?')} ms\")
    print(f\"  Max:          {r.get('Max Response Time','?')} ms\")
    print(f\"  Req/sec:      {r.get('Requests/s','?')}\")
"
    fi
done
echo ""
echo "Full CSVs: results_*u_stats.csv  |  per-request: results_*u_stats_history.csv"
