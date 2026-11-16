# Local Linux Hardware Assistant

A dependency-free, local CLI that reads selected Linux hardware details and
answers common CPU, RAM, storage, and GPU questions. It uses no LLM, sends no
hardware data over the network, and does not execute user-supplied commands.
The responses are deliberately deterministic: missing or unavailable
information is reported as unknown rather than inferred.

## Run

Requires Python 3.10 or later on Linux.

```bash
cd project-2-linux-hardware-assistant
python3 assistant.py
```

Ask questions such as “How much RAM do I have?”, “Can I add more RAM?”, “Is my
SSD NVMe?”, “What CPU do I have?”, or “What GPU do I have?”. Type `quit` to exit.
Follow-up questions such as “and can I add more?” reuse the previous topic.

## Inspection and safety

The assistant has a fixed inspection-tool registry:

| Purpose | Allow-listed tool |
| --- | --- |
| CPU | `lscpu` |
| RAM fallback | `free -b`; prefers read-only `/proc/meminfo` |
| Disk devices | `lsblk --json` with fixed output columns |
| Graphics devices | `lspci` |
| Firmware memory slots/capacity | `dmidecode --type memory` |

Commands are run as argument arrays with `shell=False`, no stdin, a timeout,
an output-size check, and a fixed locale. Only the intent router can select tools;
user text is never interpolated into a command or path. The tools inspect
system state and do not change it.

The optional `dmidecode` inspection is run as the current user only. The
assistant never requests a password, calls `sudo`, or retries with elevated
privileges. Firmware slot and maximum-capacity data may therefore be
unavailable; the assistant says so instead of guessing. For NVMe identification
it uses the reported transport or device name; it does not equate every SSD
with NVMe.

## Limitations

* This first version uses deterministic intent routing and templates, not an
  LLM. That keeps answers grounded in collected values and avoids sending
  private machine details to a model service.
* Hardware and firmware reporting varies by kernel, distribution, permissions,
  and installed utilities. Missing tools or fields produce an explicit
  unknown/unavailable response.
* A reported empty slot and board maximum are firmware data, not a guarantee of
  physical compatibility. Check the laptop model's service manual before
  purchasing memory.

## Tests

The test suite can run on non-Linux development machines; it uses fixtures and
does not inspect the developer's hardware:

```bash
python -m unittest -v
```
