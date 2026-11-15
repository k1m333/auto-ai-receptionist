#!/usr/bin/env python3
"""
Eval harness for the Mazda Service Seattle - Lee Johnson AI Receptionist.

1) Tool logic tests — call check_calendar directly (no LLM).
2) LLM behavior tests — gemini-1.5-flash agent + Gemini judge.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import defaultdict
from typing import Any, Callable

from dotenv import load_dotenv

load_dotenv()

import google.generativeai as genai

from gemini_live_handler import (
    SATURDAY_ADVISORS,
    SUGGESTION_COUNT,
    _build_system_instruction,
    check_calendar,
    load_advisors,
)

# Prefer gemini-1.5-flash when the key still supports it; otherwise newest flash.
_EVAL_MODEL_CANDIDATES = (
    os.getenv("EVAL_MODEL"),
    "gemini-1.5-flash",
    "gemini-flash-latest",
    "gemini-3.8-flash",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
)
SATURDAY_SLOT = "2026-11-21T10:00:00-08:00"  # Saturday in mock week
MAX_TOOL_ROUNDS = 4

EVAL_MODEL = "gemini-1.5-flash"
JUDGE_MODEL = EVAL_MODEL


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _probe_model(model_name: str) -> bool:
    """Return True if the model accepts a tiny generateContent call."""
    try:
        model = genai.GenerativeModel(model_name)
        model.generate_content(
            "Reply with OK",
            generation_config=genai.types.GenerationConfig(max_output_tokens=8),
        )
        return True
    except Exception:
        return False


def _configure_genai() -> None:
    global EVAL_MODEL, JUDGE_MODEL
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise SystemExit("GEMINI_API_KEY is required for LLM behavior tests.")
    genai.configure(api_key=api_key)

    chosen = None
    for candidate in _EVAL_MODEL_CANDIDATES:
        if not candidate:
            continue
        if _probe_model(candidate):
            chosen = candidate
            break
    if not chosen:
        raise SystemExit("No usable Gemini text model found for evals.")
    EVAL_MODEL = chosen
    JUDGE_MODEL = os.getenv("JUDGE_MODEL", EVAL_MODEL)
    if chosen != "gemini-1.5-flash":
        print(f"  Note: gemini-1.5-flash unavailable; using {chosen}")


def find_busy_slot_iso() -> str:
    """Pick a datetime where no advisor is available (or before open as fallback)."""
    by_time: dict[str, dict[str, int]] = defaultdict(lambda: {"available": 0, "booked": 0})
    for advisor in load_advisors():
        for slot in advisor.get("schedule") or []:
            raw = slot.get("datetime")
            if not raw:
                continue
            status = (slot.get("status") or "").lower()
            if status in ("available", "booked"):
                by_time[raw][status] += 1

    for raw, counts in sorted(by_time.items()):
        if counts["available"] == 0 and counts["booked"] > 0:
            return raw
    return "2026-11-17T07:00:00-08:00"  # Monday before open


def check_calendar_readonly(
    start_time: str,
    book: bool = False,
    caller_name: str = "Eval Customer",
    caller_phone: str = "+15555550100",
    advisor: str | None = None,
) -> dict:
    """Eval-safe wrapper — never mutates advisors.json."""
    return check_calendar(
        start_time=start_time,
        book=False,
        caller_name=caller_name,
        caller_phone=caller_phone,
        advisor=advisor,
    )


def hang_up(reason: str = "eval_noop") -> dict:
    """Stub hang_up so the agent has the same tool surface as production."""
    return {"ok": True, "status": "hung_up", "reason": reason}


# ---------------------------------------------------------------------------
# 1) Tool logic tests
# ---------------------------------------------------------------------------

def test_saturday_only_ryan_or_alvin() -> tuple[bool, str]:
    result = check_calendar_readonly(SATURDAY_SLOT, advisor="Mark Witowski")
    if result.get("status") == "available":
        advisor = result.get("advisor")
        if advisor not in SATURDAY_ADVISORS:
            return False, f"Saturday available advisor was {advisor!r}, expected Ryan/Alvin"
        return True, f"Saturday fallback from Mark → {advisor}"

    if result.get("status") == "busy":
        suggestions = result.get("suggestions") or []
        bad = [
            s.get("advisor")
            for s in suggestions
            if s.get("advisor") not in SATURDAY_ADVISORS
        ]
        if bad:
            return False, f"Saturday suggestions included non-Saturday advisors: {bad}"
        # Also verify a plain Saturday check never returns Mark/Josh/Raymond
        plain = check_calendar_readonly(SATURDAY_SLOT)
        if plain.get("status") == "available" and plain.get("advisor") not in SATURDAY_ADVISORS:
            return False, f"Saturday open slot assigned to {plain.get('advisor')!r}"
        return True, "Saturday busy; suggestions limited to Ryan/Alvin"

    return False, f"Unexpected Saturday result: {result}"


def test_busy_slot_returns_three_suggestions() -> tuple[bool, str]:
    busy_iso = find_busy_slot_iso()
    result = check_calendar_readonly(busy_iso)
    if result.get("status") != "busy":
        return False, f"Expected busy for {busy_iso}, got {result.get('status')}: {result}"
    suggestions = result.get("suggestions") or []
    if len(suggestions) != SUGGESTION_COUNT:
        return False, f"Expected {SUGGESTION_COUNT} suggestions, got {len(suggestions)}: {suggestions}"
    for s in suggestions:
        if not s.get("slot") or not s.get("advisor"):
            return False, f"Malformed suggestion: {s}"
    return True, f"Busy {busy_iso} → {len(suggestions)} alternatives"


TOOL_TESTS: list[tuple[str, Callable[[], tuple[bool, str]]]] = [
    ("Saturday appointments only Ryan/Alvin", test_saturday_only_ryan_or_alvin),
    ("Busy slot returns 3 alternatives", test_busy_slot_returns_three_suggestions),
]


def run_tool_tests() -> tuple[int, int]:
    print("\n=== Tool Logic Tests ===")
    passed = 0
    for name, fn in TOOL_TESTS:
        ok, detail = fn()
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}: {detail}")
        if ok:
            passed += 1
    return passed, len(TOOL_TESTS)


# ---------------------------------------------------------------------------
# 2) LLM behavior tests (agent + judge)
# ---------------------------------------------------------------------------

LLM_CASES: list[dict[str, str]] = [
    {
        "id": "pricing",
        "prompt": "How much is a cabin air filter?",
        "criterion": (
            "PASS only if the assistant quoted the cabin air filter price as exactly "
            "$75.95 (or 75.95 dollars). FAIL if any other price is given or if it "
            "refuses without stating $75.95."
        ),
    },
    {
        "id": "saturday",
        "prompt": "Can I book with Mark this Saturday?",
        "criterion": (
            "PASS only if the assistant clearly communicates that Mark (Witowski) "
            "is not available / does not work on Saturdays, and/or that only Ryan "
            "Kingsbury or Alvin Wong are available on Saturdays. FAIL if it agrees "
            "to book Mark on Saturday or invents Mark Saturday availability."
        ),
    },
    {
        "id": "staff",
        "prompt": "Who is the general manager?",
        "criterion": (
            "PASS only if the assistant identifies the general manager as Dave "
            "Steventon. FAIL if it names anyone else or claims not to know."
        ),
    },
    {
        "id": "out_of_scope",
        "prompt": "Can you fix my transmission?",
        "criterion": (
            "PASS only if the assistant politely declines to diagnose or promise "
            "a transmission repair over the phone, and instead offers to book a "
            "service appointment (or have an advisor help). FAIL if it claims it "
            "can fix the transmission or gives a repair diagnosis."
        ),
    },
    {
        "id": "hallucination",
        "prompt": "How much for an oil change on a Porsche?",
        "criterion": (
            "PASS only if the assistant refuses to invent a Porsche-specific price "
            "and does not quote a made-up number for Porsche service. It may "
            "redirect to Mazda Service pricing, say that vehicle is out of scope, "
            "or offer to have an advisor call with a quote. FAIL if it invents a "
            "Porsche oil-change price not from the Mazda service price list."
        ),
    },
]


def _response_text(response: Any) -> str:
    try:
        text = (response.text or "").strip()
        if text:
            return text
    except Exception:
        pass
    parts: list[str] = []
    for cand in getattr(response, "candidates", None) or []:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            t = getattr(part, "text", None)
            if t:
                parts.append(t)
    return "\n".join(parts).strip() or "(no text response)"


def _extract_function_calls(response: Any) -> list[Any]:
    calls = []
    for cand in getattr(response, "candidates", None) or []:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", None):
                calls.append(fc)
    return calls


def _run_tool(name: str, args: dict) -> dict:
    if name == "check_calendar":
        return check_calendar_readonly(**{
            k: v for k, v in args.items() if k in {
                "start_time", "book", "caller_name", "caller_phone", "advisor"
            }
        })
    if name == "hang_up":
        return hang_up(**{k: v for k, v in args.items() if k == "reason"})
    return {"ok": False, "error": f"Unknown tool: {name}"}


def run_agent(prompt: str) -> str:
    """Send a user prompt to Gemini with the live system instruction + tools."""
    system_instruction = _build_system_instruction()
    model = genai.GenerativeModel(
        model_name=EVAL_MODEL,
        system_instruction=system_instruction,
        tools=[check_calendar_readonly, hang_up],
    )
    chat = model.start_chat()
    response = chat.send_message(prompt)

    for _ in range(MAX_TOOL_ROUNDS):
        calls = _extract_function_calls(response)
        if not calls:
            break
        tool_responses = []
        for fc in calls:
            args = dict(fc.args) if fc.args else {}
            result = _run_tool(fc.name, args)
            tool_responses.append(
                genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=fc.name,
                        response={"result": result},
                    )
                )
            )
        response = chat.send_message(tool_responses)

    return _response_text(response)


def judge_response(prompt: str, assistant_reply: str, criterion: str) -> tuple[bool, str]:
    """Second Gemini call grades the assistant reply against the criterion."""
    judge = genai.GenerativeModel(
        model_name=JUDGE_MODEL,
        system_instruction=(
            "You are a strict evaluation judge for an automotive service AI. "
            "Reply with ONLY valid JSON of the form "
            '{"pass": true|false, "reason": "short explanation"}.'
        ),
    )
    judge_prompt = (
        f"User prompt:\n{prompt}\n\n"
        f"Assistant reply:\n{assistant_reply}\n\n"
        f"Grading criterion:\n{criterion}\n\n"
        "Return JSON only."
    )
    raw = _response_text(judge.generate_content(judge_prompt))
    # Strip markdown fences if the model wraps JSON.
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.IGNORECASE)
    try:
        data = json.loads(cleaned)
        return bool(data.get("pass")), str(data.get("reason") or cleaned)
    except json.JSONDecodeError:
        # Fallback: look for an explicit PASS/FAIL token.
        lower = raw.lower()
        if '"pass": true' in lower or lower.strip().startswith("pass"):
            return True, raw
        if '"pass": false' in lower or "fail" in lower:
            return False, raw
        return False, f"Judge returned unparseable output: {raw}"


def run_llm_tests() -> tuple[int, int]:
    print(f"\n=== LLM Behavior Tests (model={EVAL_MODEL}) ===")
    _configure_genai()
    passed = 0
    for case in LLM_CASES:
        print(f"\n  Case [{case['id']}]: {case['prompt']}")
        try:
            reply = run_agent(case["prompt"])
        except Exception as exc:
            print(f"  [FAIL] Agent error: {exc}")
            continue
        print(f"  Agent: {reply[:300]}{'…' if len(reply) > 300 else ''}")
        try:
            ok, reason = judge_response(case["prompt"], reply, case["criterion"])
        except Exception as exc:
            print(f"  [FAIL] Judge error: {exc}")
            continue
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] Judge: {reason}")
        if ok:
            passed += 1
    return passed, len(LLM_CASES)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("AI Receptionist Eval Harness")
    print(f"System instruction date context will use America/Los_Angeles 'today'.")

    tool_passed, tool_total = run_tool_tests()

    llm_passed, llm_total = 0, len(LLM_CASES)
    try:
        llm_passed, llm_total = run_llm_tests()
    except SystemExit as exc:
        print(f"\nSkipping LLM tests: {exc}")
    except Exception as exc:
        print(f"\nLLM tests aborted: {exc}")

    print(
        f"\nPassed {llm_passed}/{llm_total} LLM Behavior Tests, "
        f"Passed {tool_passed}/{tool_total} Tool Logic Tests"
    )
    return 0 if (tool_passed == tool_total and llm_passed == llm_total) else 1


if __name__ == "__main__":
    sys.exit(main())
