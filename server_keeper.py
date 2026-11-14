"""服务守护工具：健康检查、僵尸进程清理、后台启动、日志记录。"""
from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
LOGS_DIR = ROOT / "logs"
PORT = 8000
URL = f"http://127.0.0.1:{PORT}"
SERVER_LOG = LOGS_DIR / "server.log"


def setup_logger(name: str, path: Path) -> logging.Logger:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


def http_ok(timeout: float = 2.0) -> bool:
    """只有 HTTP 真正返回 200 才算服务正常，避免死端口误判。"""
    try:
        with urlopen(URL + "/", timeout=timeout) as resp:
            return resp.status == 200
    except (URLError, OSError):
        return False


def port_open(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=timeout):
            return True
    except OSError:
        return False


def kill_listener() -> None:
    """清理占用 8000 端口但不健康的进程。"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return
    pids: set[str] = set()
    for line in out.splitlines():
        if ":8000" in line and "LISTENING" in line.upper():
            parts = line.split()
            if parts:
                pids.add(parts[-1])
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", pid, "/F"], capture_output=True, text=True, timeout=15)
        except Exception:  # noqa: BLE001
            pass


def start_server() -> bool:
    """后台启动 run.py 并等待 HTTP 就绪，日志写入 logs/server.log。"""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    exe = sys.executable or "python"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        with open(SERVER_LOG, "a", encoding="utf-8") as log:
            proc = subprocess.Popen(
                [exe, str(ROOT / "run.py")],
                cwd=str(ROOT),
                creationflags=flags,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
    except Exception as e:  # noqa: BLE001
        setup_logger("keeper", LOGS_DIR / "watchdog.log").exception("start failed: %s", e)
        return False

    for _ in range(80):
        if http_ok(timeout=1.5):
            return True
        if proc.poll() is not None:
            break
        time.sleep(0.5)
    return http_ok(timeout=1.5)


def ensure_server_running() -> bool:
    """确保服务健康：正常则不操作；端口被死进程占用则清理后重启。"""
    if http_ok():
        return True
    if port_open():
        kill_listener()
        time.sleep(1.0)
    return start_server()
