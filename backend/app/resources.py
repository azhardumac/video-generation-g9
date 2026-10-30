import shutil
import subprocess
import sys
import re


def resource_snapshot(path: str) -> dict:
    usage = shutil.disk_usage(path)
    memory = {"total_bytes": None, "available_bytes": None}
    if sys.platform == "darwin":
        try:
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=2, check=True).stdout.strip())
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2, check=True).stdout
            page_size = 4096
            available_pages = 0
            for line in vm.splitlines():
                if line.startswith(("Pages free:", "Pages inactive:", "Pages speculative:")):
                    available_pages += int(line.split(":", 1)[1].strip().split()[0].rstrip("."))
            memory = {"total_bytes": total, "available_bytes": available_pages * page_size}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    swap = {"total_bytes": None, "used_bytes": None}
    pressure = None
    if sys.platform == "darwin":
        try:
            result = subprocess.run(["sysctl", "vm.swapusage"], capture_output=True, text=True, timeout=2, check=True).stdout
            match = re.search(r"total = ([\d.]+)([MG])\s+used = ([\d.]+)([MG])", result)
            if match:
                factor = {"M": 1024**2, "G": 1024**3}
                swap = {"total_bytes": int(float(match.group(1)) * factor[match.group(2)]),
                        "used_bytes": int(float(match.group(3)) * factor[match.group(4)])}
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        try:
            result = subprocess.run(["memory_pressure", "-Q"], capture_output=True, text=True, timeout=2, check=True).stdout
            match = re.search(r"System-wide memory free percentage:\s*(\d+)%", result)
            if match:
                pressure = {"free_percent": int(match.group(1)), "level": "normal" if int(match.group(1)) > 15 else "high"}
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    return {
        "disk_total_bytes": usage.total,
        "disk_free_bytes": usage.free,
        "memory": memory,
        "swap": swap,
        "memory_pressure": pressure,
    }
