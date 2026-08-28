#!/bin/bash
# Прогон всех тестов PolyBot. Сеть не используется — рыночные вызовы замоканы.
cd "$(dirname "$0")/.."
TOTAL=0; PASSED=0; FAILED=0
for t in tests/test_strategy.py tests/test_ledger.py tests/test_risk_blocked.py; do
  echo "── $t ──"
  OUT=$(timeout 300 python3 "$t" 2>&1)
  LINE=$(echo "$OUT" | grep -E "^ИТОГО:" | tail -1)
  echo "$OUT" | grep -E "^ FAIL" | head -20
  echo "$LINE"
  P=$(echo "$LINE" | grep -oE "[0-9]+/[0-9]+" | cut -d/ -f1); N=$(echo "$LINE" | grep -oE "[0-9]+/[0-9]+" | cut -d/ -f2)
  TOTAL=$((TOTAL+N)); PASSED=$((PASSED+P)); FAILED=$((FAILED+N-P))
done
echo
echo "════════════════════════════════"
echo "TOTAL TESTS: $TOTAL"
echo "PASSED:      $PASSED"
echo "FAILED:      $FAILED"
[ "$FAILED" -eq 0 ] || exit 1
