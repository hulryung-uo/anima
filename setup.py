#!/usr/bin/env python3
"""Interactive setup for a new Anima agent.

Generates account credentials and character identity,
then writes them to config.yaml.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from anima.naming import generate_account_name, generate_character_name

CONFIG_PATH = Path(__file__).parent / "config.yaml"

TEMPLATES = ["random", "warrior", "mage", "smith", "merchant", "ranger"]
PERSONAS = ["adventurer", "blacksmith", "merchant", "mage", "bard", "ranger"]
CITIES = {
    0: "New Haven",
    3: "Britain",
}


def _ask(prompt: str, default: str = "") -> str:
    """Prompt user for input with optional default."""
    if default:
        text = input(f"{prompt} [{default}]: ").strip()
        return text or default
    return input(f"{prompt}: ").strip()


def _ask_choice(prompt: str, choices: list[str], default: str = "") -> str:
    """Prompt user to pick from a list."""
    print(f"\n{prompt}")
    for i, c in enumerate(choices, 1):
        marker = " *" if c == default else ""
        print(f"  {i}. {c}{marker}")
    while True:
        raw = input(f"선택 (1-{len(choices)}) [{default}]: ").strip()
        if not raw and default:
            return default
        try:
            idx = int(raw)
            if 1 <= idx <= len(choices):
                return choices[idx - 1]
        except ValueError:
            if raw in choices:
                return raw
        print("  다시 선택해주세요.")


def main() -> None:
    print("=" * 50)
    print("  Anima Agent Setup")
    print("=" * 50)

    # --- Server ---
    print("\n[ 서버 설정 ]")
    host = _ask("서버 주소", "uo.hulryung.com")
    port = _ask("포트", "2593")

    # --- Account ---
    print("\n[ 계정 설정 ]")
    print("계정 이름을 직접 입력하거나, 비워두면 자동 생성합니다.")
    auto_account = generate_account_name()
    username = _ask("계정 이름", auto_account)
    password = _ask("비밀번호", username)

    # --- Character ---
    print("\n[ 캐릭터 설정 ]")
    print("캐릭터 이름을 직접 입력하거나, 비워두면 자동 생성합니다.")
    auto_name = generate_character_name()
    char_name = _ask("캐릭터 이름", auto_name)

    template = _ask_choice(
        "캐릭터 템플릿:", TEMPLATES, default="random"
    )
    city = _ask_choice(
        "시작 도시:", list(CITIES.values()), default="Britain"
    )
    city_index = next(k for k, v in CITIES.items() if v == city)

    persona = _ask_choice(
        "페르소나 (AI 성격):", PERSONAS, default="adventurer"
    )

    # --- Build config ---
    config = {
        "server": {"host": host, "port": int(port)},
        "account": {"username": username, "password": password},
        "character": {
            "name": char_name,
            "template": template,
            "city_index": city_index,
            "persona": persona,
        },
        "client": {"version": "7.0.102.3", "connection_timeout": 10.0},
        "movement": {
            "walk_delay_ms": 400,
            "run_delay_ms": 200,
            "turn_delay_ms": 100,
        },
        "map": {"resource_dir": "~/dev/uo/uo-resource"},
        "llm": {
            "base_url": "http://localhost:11434",
            "model": "gemma3:4b",
            "temperature": 0.7,
            "timeout": 10.0,
        },
        "memory": {
            "db_path": "data/anima.db",
            "max_episodes": 10000,
            "retrieval_count": 5,
        },
        "forum": {
            "enabled": False,
            "base_url": "https://www.uotavern.com/api",
            "api_key": "",
            "post_interval": 3600,
            "read_interval": 1800,
        },
    }

    # --- Preview ---
    print("\n" + "=" * 50)
    print("  설정 확인")
    print("=" * 50)
    print(f"  서버:     {host}:{port}")
    print(f"  계정:     {username} / {password}")
    print(f"  캐릭터:   {char_name}")
    print(f"  템플릿:   {template}")
    print(f"  도시:     {city}")
    print(f"  페르소나: {persona}")
    print("=" * 50)

    confirm = input("\nconfig.yaml에 저장할까요? (Y/n): ").strip().lower()
    if confirm in ("", "y", "yes"):
        with open(CONFIG_PATH, "w") as f:
            f.write("# Anima Configuration\n\n")
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
        print(f"\n저장 완료: {CONFIG_PATH}")
        print("실행: uv run python -m anima")
    else:
        print("취소됨.")


if __name__ == "__main__":
    main()
