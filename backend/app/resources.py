import shutil
import subprocess
import sys
import re
import time
from collections import deque
from threading import Lock


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


class ResourcePreflight:
    """Configurable advisory safeguards based on recent local resource samples."""

    def __init__(self, settings):
        self.settings = settings
        self._pressure_samples = deque(maxlen=max(1, settings.preflight_pressure_consecutive_samples))
        self._lock = Lock()
        self._last_pressure_sample_at = None

    def assess(self, snapshot: dict, *, observed_at: float | None = None) -> list[str]:
        warnings = []
        swap_used = snapshot.get("swap", {}).get("used_bytes")
        max_swap = self.settings.preflight_max_swap_gb * 1024**3
        if swap_used is not None and swap_used > max_swap:
            warnings.append(
                f"Swap use is {swap_used / 1024**3:.1f} GB, above the configured "
                f"{self.settings.preflight_max_swap_gb:g} GB advisory threshold."
            )
        disk_free = snapshot.get("disk_free_bytes")
        min_disk = self.settings.preflight_min_disk_gb * 1024**3
        if disk_free is not None and disk_free < min_disk:
            warnings.append(
                f"Free disk space is {disk_free / 1024**3:.1f} GB, below the configured "
                f"{self.settings.preflight_min_disk_gb:g} GB advisory threshold."
            )

        pressure = snapshot.get("memory_pressure")
        free_percent = pressure.get("free_percent") if pressure else None
        with self._lock:
            if free_percent is None:
                self._pressure_samples.clear()
                self._last_pressure_sample_at = None
            else:
                sample_time = time.monotonic() if observed_at is None else observed_at
                interval = self.settings.preflight_pressure_sample_interval_seconds
                if self._last_pressure_sample_at is None or sample_time - self._last_pressure_sample_at >= interval:
                    self._pressure_samples.append(free_percent <= self.settings.preflight_pressure_free_percent_max)
                    self._last_pressure_sample_at = sample_time
            required = max(1, self.settings.preflight_pressure_consecutive_samples)
            if len(self._pressure_samples) >= required and all(list(self._pressure_samples)[-required:]):
                warnings.append(
                    f"Memory pressure has remained above the configured safeguard for "
                    f"{required} consecutive samples ({self.settings.preflight_pressure_free_percent_max}% free or less)."
                )
        return warnings
