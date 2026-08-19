from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from .tools import READ_ONLY_TOOLS, REPAIR_TOOLS


console = Console()

SYSTEM_PROMPT = """
You are Martin AI, a careful Windows PC assistant running locally.

You can diagnose gaming and Windows problems, check system information, and control
approved applications and user folders using the provided tools.

Rules:
1. Use read-only diagnostic tools whenever evidence would help.
2. You may open approved apps and folders when the user clearly asks.
3. Closing an app or changing or repairing the PC requires approval.
4. Before requesting a repair, explain what it changes and any downside.
5. Never claim an action succeeded unless the tool result confirms it.
6. Never run arbitrary commands, scripts, PowerShell supplied by the user, or unknown executables.
7. Do not disable antivirus, security protections, Windows Update, or account security.
8. Do not request passwords, API keys, recovery keys, or secrets.
9. Keep answers practical and easy to follow.
10. When an app name is not approved, call available_apps and explain the available choices.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "system_summary",
            "description": "Collect Windows, CPU, RAM, boot-time, and disk-space information.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "top_processes",
            "description": "List processes using the most CPU and memory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30}
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recent_windows_errors",
            "description": "Read recent Windows Application or System errors and warnings.",
            "parameters": {
                "type": "object",
                "properties": {
                    "log_name": {
                        "type": "string",
                        "enum": ["Application", "System"],
                    },
                    "hours": {"type": "integer", "minimum": 1, "maximum": 168},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["log_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "nvidia_status",
            "description": "Read NVIDIA model, driver, temperature, utilization, and VRAM usage.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_diagnostic_report",
            "description": "Save a combined diagnostic report in Documents.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "available_apps",
            "description": "List applications that Martin AI is approved to control.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "app_status",
            "description": "Check whether an approved application is currently running.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Approved application name, such as Steam or Discord.",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open an approved application when the user asks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Approved application name, such as Steam or Chrome.",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_user_folder",
            "description": "Open an approved user folder such as Downloads, Documents, or Pictures.",
            "parameters": {
                "type": "object",
                "properties": {
                    "folder_name": {
                        "type": "string",
                        "enum": [
                            "desktop",
                            "downloads",
                            "documents",
                            "pictures",
                            "videos",
                            "music",
                            "reports",
                        ],
                    }
                },
                "required": ["folder_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_app",
            "description": "CONTROL ACTION: Close an approved application. Unsaved work could be lost, so approval is required.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Approved application name to close.",
                    }
                },
                "required": ["app_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "flush_dns",
            "description": "REPAIR: Flush the Windows DNS cache. Requires approval.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reset_microsoft_store_cache",
            "description": "REPAIR: Reset Microsoft Store cache. Requires approval.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clear_shader_caches",
            "description": "REPAIR: Delete DirectX and NVIDIA shader caches. Requires approval.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_sfc_scan",
            "description": "REPAIR: Run System File Checker. Requires approval.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_dism_restorehealth",
            "description": "REPAIR: Run DISM RestoreHealth. Requires approval.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def post_json(url: str, payload: dict[str, Any], timeout: int = 900) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name in READ_ONLY_TOOLS:
        try:
            return READ_ONLY_TOOLS[name](**arguments)
        except TypeError as exc:
            return {"success": False, "error": f"Invalid arguments for {name}: {exc}"}
        except Exception as exc:
            return {"success": False, "error": f"{name} failed: {exc}"}

    if name in REPAIR_TOOLS:
        console.print(
            Panel(
                f"[bold yellow]Requested action:[/bold yellow] {name}\n"
                f"Arguments: {json.dumps(arguments)}\n\n"
                "Type [bold]APPROVE[/bold] to run it. Anything else cancels.",
                title="Approval Required",
                border_style="yellow",
            )
        )
        if Prompt.ask("Approval").strip() != "APPROVE":
            return {
                "success": False,
                "cancelled": True,
                "message": "The user cancelled the action.",
            }
        try:
            return REPAIR_TOOLS[name](**arguments)
        except TypeError as exc:
            return {"success": False, "error": f"Invalid arguments for {name}: {exc}"}
        except Exception as exc:
            return {"success": False, "error": f"{name} failed: {exc}"}

    return {"success": False, "error": f"Unknown tool: {name}"}


def unload_model(base_url: str, model: str) -> None:
    try:
        post_json(
            f"{base_url}/api/generate",
            {"model": model, "prompt": "", "keep_alive": 0, "stream": False},
            timeout=30,
        )
    except Exception:
        pass


def ask_ollama(
    base_url: str,
    model: str,
    history: list[dict[str, Any]],
    keep_alive: str,
) -> str:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history]

    for _ in range(8):
        response = post_json(
            f"{base_url}/api/chat",
            {
                "model": model,
                "messages": messages,
                "tools": TOOLS,
                "stream": False,
                "think": False,
                "keep_alive": keep_alive,
                "options": {"temperature": 0.2},
            },
        )

        message = response.get("message", {})
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            return message.get("content", "").strip() or "I did not receive a usable response."

        messages.append(message)

        for call in tool_calls:
            function = call.get("function", {})
            name = function.get("name", "")
            arguments = function.get("arguments") or {}

            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}

            if not isinstance(arguments, dict):
                arguments = {}

            result = execute_tool(name, arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_name": name,
                    "content": json.dumps(result, default=str),
                }
            )

    return "I reached the tool-call limit. Try asking me to check one part of the problem at a time."


def run() -> None:
    load_dotenv()
    base_url = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
    model = os.getenv("OLLAMA_MODEL", "qwen3:8b")
    keep_alive = os.getenv("OLLAMA_KEEP_ALIVE", "5m")

    console.print(
        Panel(
            "[bold]Martin AI Local[/bold]\n"
            f"Model: {model}\n"
            "The model unloads from GPU memory after each completed answer.\n\n"
            "Try: Open Steam, Is Discord running?, Open Downloads, or Check my GPU.\n\n"
            "Type a PC problem, [bold]unload[/bold], or [bold]exit[/bold].",
            border_style="cyan",
        )
    )

    history: list[dict[str, Any]] = []

    while True:
        user_text = Prompt.ask("\n[bold cyan]You[/bold cyan]").strip()
        if not user_text:
            continue

        command = user_text.lower()

        if command in {"exit", "quit"}:
            unload_model(base_url, model)
            console.print("[green]Martin AI closed.[/green]")
            break

        if command == "unload":
            unload_model(base_url, model)
            console.print("[green]Model unloaded from memory.[/green]")
            continue

        if command in {"clear", "clear history", "reset conversation"}:
            history.clear()
            console.print("[green]Conversation history cleared.[/green]")
            continue

        history.append({"role": "user", "content": user_text})

        try:
            with console.status("Loading local AI and investigating..."):
                answer = ask_ollama(base_url, model, history, keep_alive)
        except urllib.error.URLError:
            console.print(
                "[red]Martin AI could not reach Ollama.[/red]\n"
                f"Open Ollama and confirm `ollama run {model}` works."
            )
            history.pop()
            continue
        except Exception as exc:
            console.print(f"[red]Martin AI error:[/red] {exc}")
            history.pop()
            continue
        finally:
            unload_model(base_url, model)

        history.append({"role": "assistant", "content": answer})
        console.print(Panel(answer, title="Martin AI", border_style="green"))
        console.print("[dim]Local model unloaded; GPU memory released.[/dim]")


if __name__ == "__main__":
    run()
