from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .client import JamaClient, Project
from .configuration import JamaConfiguration
from .errors import JamaError, ValidationError
from .service import Change, ConfigurationService


class Application(ttk.Frame):
    def __init__(self, master: tk.Tk, logger: logging.Logger, log_path: Path):
        super().__init__(master, padding=12)
        self.master, self.logger, self.log_path = master, logger, log_path
        self.client: JamaClient | None = None
        self.service: ConfigurationService | None = None
        self.projects: list[Project] = []
        self.current: JamaConfiguration | None = None
        self.imported: JamaConfiguration | None = None
        self.changes: list[Change] = []
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self._build()
        self.after(100, self._drain_messages)

    def _build(self) -> None:
        self.master.title("File Transfer ReqIF Tool")
        self.master.geometry("980x720")
        self.master.minsize(820, 600)
        self.grid(sticky="nsew")
        self.master.rowconfigure(0, weight=1); self.master.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1); self.rowconfigure(5, weight=1)

        ttk.Label(self, text="Jama Connect URL").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.url = ttk.Entry(self); self.url.grid(row=0, column=1, columnspan=3, sticky="ew", pady=4)
        self.url.insert(0, "https://your-instance.jamacloud.com")

        self.auth_type = tk.StringVar(value="OAuth")
        ttk.Label(self, text="Authentication").grid(row=1, column=0, sticky="w", pady=4)
        auth = ttk.Combobox(self, textvariable=self.auth_type, values=("OAuth", "Basic"), state="readonly", width=14)
        auth.grid(row=1, column=1, sticky="w", pady=4); auth.bind("<<ComboboxSelected>>", lambda _: self._update_labels())
        self.first_label = ttk.Label(self, text="Client ID"); self.first_label.grid(row=2, column=0, sticky="w", pady=4)
        self.first = ttk.Entry(self); self.first.grid(row=2, column=1, sticky="ew", pady=4)
        self.second_label = ttk.Label(self, text="Client secret"); self.second_label.grid(row=3, column=0, sticky="w", pady=4)
        self.second = ttk.Entry(self, show="•"); self.second.grid(row=3, column=1, sticky="ew", pady=4)
        self.auth_button = ttk.Button(self, text="Authenticate", command=self._authenticate); self.auth_button.grid(row=2, column=2, rowspan=2, padx=10)

        ttk.Label(self, text="Project").grid(row=4, column=0, sticky="w", pady=(12, 4))
        self.project = ttk.Combobox(self, state="disabled"); self.project.grid(row=4, column=1, sticky="ew", pady=(12, 4))
        self.load_button = ttk.Button(self, text="Load project configuration", state="disabled", command=self._load_project)
        self.load_button.grid(row=4, column=2, padx=10, pady=(12, 4))

        pane = ttk.Panedwindow(self, orient=tk.VERTICAL); pane.grid(row=5, column=0, columnspan=4, sticky="nsew", pady=10)
        changes_frame = ttk.Labelframe(pane, text="Configuration changes", padding=8)
        self.tree = ttk.Treeview(changes_frame, columns=("apply", "type", "description"), show="headings", selectmode="extended")
        self.tree.heading("apply", text="Apply"); self.tree.heading("type", text="Type"); self.tree.heading("description", text="Description")
        self.tree.column("apply", width=60, anchor="center"); self.tree.column("type", width=145); self.tree.column("description", width=650)
        self.tree.pack(fill="both", expand=True); self.tree.bind("<Double-1>", self._toggle_change)
        pane.add(changes_frame, weight=3)
        status_frame = ttk.Labelframe(pane, text="Status", padding=8)
        self.status = tk.Text(status_frame, height=9, state="disabled", wrap="word"); self.status.pack(fill="both", expand=True)
        pane.add(status_frame, weight=1)

        actions = ttk.Frame(self); actions.grid(row=6, column=0, columnspan=4, sticky="ew")
        self.export_button = ttk.Button(actions, text="Export configuration", state="disabled", command=self._export); self.export_button.pack(side="left")
        self.import_button = ttk.Button(actions, text="Choose import file", state="disabled", command=self._choose_import); self.import_button.pack(side="left", padx=8)
        self.apply_button = ttk.Button(actions, text="Apply selected changes", state="disabled", command=self._apply); self.apply_button.pack(side="left")
        ttk.Button(actions, text="Exit", command=self.master.destroy).pack(side="right")
        ttk.Label(actions, text=f"Log: {log_path}").pack(side="right", padx=12)

    def _update_labels(self) -> None:
        oauth = self.auth_type.get() == "OAuth"
        self.first_label.configure(text="Client ID" if oauth else "Username")
        self.second_label.configure(text="Client secret" if oauth else "Password")

    def _run(self, operation: Callable[[], object], done: Callable[[object], None]) -> None:
        self._set_busy(True)
        def worker() -> None:
            try: self.messages.put(("done", (done, operation())))
            except Exception as exc: self.messages.put(("error", exc))
        threading.Thread(target=worker, daemon=True).start()

    def _drain_messages(self) -> None:
        try:
            while True:
                kind, value = self.messages.get_nowait()
                if kind == "status": self._append_status(str(value))
                elif kind == "done":
                    callback, result = value  # type: ignore[misc]
                    self._set_busy(False); callback(result)
                else:
                    self._set_busy(False); self.logger.exception("Operation failed", exc_info=value)
                    self._append_status(f"ERROR: {value}"); messagebox.showerror("Operation failed", str(value))
        except queue.Empty: pass
        self.after(100, self._drain_messages)

    def _set_busy(self, busy: bool) -> None:
        self.master.configure(cursor="watch" if busy else "")
        self.auth_button.configure(state="disabled" if busy else "normal")

    def _post_status(self, text: str) -> None: self.messages.put(("status", text))
    def _append_status(self, text: str) -> None:
        self.status.configure(state="normal"); self.status.insert("end", text + "\n"); self.status.see("end"); self.status.configure(state="disabled")

    def _authenticate(self) -> None:
        def operation() -> object:
            client = JamaClient(self.url.get(), self.logger)
            if self.auth_type.get() == "OAuth": client.authenticate_oauth(self.first.get(), self.second.get())
            else: client.authenticate_basic(self.first.get(), self.second.get())
            return client, client.list_projects()
        def done(value: object) -> None:
            self.client, self.projects = value  # type: ignore[misc]
            self.service = ConfigurationService(self.client, self.logger)
            labels = [f"{p.name} ({p.key}, ID {p.id})" for p in self.projects]
            self.project.configure(values=labels, state="readonly"); self.project.current(0 if labels else -1)
            self.load_button.configure(state="normal" if labels else "disabled")
            self._append_status(f"Authenticated. Retrieved {len(labels)} projects.")
        self._run(operation, done)

    def _selected_project(self) -> Project:
        index = self.project.current()
        if index < 0: raise ValidationError("Select a project.")
        return self.projects[index]

    def _load_project(self) -> None:
        project = self._selected_project()
        def operation() -> object:
            assert self.service
            return self.service.export_project(project, self._post_status)
        def done(value: object) -> None:
            self.current = value  # type: ignore[assignment]
            self.export_button.configure(state="normal"); self.import_button.configure(state="normal")
        self._run(operation, done)

    def _export(self) -> None:
        assert self.current
        source = self.current.source
        default = f"{source.get('projectKey', 'jama')}-configuration-{datetime.now():%Y%m%d-%H%M%S}.json"
        path = Path.cwd() / default
        self.current.save(path); self.logger.info("Exported configuration to %s", path); self._append_status(f"Configuration saved: {path.resolve()}")

    def _choose_import(self) -> None:
        if not self.current: raise ValidationError("Load the target project configuration first.")
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path: return
        try:
            self.imported = JamaConfiguration.load(Path(path)); assert self.service
            self.changes = self.service.compare(self.imported, self.current)
        except (ValidationError, JamaError) as exc:
            messagebox.showerror("Invalid configuration", str(exc)); return
        self.tree.delete(*self.tree.get_children())
        for index, change in enumerate(self.changes):
            apply = "Notice" if change.kind == "relationship_notice" else "Yes"
            self.tree.insert("", "end", iid=str(index), values=(apply, change.kind.replace("_", " ").title(), change.message))
        self.apply_button.configure(state="normal" if self.changes else "disabled")
        self._append_status(f"Compared {path}: {len(self.changes)} proposed changes. Double-click a row to include/exclude it.")

    def _toggle_change(self, _: tk.Event) -> None:
        for item in self.tree.selection():
            values = list(self.tree.item(item, "values"))
            if values[0] != "Notice":
                values[0] = "No" if values[0] == "Yes" else "Yes"
                self.tree.item(item, values=values)

    def _apply(self) -> None:
        project = self._selected_project()
        selected = [self.changes[int(iid)] for iid in self.tree.get_children() if self.tree.set(iid, "apply") in ("Yes", "Notice")]
        updates = [row for row in selected if row.kind != "relationship_notice"]
        if updates and not messagebox.askyesno("Confirm changes", f"Apply {len(updates)} administrative configuration changes to {project.name}? This modifies Jama."):
            return
        def operation() -> object:
            assert self.service
            return self.service.apply(project.id, selected, self._post_status)
        def done(value: object) -> None:
            outcomes = value  # type: ignore[assignment]
            for line in outcomes: self._append_status(line)
            failed = sum(str(line).startswith("FAILED") for line in outcomes)
            messagebox.showinfo("Import complete", f"Import finished with {failed} failed change(s). Review the status and log for details.")
        self._run(operation, done)
