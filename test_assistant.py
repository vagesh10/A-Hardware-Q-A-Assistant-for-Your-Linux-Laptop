import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant import (
    COMMANDS,
    CommandResult,
    HardwareInspector,
    answer_question,
    parse_dmi_memory,
    parse_free,
    parse_lsblk,
    parse_lscpu,
    parse_lspci_gpus,
    parse_meminfo,
    run_allowlisted_command,
)


class ParserTests(unittest.TestCase):
    def test_parse_lscpu_fields(self):
        result = parse_lscpu(
            "Architecture:                    x86_64\n"
            "CPU(s):                          8\n"
            "Model name:                      Example CPU\n"
            "Thread(s) per core:              2\n"
        )
        self.assertEqual(result["architecture"], "x86_64")
        self.assertEqual(result["logical_cpus"], "8")
        self.assertEqual(result["model"], "Example CPU")

    def test_parse_meminfo_kilobytes(self):
        result = parse_meminfo("MemTotal:       16384000 kB\nMemAvailable:    8000000 kB\n")
        self.assertEqual(result["MemTotal"], 16384000 * 1024)
        self.assertEqual(result["MemAvailable"], 8000000 * 1024)

    def test_parse_free_fallback(self):
        result = parse_free(
            "               total        used        free      shared  buff/cache   available\n"
            "Mem:     16000000000 4000000000 6000000000 100000000 6000000000 11000000000\n"
        )
        self.assertEqual(result["total"], 16000000000)
        self.assertEqual(result["available"], 11000000000)

    def test_parse_lsblk_and_detect_nvme_without_guessing_ssd_bus(self):
        output = json.dumps(
            {
                "blockdevices": [
                    {"name": "nvme0n1", "type": "disk", "size": 512000000000, "tran": "nvme"},
                    {"name": "sda", "type": "disk", "size": 1000000000000, "rota": False},
                    {"name": "sda1", "type": "part", "size": 1000000000000},
                ]
            }
        )
        result = parse_lsblk(output)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["media"], "NVMe")
        self.assertEqual(result[1]["media"], "SSD (bus type unknown)")

    def test_parse_gpus_only(self):
        output = (
            "00:02.0 VGA compatible controller: Example Integrated Graphics\n"
            "01:00.0 3D controller: Example Discrete GPU\n"
            "00:14.0 USB controller: Example USB\n"
        )
        self.assertEqual(len(parse_lspci_gpus(output)), 2)

    def test_parse_dmi_reports_empty_slots_and_capacity(self):
        output = (
            "Physical Memory Array\n\tMaximum Capacity: 64 GB\n"
            "Memory Device\n\tSize: 16 GB\n\tLocator: DIMM_A1\n"
            "Memory Device\n\tSize: No Module Installed\n\tLocator: DIMM_B1\n"
        )
        result = parse_dmi_memory(output)
        self.assertEqual(result["installed_count"], 1)
        self.assertEqual(result["empty_slots"], 1)
        self.assertEqual(result["maximum_capacity"], "64 GB")


class SafetyTests(unittest.TestCase):
    def test_only_fixed_commands_and_arguments_are_run_without_shell(self):
        with patch("assistant.shutil.which", return_value="C:\\tools\\lscpu.exe"), patch(
            "assistant.subprocess.run",
            return_value=type(
                "ProcessResult", (), {"stdout": "CPU(s): 4", "stderr": "", "returncode": 0}
            )(),
        ) as run:
            self.assertEqual(run_allowlisted_command("cpu").stdout, "CPU(s): 4")
        arguments = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(arguments, ["C:\\tools\\lscpu.exe"])
        self.assertFalse(options["shell"])
        self.assertEqual(options["stdin"], -3)
        with self.assertRaises(ValueError):
            run_allowlisted_command("arbitrary")

    def test_all_allowlisted_commands_are_read_only_inspection_tools(self):
        self.assertEqual(
            set(COMMANDS),
            {"cpu", "memory", "storage", "pci", "memory_modules"},
        )
        self.assertEqual(COMMANDS["memory_modules"], ("dmidecode", ("--type", "memory")))

    def test_upgrade_answer_does_not_escalate_privileges(self):
        def fake_runner(name):
            if name == "memory_modules":
                return CommandResult(error="dmidecode: permission denied")
            return CommandResult()

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "meminfo").write_text("MemTotal: 16777216 kB\n", encoding="ascii")
            inspector = HardwareInspector(fake_runner, Path(directory))
            response, topic = answer_question("Can I add more RAM?", inspector)
        self.assertEqual(topic, "memory")
        self.assertIn("available slots or maximum supported capacity", response)
        self.assertIn("did not attempt to run with elevated privileges", response)


class AssistantTests(unittest.TestCase):
    def test_answers_ram_and_follows_up_about_upgrade(self):
        def fake_runner(name):
            if name == "memory_modules":
                return CommandResult(
                    stdout=(
                        "Physical Memory Array\n\tMaximum Capacity: 64 GB\n"
                        "Memory Device\n\tSize: 16 GB\n"
                        "Memory Device\n\tSize: No Module Installed\n"
                    )
                )
            return CommandResult()

        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "meminfo").write_text("MemTotal: 16777216 kB\n", encoding="ascii")
            inspector = HardwareInspector(fake_runner, Path(directory))
            first, topic = answer_question("How much RAM do I have?", inspector)
            follow_up, next_topic = answer_question("And can I add more?", inspector, topic)
        self.assertIn("16.0 GiB", first)
        self.assertIn("1 populated module(s) and 1 empty slot(s)", follow_up)
        self.assertIn("64 GB", follow_up)
        self.assertEqual(topic, next_topic)

    def test_unknown_question_does_not_run_tools(self):
        def forbidden_runner(_):
            self.fail("An unrelated question must not inspect hardware")

        response, topic = answer_question("What is the weather?", HardwareInspector(forbidden_runner))
        self.assertIn("I can check CPU, RAM", response)
        self.assertIsNone(topic)

    def test_gpu_and_storage_questions_use_the_matching_tool(self):
        calls = []

        def fake_runner(name):
            calls.append(name)
            if name == "pci":
                return CommandResult(stdout="00:02.0 VGA compatible controller: Example GPU\n")
            if name == "storage":
                return CommandResult(
                    stdout=json.dumps(
                        {
                            "blockdevices": [
                                {"name": "nvme0n1", "type": "disk", "size": 1000000000, "tran": "nvme"}
                            ]
                        }
                    )
                )
            return CommandResult()

        inspector = HardwareInspector(fake_runner)
        gpu, _ = answer_question("What GPU do I have?", inspector)
        storage, _ = answer_question("Is my SSD NVMe?", inspector)
        self.assertIn("Example GPU", gpu)
        self.assertIn("NVMe", storage)
        self.assertEqual(calls, ["pci", "storage"])


if __name__ == "__main__":
    unittest.main()
