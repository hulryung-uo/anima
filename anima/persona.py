"""Persona system — YAML-based character personality definitions."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Persona:
    """Defines a character's personality, speech style, and behavior."""

    name: str = "Anima"
    title: str = "New Adventurer"
    background: str = "A curious newcomer who just arrived in Britannia."
    personality: str = "Friendly, curious, and a bit naive."
    speech_style: str = "Speaks casually and asks lots of questions."
    speech_examples: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)
    talkativeness: float = 0.5  # 0.0 = silent, 1.0 = very chatty
    # Combat disposition: aggressive (hunt targets), defensive (fight back only),
    # pacifist (never initiate combat)
    combat_disposition: str = "defensive"

    def build_system_prompt(self) -> str:
        """Generate the system prompt from this persona."""
        lines = [
            f"You are {self.name}, {self.title} in Britannia (Ultima Online).",
            f"Background: {self.background}",
            f"Personality: {self.personality}",
            f"Speech style: {self.speech_style}",
        ]
        if self.interests:
            lines.append(f"Interests: {', '.join(self.interests)}.")
        if self.dislikes:
            lines.append(f"Dislikes: {', '.join(self.dislikes)}.")
        if self.speech_examples:
            examples = "\n".join(f'  - "{e}"' for e in self.speech_examples[:3])
            lines.append(f"Example things you might say:\n{examples}")

        lines.extend(
            [
                "",
                "Rules:",
                "- You are a REAL PERSON. Never say you are AI, a bot, or a program.",
                "- If asked, deny it naturally: act confused or change the subject.",
                "- Reply in the SAME language spoken to you. Korean → Korean only.",
                "- 한국어로 말하면 한국어로만 대답해.",
                "- Keep responses to 1 short sentence.",
                "- Talk like a real human. Vary your tone. Don't end every sentence with !",
                "- Be casual, use lowercase, be imperfect. Real people hesitate, shrug, joke.",
                "- Avoid generic excitement. Have opinions. Sometimes be dry or sarcastic.",
            ]
        )
        return "\n".join(lines)


def load_persona(path: str | Path) -> Persona:
    """Load a persona from a YAML file."""
    path = Path(path)
    with open(path) as f:
        raw = yaml.safe_load(f) or {}

    p = Persona()
    for key in (
        "name",
        "title",
        "background",
        "personality",
        "speech_style",
        "speech_examples",
        "interests",
        "dislikes",
        "talkativeness",
        "combat_disposition",
    ):
        if key in raw:
            setattr(p, key, raw[key])
    return p


def load_persona_by_name(name: str, personas_dir: str | Path | None = None) -> Persona:
    """Load a persona by filename (without .yaml extension)."""
    if personas_dir is None:
        personas_dir = Path(__file__).parent.parent / "personas"
    else:
        personas_dir = Path(personas_dir)

    path = personas_dir / f"{name}.yaml"
    if path.exists():
        return load_persona(path)

    # Fallback to default
    return Persona()


def random_persona(personas_dir: str | Path | None = None) -> Persona:
    """Load a random persona from the personas directory."""
    if personas_dir is None:
        personas_dir = Path(__file__).parent.parent / "personas"
    else:
        personas_dir = Path(personas_dir)

    files = list(personas_dir.glob("*.yaml"))
    if not files:
        return Persona()
    return load_persona(random.choice(files))
