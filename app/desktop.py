"""Desktop launcher window for Egypt Lead Scraper (no black console)."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

APP_TITLE = "Egypt Lead Scraper"
LEADS_PATH = "/leads"


def _ensure_project_root() -> Path:
    """Keep cwd on the project folder so .env, data/, exports/ resolve."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates = [exe_dir.parent.parent, exe_dir.parent, exe_dir]
    else:
        candidates = [Path(__file__).resolve().parent.parent]

    for root in candidates:
        if (root / "config" / "egypt_locations.yaml").exists() or (root / ".env").exists():
            os.chdir(root)
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            return root

    root = candidates[0]
    os.chdir(root)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


_PROJECT_ROOT = _ensure_project_root()

from app.config import settings  # noqa: E402


def _log_path() -> Path:
    path = Path(settings.project_root) / "data" / "launcher.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def log(msg: str) -> None:
    try:
        with _log_path().open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _base_url() -> str:
    return f"http://{settings.host}:{settings.port}"


def _leads_url() -> str:
    return f"{_base_url()}{LEADS_PATH}"


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        return sock.connect_ex((host, port)) == 0


def wait_until_up(timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if port_in_use(settings.host, settings.port):
            return True
        time.sleep(0.3)
    return False


class DesktopApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.geometry("440x360")
        self.root.minsize(380, 300)
        self.root.configure(bg="#f4f1eb")

        self._server_thread: threading.Thread | None = None
        self._server_error: str | None = None
        self._status = tk.StringVar(value="Ready")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(200, self._auto_start)

    def _build_ui(self) -> None:
        pad = {"padx": 20, "pady": 8}

        header = tk.Frame(self.root, bg="#f4f1eb")
        header.pack(fill="x", **pad)

        tk.Label(
            header,
            text=APP_TITLE,
            font=("Segoe UI", 16, "bold"),
            bg="#f4f1eb",
            fg="#1c1917",
        ).pack(anchor="w")

        tk.Label(
            header,
            text="Local lead collector · runs on this PC",
            font=("Segoe UI", 10),
            bg="#f4f1eb",
            fg="#78716c",
        ).pack(anchor="w")

        card = tk.Frame(self.root, bg="#fffcf7", highlightbackground="#e7e0d4", highlightthickness=1)
        card.pack(fill="both", expand=True, padx=20, pady=8)

        tk.Label(
            card,
            textvariable=self._status,
            font=("Segoe UI", 11),
            bg="#fffcf7",
            fg="#0f766e",
            wraplength=360,
            justify="left",
        ).pack(anchor="w", padx=16, pady=(16, 8))

        btn_row = tk.Frame(card, bg="#fffcf7")
        btn_row.pack(fill="x", padx=16, pady=8)

        style_kwargs = {
            "font": ("Segoe UI", 10, "bold"),
            "relief": "flat",
            "cursor": "hand2",
            "padx": 14,
            "pady": 10,
        }

        self.open_btn = tk.Button(
            btn_row,
            text="Open Leads",
            bg="#0f766e",
            fg="white",
            activebackground="#0d5f59",
            activeforeground="white",
            command=self.open_leads,
            **style_kwargs,
        )
        self.open_btn.pack(fill="x", pady=(0, 8))

        self.retry_btn = tk.Button(
            btn_row,
            text="Retry start",
            bg="#e7e0d4",
            fg="#1c1917",
            activebackground="#d6cec0",
            command=self.start_server,
            **style_kwargs,
        )
        self.retry_btn.pack(fill="x", pady=(0, 8))

        self.stop_btn = tk.Button(
            btn_row,
            text="Stop app",
            bg="#e7e0d4",
            fg="#1c1917",
            activebackground="#d6cec0",
            command=self.stop_app,
            **style_kwargs,
        )
        self.stop_btn.pack(fill="x")

        tk.Label(
            card,
            text="Keep this window open while scraping.\nClosing it stops the app.",
            font=("Segoe UI", 9),
            bg="#fffcf7",
            fg="#78716c",
            justify="left",
        ).pack(anchor="w", padx=16, pady=(4, 16))

    def _set_status(self, text: str) -> None:
        self._status.set(text)
        self.root.update_idletasks()

    def _auto_start(self) -> None:
        log(f"auto_start cwd={os.getcwd()} frozen={getattr(sys, 'frozen', False)}")
        if port_in_use(settings.host, settings.port):
            self._set_status("Already running — opening Leads…")
            self.open_leads()
            self._set_status("Running")
            return
        self.start_server()

    def start_server(self) -> None:
        if port_in_use(settings.host, settings.port):
            self._set_status("Running — opening Leads…")
            self.open_leads()
            return

        if self._server_thread and self._server_thread.is_alive():
            self._set_status("Still starting…")
            return

        self._server_error = None
        self._set_status("Starting… (first launch can take up to a minute)")

        def _run() -> None:
            try:
                import asyncio
                import logging

                import uvicorn

                from app.main import app as fastapi_app

                # pythonw / windowed .exe have no console — uvicorn crashes on stdout.isatty()
                if sys.stdout is None:
                    sys.stdout = open(os.devnull, "w", encoding="utf-8")
                if sys.stderr is None:
                    sys.stderr = open(os.devnull, "w", encoding="utf-8")

                logging.getLogger("uvicorn").setLevel(logging.WARNING)
                logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
                logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
                log("uvicorn starting")

                config = uvicorn.Config(
                    fastapi_app,
                    host=settings.host,
                    port=settings.port,
                    log_level="warning",
                    access_log=False,
                    log_config=None,
                )
                server = uvicorn.Server(config)
                asyncio.run(server.serve())
            except Exception:
                self._server_error = traceback.format_exc()
                log(f"SERVER CRASH:\n{self._server_error}")

        self._server_thread = threading.Thread(target=_run, daemon=True, name="uvicorn")
        self._server_thread.start()

        def _wait_and_open() -> None:
            if wait_until_up(60):
                self.root.after(0, lambda: self._set_status("Running — Leads opened"))
                self.root.after(0, self.open_leads)
                return

            err = self._server_error or "Server did not become ready in time."
            short = err.strip().splitlines()[-1] if err else "Unknown error"
            log(f"wait failed: {short}")
            self.root.after(
                0,
                lambda: self._set_status(f"Start failed: {short}\nSee data/launcher.log"),
            )

        threading.Thread(target=_wait_and_open, daemon=True, name="wait-up").start()

    def open_leads(self) -> None:
        if not port_in_use(settings.host, settings.port):
            self._set_status("Server is not running yet. Click Retry start.")
            return
        webbrowser.open(_leads_url())

    def stop_app(self) -> None:
        if messagebox.askyesno(APP_TITLE, "Stop Egypt Lead Scraper and close?"):
            self.root.destroy()

    def _on_close(self) -> None:
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


def main() -> None:
    settings.exports_dir.mkdir(parents=True, exist_ok=True)
    settings.profiles_dir.mkdir(parents=True, exist_ok=True)
    (Path(settings.project_root) / "data").mkdir(parents=True, exist_ok=True)
    log("launcher main()")
    DesktopApp().run()


if __name__ == "__main__":
    main()
