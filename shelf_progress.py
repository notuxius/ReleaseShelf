"""Download progress shared by the catalog footer and app-info dialog."""

from tkinter import ttk


class DownloadProgress(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, style="Surface.TFrame")
        self.columnconfigure(0, weight=1)
        self.bar = ttk.Progressbar(self, length=140, maximum=100,
                                   style="Download.Horizontal.TProgressbar")
        self.bar.grid(row=0, column=0, sticky="ew")
        self.percent = ttk.Label(self, width=5, anchor="e", style="Small.TLabel")
        self.percent.grid(row=0, column=1, padx=(6, 0))

    def update_progress(self, received, total):
        if received is None:
            self.bar.stop()
            self.bar.configure(value=0)
            self.percent.configure(text="")
            self.grid_remove()
            return
        known_size = total is not None and total > 0
        mode = "determinate" if known_size else "indeterminate"
        if str(self.bar.cget("mode")) != mode or not self.winfo_manager():
            self.bar.stop()
            self.bar.configure(mode=mode, value=0)
            if mode == "indeterminate":
                self.bar.start(40)
        if known_size:
            percent = min(100, received * 100 / total)
            self.bar.configure(value=percent)
            self.percent.configure(text=f"{int(percent)}%")
            self.percent.grid()
        else:
            self.percent.configure(text="")
            self.percent.grid_remove()
        self.grid()
