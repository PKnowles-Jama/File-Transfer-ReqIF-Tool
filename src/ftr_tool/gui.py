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
from .configuration import ItemTypeConfiguration, JamaConfiguration
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
        self.import_path: Path | None = None
        self.changes: list[Change] = []
        self._last_url_value = ""
        self._last_auth_type = "OAuth"
        self._url_locked = False
        self._url_update_pending = False
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.info_server = tk.StringVar(value="Server: not authenticated")
        self.info_auth = tk.StringVar(value="Authentication: OAuth")
        self.info_project = tk.StringVar(value="Project: none selected")
        self.info_current = tk.StringVar(value="Loaded config: none")
        self.info_import = tk.StringVar(value="Import file: none")
        self.info_changes = tk.StringVar(value="Selected changes: 0 of 0 actionable, 0 notices (0 total)")
        self._build()
        self._update_labels()
        self._refresh_project_info()
        self.after(100, self._drain_messages)

    def _build(self) -> None:
        self.master.title("File Transfer ReqIF Tool")
        self.master.geometry("980x720")
        self.master.minsize(820, 600)
        self.grid(sticky="nsew")
        self.master.rowconfigure(0, weight=1); self.master.columnconfigure(0, weight=1)
        self.columnconfigure(1, weight=1); self.rowconfigure(6, weight=1)

        ttk.Label(self, text="Jama Connect URL").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        self.url = ttk.Entry(self); self.url.grid(row=0, column=1, columnspan=1, sticky="ew", pady=4)
        self.url.insert(0, "https://your-instance.jamacloud.com")
        self.update_url_button = ttk.Button(self, text="Update URL", state="disabled", command=self._request_url_update)
        self.update_url_button.grid(row=0, column=2, padx=6, pady=4)
        self.submit_url_button = ttk.Button(self, text="Submit URL", state="disabled", command=self._submit_url_update)
        self.submit_url_button.grid(row=0, column=3, padx=(0, 2), pady=4)

        self.auth_type = tk.StringVar(value="OAuth")
        ttk.Label(self, text="Authentication").grid(row=1, column=0, sticky="w", pady=4)
        auth = ttk.Combobox(self, textvariable=self.auth_type, values=("OAuth", "Basic"), state="readonly", width=14)
        auth.grid(row=1, column=1, sticky="w", pady=4); auth.bind("<<ComboboxSelected>>", self._on_auth_mode_updated)
        self.first_label = ttk.Label(self, text="Client ID"); self.first_label.grid(row=2, column=0, sticky="w", pady=4)
        self.first = ttk.Entry(self); self.first.grid(row=2, column=1, sticky="ew", pady=4)
        self.second_label = ttk.Label(self, text="Client secret"); self.second_label.grid(row=3, column=0, sticky="w", pady=4)
        self.second = ttk.Entry(self, show="•"); self.second.grid(row=3, column=1, sticky="ew", pady=4)
        self.auth_button = ttk.Button(self, text="Authenticate", command=self._authenticate); self.auth_button.grid(row=2, column=2, rowspan=2, padx=10)

        ttk.Label(self, text="Project").grid(row=4, column=0, sticky="w", pady=(12, 4))
        self.project = ttk.Combobox(self, state="disabled"); self.project.grid(row=4, column=1, sticky="ew", pady=(12, 4))
        self.load_button = ttk.Button(self, text="Load project configuration", state="disabled", command=self._load_project)
        self.load_button.grid(row=4, column=2, padx=10, pady=(12, 4))

        relevant = ttk.Labelframe(self, text="Relevant info", padding=8)
        relevant.grid(row=5, column=0, columnspan=4, sticky="ew", pady=(8, 4))
        ttk.Label(relevant, textvariable=self.info_server).grid(row=0, column=0, sticky="w")
        ttk.Label(relevant, textvariable=self.info_auth).grid(row=1, column=0, sticky="w")
        ttk.Label(relevant, textvariable=self.info_project).grid(row=2, column=0, sticky="w")
        ttk.Label(relevant, textvariable=self.info_current).grid(row=3, column=0, sticky="w")
        ttk.Label(relevant, textvariable=self.info_import).grid(row=4, column=0, sticky="w")
        ttk.Label(relevant, textvariable=self.info_changes).grid(row=5, column=0, sticky="w")

        pane = ttk.Panedwindow(self, orient=tk.VERTICAL); pane.grid(row=6, column=0, columnspan=4, sticky="nsew", pady=10)
        changes_frame = ttk.Labelframe(pane, text="Configuration changes", padding=8)
        self.tree = ttk.Treeview(changes_frame, columns=("apply", "type", "description"), show="headings", selectmode="extended")
        self.tree.heading("apply", text="Apply"); self.tree.heading("type", text="Type"); self.tree.heading("description", text="Description")
        self.tree.column("apply", width=60, anchor="center"); self.tree.column("type", width=145); self.tree.column("description", width=650)
        self.tree.pack(fill="both", expand=True); self.tree.bind("<Double-1>", self._toggle_change)
        pane.add(changes_frame, weight=3)
        status_frame = ttk.Labelframe(pane, text="Status", padding=8)
        self.status = tk.Text(status_frame, height=9, state="disabled", wrap="word"); self.status.pack(fill="both", expand=True)
        pane.add(status_frame, weight=1)

        actions = ttk.Frame(self); actions.grid(row=7, column=0, columnspan=4, sticky="ew")
        self.export_button = ttk.Button(actions, text="Export configuration", state="disabled", command=self._export); self.export_button.pack(side="left")
        self.export_selected_button = ttk.Button(actions, text="Export selected (Yes) changes", state="disabled", command=self._export_selected)
        self.export_selected_button.pack(side="left", padx=(8, 0))
        self.import_button = ttk.Button(actions, text="Choose import file", state="disabled", command=self._choose_import); self.import_button.pack(side="left", padx=8)
        self.apply_button = ttk.Button(actions, text="Apply selected changes", state="disabled", command=self._apply); self.apply_button.pack(side="left")
        ttk.Button(actions, text="Exit", command=self.master.destroy).pack(side="right")
        ttk.Label(actions, text=f"Log: {self.log_path}").pack(side="right", padx=12)
        self.project.bind("<<ComboboxSelected>>", lambda _: self._refresh_project_info())
        self._last_url_value = self.url.get().strip()
        self._last_auth_type = self.auth_type.get()

    def _set_url_locked(self, locked: bool) -> None:
        self._url_locked = locked
        self.url.configure(state="readonly" if locked else "normal")

    def _request_url_update(self) -> None:
        if not self._url_locked:
            return
        self._set_url_locked(False)
        self.submit_url_button.configure(state="normal")
        self._url_update_pending = True
        self._reset_after_connection_context_change("Jama Connect URL")
        self._append_status("Jama Connect URL unlocked. Submit the updated URL before authenticating again.")

    def _submit_url_update(self) -> None:
        value = self.url.get().strip()
        if not value:
            messagebox.showerror("Invalid URL", "Jama Connect URL is required.")
            return
        self._last_url_value = value
        self._url_update_pending = False
        self.submit_url_button.configure(state="disabled")
        self._set_url_locked(True)
        self._append_status("Updated Jama Connect URL submitted.")

    def _clear_auth_and_project_inputs(self) -> None:
        self.first.delete(0, "end")
        self.second.delete(0, "end")
        self.project.set("")

    def _reset_after_connection_context_change(self, trigger: str) -> None:
        self.client = None
        self.service = None
        self.projects = []
        self.current = None
        self._clear_auth_and_project_inputs()
        self._clear_compared_changes()
        self.project.configure(values=(), state="disabled")
        self.load_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self.export_selected_button.configure(state="disabled")
        self.import_button.configure(state="disabled")
        self.info_server.set("Server: not authenticated")
        self._update_loaded_summary()
        self._refresh_project_info()
        self._append_status(f"{trigger} updated. Cleared credentials, project selection, and configuration changes.")

    def _on_auth_mode_updated(self, _: tk.Event) -> None:
        previous = self._last_auth_type
        self._update_labels()
        current = self.auth_type.get()
        if current == previous:
            return
        self._last_auth_type = current
        self._reset_after_connection_context_change("Authentication mode")

    def _update_labels(self) -> None:
        oauth = self.auth_type.get() == "OAuth"
        self.first_label.configure(text="Client ID" if oauth else "Username")
        self.second_label.configure(text="Client secret" if oauth else "Password")
        self.info_auth.set(f"Authentication: {'OAuth client credentials' if oauth else 'Basic username/password'}")

    def _refresh_project_info(self) -> None:
        index = self.project.current() if hasattr(self, "project") else -1
        if index < 0 or index >= len(self.projects):
            self.info_project.set("Project: none selected")
            return
        selected = self.projects[index]
        self.info_project.set(f"Project: {selected.name} (key {selected.key}, ID {selected.id})")

    def _update_loaded_summary(self) -> None:
        if not self.current:
            self.info_current.set("Loaded config: none")
            return
        self.info_current.set(
            "Loaded config: "
            f"{len(self.current.itemTypes)} item types, "
            f"{len(self.current.picklists)} picklists, "
            f"{len(self.current.relationshipRules)} relationship rules, "
            f"{len(self.current.users)} users"
        )

    def _update_change_selection_summary(self) -> None:
        rows = [self.tree.set(iid, "apply") for iid in self.tree.get_children()]
        total = len(rows)
        notices = sum(value == "Notice" for value in rows)
        selected = sum(value == "Yes" for value in rows)
        actionable = sum(value in ("Yes", "No") for value in rows)
        self.info_changes.set(f"Selected changes: {selected} of {actionable} actionable, {notices} notices ({total} total)")

    def _update_import_summary(self) -> None:
        if not self.imported:
            self.info_import.set("Import file: none")
            return
        file_label = self.import_path.name if self.import_path else "unknown file"
        source = self.imported.source
        project = source.get("projectName") or source.get("projectKey") or source.get("projectId") or "unknown"
        self.info_import.set(
            "Import file: "
            f"{file_label}, format {self.imported.format} v{self.imported.version}, "
            f"source {project}, exported {self.imported.exportedAt or 'unknown time'}"
        )

    def _clear_compared_changes(self) -> None:
        self.imported = None
        self.import_path = None
        self.changes = []
        self.tree.delete(*self.tree.get_children())
        self.apply_button.configure(state="disabled")
        self.export_selected_button.configure(state="disabled")
        self._update_import_summary()
        self._update_change_selection_summary()

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
        if busy:
            self.update_url_button.configure(state="disabled")
            self.submit_url_button.configure(state="disabled")
        elif self.client is not None:
            self.update_url_button.configure(state="normal")
            if self._url_update_pending:
                self.submit_url_button.configure(state="normal")

    def _post_status(self, text: str) -> None: self.messages.put(("status", text))
    def _append_status(self, text: str) -> None:
        self.status.configure(state="normal"); self.status.insert("end", text + "\n"); self.status.see("end"); self.status.configure(state="disabled")

    def _authenticate(self) -> None:
        if self._url_update_pending:
            messagebox.showwarning("Submit URL", "Submit the updated Jama Connect URL before authenticating.")
            return
        self._last_url_value = self.url.get().strip()
        self._last_auth_type = self.auth_type.get()
        if self.client is not None:
            self._clear_compared_changes()
        def operation() -> object:
            client = JamaClient(self.url.get(), self.logger)
            if self.auth_type.get() == "OAuth": client.authenticate_oauth(self.first.get(), self.second.get())
            else: client.authenticate_basic(self.first.get(), self.second.get())
            return client, client.list_projects()
        def done(value: object) -> None:
            self.client, self.projects = value  # type: ignore[misc]
            self.service = ConfigurationService(self.client, self.logger)
            self.current = None
            self._clear_compared_changes()
            self.export_button.configure(state="disabled")
            self.export_selected_button.configure(state="disabled")
            self.import_button.configure(state="disabled")
            self.info_server.set(f"Server: {self.client.base_url}")
            self._set_url_locked(True)
            self.update_url_button.configure(state="normal")
            self.submit_url_button.configure(state="disabled")
            self._update_loaded_summary()
            labels = [f"{p.name} ({p.key}, ID {p.id})" for p in self.projects]
            self.project.configure(values=labels, state="readonly"); self.project.current(0 if labels else -1)
            self.load_button.configure(state="normal" if labels else "disabled")
            self._refresh_project_info()
            self._append_status(f"Authenticated. Retrieved {len(labels)} projects.")
        self._run(operation, done)

    def _selected_project(self) -> Project:
        index = self.project.current()
        if index < 0: raise ValidationError("Select a project.")
        return self.projects[index]

    def _load_project(self) -> None:
        project = self._selected_project()
        self._clear_compared_changes()
        def operation() -> object:
            assert self.service
            return self.service.export_project(project, self._post_status)
        def done(value: object) -> None:
            self.current = value  # type: ignore[assignment]
            self.export_button.configure(state="normal"); self.import_button.configure(state="normal")
            self._update_loaded_summary()
        self._run(operation, done)

    def _export(self) -> None:
        assert self.current
        source = self.current.source
        default = f"{source.get('projectKey', 'jama')}-configuration-{datetime.now():%Y%m%d-%H%M%S}.json"
        path = Path.cwd() / default
        self.current.save(path); self.logger.info("Exported configuration to %s", path); self._append_status(f"Configuration saved: {path.resolve()}")

    def _export_selected(self) -> None:
        if not self.imported or not self.service:
            raise ValidationError("Choose an import file and compare changes first.")
        selected = [self.changes[int(iid)] for iid in self.tree.get_children() if self.tree.set(iid, "apply") == "Yes"]
        if not selected:
            messagebox.showinfo("No selected changes", "Mark at least one change as Yes before exporting a selected configuration.")
            return
        selected_configuration = self.service.export_selected_configuration(self.imported, selected)
        source = selected_configuration.source
        default = f"{source.get('projectKey', 'jama')}-selected-configuration-{datetime.now():%Y%m%d-%H%M%S}.json"
        path = Path.cwd() / default
        selected_configuration.save(path)
        self.logger.info("Exported selected configuration to %s", path)
        self._append_status(f"Selected configuration saved: {path.resolve()}")

    def _choose_import(self) -> None:
        if not self.current: raise ValidationError("Load the target project configuration first.")
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path: return
        import_path = Path(path)
        try:
            self.imported = JamaConfiguration.load(import_path); assert self.service
            if not self._collect_item_type_associations(self.imported, self.current):
                return
            self.changes = self.service.compare(self.imported, self.current)
        except (ValidationError, JamaError) as exc:
            messagebox.showerror("Invalid configuration", str(exc)); return
        self.import_path = import_path
        self._update_import_summary()
        self.tree.delete(*self.tree.get_children())
        for index, change in enumerate(self.changes):
            apply = "Notice" if change.kind in ("relationship_notice", "instance_item_type_notice", "instance_picklist_notice") else "Yes"
            self.tree.insert("", "end", iid=str(index), values=(apply, change.kind.replace("_", " ").title(), change.message))
        self.apply_button.configure(state="normal" if self.changes else "disabled")
        self.export_selected_button.configure(state="normal" if self.changes else "disabled")
        self._update_change_selection_summary()
        self._append_status(f"Compared {path}: {len(self.changes)} proposed changes. Double-click a row to include/exclude it.")

    def _collect_item_type_associations(self, desired: JamaConfiguration, current: JamaConfiguration) -> bool:
        assert self.service
        suggestions = self.service.association_candidates(desired, current)
        if not suggestions:
            return True
        for source_name, options in suggestions.items():
            selection = self._prompt_item_type_association(source_name, options)
            if selection is False:
                self._append_status("Import cancelled while selecting item type association mappings.")
                return False
            source = next((row for row in desired.itemTypes if row.name == source_name), None)
            if source is None:
                continue
            self.service.apply_item_type_association(source, selection if isinstance(selection, ItemTypeConfiguration) else None)
            if isinstance(selection, ItemTypeConfiguration):
                self._append_status(f"Mapped '{source_name}' to '{selection.name}' for field comparison.")
            else:
                self._append_status(f"Keeping '{source_name}' as a new item type to create in the target project.")
        return True

    def _prompt_item_type_association(
        self,
        source_name: str,
        options: list[ItemTypeConfiguration],
    ) -> ItemTypeConfiguration | None | bool:
        dialog = tk.Toplevel(self)
        dialog.title("Associate missing item type")
        dialog.transient(self.master)
        dialog.resizable(False, False)
        dialog.grab_set()

        frame = ttk.Frame(dialog, padding=12)
        frame.grid(sticky="nsew")
        ttk.Label(
            frame,
            text=(
                f"The imported item type '{source_name}' does not exist in this target.\n"
                "Choose an existing target item type to compare/add fields against,\n"
                "or keep it as a new item type to create."
            ),
            justify="left",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        values = ["Create as new item type (no mapping)"] + [row.name for row in options]
        selected = tk.StringVar(value=values[0])
        chooser = ttk.Combobox(frame, values=values, textvariable=selected, state="readonly", width=54)
        chooser.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        chooser.current(0)

        result: dict[str, ItemTypeConfiguration | None | bool] = {"value": None}

        def accept() -> None:
            name = selected.get().strip()
            if name == values[0]:
                result["value"] = None
            else:
                result["value"] = next((row for row in options if row.name == name), None)
            dialog.destroy()

        def cancel() -> None:
            result["value"] = False
            dialog.destroy()

        ttk.Button(frame, text="Use selection", command=accept).grid(row=2, column=0, sticky="w")
        ttk.Button(frame, text="Cancel import", command=cancel).grid(row=2, column=2, sticky="e")
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.wait_window()
        return result["value"]

    def _toggle_change(self, _: tk.Event) -> None:
        for item in self.tree.selection():
            values = list(self.tree.item(item, "values"))
            if values[0] != "Notice":
                values[0] = "No" if values[0] == "Yes" else "Yes"
                self.tree.item(item, values=values)
        self._update_change_selection_summary()

    def _apply(self) -> None:
        project = self._selected_project()
        selected = [self.changes[int(iid)] for iid in self.tree.get_children() if self.tree.set(iid, "apply") in ("Yes", "Notice")]
        updates = [
            row
            for row in selected
            if row.kind not in ("relationship_notice", "instance_item_type_notice", "instance_picklist_notice")
        ]
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
