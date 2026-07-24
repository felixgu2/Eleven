"""Client for the Azure AI Foundry agent ("PhysicalCare AI") that powers
the Coach chat and daily Mission Generator. Auth is a plain resource API
key against the agent-invocation endpoint - no Azure AD needed; that's
only required for the separate thread/run management API, which this
app doesn't use.
"""
import json
import os
import urllib.request

from dotenv import load_dotenv

load_dotenv()

AZURE_AI_ENDPOINT = os.environ["AZURE_AI_ENDPOINT"].rstrip("/")
AZURE_AI_KEY = os.environ["AZURE_AI_KEY"]
AGENT_NAME = os.environ.get("AZURE_AI_AGENT_NAME", "care67")
AGENT_VERSION = os.environ.get("AZURE_AI_AGENT_VERSION", "1")

_RESPONSES_URL = f"{AZURE_AI_ENDPOINT}/openai/v1/responses"


def _call_agent(input_messages):
    body = {
        "input": input_messages,
        "agent_reference": {"type": "agent_reference", "name": AGENT_NAME, "version": AGENT_VERSION},
    }
    req = urllib.request.Request(
        _RESPONSES_URL,
        data=json.dumps(body).encode(),
        headers={"api-key": AZURE_AI_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as resp:
        data = json.load(resp)
    return _extract_text(data)


def _extract_text(data):
    for item in data.get("output", []):
        if item.get("type") == "message" and item.get("role") == "assistant":
            return "".join(
                c.get("text", "") for c in item.get("content", []) if c.get("type") == "output_text"
            )
    return ""


def _strip_code_fence(text):
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return text.strip()


def coach_reply(history):
    """history: list of {"role": "user"|"assistant", "text": str}, oldest first."""
    input_messages = [{"role": h["role"], "content": h["text"]} for h in history]
    return _call_agent(input_messages)


def generate_mission_json(context_text):
    """Ask the agent's Mission Generator Mode for a structured mission,
    returning the parsed dict described in the agent's own JSON schema."""
    prompt = (
        "generate_mission\n\n" + context_text +
        "\n\nReturn only the JSON object described in your instructions, with no extra commentary."
    )
    raw = _call_agent([{"role": "user", "content": prompt}])
    return json.loads(_strip_code_fence(raw))


def generate_badges_json(count, weather=None):
    """Ask the agent for `count` collectible walking-badge ideas (name,
    icon, description). This isn't one of the agent's two documented
    modes, but it follows ad-hoc JSON instructions fine in practice;
    callers should still have a procedural fallback for robustness."""
    weather_clause = f" It's currently {weather['label'].lower()} outside." if weather else ""
    prompt = (
        f"generate_badges\n\n"
        f"Generate {count} fun, unique collectible badge ideas for a walking/movement "
        f"gamification feature in a physical-recovery app (Pokemon-Go-style badges placed "
        f"on a map for a user to walk to).{weather_clause}\n\n"
        f"Return ONLY a JSON array (no commentary, no code fences) of exactly {count} objects, "
        f"each with keys: name (2-4 words), icon (a single emoji), description (one encouraging "
        f"sentence under 15 words). Theme it around nature, movement, recovery, and small wins. "
        f"No duplicates."
    )
    raw = _call_agent([{"role": "user", "content": prompt}])
    return json.loads(_strip_code_fence(raw))
