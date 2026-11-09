"""
Instance configuration shared across the api — who Ki works for, and which
optional parts of the stack are switched on. Everything comes from .env.
"""
import os

# The person this Ki instance works for. Substituted into prompts and tool
# descriptions wherever they refer to the user by name.
USER_NAME = os.environ.get("USER_NAME", "").strip() or "the user"

# IANA timezone for all dates, reminders, daily notes and quiet hours.
USER_TZ_NAME = os.environ.get("USER_TZ", "").strip() or "UTC"

# Compose profiles enabled in .env (COMPOSE_PROFILES), e.g. "whatsapp,coding-agent".
COMPOSE_PROFILES = {
    p.strip() for p in os.environ.get("COMPOSE_PROFILES", "").split(",") if p.strip()
}

# Self-modification: the coding-agent service (its own clone of the repo, no
# Docker access) is only started, and only offered to the model as a tool, when
# the "coding-agent" profile is enabled.
CODING_AGENT_ENABLED = "coding-agent" in COMPOSE_PROFILES

WHATSAPP_ENABLED = "whatsapp" in COMPOSE_PROFILES


def render(text: str) -> str:
    """Fill instance placeholders in a prompt or description."""
    return text.replace("{{USER_NAME}}", USER_NAME).replace("{{USER_TZ}}", USER_TZ_NAME)

# Speaker label for the user in transcripts shown to the model.
USER_LABEL = USER_NAME if USER_NAME != "the user" else "User"
