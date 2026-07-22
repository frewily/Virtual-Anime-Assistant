import time
import psutil
from datetime import timedelta


class SystemMonitor:
    def get_status(self) -> dict:
        cpu_percent = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        uptime_seconds = int(time.time() - psutil.boot_time())
        uptime = str(timedelta(seconds=uptime_seconds)).split(".")[0]

        return {
            "cpu": {
                "percent": cpu_percent,
                "cores": psutil.cpu_count(logical=True),
            },
            "memory": {
                "total": _format_bytes(memory.total),
                "used": _format_bytes(memory.used),
                "percent": memory.percent,
            },
            "uptime": str(uptime),
        }


def _format_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
