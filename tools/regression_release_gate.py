from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

CHECKS = [
    ("final_audit_check2", [sys.executable, "tools/final_audit_check2.py"]),
    ("extreme_chaos_audit", [sys.executable, "tools/extreme_chaos_audit.py"]),
    ("zero_state_check", [sys.executable, "tools/zero_state_check.py"]),
    ("ux_selection_audit", [sys.executable, "tools/ux_selection_audit.py"]),
    ("e2e_new_order_playwright", ["node", "tools/e2e_new_order_playwright.cjs"]),
    ("force_password_proxy_host_audit", [sys.executable, "tools/force_password_proxy_host_audit.py"]),
    ("stress_smoke", [sys.executable, "tools/stress_smoke.py"]),
    ("unit_core_runtime", [sys.executable, "-m", "unittest", "tests.test_core_runtime", "-v"]),
    ("unit_batch_date", [sys.executable, "-m", "unittest", "tests.test_batch_date", "-v"]),
    ("unit_driver_report_fix", [sys.executable, "-m", "unittest", "tests.test_driver_report_fix", "-v"]),
    ("unit_scheduled_status", [sys.executable, "-m", "unittest", "tests.test_scheduled_status", "-v"]),
]


def main() -> int:
    failures = []
    for name, cmd in CHECKS:
        print(f"\n==> {name}")
        proc = subprocess.run(cmd, cwd=str(ROOT))
        if proc.returncode != 0:
            failures.append(name)
    if failures:
        print("\nFALHA RELEASE GATE:", ", ".join(failures))
        return 1
    print("\nOK RELEASE GATE: todos os checks passaram.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
