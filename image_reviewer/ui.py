from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from PIL import ImageTk

from .core import (
    Category, CategoryAuditLog, GroupConflictError, GroupMoveError, ReviewEngine,
    ScanResult, StateStore, VIEW_CODES, configure_logging, scan_images, scan_product_groups,
)
from .preview import AsyncPreviewLoader, PreviewResult

BG = "#f4f6f9"
CARD = "#ffffff"
TEXT = "#172033"
MUTED = "#687386"


class ConfigPage(ttk.Frame):
    def __init__(self, master: "ReviewApp"):
        super().__init__(master, padding=24, style="Page.TFrame")
        self.app = master
        self.source_var = tk.StringVar()
        self.view_var = tk.StringVar(value="A")
        self.rows: list[dict] = []
        self.scan_result: ScanResult | None = None

        ttk.Label(self, text="图片人工复核分类", style="Title.TLabel").pack(anchor="w")
        ttk.Label(self, text="按产品目检一个视角，分类时自动移动该产品的全部视角图片", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 18))

        source = ttk.LabelFrame(self, text=" 第 1 步  选择图片目录 ", padding=14, style="Card.TLabelframe")
        source.pack(fill="x", pady=(0, 12))
        ttk.Entry(source, textvariable=self.source_var, font=("Microsoft YaHei UI", 10)).pack(side="left", fill="x", expand=True, ipady=5, padx=(0, 10))
        ttk.Button(source, text="选择目录", command=self.choose_source, style="Primary.TButton").pack(side="left")

        inspect = ttk.LabelFrame(self, text=" 第 2 步  扫描产品并选择本批次目检视角 ", padding=14, style="Card.TLabelframe")
        inspect.pack(fill="x", pady=(0, 12))
        ttk.Label(inspect, text="目检视角：").pack(side="left")
        self.view_combo = ttk.Combobox(inspect, textvariable=self.view_var, values=VIEW_CODES, state="readonly", width=8, font=("Microsoft YaHei UI", 11))
        self.view_combo.pack(side="left", padx=(0, 16))
        self.view_combo.bind("<<ComboboxSelected>>", lambda _event: self.analyze())
        self.stats_label = ttk.Label(inspect, text="选择目录后自动扫描", style="Muted.TLabel")
        self.stats_label.pack(side="left", fill="x", expand=True)

        category_card = ttk.LabelFrame(self, text=" 第 3 步  配置分类目标（最多 9 个） ", padding=14, style="Card.TLabelframe")
        category_card.pack(fill="both", expand=True)
        self.category_rows = ttk.Frame(category_card, style="Card.TFrame")
        self.category_rows.pack(fill="both", expand=True)
        header = ttk.Frame(self.category_rows, style="Card.TFrame")
        header.pack(fill="x", pady=(0, 5))
        ttk.Label(header, text="快捷键", width=8, style="MutedCard.TLabel").pack(side="left")
        ttk.Label(header, text="分类名称", width=18, style="MutedCard.TLabel").pack(side="left")
        ttk.Label(header, text="目标文件夹", style="MutedCard.TLabel").pack(side="left", fill="x", expand=True)
        ttk.Label(header, text="现有图片", width=10, style="MutedCard.TLabel").pack(side="left")
        controls = ttk.Frame(self, style="Page.TFrame")
        controls.pack(fill="x", pady=(14, 0))
        ttk.Button(controls, text="＋ 添加分类", command=self.add_row).pack(side="left")
        ttk.Button(controls, text="开始复核  →", command=self.start, style="Primary.TButton").pack(side="right")
        for name in ("OK", "NG", "RECHECK"):
            self.add_row(name)

    def choose_source(self) -> None:
        folder = filedialog.askdirectory(title="选择待复核图片目录", initialdir=self.source_var.get() or None)
        if folder:
            self.source_var.set(folder)
            first_scan = scan_product_groups(Path(folder), "A")
            available = [view for view in VIEW_CODES if first_scan.view_counts[view]]
            self.view_combo.configure(values=available or VIEW_CODES)
            self.view_var.set("A" if "A" in available or not available else available[0])
            self.analyze()

    def analyze(self) -> None:
        source = Path(self.source_var.get().strip())
        if not source.is_dir():
            self.scan_result = None
            self.stats_label.configure(text="请选择有效目录")
            return
        self.scan_result = scan_product_groups(source, self.view_var.get() or "A")
        scan = self.scan_result
        views = "  ".join(f"{view}:{scan.view_counts[view]}" for view in VIEW_CODES if scan.view_counts[view]) or "未识别到视角"
        self.stats_label.configure(text=(
            f"图片 {scan.image_count}  |  产品 {scan.product_count}  |  待复核 {len(scan.groups)}  |  "
            f"缺少{self.view_var.get()}图 {scan.missing_review_count}  |  忽略 {len(scan.ignored)}  |  重复视角 {scan.duplicate_view_count}\n视角统计：{views}"
        ))

    def add_row(self, name: str = "") -> None:
        if len(self.rows) >= 9:
            messagebox.showinfo("提示", "一次最多配置 9 个分类。")
            return
        frame = ttk.Frame(self.category_rows, style="Card.TFrame")
        frame.pack(fill="x", pady=4)
        row = {"frame": frame, "number": ttk.Label(frame, width=8, style="Key.TLabel"), "name": tk.StringVar(value=name), "folder": tk.StringVar(), "count": ttk.Label(frame, text="—", width=10, style="MutedCard.TLabel")}
        row["number"].pack(side="left")
        ttk.Entry(frame, textvariable=row["name"], width=16).pack(side="left", ipady=4, padx=(0, 8))
        ttk.Entry(frame, textvariable=row["folder"]).pack(side="left", fill="x", expand=True, ipady=4, padx=(0, 8))
        ttk.Button(frame, text="浏览", command=lambda r=row: self.choose_target(r)).pack(side="left", padx=(0, 6))
        row["count"].pack(side="left")
        ttk.Button(frame, text="删除", command=lambda r=row: self.remove_row(r)).pack(side="left")
        self.rows.append(row)
        self.refresh_numbers()

    def choose_target(self, row: dict) -> None:
        folder = filedialog.askdirectory(title="选择分类目标文件夹", initialdir=row["folder"].get() or None)
        if folder:
            row["folder"].set(folder)
            row["count"].configure(text=str(len(scan_images(Path(folder)))))

    def remove_row(self, row: dict) -> None:
        self.rows.remove(row)
        row["frame"].destroy()
        self.refresh_numbers()

    def refresh_numbers(self) -> None:
        for index, row in enumerate(self.rows, 1):
            row["number"].configure(text=f"{index} 键")

    def start(self) -> None:
        source = Path(self.source_var.get().strip())
        if not source.is_dir():
            messagebox.showerror("配置错误", "请选择有效的图片目录。")
            return
        self.analyze()
        if not self.scan_result or not self.scan_result.groups:
            messagebox.showwarning("没有可复核产品", f"没有找到包含 {self.view_var.get()} 视角的产品。")
            return
        categories = [Category(row["name"].get().strip(), row["folder"].get().strip()) for row in self.rows if row["name"].get().strip() or row["folder"].get().strip()]
        if not categories or any(not item.name or not item.destination for item in categories):
            messagebox.showerror("配置错误", "每个分类都必须填写名称并选择目标文件夹。")
            return
        if len({item.name.casefold() for item in categories}) != len(categories):
            messagebox.showerror("配置错误", "分类名称不能重复。")
            return
        for category in categories:
            target = Path(category.destination)
            if target.resolve() == source.resolve():
                messagebox.showerror("配置错误", f"“{category.name}”的目标目录不能与源目录相同。")
                return
            try:
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                messagebox.showerror("目录错误", f"无法访问“{category.name}”目录：\n{exc}")
                return
        scan = self.scan_result
        detail = f"将目检 {len(scan.groups)} 个产品的 {self.view_var.get()} 图。\n分类一次会移动该产品的全部视角。"
        if scan.ignored or scan.missing_review_count or scan.duplicate_view_count:
            detail += f"\n\n另外：忽略 {len(scan.ignored)} 张，缺少目检视角 {scan.missing_review_count} 个产品，重复视角 {scan.duplicate_view_count} 项。"
        if not messagebox.askyesno("确认开始", detail + "\n\n是否开始？"):
            return
        self.app.open_review(ReviewEngine.create(source, categories, self.view_var.get(), self.app.store))


class ReviewPage(ttk.Frame):
    def __init__(self, master: "ReviewApp", engine: ReviewEngine):
        super().__init__(master, style="Page.TFrame")
        self.app, self.engine = master, engine
        self.zoom, self.rotation = 1.0, 0
        self.photo: ImageTk.PhotoImage | None = None
        self.preview_token = 0
        self.current_preview_path = ""
        self.drag_origin: tuple[int, int] | None = None
        self.zoom_after_id = None
        self.resize_after_id = None
        self.poll_after_id = None
        self.replayed_deferred = False

        header = ttk.Frame(self, padding=(18, 12), style="Card.TFrame")
        header.pack(fill="x")
        self.product_label = ttk.Label(header, style="Header.TLabel")
        self.product_label.pack(side="left")
        self.progress_label = ttk.Label(header, style="MutedCard.TLabel")
        self.progress_label.pack(side="right")

        main = ttk.Frame(self, padding=(14, 12), style="Page.TFrame")
        main.pack(fill="both", expand=True)
        sidebar = ttk.Frame(main, width=245, padding=14, style="Card.TFrame")
        sidebar.pack(side="left", fill="y", padx=(0, 12))
        sidebar.pack_propagate(False)
        ttk.Label(sidebar, text="当前产品", style="Section.TLabel").pack(anchor="w")
        self.review_name_label = ttk.Label(sidebar, text="", wraplength=215, style="BodyCard.TLabel")
        self.review_name_label.pack(anchor="w", pady=(8, 14))
        ttk.Separator(sidebar).pack(fill="x", pady=(0, 12))
        ttk.Label(sidebar, text="包含的视角", style="Section.TLabel").pack(anchor="w")
        self.views_label = ttk.Label(sidebar, text="", wraplength=215, justify="left", style="BodyCard.TLabel")
        self.views_label.pack(anchor="w", pady=(8, 14))
        self.group_count_label = ttk.Label(sidebar, text="", wraplength=215, style="Notice.TLabel")
        self.group_count_label.pack(anchor="w", fill="x", pady=(0, 14))
        ttk.Label(sidebar, text="原始目录", style="Section.TLabel").pack(anchor="w")
        self.source_label = ttk.Label(sidebar, text=engine.state.source, wraplength=215, style="MutedCard.TLabel")
        self.source_label.pack(anchor="w", pady=(8, 0))

        viewer = ttk.Frame(main, style="Viewer.TFrame")
        viewer.pack(side="left", fill="both", expand=True)
        toolbar = ttk.Frame(viewer, padding=7, style="Toolbar.TFrame")
        toolbar.pack(fill="x")
        for text, command in (("⟲ 左转", lambda: self.rotate(-90)), ("⟳ 右转", lambda: self.rotate(90)), ("适应窗口", self.fit), ("← 上一个", lambda: self.navigate(-1)), ("下一个 →", lambda: self.navigate(1))):
            ttk.Button(toolbar, text=text, command=command).pack(side="left", padx=3)
        self.zoom_label = ttk.Label(toolbar, text="100%", style="Toolbar.TLabel")
        self.zoom_label.pack(side="right", padx=8)
        self.canvas = tk.Canvas(viewer, bg="#151a24", highlightthickness=0, cursor="fleur")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self.on_resize)
        self.canvas.bind("<MouseWheel>", self.on_wheel)
        self.canvas.bind("<ButtonPress-1>", self.drag_start)
        self.canvas.bind("<B1-Motion>", self.drag_move)

        actions = ttk.Frame(self, padding=(14, 8, 14, 12), style="Card.TFrame")
        actions.pack(fill="x")
        helper = ttk.Frame(actions, style="Card.TFrame")
        helper.pack(fill="x", pady=(0, 8))
        ttk.Button(helper, text="暂不处理  Space", command=self.defer).pack(side="left")
        ttk.Button(helper, text="撤销整组  Ctrl+Z", command=self.undo).pack(side="left", padx=6)
        ttk.Button(helper, text="结束任务", command=self.finish_task).pack(side="right")
        category_bar = ttk.Frame(actions, style="Card.TFrame")
        category_bar.pack(fill="x")
        colors = {"OK": ("#238636", "white"), "NG": ("#cf222e", "white"), "RECHECK": ("#d97706", "white")}
        for index, category in enumerate(engine.state.categories):
            bg, fg = colors.get(category.name.upper(), ("#44546f", "white"))
            button = tk.Button(category_bar, text=f"{index + 1}  {category.name}", command=lambda i=index: self.classify(i), bg=bg, fg=fg, activebackground=bg, activeforeground=fg, relief="flat", font=("Microsoft YaHei UI", 11, "bold"), padx=12, pady=9, cursor="hand2")
            button.pack(side="left", fill="x", expand=True, padx=4)
        self.status_var = tk.StringVar(value="准备就绪")
        ttk.Label(self, textvariable=self.status_var, anchor="w", padding=(10, 5), style="Status.TLabel").pack(fill="x")

        self.bind_all("<Control-z>", lambda _event: self.undo())
        self.bind_all("<space>", lambda _event: self.defer())
        self.bind_all("<Left>", lambda _event: self.navigate(-1))
        self.bind_all("<Right>", lambda _event: self.navigate(1))
        for index in range(min(9, len(engine.state.categories))):
            self.bind_all(str(index + 1), lambda _event, i=index: self.classify(i))
        self.poll_results()
        self.after(100, self.load_current)

    def destroy(self) -> None:
        for sequence in ("<Control-z>", "<space>", "<Left>", "<Right>"):
            self.unbind_all(sequence)
        for index in range(9):
            self.unbind_all(str(index + 1))
        for after_id in (self.zoom_after_id, self.resize_after_id, self.poll_after_id):
            if after_id:
                try:
                    self.after_cancel(after_id)
                except tk.TclError:
                    pass
        super().destroy()

    def poll_results(self) -> None:
        for result in self.app.preview_loader.poll():
            if result.token == self.preview_token and result.path == self.current_preview_path:
                self.accept_preview(result)
        self.poll_after_id = self.after(45, self.poll_results)

    def accept_preview(self, result: PreviewResult) -> None:
        self.canvas.delete("all")
        if result.error or result.image is None:
            self.canvas.create_text(self.canvas.winfo_width() / 2, self.canvas.winfo_height() / 2, text=f"图片无法打开\n{result.error}", fill="#ffb4ab", font=("Microsoft YaHei UI", 15), justify="center")
            self.status_var.set("图片加载失败，可暂不处理后继续")
            return
        self.photo = ImageTk.PhotoImage(result.image)
        self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2, image=self.photo, tags="picture")
        size = result.original_size or result.image.size
        self.status_var.set(f"原图 {size[0]} × {size[1]}  |  预览 {result.image.width} × {result.image.height}")

    def request_preview(self) -> None:
        group = self.engine.current
        if not group:
            return
        self.preview_token += 1
        self.current_preview_path = group.review_image
        width = min(8000, max(320, int(self.canvas.winfo_width() * self.zoom)))
        height = min(8000, max(240, int(self.canvas.winfo_height() * self.zoom)))
        self.canvas.delete("all")
        if self.photo:
            self.canvas.create_image(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2, image=self.photo, tags="picture")
        self.canvas.create_text(14, 14, text="正在生成预览…", fill="#b8c0d0", anchor="nw", tags="loading")
        self.app.preview_loader.request(self.preview_token, group.review_image, (width, height), self.rotation)

    def load_current(self) -> None:
        group = self.engine.current
        if group is None:
            if self.engine.state.deferred and not self.replayed_deferred:
                self.engine.restore_deferred()
                self.replayed_deferred = True
                messagebox.showinfo("再次确认", "第一轮已结束，现在重新显示暂不处理的产品。")
                group = self.engine.current
            else:
                self.app.finish_and_show_summary(self.engine)
                return
        if group is None:
            return
        self.photo = None
        self.zoom, self.rotation = 1.0, 0
        views = []
        for view in VIEW_CODES:
            paths = group.view_map.get(view, [])
            if paths:
                suffix = f" ×{len(paths)}" if len(paths) > 1 else ""
                views.append(f"{view}  {Path(paths[0]).name}{suffix}")
        if not views:
            views = [Path(path).name for path in group.members]
        duplicate = f"\n⚠ 重复视角：{', '.join(group.duplicate_views)}" if group.duplicate_views else ""
        self.product_label.configure(text=f"产品：{group.product_id}")
        self.review_name_label.configure(text=f"目检 {self.engine.state.review_view or '旧版'} 图\n{Path(group.review_image).name}")
        self.views_label.configure(text="\n".join(views) + duplicate)
        self.group_count_label.configure(text=f"本次分类将移动该产品的 {len(group.members)} 张图片")
        self.update_progress()
        self.zoom_label.configure(text="100%")
        self.request_preview()

    def update_progress(self) -> None:
        state = self.engine.state
        self.progress_label.configure(text=f"已分类 {len(state.history)} 个产品 / {self.engine.completed_image_count} 张图片    待处理 {len(state.pending)}    暂不处理 {len(state.deferred)}    总产品 {state.total}")

    def on_resize(self, _event: tk.Event) -> None:
        if self.resize_after_id:
            self.after_cancel(self.resize_after_id)
        if self.zoom == 1.0:
            self.resize_after_id = self.after(180, self.request_preview)

    def on_wheel(self, event: tk.Event) -> None:
        self.zoom = min(8.0, max(0.1, self.zoom * (1.15 if event.delta > 0 else 1 / 1.15)))
        self.zoom_label.configure(text=f"{self.zoom * 100:.0f}%")
        if self.zoom_after_id:
            self.after_cancel(self.zoom_after_id)
        self.zoom_after_id = self.after(120, self.request_preview)

    def fit(self) -> None:
        self.zoom = 1.0
        self.zoom_label.configure(text="100%")
        self.request_preview()

    def rotate(self, amount: int) -> None:
        self.rotation = (self.rotation + amount) % 360
        self.request_preview()

    def drag_start(self, event: tk.Event) -> None:
        self.drag_origin = (event.x, event.y)

    def drag_move(self, event: tk.Event) -> None:
        if self.drag_origin:
            self.canvas.move("picture", event.x - self.drag_origin[0], event.y - self.drag_origin[1])
            self.drag_origin = (event.x, event.y)

    def navigate(self, delta: int) -> None:
        self.engine.navigate(delta)
        self.load_current()

    def defer(self) -> None:
        if self.engine.current:
            self.engine.defer_current()
            self.load_current()

    def classify(self, index: int) -> None:
        group = self.engine.current
        if not group:
            return
        try:
            self.engine.classify(index)
        except GroupConflictError as exc:
            messagebox.showwarning("整组未移动", f"{exc}\n\n该产品没有任何图片被移动。可按 Space 暂不处理。")
            return
        except (GroupMoveError, OSError) as exc:
            messagebox.showerror("整组移动失败", str(exc))
            return
        self.load_current()

    def undo(self) -> None:
        try:
            self.engine.undo()
        except (GroupConflictError, GroupMoveError, OSError, RuntimeError) as exc:
            messagebox.showwarning("无法整组撤销", str(exc))
            return
        self.load_current()

    def finish_task(self) -> None:
        remaining = len(self.engine.state.pending) + len(self.engine.state.deferred)
        if remaining and not messagebox.askyesno("结束任务", f"还有 {remaining} 个产品未分类，它们将保留在源目录。\n\n确定结束当前任务吗？"):
            return
        self.app.finish_and_show_summary(self.engine)


class SummaryPage(ttk.Frame):
    def __init__(self, master: "ReviewApp", engine: ReviewEngine, remaining: int):
        super().__init__(master, padding=28, style="Page.TFrame")
        ttk.Label(self, text="本次复核已结束", style="Title.TLabel").pack(anchor="w")
        ttk.Label(self, text=f"任务编号 {engine.state.session_id}    目检视角 {engine.state.review_view or '旧版任务'}", style="Subtitle.TLabel").pack(anchor="w", pady=(3, 20))
        card = ttk.Frame(self, padding=18, style="Card.TFrame")
        card.pack(fill="both", expand=True)
        ttk.Label(card, text="分类汇总", style="Section.TLabel").pack(anchor="w", pady=(0, 10))
        tree = ttk.Treeview(card, columns=("category", "products", "images", "log"), show="headings", height=max(4, len(engine.state.categories)))
        for column, title, width in (("category", "分类", 130), ("products", "产品数", 90), ("images", "图片数", 90), ("log", "记录文件", 520)):
            tree.heading(column, text=title)
            tree.column(column, width=width, stretch=column == "log", anchor="w" if column in ("category", "log") else "center")
        for category in engine.state.categories:
            products, images = engine.category_summary().get(category.name, (0, 0))
            tree.insert("", "end", values=(category.name, products, images, str(CategoryAuditLog(Path(category.destination)).path)))
        tree.pack(fill="both", expand=True)
        info = f"已分类 {engine.completed_count} 个产品，共移动 {engine.completed_image_count} 张图片。"
        if remaining:
            info += f"  未分类 {remaining} 个产品仍保留在源目录。"
        ttk.Label(card, text=info, style="BodyCard.TLabel").pack(anchor="w", pady=(14, 0))
        ttk.Label(card, text=f"失败操作 {engine.state.failure_count}  |  忽略图片 {engine.state.ignored_count}  |  缺少目检视角产品 {engine.state.missing_review_count}  |  重复视角 {engine.state.duplicate_view_count}", style="MutedCard.TLabel").pack(anchor="w", pady=(5, 0))
        ttk.Button(self, text="新建复核任务", command=master.show_config, style="Primary.TButton").pack(anchor="e", pady=(16, 0))


class ReviewApp(tk.Tk):
    def __init__(self):
        super().__init__()
        configure_logging()
        self.store = StateStore()
        self.preview_loader = AsyncPreviewLoader()
        self.title("图片人工复核分类工具 v2")
        self.geometry("1280x820")
        self.minsize(980, 650)
        self.configure(bg=BG)
        self.current_page: ttk.Frame | None = None
        self.setup_styles()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.startup)

    def setup_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Page.TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("Viewer.TFrame", background="#151a24")
        style.configure("Toolbar.TFrame", background="#252c3a")
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Subtitle.TLabel", background=BG, foreground=MUTED, font=("Microsoft YaHei UI", 10))
        style.configure("Header.TLabel", background=CARD, foreground=TEXT, font=("Microsoft YaHei UI", 13, "bold"))
        style.configure("Section.TLabel", background=CARD, foreground=TEXT, font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("BodyCard.TLabel", background=CARD, foreground=TEXT, font=("Microsoft YaHei UI", 9))
        style.configure("Muted.TLabel", background=CARD, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("MutedCard.TLabel", background=CARD, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Notice.TLabel", background="#e8f1ff", foreground="#175cd3", padding=8, font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("Key.TLabel", background=CARD, foreground="#175cd3", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Toolbar.TLabel", background="#252c3a", foreground="#e8ecf4")
        style.configure("Status.TLabel", background="#e9edf4", foreground="#39445a")
        style.configure("Card.TLabelframe", background=CARD, bordercolor="#d8deea")
        style.configure("Card.TLabelframe.Label", background=BG, foreground=TEXT, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"), padding=(14, 7), background="#2563eb", foreground="white")
        style.map("Primary.TButton", background=[("active", "#1d4ed8")])

    def startup(self) -> None:
        try:
            state = self.store.load()
        except Exception as exc:
            logging.exception("Could not load session")
            messagebox.showwarning("恢复失败", f"上次任务状态无法读取，将创建新任务。\n{exc}")
            self.store.clear()
            state = None
        if state and Path(state.source).is_dir():
            mode = "旧版单图任务" if not state.review_view else f"{state.review_view} 视角产品组任务"
            if messagebox.askyesno("恢复任务", f"发现未完成的{mode}：\n{state.source}\n\n是否继续？"):
                self.open_review(ReviewEngine(state, self.store))
                return
            self.store.clear()
        self.show_config()

    def swap(self, page: ttk.Frame) -> None:
        if self.current_page:
            self.current_page.destroy()
        self.current_page = page
        page.pack(fill="both", expand=True)

    def show_config(self) -> None:
        self.swap(ConfigPage(self))

    def open_review(self, engine: ReviewEngine) -> None:
        self.swap(ReviewPage(self, engine))

    def finish_and_show_summary(self, engine: ReviewEngine) -> None:
        remaining = len(engine.state.pending) + len(engine.state.deferred)
        self.store.clear()
        self.swap(SummaryPage(self, engine, remaining))

    def on_close(self) -> None:
        if isinstance(self.current_page, ReviewPage) and (self.current_page.engine.state.pending or self.current_page.engine.state.deferred):
            if not messagebox.askyesno("退出确认", "任务尚未完成，产品组进度已经保存。确定退出吗？"):
                return
        self.preview_loader.close()
        self.destroy()


def main() -> None:
    ReviewApp().mainloop()
