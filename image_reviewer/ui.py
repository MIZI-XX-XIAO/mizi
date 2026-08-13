from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import Image, ImageOps, ImageTk, UnidentifiedImageError

from .core import Category, ReviewEngine, StateStore, configure_logging, scan_images


class ConfigPage(ttk.Frame):
    def __init__(self, master: "ReviewApp"):
        super().__init__(master, padding=18)
        self.app = master
        self.source_var = tk.StringVar()
        self.rows: list[tuple[ttk.Frame, tk.StringVar, tk.StringVar]] = []
        ttk.Label(self, text="图片人工复核分类", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w", pady=(0, 16))
        source_box = ttk.LabelFrame(self, text="1. 选择待复核图片目录", padding=10)
        source_box.pack(fill="x")
        ttk.Entry(source_box, textvariable=self.source_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(source_box, text="浏览…", command=self.choose_source).pack(side="left")
        self.scan_label = ttk.Label(self, text="尚未选择目录")
        self.scan_label.pack(anchor="w", pady=(6, 12))
        self.category_box = ttk.LabelFrame(self, text="2. 配置分类（最多 9 个）", padding=10)
        self.category_box.pack(fill="both", expand=True)
        header = ttk.Frame(self.category_box)
        header.pack(fill="x")
        ttk.Label(header, text="分类名称", width=20).pack(side="left")
        ttk.Label(header, text="目标文件夹").pack(side="left")
        controls = ttk.Frame(self)
        controls.pack(fill="x", pady=(12, 0))
        ttk.Button(controls, text="＋ 添加分类", command=self.add_row).pack(side="left")
        ttk.Button(controls, text="开始复核", command=self.start, style="Accent.TButton").pack(side="right")
        for name in ("OK", "NG", "RECHECK"):
            self.add_row(name)

    def choose_source(self) -> None:
        folder = filedialog.askdirectory(title="选择待复核图片目录", initialdir=self.source_var.get() or None)
        if folder:
            self.source_var.set(folder)
            self.scan_label.config(text=f"检测到 {len(scan_images(Path(folder)))} 张支持的图片（不包含子目录）")

    def add_row(self, name: str = "") -> None:
        if len(self.rows) >= 9:
            messagebox.showinfo("提示", "一次最多配置 9 个分类。")
            return
        frame = ttk.Frame(self.category_box)
        frame.pack(fill="x", pady=4)
        name_var, folder_var = tk.StringVar(value=name), tk.StringVar()
        number = len(self.rows) + 1
        ttk.Label(frame, text=str(number), width=3).pack(side="left")
        ttk.Entry(frame, textvariable=name_var, width=18).pack(side="left", padx=(0, 8))
        ttk.Entry(frame, textvariable=folder_var).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ttk.Button(frame, text="浏览…", command=lambda: self.choose_target(folder_var)).pack(side="left", padx=(0, 5))
        ttk.Button(frame, text="删除", command=lambda: self.remove_row(frame)).pack(side="left")
        self.rows.append((frame, name_var, folder_var))

    def remove_row(self, frame: ttk.Frame) -> None:
        for row in self.rows:
            if row[0] is frame:
                self.rows.remove(row)
                frame.destroy()
                break

    @staticmethod
    def choose_target(variable: tk.StringVar) -> None:
        folder = filedialog.askdirectory(title="选择分类目标文件夹", initialdir=variable.get() or None)
        if folder:
            variable.set(folder)

    def start(self) -> None:
        source = Path(self.source_var.get().strip())
        if not source.is_dir():
            messagebox.showerror("配置错误", "请选择有效的待复核图片目录。")
            return
        categories = [Category(name.get().strip(), folder.get().strip()) for _, name, folder in self.rows if name.get().strip() or folder.get().strip()]
        if not categories or any(not x.name or not x.destination for x in categories):
            messagebox.showerror("配置错误", "每个分类都必须填写名称并选择目标文件夹。")
            return
        if len({x.name.casefold() for x in categories}) != len(categories):
            messagebox.showerror("配置错误", "分类名称不能重复。")
            return
        source_resolved = source.resolve()
        for category in categories:
            target = Path(category.destination)
            if target.resolve() == source_resolved:
                messagebox.showerror("配置错误", f"“{category.name}”的目标目录不能与源目录相同。")
                return
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("目录错误", f"无法创建或访问“{category.name}”目录：\n{exc}")
                return
        images = scan_images(source)
        if not images:
            messagebox.showwarning("没有图片", "所选目录当前层级中没有支持的图片。")
            return
        conflicts = sum(1 for image in images for c in categories if (Path(c.destination) / image.name).exists())
        if conflicts and not messagebox.askyesno("发现同名文件", f"目标目录中检测到 {conflicts} 个潜在同名文件。\n软件不会覆盖文件，操作时会逐项提示。是否继续？"):
            return
        self.app.open_review(ReviewEngine.create(source, categories, self.app.store))


class ReviewPage(ttk.Frame):
    def __init__(self, master: "ReviewApp", engine: ReviewEngine):
        super().__init__(master)
        self.app, self.engine = master, engine
        self.zoom, self.rotation = 1.0, 0
        self.pil_image: Image.Image | None = None
        self.photo: ImageTk.PhotoImage | None = None
        self.drag_origin: tuple[int, int] | None = None
        top = ttk.Frame(self, padding=(10, 8))
        top.pack(fill="x")
        self.file_label = ttk.Label(top, font=("Microsoft YaHei UI", 11, "bold"))
        self.file_label.pack(side="left")
        self.progress_label = ttk.Label(top)
        self.progress_label.pack(side="right")
        self.canvas = tk.Canvas(self, bg="#202124", highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        tools = ttk.Frame(self, padding=(10, 6))
        tools.pack(fill="x")
        for text, cmd in (("⟲ 左转", lambda: self.rotate(-90)), ("⟳ 右转", lambda: self.rotate(90)), ("适应窗口", self.fit), ("← 上一张", lambda: self.navigate(-1)), ("下一张 →", lambda: self.navigate(1)), ("暂不处理  Space", self.defer), ("撤销  Ctrl+Z", self.undo), ("结束任务", self.finish_task)):
            ttk.Button(tools, text=text, command=cmd).pack(side="left", padx=3)
        self.categories_frame = ttk.Frame(self, padding=(10, 6))
        self.categories_frame.pack(fill="x")
        for idx, category in enumerate(engine.state.categories):
            ttk.Button(self.categories_frame, text=f"{idx + 1}  {category.name}", command=lambda i=idx: self.classify(i)).pack(side="left", fill="x", expand=True, padx=3)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w", padding=4).pack(fill="x")
        self.canvas.bind("<Configure>", lambda _e: self.render())
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<ButtonPress-1>", self.drag_start)
        self.canvas.bind("<B1-Motion>", self.drag_move)
        self.bind_all("<Control-z>", lambda _e: self.undo())
        self.bind_all("<space>", lambda _e: self.defer())
        self.bind_all("<Left>", lambda _e: self.navigate(-1))
        self.bind_all("<Right>", lambda _e: self.navigate(1))
        for idx in range(min(9, len(engine.state.categories))):
            self.bind_all(str(idx + 1), lambda _e, i=idx: self.classify(i))
        self.load_current()

    def destroy(self) -> None:
        self.unbind_all("<Control-z>"); self.unbind_all("<space>"); self.unbind_all("<Left>"); self.unbind_all("<Right>")
        for idx in range(9): self.unbind_all(str(idx + 1))
        super().destroy()

    def load_current(self) -> None:
        current = self.engine.current
        if current is None:
            if self.engine.restore_deferred():
                messagebox.showinfo("再次确认", "第一轮已结束，现在重新显示暂不处理的图片。")
                current = self.engine.current
            else:
                self.pil_image = None
                self.canvas.delete("all")
                self.canvas.create_text(self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2, text="全部图片已复核完成", fill="white", font=("Microsoft YaHei UI", 24))
                self.file_label.config(text="任务完成")
                self.update_progress()
                self.app.store.clear()
                messagebox.showinfo("完成", f"全部图片已处理完成。\n复核记录：{self.engine.audit.path}")
                return
        assert current is not None
        try:
            with Image.open(current) as opened:
                self.pil_image = ImageOps.exif_transpose(opened).convert("RGB")
            self.rotation, self.zoom = 0, 1.0
            self.file_label.config(text=f"{current.name}    {self.pil_image.width} × {self.pil_image.height}")
            self.status_var.set(str(current))
            self.fit()
        except (OSError, UnidentifiedImageError) as exc:
            logging.exception("Cannot open image: %s", current)
            self.pil_image = None
            self.canvas.delete("all")
            self.canvas.create_text(self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2, text=f"图片无法打开\n{exc}", fill="#ffb4ab", font=("Microsoft YaHei UI", 16), justify="center")
            self.status_var.set("图片损坏或格式不受支持，可暂不处理后继续")
        self.update_progress()

    def update_progress(self) -> None:
        s = self.engine.state
        self.progress_label.config(text=f"已分类 {len(s.history)}  |  待处理 {len(s.pending)}  |  暂不处理 {len(s.deferred)}  |  总数 {s.total}")

    def render(self) -> None:
        if self.pil_image is None or self.canvas.winfo_width() < 10:
            return
        image = self.pil_image.rotate(-self.rotation, expand=True)
        width, height = max(1, int(image.width * self.zoom)), max(1, int(image.height * self.zoom))
        preview = image.resize((width, height), Image.Resampling.LANCZOS)
        self.photo = ImageTk.PhotoImage(preview)
        x, y = self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2
        self.canvas.delete("all")
        self.canvas.create_image(x, y, image=self.photo, tags="picture")

    def fit(self) -> None:
        if self.pil_image is None:
            return
        width, height = self.pil_image.size
        if self.rotation % 180: width, height = height, width
        self.zoom = min(max(1, self.canvas.winfo_width() - 20) / width, max(1, self.canvas.winfo_height() - 20) / height, 1.0)
        self.render()

    def rotate(self, amount: int) -> None:
        self.rotation = (self.rotation + amount) % 360
        self.fit()

    def on_wheel(self, event: tk.Event) -> None:
        self.zoom = min(8.0, max(0.05, self.zoom * (1.15 if event.delta > 0 else 1 / 1.15)))
        self.render()

    def drag_start(self, event: tk.Event) -> None:
        self.drag_origin = (event.x, event.y)

    def drag_move(self, event: tk.Event) -> None:
        if self.drag_origin:
            dx, dy = event.x - self.drag_origin[0], event.y - self.drag_origin[1]
            self.canvas.move("picture", dx, dy)
            self.drag_origin = (event.x, event.y)

    def navigate(self, delta: int) -> None:
        self.engine.navigate(delta); self.load_current()

    def defer(self) -> None:
        if self.engine.current:
            self.engine.defer_current(); self.load_current()

    def classify(self, index: int) -> None:
        if not self.engine.current: return
        try:
            self.engine.classify(index)
        except FileExistsError as exc:
            answer = messagebox.askyesnocancel("同名文件", f"目标文件已存在，绝不会覆盖：\n{exc}\n\n是：自动追加序号后移动\n否：跳过并留在源目录\n取消：返回")
            if answer is None: return
            if answer is False:
                self.engine.classify(index, "skip")
                self.status_var.set("已跳过同名图片，图片仍在源目录")
                self.load_current()
                return
            try: self.engine.classify(index, "rename")
            except Exception as inner: messagebox.showerror("移动失败", str(inner)); return
        except Exception as exc:
            messagebox.showerror("移动失败", f"图片未被移动：\n{exc}"); return
        self.load_current()

    def finish_task(self) -> None:
        remaining = len(self.engine.state.pending) + len(self.engine.state.deferred)
        if remaining and not messagebox.askyesno(
            "结束任务",
            f"还有 {remaining} 张图片未分类，它们将保留在源目录。\n复核记录不会删除。\n\n确定结束当前任务吗？",
        ):
            return
        self.app.store.clear()
        messagebox.showinfo("任务已结束", f"未分类图片仍保留在源目录。\n复核记录：{self.engine.audit.path}")
        self.app.show_config()

    def undo(self) -> None:
        try:
            self.engine.undo()
        except FileExistsError as exc:
            if not messagebox.askyesno("无法原名撤销", f"原位置已存在同名文件：\n{exc}\n\n是否自动追加序号后恢复？"): return
            try: self.engine.undo("rename")
            except Exception as inner: messagebox.showerror("撤销失败", str(inner)); return
        except Exception as exc:
            messagebox.showwarning("无法撤销", str(exc)); return
        self.load_current()


class ReviewApp(tk.Tk):
    def __init__(self):
        super().__init__()
        configure_logging()
        self.store = StateStore()
        self.title("图片人工复核分类工具")
        self.geometry("1180x760")
        self.minsize(900, 600)
        self.current_page: ttk.Frame | None = None
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.startup)

    def startup(self) -> None:
        try: state = self.store.load()
        except Exception as exc:
            logging.exception("Could not load session")
            messagebox.showwarning("恢复失败", f"上次任务状态无法读取，将创建新任务。\n{exc}")
            self.store.clear(); state = None
        if state and Path(state.source).is_dir():
            if messagebox.askyesno("恢复任务", f"发现未完成任务：\n{state.source}\n\n是否继续？"):
                self.open_review(ReviewEngine(state, self.store)); return
            self.store.clear()
        self.show_config()

    def swap(self, page: ttk.Frame) -> None:
        if self.current_page: self.current_page.destroy()
        self.current_page = page; page.pack(fill="both", expand=True)

    def show_config(self) -> None:
        self.swap(ConfigPage(self))

    def open_review(self, engine: ReviewEngine) -> None:
        self.swap(ReviewPage(self, engine))

    def on_close(self) -> None:
        if isinstance(self.current_page, ReviewPage) and (self.current_page.engine.state.pending or self.current_page.engine.state.deferred):
            if not messagebox.askyesno("退出确认", "任务尚未完成，进度已保存。确定退出吗？"): return
        self.destroy()


def main() -> None:
    ReviewApp().mainloop()
