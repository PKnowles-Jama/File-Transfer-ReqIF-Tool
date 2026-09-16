from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from .client import JamaClient, Project
from .configuration import ItemTypeConfiguration, JamaConfiguration, PicklistConfiguration
from .errors import JamaError, ValidationError
from .service import Change, ConfigurationService


class ScrollableContentFrame(ttk.Frame):
    def __init__(self, master: tk.Misc, *, padding: int | tuple[int, ...] = 0):
        super().__init__(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.content = ttk.Frame(self.canvas, padding=padding)
        self._content_window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.content.bind("<Configure>", self._on_content_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        for widget in (self.canvas, self.content):
            widget.bind("<Enter>", self._bind_mousewheel, add="+")
            widget.bind("<Leave>", self._unbind_mousewheel, add="+")

    def _on_content_configure(self, _: tk.Event) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._content_window, width=event.width)

    def _bind_mousewheel(self, _: tk.Event) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

    def _unbind_mousewheel(self, _: tk.Event) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event: tk.Event) -> None:
        delta = getattr(event, "delta", 0)
        if not delta:
            return
        steps = -int(delta / 120) if delta % 120 == 0 else (-1 if delta > 0 else 1)
        self.canvas.yview_scroll(steps, "units")


class Application(ttk.Frame):
    def __init__(self, master: tk.Tk, logger: logging.Logger, log_path: Path):
        super().__init__(master, padding=12)
        self.master, self.logger, self.log_path = master, logger, log_path
        self.client: JamaClient | None = None
        self.service: ConfigurationService | None = None
        self.projects: list[Project] = []
        self.selected_project: Project | None = None
        self.current: JamaConfiguration | None = None
        self.imported: JamaConfiguration | None = None
        self.import_path: Path | None = None
        self.changes: list[Change] = []
        self._last_url_value = ""
        self._last_auth_type = "OAuth"
        self._url_locked = False
        self._url_update_pending = False
        self.export_window: ExportWindow | None = None
        self.import_window: ImportWindow | None = None
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.info_server = tk.StringVar(value="Server: not authenticated")
        self.info_auth = tk.StringVar(value="Authentication: OAuth")
        self.info_project = tk.StringVar(value="Project context: choose in Export or Import GUI")
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

        ttk.Label(
            self,
            text="Project selection is available inside the Export GUI and Import GUI.",
            wraplength=620,
            justify="left",
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(12, 4))

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
        tree_scrollbar = ttk.Scrollbar(changes_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True); self.tree.bind("<Double-1>", self._toggle_change)
        tree_scrollbar.pack(side="right", fill="y")
        pane.add(changes_frame, weight=3)
        status_frame = ttk.Labelframe(pane, text="Status", padding=8)
        self.status = tk.Text(status_frame, height=9, state="disabled", wrap="word")
        status_scrollbar = ttk.Scrollbar(status_frame, orient="vertical", command=self.status.yview)
        self.status.configure(yscrollcommand=status_scrollbar.set)
        self.status.pack(side="left", fill="both", expand=True)
        status_scrollbar.pack(side="right", fill="y")
        pane.add(status_frame, weight=1)

        actions = ttk.Frame(self); actions.grid(row=7, column=0, columnspan=4, sticky="ew")
        self.export_button = ttk.Button(actions, text="Open Export GUI", state="disabled", command=self._open_export_gui); self.export_button.pack(side="left")
        self.import_gui_button = ttk.Button(actions, text="Open Import GUI", state="disabled", command=self._open_import_gui)
        self.import_gui_button.pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Exit", command=self.master.destroy).pack(side="right")
        ttk.Label(actions, text=f"Log: {self.log_path}").pack(side="right", padx=12)
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

    def _reset_after_connection_context_change(self, trigger: str) -> None:
        self.client = None
        self.service = None
        self.projects = []
        self.selected_project = None
        self.current = None
        self._clear_auth_and_project_inputs()
        self._clear_compared_changes()
        self.export_button.configure(state="disabled")
        self.import_gui_button.configure(state="disabled")
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
        if self.selected_project is None:
            self.info_project.set("Project context: choose in Export or Import GUI")
            return
        self.info_project.set(
            f"Project context: {self.selected_project.name} (key {self.selected_project.key}, ID {self.selected_project.id})"
        )

    def _project_labels(self) -> list[str]:
        return [f"{project.name} ({project.key}, ID {project.id})" for project in self.projects]

    def _project_index(self, project: Project | None) -> int | None:
        if project is None:
            return None
        for index, candidate in enumerate(self.projects):
            if candidate.id == project.id:
                return index
        return None

    def _project_from_index(self, index: int) -> Project:
        if index < 0 or index >= len(self.projects):
            raise ValidationError("Select a project.")
        return self.projects[index]

    def _default_project(self) -> Project:
        if self.selected_project is not None:
            index = self._project_index(self.selected_project)
            if index is not None:
                return self.projects[index]
        if not self.projects:
            raise ValidationError("No Jama projects are available for this account.")
        return self.projects[0]

    def _set_selected_project(self, project: Project | None) -> None:
        if project is not None and self.selected_project is not None and self.selected_project.id != project.id:
            self.current = None
            self._update_loaded_summary()
        self.selected_project = project
        self._refresh_project_info()

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
            self.selected_project = None
            self.current = None
            self._clear_compared_changes()
            self.export_button.configure(state="normal")
            self.import_gui_button.configure(state="normal")
            self.info_server.set(f"Server: {self.client.base_url}")
            self._set_url_locked(True)
            self.update_url_button.configure(state="normal")
            self.submit_url_button.configure(state="disabled")
            self._update_loaded_summary()
            labels = self._project_labels()
            self.import_gui_button.configure(state="normal" if labels else "disabled")
            self._refresh_project_info()
            self._append_status(f"Authenticated. Retrieved {len(labels)} projects.")
        self._run(operation, done)

    def _selected_project(self) -> Project:
        return self._default_project()

    def _load_project(self) -> None:
        project = self._selected_project()
        self._clear_compared_changes()
        def operation() -> object:
            assert self.service
            return self.service.export_project(project, self._post_status)
        def done(value: object) -> None:
            self.current = value  # type: ignore[assignment]
            self.export_button.configure(state="normal"); self.import_gui_button.configure(state="normal")
            self._update_loaded_summary()
        self._run(operation, done)

    def _load_current_configuration_sync(self, project: Project, *, force_refresh: bool = False) -> JamaConfiguration:
        current_project_id = self.current.source.get("projectId") if self.current is not None else None
        if self.current is not None and current_project_id == project.id and not force_refresh:
            return self.current
        if self.service is None:
            raise ValidationError("Authenticate before loading project configuration.")
        self._set_selected_project(project)
        current = self.service.export_project(project, self._append_status)
        self.current = current
        self._update_loaded_summary()
        return current

    def _open_export_gui(self) -> None:
        try:
            if self.service is None:
                raise ValidationError("Authenticate before opening the Export GUI.")
            if not self.projects:
                raise ValidationError("No Jama projects are available for this account.")
        except ValidationError as exc:
            messagebox.showerror("Export GUI", str(exc))
            return
        if self.export_window is not None and self.export_window.winfo_exists():
            self.export_window.focus_set()
            return
        self.export_window = ExportWindow(self)

    def _open_import_gui(self) -> None:
        try:
            if self.service is None:
                raise ValidationError("Authenticate before opening the Import GUI.")
            if not self.projects:
                raise ValidationError("No Jama projects are available for this account.")
        except ValidationError as exc:
            messagebox.showerror("Import GUI", str(exc))
            return
        if self.import_window is not None and self.import_window.winfo_exists():
            self.import_window.focus_set()
            return
        self.import_window = ImportWindow(self)

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
        if self.service is None:
            raise ValidationError("Authenticate before comparing or importing a configuration.")
        project = self._selected_project()
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path: return
        import_path = Path(path)
        try:
            self.current = self._load_current_configuration_sync(project, force_refresh=True)
            self._append_status("Loaded latest target project configuration for legacy compare/import.")
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


class ExportWindow(tk.Toplevel):
    def __init__(self, app: Application):
        super().__init__(app.master)
        self.app = app
        self.title("Export GUI")
        self.geometry("700x260")
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.columnconfigure(0, weight=1)
        scroller = ScrollableContentFrame(self, padding=12)
        scroller.grid(sticky="nsew")
        frame = scroller.content
        frame.columnconfigure(0, weight=1)
        self.project_name = tk.StringVar(value="")
        self.project_summary = tk.StringVar(value="Project: none selected")
        ttk.Label(frame, text="Export Project Configuration", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="Project", font=("Segoe UI", 10, "bold")).grid(row=1, column=0, sticky="w", pady=(8, 4))
        self.project_selector = ttk.Combobox(frame, textvariable=self.project_name, values=self.app._project_labels(), state="readonly", width=54)
        self.project_selector.grid(row=2, column=0, sticky="ew")
        self.project_selector.bind("<<ComboboxSelected>>", self._on_project_selected)
        ttk.Label(frame, textvariable=self.project_summary, wraplength=650, justify="left").grid(row=3, column=0, sticky="w", pady=(8, 12))
        ttk.Label(
            frame,
            text="Use this separate Export GUI to retrieve the latest project administrative configuration and save it to a configuration file.",
            wraplength=650,
            justify="left",
        ).grid(row=4, column=0, sticky="w", pady=(0, 12))
        actions = ttk.Frame(frame)
        actions.grid(row=5, column=0, sticky="w")
        ttk.Button(actions, text="Export latest configuration", command=self._export).pack(side="left")
        ttk.Button(actions, text="Go to Import GUI", command=self._go_to_import).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="Return to main", command=self._close).pack(side="left", padx=(8, 0))
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(frame, textvariable=self.status_var, wraplength=650, justify="left").grid(row=6, column=0, sticky="w", pady=(16, 0))
        default_index = self.app._project_index(self.app.selected_project)
        if default_index is None and self.app.projects:
            default_index = 0
        if default_index is not None:
            self.project_selector.current(default_index)
            self._sync_selected_project()

    def _selected_project(self) -> Project:
        return self.app._project_from_index(self.project_selector.current())

    def _sync_selected_project(self) -> None:
        project = self._selected_project()
        self.app._set_selected_project(project)
        self.project_summary.set(f"Project: {project.name} ({project.key}, ID {project.id})")

    def _on_project_selected(self, _: tk.Event) -> None:
        self._sync_selected_project()

    def _export(self) -> None:
        project = self._selected_project()
        current = self.app._load_current_configuration_sync(project, force_refresh=True)
        source = current.source
        default = f"{source.get('projectKey', 'jama')}-configuration-{datetime.now():%Y%m%d-%H%M%S}.json"
        path = Path.cwd() / default
        current.save(path)
        self.app.logger.info("Exported configuration to %s", path)
        self.app._append_status(f"Configuration saved: {path.resolve()}")
        self.status_var.set(f"Exported configuration to {path.resolve()}")

    def _go_to_import(self) -> None:
        self._close()
        self.app._open_import_gui()

    def _close(self) -> None:
        self.app.export_window = None
        self.destroy()


class ImportWindow(tk.Toplevel):
    PICKLIST_PLACEHOLDER = "Select Dropdown Option"
    CREATE_NEW = "Create New"

    def __init__(self, app: Application):
        super().__init__(app.master)
        self.app = app
        self.service = app.service
        self.project: Project | None = app._selected_project()
        self.title("Import GUI")
        self.geometry("1080x760")
        self.minsize(980, 700)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        if self.service is None:
            raise ValidationError("Authenticate before opening the Import GUI.")
        self.import_path: Path | None = None
        self.original_imported: JamaConfiguration | None = None
        self.current: JamaConfiguration | None = None
        self.working: JamaConfiguration | None = None
        self.picklist_controls: dict[str, dict[str, object]] = {}
        self.item_type_controls: dict[str, dict[str, object]] = {}
        self.existing_picklist_names: list[str] = []
        self.existing_item_type_names: list[str] = []
        self.picklist_targets: dict[str, tuple[object, bool]] = {}
        self.item_type_targets: dict[str, tuple[ItemTypeConfiguration, bool]] = {}
        self.outcomes: list[str] = []
        self.project_updates_path: Path | None = None
        self.project_updates_text = ""
        self.import_file_summary = tk.StringVar(value="Import file: none")
        self.app._set_selected_project(self.project)

        header = ttk.Frame(self, padding=12)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Import Configuration", font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w")
        setup = ttk.Frame(header)
        setup.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.choose_file_button = ttk.Button(setup, text="Choose Import File", command=self._choose_import_file)
        self.choose_file_button.grid(row=0, column=0, padx=(0, 12))
        ttk.Label(header, textvariable=self.import_file_summary, justify="left").grid(row=2, column=0, sticky="w", pady=(6, 0))
        nav = ttk.Frame(header)
        nav.grid(row=0, column=1, rowspan=3, sticky="e")
        ttk.Button(nav, text="Go to Export GUI", command=self._go_to_export).pack(side="left")
        ttk.Button(nav, text="Return to main", command=self._close).pack(side="left", padx=(8, 0))

        body = ttk.Frame(self, padding=(12, 0, 12, 12))
        body.grid(row=1, column=0, sticky="nsew")
        body.columnconfigure(0, weight=1)
        body.rowconfigure(2, weight=1)
        self.stage_title = tk.StringVar(value="")
        self.stage_hint = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.stage_title, font=("Segoe UI", 11, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(body, textvariable=self.stage_hint, wraplength=1000, justify="left").grid(row=1, column=0, sticky="nw", pady=(4, 8))
        self.stage_scroller = ScrollableContentFrame(body)
        self.stage_scroller.grid(row=2, column=0, sticky="nsew")
        self.content = self.stage_scroller.content
        self.content.columnconfigure(0, weight=1)
        self.footer = ttk.Frame(body)
        self.footer.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        status_frame = ttk.Frame(body)
        status_frame.grid(row=4, column=0, sticky="nsew", pady=(12, 0))
        status_frame.columnconfigure(0, weight=1)
        self.status = tk.Text(status_frame, height=10, state="disabled", wrap="word")
        status_scrollbar = ttk.Scrollbar(status_frame, orient="vertical", command=self.status.yview)
        self.status.configure(yscrollcommand=status_scrollbar.set)
        self.status.grid(row=0, column=0, sticky="nsew")
        status_scrollbar.grid(row=0, column=1, sticky="ns")
        self._append_status("Choose an import file to begin staged import.")
        self._show_setup_stage()

    def _set_setup_controls_enabled(self, enabled: bool) -> None:
        self.choose_file_button.configure(state="normal" if enabled else "disabled")

    def _show_setup_stage(self) -> None:
        self._clear_frame(self.content)
        self._clear_frame(self.footer)
        self.stage_title.set("Step 0: Choose import file")
        self.stage_hint.set(
            "Choose a configuration file, then start the staged Picklist and Item Type import workflow for Jama Connect instance updates."
        )
        ttk.Label(
            self.content,
            text="Choose an import file before continuing.",
            wraplength=980,
            justify="left",
        ).grid(row=0, column=0, sticky="w")
        ttk.Button(self.footer, text="Start Import", command=self._begin_import).pack(side="left")
        ttk.Button(self.footer, text="Exit", command=self._close).pack(side="left", padx=(8, 0))

    def _choose_import_file(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json"), ("All files", "*.*")])
        if not path:
            return
        self.import_path = Path(path)
        self.import_file_summary.set(f"Import file: {self.import_path.name}")
        self._append_status(f"Selected import file: {self.import_path}")

    def _begin_import(self) -> None:
        try:
            if self.import_path is None:
                raise ValidationError("Choose an import file before starting the Import GUI workflow.")
            assert self.project is not None
            imported = JamaConfiguration.load(self.import_path)
            current = self.app._load_current_configuration_sync(self.project, force_refresh=True)
        except (ValidationError, JamaError) as exc:
            messagebox.showerror("Import GUI", str(exc))
            return
        assert self.service is not None
        self.original_imported = imported
        self.current = current
        self.working = self.service.copy_configuration(imported)
        self.picklist_targets = self._picklist_targets()
        self.item_type_targets = self._item_type_targets()
        self.outcomes = []
        self.project_updates_path = None
        self.project_updates_text = ""
        self.app.imported = imported
        self.app.import_path = self.import_path
        self.app._update_import_summary()
        self._set_setup_controls_enabled(False)
        self._append_status("Loaded latest project configuration for import staging.")
        self._show_picklist_stage()

    def _append_status(self, text: str) -> None:
        self.status.configure(state="normal")
        self.status.insert("end", text + "\n")
        self.status.see("end")
        self.status.configure(state="disabled")
        self.app._append_status(text)

    def _close(self) -> None:
        self.app.import_window = None
        self.destroy()

    def _go_to_export(self) -> None:
        self._close()
        self.app._open_export_gui()

    def _clear_frame(self, frame: ttk.Frame) -> None:
        for child in frame.winfo_children():
            child.destroy()

    @staticmethod
    def _set_boolean_var(value: object, selected: bool) -> None:
        setter = getattr(value, "set", None)
        if callable(setter):
            setter(selected)

    def _set_picklist_option_selections(self, selected: bool) -> None:
        for control in self.picklist_controls.values():
            for value in control["option_vars"].values():  # type: ignore[index]
                self._set_boolean_var(value, selected)

    def _set_item_type_field_selections(self, selected: bool) -> None:
        for control in self.item_type_controls.values():
            for value in control["field_vars"].values():  # type: ignore[index]
                self._set_boolean_var(value, selected)

    def _set_picklist_mapping_enabled(self, enabled: bool) -> None:
        for source_name, control in self.picklist_controls.items():
            self._set_boolean_var(control["enabled"], enabled)
            self._refresh_picklist_detail(source_name)

    def _set_item_type_mapping_enabled(self, enabled: bool) -> None:
        for source_name, control in self.item_type_controls.items():
            self._set_boolean_var(control["enabled"], enabled)
            self._refresh_item_type_detail(source_name)

    def _deselect_all_item_types(self) -> None:
        self._set_item_type_mapping_enabled(False)

    def _select_all_item_types(self) -> None:
        self._set_item_type_mapping_enabled(True)

    @staticmethod
    def _actionable_changes(changes: list[Change]) -> list[Change]:
        return [change for change in changes if not change.kind.endswith("_notice")]

    def _picklist_targets(self) -> dict[str, tuple[object, bool]]:
        assert self.current is not None
        assert self.service is not None
        result: dict[str, tuple[object, bool]] = {}
        for row in self.current.picklists:
            result[row.name.casefold()] = (row, True)
        for key, row in self.service._instance_picklists_by_name().items():
            if key not in result:
                result[key] = (row, False)
        return result

    def _item_type_targets(self) -> dict[str, tuple[ItemTypeConfiguration, bool]]:
        assert self.current is not None
        assert self.service is not None
        result: dict[str, tuple[ItemTypeConfiguration, bool]] = {}
        for row in self.current.itemTypes:
            result[row.name.casefold()] = (row, True)
        for key, row in self.service._instance_item_types_by_name().items():
            if key not in result:
                result[key] = (row, False)
        return result

    def _exact_picklist_target(self, source_name: str) -> tuple[PicklistConfiguration, bool] | None:
        target = self.picklist_targets.get(source_name.casefold())
        if target is None:
            return None
        row, in_project = target
        return row, in_project

    def _exact_item_type_target(self, source_name: str) -> tuple[ItemTypeConfiguration, bool] | None:
        return self.item_type_targets.get(source_name.casefold())

    def _resolved_picklist_selection(self, source_name: str) -> str:
        selection = self.picklist_controls[source_name]["selected"].get()  # type: ignore[index]
        if selection != self.PICKLIST_PLACEHOLDER:
            return selection
        exact_target = self._exact_picklist_target(source_name)
        return exact_target[0].name if exact_target is not None else selection

    def _resolved_item_type_selection(self, source_name: str) -> str:
        selection = self.item_type_controls[source_name]["selected"].get()  # type: ignore[index]
        if selection != self.PICKLIST_PLACEHOLDER:
            return selection
        exact_target = self._exact_item_type_target(source_name)
        return exact_target[0].name if exact_target is not None else selection

    def _uses_manual_picklist_mapping(self, source_name: str, selection: str) -> bool:
        return selection not in {self.PICKLIST_PLACEHOLDER, self.CREATE_NEW} and selection.casefold() != source_name.casefold()

    def _uses_manual_item_type_mapping(self, source_name: str, selection: str) -> bool:
        return selection not in {self.PICKLIST_PLACEHOLDER, self.CREATE_NEW} and selection.casefold() != source_name.casefold()

    def _create_overview_panel(self, parent: ttk.Frame, title: str, lines: list[str], row: int) -> None:
        panel = tk.Frame(parent, bg="#eef6ff", bd=1, relief="solid", padx=10, pady=8)
        panel.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        panel.grid_columnconfigure(0, weight=1)
        tk.Label(panel, text=title, bg="#eef6ff", fg="#163a63", font=("Segoe UI", 10, "bold"), anchor="w", justify="left").grid(row=0, column=0, sticky="w")
        for index, line in enumerate(lines, start=1):
            tk.Label(panel, text=line, bg="#eef6ff", fg="#163a63", font=("Segoe UI", 9, "italic"), anchor="w", justify="left", wraplength=920).grid(row=index, column=0, sticky="w", pady=(3 if index == 1 else 1, 0))

    def _picklist_overview_rows(self) -> list[tuple[PicklistConfiguration, PicklistConfiguration, bool, list[str]]]:
        assert self.working is not None
        rows: list[tuple[PicklistConfiguration, PicklistConfiguration, bool, list[str]]] = []
        for picklist in self.working.picklists:
            exact_target = self._exact_picklist_target(picklist.name)
            if exact_target is None:
                continue
            target, in_project = exact_target
            existing_options = {row.name.casefold() for row in target.options}
            missing = [row.name for row in picklist.options if row.name.casefold() not in existing_options]
            rows.append((picklist, target, in_project, missing))
        return rows

    def _manual_picklists(self) -> list[PicklistConfiguration]:
        assert self.working is not None
        return [picklist for picklist in self.working.picklists if self._exact_picklist_target(picklist.name) is None]

    def _item_type_overview_rows(self) -> list[tuple[ItemTypeConfiguration, ItemTypeConfiguration, bool, list[Change]]]:
        assert self.working is not None
        assert self.current is not None
        configured = self._configured_working_copy_for_item_types()
        all_changes = self.service.compare(configured, self.current)
        rows: list[tuple[ItemTypeConfiguration, ItemTypeConfiguration, bool, list[Change]]] = []
        for item_type in self.working.itemTypes:
            exact_target = self._exact_item_type_target(item_type.name)
            if exact_target is None:
                continue
            target, in_project = exact_target
            related = self._item_type_related_changes(all_changes, item_type.name)
            rows.append((item_type, target, in_project, related))
        return rows

    def _manual_item_types(self) -> list[ItemTypeConfiguration]:
        assert self.working is not None
        return [item_type for item_type in self.working.itemTypes if self._exact_item_type_target(item_type.name) is None]

    def _show_picklist_stage(self) -> None:
        self._clear_frame(self.content)
        self._clear_frame(self.footer)
        self.picklist_controls = {}
        self.existing_picklist_names = []
        self.stage_title.set("Step 1: Picklist mappings")
        self.stage_hint.set(
            "Picklists that already exist in Jama Connect are summarized first in a highlighted overview. "
            "Only picklists that do not already exist in Jama appear in the mapping area below. Picklist updates are submitted first. "
            "After acceptance, the tool refreshes configuration data and then moves to Item Type mappings."
        )
        existing_names = [row[0].name for row in self.picklist_targets.values()]
        dropdown_values = [self.PICKLIST_PLACEHOLDER, self.CREATE_NEW, *sorted(existing_names, key=str.casefold)]
        overview_rows = self._picklist_overview_rows()
        self.existing_picklist_names = [picklist.name for picklist, _target, _in_project, _missing in overview_rows]
        row_index = 0
        if overview_rows:
            lines = []
            for picklist, target, in_project, missing in overview_rows:
                suffix = f"missing options: {', '.join(missing)}" if missing else "no new options needed"
                lines.append(f"{picklist.name} -> already exists in Jama Connect as '{target.name}'; {suffix}.")
            self._create_overview_panel(self.content, "Existing Picklists Detected", lines, row_index)
            row_index += 1
        manual_picklists = self._manual_picklists()
        if manual_picklists:
            ttk.Label(self.content, text="Picklists needing mapping or creation", font=("Segoe UI", 10, "bold")).grid(row=row_index, column=0, sticky="w", pady=(0, 8))
            row_index += 1
        else:
            ttk.Label(self.content, text="No picklist mappings are required below. Review the existing-picklist overview above and submit to continue.", wraplength=940, justify="left").grid(row=row_index, column=0, sticky="w", pady=(0, 8))
            row_index += 1
        for picklist in manual_picklists:
            row_frame = ttk.Labelframe(self.content, text=picklist.name, padding=8)
            row_frame.grid(row=row_index, column=0, sticky="ew", pady=(0, 8))
            row_frame.columnconfigure(2, weight=1)
            enabled = tk.BooleanVar(value=True)
            selected = tk.StringVar(value=self.PICKLIST_PLACEHOLDER)
            ttk.Checkbutton(row_frame, text="On", variable=enabled, command=lambda name=picklist.name: self._refresh_picklist_detail(name)).grid(row=0, column=0, sticky="w")
            ttk.Label(row_frame, text="Mapping").grid(row=0, column=1, sticky="w", padx=(12, 6))
            chooser = ttk.Combobox(row_frame, values=dropdown_values, textvariable=selected, state="readonly", width=42)
            chooser.grid(row=0, column=2, sticky="ew")
            chooser.bind("<<ComboboxSelected>>", lambda _event, name=picklist.name: self._refresh_picklist_detail(name))
            detail = ttk.Frame(row_frame)
            detail.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
            self.picklist_controls[picklist.name] = {
                "enabled": enabled,
                "selected": selected,
                "detail": detail,
                "option_vars": {},
                "picklist": picklist,
            }
            self._refresh_picklist_detail(picklist.name)
            row_index += 1
        ttk.Button(self.footer, text="Select All Picklist Options", command=lambda: self._set_picklist_option_selections(True)).pack(side="left")
        ttk.Button(self.footer, text="Deselect All Picklist Options", command=lambda: self._set_picklist_option_selections(False)).pack(side="left", padx=(8, 0))
        ttk.Button(self.footer, text="Skip Picklist Stage", command=self._skip_picklists).pack(side="left", padx=(8, 0))
        ttk.Button(self.footer, text="Submit Picklist Mappings", command=self._submit_picklists).pack(side="left", padx=(8, 0))
        ttk.Button(self.footer, text="Exit", command=self._close).pack(side="left", padx=(8, 0))

    def _refresh_picklist_detail(self, source_name: str) -> None:
        control = self.picklist_controls[source_name]
        detail = control["detail"]
        assert isinstance(detail, ttk.Frame)
        self._clear_frame(detail)
        control["option_vars"] = {}
        enabled = control["enabled"].get()  # type: ignore[index]
        selection = control["selected"].get()  # type: ignore[index]
        picklist = control["picklist"]
        if not enabled:
            ttk.Label(detail, text="This picklist is turned off and will not be updated.").grid(row=0, column=0, sticky="w")
            return
        if selection == self.PICKLIST_PLACEHOLDER:
            ttk.Label(detail, text="Select Dropdown Option to configure this picklist mapping.").grid(row=0, column=0, sticky="w")
            return
        row_index = 0
        if selection == self.CREATE_NEW:
            option_names = [row.name for row in picklist.options if row.name.casefold() != "unassigned"]
            if any(row.name.casefold() == "unassigned" for row in picklist.options):
                ttk.Label(detail, text="'Unassigned' will be skipped because Jama creates it automatically for new picklists.").grid(row=row_index, column=0, sticky="w")
                row_index += 1
            if not option_names:
                ttk.Label(detail, text="No picklist options need to be added for this new picklist.").grid(row=row_index, column=0, sticky="w")
                return
            ttk.Label(detail, text="Options to add:").grid(row=row_index, column=0, sticky="w")
            row_index += 1
            for index, option_name in enumerate(option_names, start=row_index):
                var = tk.BooleanVar(value=True)
                control["option_vars"][option_name] = var
                ttk.Checkbutton(detail, text=option_name, variable=var).grid(row=index, column=0, sticky="w")
            return
        target, _in_project = self.picklist_targets[selection.casefold()]
        existing = {row.name.casefold() for row in target.options}
        missing = [row.name for row in picklist.options if row.name.casefold() not in existing]
        if not missing:
            ttk.Label(detail, text="No new picklist options need to be added for this existing Jama picklist.").grid(row=row_index, column=0, sticky="w")
            return
        ttk.Label(detail, text="Missing picklist options to add:").grid(row=row_index, column=0, sticky="w")
        row_index += 1
        for index, option_name in enumerate(missing, start=row_index):
            var = tk.BooleanVar(value=True)
            control["option_vars"][option_name] = var
            ttk.Checkbutton(detail, text=option_name, variable=var).grid(row=index, column=0, sticky="w")

    def _submit_picklists(self) -> None:
        assert self.project is not None
        assert self.service is not None
        assert self.original_imported is not None
        for source_name, control in self.picklist_controls.items():
            enabled = control["enabled"].get()  # type: ignore[index]
            selection = self._resolved_picklist_selection(source_name)
            if enabled and selection == self.PICKLIST_PLACEHOLDER:
                messagebox.showerror("Picklist mapping required", f"Select a dropdown option for '{source_name}' or turn it off.")
                return
        changes = self._build_picklist_changes()
        actionable_changes = self._actionable_changes(changes)
        if actionable_changes and not self._confirm_changes("Accept Picklist Changes", self._summarize_picklist_changes(actionable_changes)):
            return
        if changes:
            outcomes = self.service.apply(self.project.id, changes, self._append_status)
            for line in outcomes:
                self._append_status(line)
        else:
            self._append_status("No picklist updates were selected.")
        self.service.invalidate_instance_metadata_cache()
        self.current = self.app._load_current_configuration_sync(self.project, force_refresh=True)
        self.picklist_targets = self._picklist_targets()
        self.working = self.service.copy_configuration(self.original_imported)
        for source_name, control in self.picklist_controls.items():
            selection = self._resolved_picklist_selection(source_name)
            if control["enabled"].get() and self._uses_manual_picklist_mapping(source_name, selection):  # type: ignore[index]
                self.service.apply_picklist_mapping(self.working, source_name, selection)
        self._append_status("Picklist mapping stage complete. Refreshed current Jama configuration data.")
        self._show_item_type_stage()

    def _skip_picklists(self) -> None:
        assert self.service is not None
        assert self.original_imported is not None
        if not messagebox.askyesno("Skip Picklists", "Skip all picklist mappings and move directly to Item Type mappings?"):
            return
        self._set_picklist_mapping_enabled(False)
        self.working = self.service.copy_configuration(self.original_imported)
        self._append_status("Skipped picklist mapping stage. No picklist changes were applied.")
        self._show_item_type_stage()

    def _build_picklist_changes(self) -> list[Change]:
        changes: list[Change] = []
        for picklist, target, in_project, _missing in self._picklist_overview_rows():
            if not in_project:
                changes.append(Change(
                    "instance_picklist_notice",
                    f"instance-picklist:{target.name}",
                    f"The existing {target.name} needs to be added to the Jama Connect Project.",
                    {"picklistName": target.name, "instancePicklistId": target.id},
                ))
            existing_options = {row.name.casefold() for row in target.options}
            for option in picklist.options:
                if option.name.casefold() in existing_options:
                    continue
                payload = asdict(option) | {"picklistName": target.name, "targetPicklistId": target.id}
                kind = "picklist_option" if in_project else "instance_picklist_option"
                message = (
                    f"Add option ‘{option.name}’ to ‘{target.name}’"
                    if in_project
                    else f"Add option '{option.name}' to existing Jama Connect picklist '{target.name}'"
                )
                changes.append(Change(kind, f"option:{target.name}:{option.name}", message, payload))
        for source_name, control in self.picklist_controls.items():
            enabled = control["enabled"].get()  # type: ignore[index]
            if not enabled:
                continue
            selection = control["selected"].get()  # type: ignore[index]
            picklist = control["picklist"]
            option_vars = control["option_vars"]  # type: ignore[index]
            if selection == self.CREATE_NEW:
                changes.append(Change("picklist", f"picklist:{picklist.name}", f"Create picklist ‘{picklist.name}’", asdict(picklist)))
                for option in picklist.options:
                    if option.name.casefold() == "unassigned":
                        continue
                    if option.name in option_vars and option_vars[option.name].get():
                        payload = asdict(option) | {"picklistName": picklist.name}
                        changes.append(Change("picklist_option", f"option:{picklist.name}:{option.name}", f"Add option ‘{option.name}’ to ‘{picklist.name}’", payload))
                continue
            target, in_project = self.picklist_targets[selection.casefold()]
            if not in_project:
                changes.append(Change(
                    "instance_picklist_notice",
                    f"instance-picklist:{selection}",
                    f"The existing Jama Connect picklist '{selection}' is already available in the instance.",
                    {"picklistName": selection, "instancePicklistId": target.id},
                ))
            existing_options = {row.name.casefold() for row in target.options}
            for option in picklist.options:
                if option.name.casefold() in existing_options:
                    continue
                if option.name in option_vars and option_vars[option.name].get():
                    payload = asdict(option) | {"picklistName": selection, "targetPicklistId": target.id}
                    kind = "picklist_option" if in_project else "instance_picklist_option"
                    message = (
                        f"Add option ‘{option.name}’ to ‘{selection}’"
                        if in_project
                        else f"Add option '{option.name}' to existing Jama Connect picklist '{selection}'"
                    )
                    changes.append(Change(kind, f"option:{selection}:{option.name}", message, payload))
        return changes

    def _summarize_picklist_changes(self, changes: list[Change]) -> str:
        if not changes:
            return "No picklist updates are selected."
        lines = ["Picklist changes to apply:"]
        for change in changes:
            lines.append(f"- {change.message}")
        return "\n".join(lines)

    def _show_item_type_stage(self) -> None:
        self._clear_frame(self.content)
        self._clear_frame(self.footer)
        self.item_type_controls = {}
        self.existing_item_type_names = []
        self.item_type_targets = self._item_type_targets()
        self.stage_title.set("Step 2: Item Type mappings")
        self.stage_hint.set(
            "Item Types that already exist in Jama Connect are summarized first in a highlighted overview. "
            "Only Item Types that do not already exist in Jama appear in the mapping area below. After picklist updates were applied, the tool refreshed Jama configuration information and re-compares fields for this stage."
        )
        existing_names = [row[0].name for row in self.item_type_targets.values()]
        dropdown_values = [self.PICKLIST_PLACEHOLDER, self.CREATE_NEW, *sorted(existing_names, key=str.casefold)]
        overview_rows = self._item_type_overview_rows()
        self.existing_item_type_names = [item_type.name for item_type, _target, _in_project, _related in overview_rows]
        row_index = 0
        if overview_rows:
            lines = []
            for item_type, target, in_project, related in overview_rows:
                actionable = [row.message for row in related if row.kind in {"field", "instance_field"}]
                notices = [row.message for row in related if row.kind in {"field_notice", "instance_item_type_notice"}]
                if actionable:
                    suffix = f"field updates: {'; '.join(actionable)}"
                elif notices:
                    suffix = f"notices: {'; '.join(notices)}"
                else:
                    suffix = "no new fields needed"
                lines.append(f"{item_type.name} -> already exists in Jama Connect as '{target.name}'; {suffix}.")
            self._create_overview_panel(self.content, "Existing Item Types Detected", lines, row_index)
            row_index += 1
        manual_item_types = self._manual_item_types()
        if manual_item_types:
            ttk.Label(self.content, text="Item Types needing mapping or creation", font=("Segoe UI", 10, "bold")).grid(row=row_index, column=0, sticky="w", pady=(0, 8))
            row_index += 1
        else:
            ttk.Label(self.content, text="No item type mappings are required below. Review the existing-item-type overview above and submit to continue.", wraplength=940, justify="left").grid(row=row_index, column=0, sticky="w", pady=(0, 8))
            row_index += 1
        for item_type in manual_item_types:
            row_frame = ttk.Labelframe(self.content, text=item_type.name, padding=8)
            row_frame.grid(row=row_index, column=0, sticky="ew", pady=(0, 8))
            row_frame.columnconfigure(2, weight=1)
            enabled = tk.BooleanVar(value=True)
            selected = tk.StringVar(value=self.PICKLIST_PLACEHOLDER)
            ttk.Checkbutton(row_frame, text="On", variable=enabled, command=lambda name=item_type.name: self._refresh_item_type_detail(name)).grid(row=0, column=0, sticky="w")
            ttk.Label(row_frame, text="Mapping").grid(row=0, column=1, sticky="w", padx=(12, 6))
            chooser = ttk.Combobox(row_frame, values=dropdown_values, textvariable=selected, state="readonly", width=42)
            chooser.grid(row=0, column=2, sticky="ew")
            chooser.bind("<<ComboboxSelected>>", lambda _event, name=item_type.name: self._refresh_item_type_detail(name))
            detail = ttk.Frame(row_frame)
            detail.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 0))
            self.item_type_controls[item_type.name] = {
                "enabled": enabled,
                "selected": selected,
                "detail": detail,
                "field_vars": {},
                "item_type": item_type,
            }
            self._refresh_item_type_detail(item_type.name)
            row_index += 1
        ttk.Button(self.footer, text="Select All Item Types", command=self._select_all_item_types).pack(side="left")
        ttk.Button(self.footer, text="Deselect All Item Types", command=self._deselect_all_item_types).pack(side="left", padx=(8, 0))
        ttk.Button(self.footer, text="Skip Item Type Stage", command=self._skip_item_types).pack(side="left", padx=(8, 0))
        ttk.Button(self.footer, text="Submit Item Type Mappings", command=self._submit_item_types).pack(side="left", padx=(8, 0))
        ttk.Button(self.footer, text="Exit", command=self._close).pack(side="left", padx=(8, 0))

    def _configured_working_copy_for_item_types(self) -> JamaConfiguration:
        assert self.service is not None
        assert self.working is not None
        configured = self.service.copy_configuration(self.working)
        for source_name, control in self.item_type_controls.items():
            source = next((row for row in configured.itemTypes if row.name == source_name), None)
            if source is None:
                continue
            if not control["enabled"].get():  # type: ignore[index]
                self.service.apply_item_type_association(source, None)
                continue
            selection = self._resolved_item_type_selection(source_name)
            if selection in {self.PICKLIST_PLACEHOLDER, self.CREATE_NEW}:
                self.service.apply_item_type_association(source, None)
                continue
            if not self._uses_manual_item_type_mapping(source_name, selection):
                self.service.apply_item_type_association(source, None)
                continue
            target, _in_project = self.item_type_targets[selection.casefold()]
            self.service.apply_item_type_association(source, target)
        return configured

    def _item_type_related_changes(self, changes: list[Change], source_name: str) -> list[Change]:
        related: list[Change] = []
        for change in changes:
            payload = change.payload
            if change.kind == "item_type" and str(payload.get("name", "")) == source_name:
                related.append(change)
                continue
            if str(payload.get("sourceItemTypeName", "")) == source_name:
                related.append(change)
                continue
            if str(payload.get("associatedFromItemTypeName", "")) == source_name:
                related.append(change)
                continue
            if str(payload.get("itemTypeName", "")) == source_name and change.kind in {"field", "instance_field", "field_notice", "instance_item_type_notice"}:
                related.append(change)
        return related

    def _refresh_item_type_detail(self, source_name: str) -> None:
        assert self.service is not None
        assert self.current is not None
        control = self.item_type_controls[source_name]
        detail = control["detail"]
        assert isinstance(detail, ttk.Frame)
        self._clear_frame(detail)
        control["field_vars"] = {}
        enabled = control["enabled"].get()  # type: ignore[index]
        selection = control["selected"].get()  # type: ignore[index]
        if not enabled:
            ttk.Label(detail, text="This item type is turned off and will not be updated.").grid(row=0, column=0, sticky="w")
            return
        if selection == self.PICKLIST_PLACEHOLDER:
            ttk.Label(detail, text="Select Dropdown Option to configure this item type mapping.").grid(row=0, column=0, sticky="w")
            return
        configured = self._configured_working_copy_for_item_types()
        all_changes = self.service.compare(configured, self.current)
        related = self._item_type_related_changes(all_changes, source_name)
        actionable = [row for row in related if row.kind in {"field", "instance_field"}]
        notices = [row for row in related if row.kind in {"field_notice", "instance_item_type_notice"}]
        if not actionable and not notices:
            ttk.Label(detail, text="No field changes are required for this mapping.").grid(row=0, column=0, sticky="w")
            return
        row_index = 0
        if actionable:
            ttk.Label(detail, text="Field changes to consider:").grid(row=row_index, column=0, sticky="w")
            row_index += 1
            for change in actionable:
                var = tk.BooleanVar(value=True)
                control["field_vars"][change.key] = var
                ttk.Checkbutton(detail, text=change.message, variable=var).grid(row=row_index, column=0, sticky="w")
                row_index += 1
        for change in notices:
            ttk.Label(detail, text=f"Notice: {change.message}", wraplength=920, justify="left").grid(row=row_index, column=0, sticky="w", pady=(4, 0))
            row_index += 1

    def _submit_item_types(self) -> None:
        assert self.project is not None
        assert self.service is not None
        assert self.current is not None
        for source_name, control in self.item_type_controls.items():
            enabled = control["enabled"].get()  # type: ignore[index]
            selection = self._resolved_item_type_selection(source_name)
            if enabled and selection == self.PICKLIST_PLACEHOLDER:
                messagebox.showerror("Item Type mapping required", f"Select a dropdown option for '{source_name}' or turn it off.")
                return
        configured = self._configured_working_copy_for_item_types()
        all_changes = self.service.compare(configured, self.current)
        selected_changes: list[Change] = []
        for source_name in self.existing_item_type_names:
            related = self._item_type_related_changes(all_changes, source_name)
            selected_changes.extend([row for row in related if row.kind in {"field", "instance_field", "field_notice", "instance_item_type_notice"}])
        for source_name, control in self.item_type_controls.items():
            if not control["enabled"].get():  # type: ignore[index]
                continue
            selection = self._resolved_item_type_selection(source_name)
            related = self._item_type_related_changes(all_changes, source_name)
            field_vars = control["field_vars"]  # type: ignore[index]
            if selection == self.CREATE_NEW:
                selected_changes.extend([row for row in related if row.kind == "item_type"])
            selected_changes.extend([row for row in related if row.kind in {"field_notice", "instance_item_type_notice"}])
            for change in related:
                if change.kind in {"field", "instance_field"} and change.key in field_vars and field_vars[change.key].get():
                    selected_changes.append(change)
        # preserve compare order while removing duplicates
        seen: set[str] = set()
        ordered_selected: list[Change] = []
        for change in all_changes:
            if change.key in seen:
                continue
            if any(change.key == row.key for row in selected_changes):
                ordered_selected.append(change)
                seen.add(change.key)
        actionable_changes = self._actionable_changes(ordered_selected)
        if actionable_changes and not self._confirm_changes("Accept Item Type Changes", self._summarize_item_type_changes(actionable_changes)):
            return
        outcomes = self.service.apply(self.project.id, ordered_selected, self._append_status)
        self.outcomes = outcomes
        for line in outcomes:
            self._append_status(line)
        self.service.invalidate_instance_metadata_cache()
        self.project_updates_path, self.project_updates_text = self._write_project_configuration_updates(ordered_selected)
        self._show_completion()

    def _skip_item_types(self) -> None:
        if not messagebox.askyesno("Skip Item Types", "Skip all item type mappings and finish the import workflow?"):
            return
        self._set_item_type_mapping_enabled(False)
        self.outcomes = ["Skipped item type mapping stage. No item type changes were applied."]
        self.project_updates_path, self.project_updates_text = self._write_project_configuration_updates([])
        self._show_completion()

    def _summarize_item_type_changes(self, changes: list[Change]) -> str:
        if not changes:
            return "No item type updates are selected."
        lines = ["Item Type changes to apply:"]
        for change in changes:
            lines.append(f"- {change.message}")
        return "\n".join(lines)

    def _confirm_changes(self, title: str, text: str) -> bool:
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("900x520")
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(0, weight=1)
        frame = ttk.Frame(dialog, padding=12)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        text_box = tk.Text(frame, wrap="word")
        text_box.grid(row=0, column=0, sticky="nsew")
        text_box.insert("1.0", text)
        text_box.configure(state="disabled")
        buttons = ttk.Frame(frame)
        buttons.grid(row=1, column=0, sticky="e", pady=(8, 0))
        result = {"accepted": False}
        def accept() -> None:
            result["accepted"] = True
            dialog.destroy()
        def cancel() -> None:
            dialog.destroy()
        ttk.Button(buttons, text="Accept", command=accept).pack(side="left")
        ttk.Button(buttons, text="Cancel", command=cancel).pack(side="left", padx=(8, 0))
        dialog.wait_window()
        return bool(result["accepted"])

    def _show_completion(self) -> None:
        self._clear_frame(self.content)
        self._clear_frame(self.footer)
        self.stage_title.set("Import complete")
        self.stage_hint.set("Review the changes made and the generated follow-up list below.")
        summary = tk.Text(self.content, height=24, wrap="word")
        summary.grid(row=0, column=0, sticky="nsew")
        for line in self.outcomes:
            summary.insert("end", line + "\n")
        if self.project_updates_path is not None:
            summary.insert("end", f"\nConfiguration follow-up file: {self.project_updates_path.resolve()}\n")
        if self.project_updates_text:
            summary.insert("end", "\n")
            summary.insert("end", self.project_updates_text)
        summary.configure(state="disabled")
        ttk.Button(self.footer, text="Exit", command=self._close).pack(side="left")

    def _project_configuration_item_type_names(self, changes: list[Change]) -> list[str]:
        names: list[str] = []
        seen: set[str] = set()
        for change in changes:
            if change.kind == "instance_item_type_notice":
                name = str(change.payload.get("itemTypeName", "")).strip()
            elif change.kind == "item_type":
                name = str(change.payload.get("name", "")).strip()
            else:
                continue
            key = name.casefold()
            if not name or key in seen:
                continue
            seen.add(key)
            names.append(name)
        return names

    def _skipped_item_type_names(self) -> list[str]:
        skipped: list[str] = []
        for source_name, control in self.item_type_controls.items():
            if not control["enabled"].get():  # type: ignore[index]
                skipped.append(source_name)
        return sorted(skipped, key=str.casefold)

    def _project_configuration_updates_text(self, changes: list[Change]) -> str:
        assert self.project is not None
        assert self.import_path is not None
        assert self.service is not None
        item_type_names = self._project_configuration_item_type_names(changes)
        skipped_item_types = self._skipped_item_type_names()
        self.service.invalidate_instance_metadata_cache()
        instance_types = self.service._instance_item_types_by_name()
        lines = [
            "Configuration Follow-Up",
            f"Generated: {datetime.now():%Y-%m-%d %H:%M:%S}",
            f"Import file: {self.import_path.name}",
            "",
            "1. Item Types identified for follow-up configuration",
        ]
        if item_type_names:
            for name in item_type_names:
                match = instance_types.get(name.casefold())
                api_id = match.id if match is not None else None
                suffix = f"API ID {api_id}" if api_id is not None else "API ID unavailable - verify the item type exists in the Jama Connect instance"
                lines.append(f"- {name} ({suffix})")
        else:
            lines.append("- None")
        lines.extend([
            "",
            "2. Item Types not configured / warning",
        ])
        if skipped_item_types:
            for name in skipped_item_types:
                lines.append(f"- {name} (skipped during import; not mapped to an existing item type and not created as a new item type)")
        else:
            lines.append("- None")
        lines.append("")
        return "\n".join(lines)

    def _write_project_configuration_updates(self, changes: list[Change]) -> tuple[Path | None, str]:
        assert self.project is not None
        text = self._project_configuration_updates_text(changes)
        path = Path.cwd() / f"configuration-follow-up-{datetime.now():%Y%m%d-%H%M%S}.txt"
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            warning = f"WARNING: Could not save configuration follow-up file: {exc}"
            self._append_status(warning)
            return None, text
        self._append_status(f"Configuration follow-up saved: {path.resolve()}")
        return path, text

