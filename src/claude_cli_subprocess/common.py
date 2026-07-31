import os
import re

def build_claude_env() -> dict:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    return env

def is_usage_limit(text: str) -> bool:
    low = (text or "").lower()
    if re.search(r"reached\s*\|\s*\d{9,13}", text or ""):
        return True
    return any(s in low for s in
               ("usage limit", "rate limit", "5-hour", "limit reached",
                "too many requests", "quota exceeded"))
