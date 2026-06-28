#!/usr/bin/env bash
# PreToolUse hook (EnterPlanMode|ExitPlanMode): remind to keep the sibling Android
# client contract-compatible, and surface its current git state into context.
repo="C:/Users/satta/Desktop/Projects/pothole-detection-mobile-app"
S=$(git -C "$repo" status -s 2>/dev/null)
C=$(git -C "$repo" log --oneline -5 2>/dev/null)
export S C
python - <<'PY'
import json, os
s = os.environ.get("S", "").strip() or "(clean)"
c = os.environ.get("C", "").strip() or "(unavailable — repo missing or no commits)"
msg = (
    "PLAN REMINDER — this server has an Android client (sibling repo) at "
    "C:/Users/satta/Desktop/Projects/pothole-detection-mobile-app. Before finalizing any "
    "plan that touches the wire contract (REST endpoints, JSON field names/shapes, or DB "
    "columns the client reads), check that client for backward compatibility.\n\n"
    "Mobile app — recent commits:\n" + c + "\n\n"
    "Mobile app — working tree (empty = clean):\n" + s
)
print(json.dumps({
    "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": msg}
}))
PY
