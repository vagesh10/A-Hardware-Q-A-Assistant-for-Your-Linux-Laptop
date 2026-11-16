from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


COMMANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "cpu": ("lscpu", ()),
    "memory": ("free", ("-b",)),
    "storage": (
        "lsblk",
        ("--json", "--bytes", "-o", "NAME,TYPE,SIZE,ROTA,TRAN,MODEL,FSTYPE,MOUNTPOINTS"),
    ),
    "pci": ("lspci", ()),
    "memory_modules": ("dmidecode", ("--type", "memory")),
}
COMMAND_TIMEOUT_SECONDS = 5
MAX_OUTPUT_BYTES = 256 * 1024
GIB = 1024**3
MIB = 1024**2


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0
    error: str | None = None


def run_allowlisted_command(name: str) -> CommandResult:
    """Run only a fixed, read-only command with fixed arguments; never invoke a shell."""
    spec = COMMANDS.get(name)
    if spec is None:
        raise ValueError(f"Unknown inspection tool: {name}")
    executable, arguments = spec
    executable_path = shutil.which(executable)
    if executable_path is None:
        return CommandResult(error=f"{executable} is not installed")
    try:
        result = subprocess.run(
            [executable_path, *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except subprocess.TimeoutExpired:
        return CommandResult(error=f"{executable} timed out")
    except OSError as exc:
        return CommandResult(error=f"{executable} could not be run: {exc}")

    stdout = result.stdout[:MAX_OUTPUT_BYTES]
    stderr = result.stderr[:MAX_OUTPUT_BYTES]
    if result.returncode != 0:
        detail = stderr.strip() or f"exited with status {result.returncode}"
        return CommandResult(stdout=stdout, stderr=stderr, returncode=result.returncode, error=detail)
    if len(result.stdout) > MAX_OUTPUT_BYTES:
        return CommandResult(error=f"{executable} output exceeded the safety limit")
    return CommandResult(stdout=stdout, stderr=stderr, returncode=result.returncode)


def parse_lscpu(output: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip().lower()] = value.strip()
    return {
        "model": fields.get("model name", ""),
        "architecture": fields.get("architecture", ""),
        "logical_cpus": fields.get("cpu(s)", ""),
        "sockets": fields.get("socket(s)", ""),
        "cores_per_socket": fields.get("core(s) per socket", ""),
        "threads_per_core": fields.get("thread(s) per core", ""),
    }


def parse_meminfo(output: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if not separator:
            continue
        match = re.match(r"\s*(\d+)\s*(kB)?", value, re.IGNORECASE)
        if match:
            amount = int(match.group(1))
            values[key] = amount * 1024 if match.group(2) else amount
    return values


def parse_free(output: str) -> dict[str, int]:
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0].lower().rstrip(":") == "mem" and len(fields) >= 7:
            try:
                return {
                    "total": int(fields[1]),
                    "used": int(fields[2]),
                    "free": int(fields[3]),
                    "available": int(fields[6]),
                }
            except ValueError:
                return {}
    return {}


def parse_lsblk(output: str) -> list[dict[str, object]]:
    try:
        devices = json.loads(output).get("blockdevices", [])
    except (json.JSONDecodeError, AttributeError):
        return []

    result: list[dict[str, object]] = []

    def visit(device: object) -> None:
        if not isinstance(device, dict):
            return
        if device.get("type") == "disk":
            size = device.get("size")
            try:
                size_bytes = int(size) if size is not None else None
            except (TypeError, ValueError):
                size_bytes = None
            transport = str(device.get("tran") or "").lower()
            name = str(device.get("name") or "")
            rotational = device.get("rota")
            if transport == "nvme" or name.lower().startswith("nvme"):
                media = "NVMe"
            elif rotational is False or rotational == 0:
                media = "SSD (bus type unknown)"
            elif rotational is True or rotational == 1:
                media = "rotational drive"
            else:
                media = "unknown"
            result.append(
                {
                    "name": name or "unknown",
                    "model": str(device.get("model") or "").strip() or None,
                    "size_bytes": size_bytes,
                    "transport": transport or None,
                    "media": media,
                }
            )
        children = device.get("children", [])
        if isinstance(children, list):
            for child in children:
                visit(child)

    if isinstance(devices, list):
        for device in devices:
            visit(device)
    return result


def parse_lspci_gpus(output: str) -> list[str]:
    markers = ("vga compatible controller", "3d controller", "display controller")
    return [
        line.strip()
        for line in output.splitlines()
        if any(marker in line.lower() for marker in markers)
    ]


def parse_dmi_memory(output: str) -> dict[str, object]:
    sections = re.split(r"(?m)^\s*Memory Device\s*$", output)
    modules: list[dict[str, object]] = []
    maximum_capacity: str | None = None
    for section in sections:
        lines = [line.strip() for line in section.splitlines() if ":" in line]
        if not lines:
            continue
        fields = dict(line.split(":", 1) for line in lines)
        if "Maximum Capacity" in fields and maximum_capacity is None:
            maximum_capacity = fields["Maximum Capacity"].strip() or None
        if "Size" in fields:
            modules.append(
                {
                    "size": fields["Size"].strip(),
                    "locator": fields.get("Locator", "").strip() or None,
                }
            )
    installed = [module for module in modules if module["size"] != "No Module Installed"]
    empty_slots = sum(module["size"] == "No Module Installed" for module in modules)
    return {
        "modules": modules,
        "installed_count": len(installed),
        "empty_slots": empty_slots,
        "maximum_capacity": maximum_capacity,
    }


class HardwareInspector:
    def __init__(
        self,
        command_runner: Callable[[str], CommandResult] = run_allowlisted_command,
        proc_root: Path = Path("/proc"),
    ):
        self.command_runner = command_runner
        self.proc_root = proc_root

    def cpu(self) -> dict[str, str] | None:
        result = self.command_runner("cpu")
        if result.error:
            return None
        data = parse_lscpu(result.stdout)
        return data if any(data.values()) else None

    def memory(self) -> dict[str, int] | None:
        try:
            meminfo = (self.proc_root / "meminfo").read_text(encoding="ascii")
            data = parse_meminfo(meminfo)
        except (OSError, UnicodeError):
            data = {}
        if "MemTotal" in data:
            return {
                "total": data["MemTotal"],
                "available": data.get("MemAvailable", 0),
            }
        result = self.command_runner("memory")
        data = parse_free(result.stdout) if not result.error else {}
        return data or None

    def storage(self) -> list[dict[str, object]] | None:
        result = self.command_runner("storage")
        devices = parse_lsblk(result.stdout) if not result.error else []
        return devices or None

    def gpu(self) -> list[str] | None:
        result = self.command_runner("pci")
        devices = parse_lspci_gpus(result.stdout) if not result.error else []
        return devices or None

    def memory_modules(self) -> tuple[dict[str, object] | None, str | None]:
        result = self.command_runner("memory_modules")
        if result.error:
            return None, result.error
        details = parse_dmi_memory(result.stdout)
        if not details["modules"] and details["maximum_capacity"] is None:
            return None, "Firmware memory details were not reported"
        return details, None


def _gib(value: int) -> str:
    return f"{value / GIB:.1f} GiB"


def answer_question(
    question: str, inspector: HardwareInspector, previous_topic: str | None = None
) -> tuple[str, str | None]:
    lowered = question.lower()
    if any(word in lowered for word in ("gpu", "graphics", "video card", "graphics card")):
        topic = "gpu"
    elif any(word in lowered for word in ("ssd", "nvme", "disk", "storage", "drive")):
        topic = "storage"
    elif any(word in lowered for word in ("ram", "memory", "dimms", "dimm")):
        topic = "memory"
    elif any(word in lowered for word in ("cpu", "processor", "cores")):
        topic = "cpu"
    elif previous_topic and any(
        phrase in lowered for phrase in ("and ", "what about", "how much", "more", "it?")
    ):
        topic = previous_topic
    else:
        return (
            "I can check CPU, RAM, memory slots, storage, and GPU information. "
            "Try asking, for example, “How much RAM do I have?”",
            previous_topic,
        )

    if topic == "cpu":
        cpu = inspector.cpu()
        if not cpu:
            return "I couldn't read CPU information from this system.", topic
        parts = [value for value in (cpu["model"], cpu["architecture"]) if value]
        if cpu["logical_cpus"]:
            parts.append(f"{cpu['logical_cpus']} logical CPUs")
        if not parts:
            return "The CPU model and topology weren't reported by lscpu.", topic
        return "CPU: " + ", ".join(parts) + ".", topic

    if topic == "memory":
        upgrade_question = any(
            word in lowered
            for word in ("add", "upgrade", "slot", "maximum", "max capacity", "expand")
        )
        if upgrade_question:
            details, error = inspector.memory_modules()
            if not details:
                installed = inspector.memory()
                base = f"Your installed RAM is {_gib(installed['total'])}. " if installed else ""
                return (
                    base
                    + "I couldn't determine the available slots or maximum supported capacity"
                    + (f": {error}." if error else ".")
                    + " I did not attempt to run with elevated privileges.",
                    topic,
                )
            parts: list[str] = []
            installed = inspector.memory()
            if installed:
                parts.append(f"You have {_gib(installed['total'])} of installed RAM")
            parts.append(
                f"{details['installed_count']} populated module(s) and {details['empty_slots']} empty slot(s)"
            )
            if details["maximum_capacity"]:
                parts.append(f"firmware reports a maximum capacity of {details['maximum_capacity']}")
            else:
                parts.append("firmware did not report the maximum supported capacity")
            return "; ".join(parts) + ".", topic
        memory = inspector.memory()
        if not memory:
            return "I couldn't read the installed RAM amount from /proc/meminfo or free.", topic
        return f"This system has {_gib(memory['total'])} of RAM.", topic

    if topic == "storage":
        devices = inspector.storage()
        if not devices:
            return "I couldn't read storage-device details; lsblk may be unavailable.", topic
        descriptions = []
        for device in devices:
            label = str(device["name"])
            if device["model"]:
                label += f" ({device['model']})"
            size = device["size_bytes"]
            if isinstance(size, int):
                label += f", {size / GIB:.1f} GiB"
            label += f", {device['media']}"
            descriptions.append(label)
        return "Storage: " + "; ".join(descriptions) + ".", topic

    devices = inspector.gpu()
    if not devices:
        return "I couldn't identify a GPU; lspci may be missing or this system may not report PCI graphics devices.", topic
    return "Graphics device(s): " + "; ".join(devices) + ".", topic


def main() -> None:
    if os.name != "posix" or not Path("/proc").is_dir():
        print("This assistant is designed for Linux systems with /proc.")
        return
    inspector = HardwareInspector()
    previous_topic: str | None = None
    print("Local Linux hardware assistant. Type 'quit' to exit. No data is sent over the network.")
    while True:
        try:
            question = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break
        if question.lower() in {"quit", "exit"}:
            print("Goodbye.")
            break
        if not question:
            continue
        response, previous_topic = answer_question(question, inspector, previous_topic)
        print(f"Assistant: {response}")


if __name__ == "__main__":
    main()
