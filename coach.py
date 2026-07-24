"""Rule-based AI Coach. Keyword-matched canned responses rather than a
real LLM call - this keeps the basic build dependency-free. Swapping in
a real model later just means replacing `reply_to()`'s body.
"""
import re

from plan_engine import generate_plan

_RULES = [
    (r"\b(tired|exhaust|no energy|drained)\b", "low",
     "No problem! Let's try a gentle, short routine so you still move today without overdoing it."),
    (r"\b(pain|hurt|sore|ache|discomfort)\b", "low",
     "Sorry to hear that. Let's keep things gentle and avoid the sore area today. "
     "If the pain is sharp or getting worse, please check in with your doctor or physical therapist."),
    (r"\b(great|good|energetic|strong|amazing)\b", "great",
     "Love that energy! Let's put it to use with a slightly more active routine."),
    (r"\b(no time|busy|short on time|five minutes|5 min)\b", "okay",
     "Totally understandable - here's a quick routine that fits a tight schedule."),
    (r"\b(hi|hello|hey)\b", "okay",
     "Hi there! How are you feeling today - tired, okay, good, or great?"),
]

_DEFAULT = ("Thanks for checking in! Here's a balanced routine to keep your "
            "recovery on track today.")


def reply_to(message):
    text = (message or "").lower()
    energy = "okay"
    reply = _DEFAULT
    for pattern, matched_energy, response in _RULES:
        if re.search(pattern, text):
            energy = matched_energy
            reply = response
            break

    time_available = 8 if energy == "low" else 10
    routine, total_minutes = generate_plan(energy, time_available, "home", [])
    return {
        "reply": reply,
        "routine": routine,
        "routine_minutes": total_minutes,
    }
