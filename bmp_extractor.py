from __future__ import annotations

import csv
import hashlib
import os
import queue
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Iterable


APP_TITLE = "多级文件整理工具"
INVALID_NAME_CHARS = '<>:"/\\|?*'


@dataclass(frozen=True)
class ExtractOptions:
    source: Path
    output: Path
    mode: str  # level / name
    target_level: int = 2  # root is level 1
    target_name: str = ""
    recursive_files: bool = True
    structure_mode: str = "relative"  # relative / combined / simple
    relative_keep_levels: int = 0  # 0 means all relative parents
    combined_levels: int = 2
    simple_naming: str = "current"  # current / ancestor / custom
    ancestor_steps: int = 1
    custom_name: str = "提取结果"
    directory_conflict: str = "merge"  # merge / number
    file_conflict: str = "number"  # skip / overwrite / number
    skip_identical: bool = True
    write_csv: bool = True


@dataclass(frozen=True)
class TargetPlan:
    target: Path
    output_folder: Path


@dataclass
class ExtractResult:
    matched_folders: int = 0
    total_files: int = 0
    copied_files: int = 0
    overwritten_files: int = 0
    renamed_files: int = 0
    identical_skipped: int = 0
    conflict_skipped: int = 0
    cancelled_files: int = 0
    errors: list[str] = field(default_factory=list)
    csv_path: Path | None = None


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def safe_component(name: str, fallback: str = "未命名") -> str:
    cleaned = "".join("_" if char in INVALID_NAME_CHARS else char for char in name)
    cleaned = cleaned.strip().rstrip(".")
    return cleaned or fallback


def path_key(path: Path) -> str:
    try:
        return str(path.resolve()).casefold()
    except OSError:
        return str(path.absolute()).casefold()


def unique_path(folder: Path, filename: str, reserved: set[str] | None = None) -> Path:
    path_name = Path(filename)
    stem = path_name.stem
    suffix = path_name.suffix
    candidate = folder / filename
    index = 2
    while candidate.exists() or (reserved is not None and path_key(candidate) in reserved):
        candidate = folder / f"{stem}_{index}{suffix}"
        index += 1
    if reserved is not None:
        reserved.add(path_key(candidate))
    return candidate


def walk_directories(root: Path, excluded: Path | None = None) -> Iterable[Path]:
    for current_text, dir_names, _ in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_text)
        if excluded:
            dir_names[:] = [
                name for name in dir_names if not is_within(current / name, excluded)
            ]
        for name in sorted(dir_names, key=str.casefold):
            yield current / name


def find_target_folders(options: ExtractOptions) -> list[Path]:
    source = options.source.resolve()
    output = options.output.resolve()
    excluded = output if is_within(output, source) else None
    targets: list[Path] = []

    if options.mode == "level":
        wanted_depth = options.target_level - 1
        for folder in walk_directories(source, excluded):
            try:
                depth = len(folder.resolve().relative_to(source).parts)
            except (OSError, ValueError):
                continue
            if depth == wanted_depth:
                targets.append(folder)
    else:
        wanted_name = options.target_name.casefold()
        for folder in walk_directories(source, excluded):
            if folder.name.casefold() == wanted_name:
                targets.append(folder)

    return sorted(targets, key=lambda item: str(item).casefold())


def files_in_folder(
    folder: Path, recursive: bool, excluded: Path | None = None
) -> list[Path]:
    if not recursive:
        return sorted(
            (
                item
                for item in folder.iterdir()
                if item.is_file() and not (excluded and is_within(item, excluded))
            ),
            key=lambda item: item.name.casefold(),
        )

    files: list[Path] = []
    for current_text, dir_names, file_names in os.walk(
        folder, topdown=True, followlinks=False
    ):
        current = Path(current_text)
        if excluded:
            dir_names[:] = [
                name for name in dir_names if not is_within(current / name, excluded)
            ]
        for name in sorted(file_names, key=str.casefold):
            path = current / name
            if not (excluded and is_within(path, excluded)):
                files.append(path)
    return files


def simple_name_for(target: Path, options: ExtractOptions) -> str:
    if options.simple_naming == "current":
        return safe_component(target.name)
    if options.simple_naming == "custom":
        return safe_component(options.custom_name, "提取结果")

    selected = target
    source = options.source.resolve()
    for _ in range(options.ancestor_steps):
        if selected.resolve() == source or selected.parent == selected:
            break
        selected = selected.parent
    return safe_component(selected.name, target.name)


def desired_relative_output(target: Path, options: ExtractOptions) -> Path:
    source = options.source.resolve()
    relative = target.resolve().relative_to(source)
    parts = list(relative.parts)

    if options.structure_mode == "relative":
        if options.relative_keep_levels > 0:
            parts = parts[-options.relative_keep_levels :]
        return Path(*(safe_component(part) for part in parts))

    if options.structure_mode == "combined":
        selected = parts[-options.combined_levels :]
        return Path("__".join(safe_component(part) for part in selected))

    return Path(simple_name_for(target, options))


def _numbered_directory(output: Path, relative: Path, reserved: set[str]) -> Path:
    parent = output / relative.parent
    base_name = relative.name
    candidate = parent / base_name
    index = 2
    while path_key(candidate) in reserved or candidate.exists():
        candidate = parent / f"{base_name}_{index}"
        index += 1
    reserved.add(path_key(candidate))
    return candidate


def plan_output_folders(
    targets: list[Path], options: ExtractOptions
) -> list[TargetPlan]:
    output = options.output.resolve()
    reserved: set[str] = set()
    merged: dict[str, Path] = {}
    plans: list[TargetPlan] = []

    for target in targets:
        relative = desired_relative_output(target, options)
        desired = output / relative
        key = path_key(desired)
        if options.directory_conflict == "merge":
            output_folder = merged.get(key)
            if output_folder is None:
                output_folder = (
                    desired
                    if not desired.exists() or desired.is_dir()
                    else _numbered_directory(output, relative, reserved)
                )
                merged[key] = output_folder
        else:
            output_folder = _numbered_directory(output, relative, reserved)
        plans.append(TargetPlan(target, output_folder))
    return plans


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def files_identical(source: Path, destination: Path) -> bool:
    try:
        if source.stat().st_size != destination.stat().st_size:
            return False
        return sha256_file(source) == sha256_file(destination)
    except OSError:
        return False


def desired_file_path(
    source_file: Path, plan: TargetPlan, options: ExtractOptions
) -> Path:
    if options.structure_mode == "relative" and options.recursive_files:
        try:
            inside_target = source_file.relative_to(plan.target)
        except ValueError:
            inside_target = Path(source_file.name)
        return plan.output_folder / inside_target
    return plan.output_folder / source_file.name


def validate_options(options: ExtractOptions) -> None:
    if not options.source.is_dir():
        raise ValueError("根目录不存在或不是文件夹。")
    if options.source.resolve() == options.output.resolve():
        raise ValueError("输出目录不能与根目录相同。")
    if options.mode not in {"level", "name"}:
        raise ValueError("目录查找方式无效。")
    if options.mode == "level" and options.target_level < 2:
        raise ValueError("目标层级必须是2级或更深层级。")
    if options.mode == "name" and not options.target_name.strip():
        raise ValueError("请输入需要查找的文件夹名称。")
    if options.structure_mode not in {"relative", "combined", "simple"}:
        raise ValueError("输出结构方式无效。")
    if options.relative_keep_levels < 0:
        raise ValueError("保留相对目录级数不能小于0。")
    if options.combined_levels < 1:
        raise ValueError("组合目录级数至少为1。")
    if options.simple_naming == "ancestor" and options.ancestor_steps < 1:
        raise ValueError("向上级数至少为1。")
    if options.simple_naming == "custom" and not options.custom_name.strip():
        raise ValueError("请输入自定义输出文件夹名称。")
    if options.directory_conflict not in {"merge", "number"}:
        raise ValueError("同名目录处理方式无效。")
    if options.file_conflict not in {"skip", "overwrite", "number"}:
        raise ValueError("同名文件处理方式无效。")


CSV_HEADERS = [
    "时间",
    "源文件",
    "目标文件",
    "处理结果",
    "源文件大小",
    "SHA256",
    "说明",
]


def extract_files(
    options: ExtractOptions,
    progress: Callable[[int, int, str], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ExtractResult:
    validate_options(options)
    source = options.source.resolve()
    output = options.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    excluded = output if is_within(output, source) else None

    targets = find_target_folders(options)
    plans = plan_output_folders(targets, options)
    jobs: list[tuple[TargetPlan, list[Path]]] = []
    total = 0
    for plan in plans:
        files = files_in_folder(plan.target, options.recursive_files, excluded)
        jobs.append((plan, files))
        total += len(files)

    result = ExtractResult(matched_folders=len(targets), total_files=total)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    csv_path = output / f"提取记录_{timestamp}.csv"
    csv_stream = None
    writer = None
    if options.write_csv:
        csv_stream = csv_path.open("w", encoding="utf-8-sig", newline="")
        writer = csv.writer(csv_stream)
        writer.writerow(CSV_HEADERS)
        result.csv_path = csv_path

    reserved_files: set[str] = set()
    processed = 0
    try:
        for plan, files in jobs:
            if should_cancel and should_cancel():
                break
            plan.output_folder.mkdir(parents=True, exist_ok=True)

            for source_file in files:
                if should_cancel and should_cancel():
                    break
                desired = desired_file_path(source_file, plan, options)
                destination = desired
                action = "复制"
                note = ""
                digest = ""

                try:
                    conflict = desired.exists() or path_key(desired) in reserved_files
                    if desired.exists() and options.skip_identical:
                        if files_identical(source_file, desired):
                            action = "跳过-内容相同"
                            result.identical_skipped += 1
                            digest = sha256_file(source_file)
                            note = "源文件与目标文件SHA-256一致"
                        else:
                            conflict = True

                    if action == "复制" and conflict:
                        if options.file_conflict == "skip":
                            action = "跳过-同名冲突"
                            result.conflict_skipped += 1
                            note = "目标已存在且内容不相同"
                        elif options.file_conflict == "overwrite":
                            action = "覆盖"
                            result.overwritten_files += 1
                        else:
                            destination = unique_path(
                                desired.parent, desired.name, reserved_files
                            )
                            action = "复制-自动编号"
                            result.renamed_files += 1

                    if action in {"复制", "覆盖", "复制-自动编号"}:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_file, destination)
                        reserved_files.add(path_key(destination))
                        result.copied_files += 1

                    if writer:
                        writer.writerow(
                            [
                                datetime.now().isoformat(timespec="seconds"),
                                str(source_file),
                                str(destination),
                                action,
                                source_file.stat().st_size,
                                digest,
                                note,
                            ]
                        )
                    detail = f"{action}：{source_file.name} → {destination}"
                except OSError as exc:
                    result.errors.append(f"{source_file}: {exc}")
                    detail = f"失败：{source_file}（{exc}）"
                    if writer:
                        writer.writerow(
                            [
                                datetime.now().isoformat(timespec="seconds"),
                                str(source_file),
                                str(destination),
                                "失败",
                                "",
                                "",
                                str(exc),
                            ]
                        )

                processed += 1
                if progress:
                    progress(processed, total, detail)
    finally:
        if csv_stream:
            csv_stream.close()

    result.cancelled_files = max(0, total - processed)
    return result


class ExtractorApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("980x820")
        self.minsize(860, 720)

        self.source_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="level")
        self.level_var = tk.IntVar(value=3)
        self.target_name_var = tk.StringVar(value="RawImages")
        self.recursive_var = tk.BooleanVar(value=True)

        self.structure_var = tk.StringVar(value="relative")
        self.relative_levels_var = tk.IntVar(value=0)
        self.combined_levels_var = tk.IntVar(value=2)
        self.simple_naming_var = tk.StringVar(value="current")
        self.ancestor_var = tk.IntVar(value=1)
        self.custom_name_var = tk.StringVar(value="提取结果")

        self.directory_conflict_var = tk.StringVar(value="merge")
        self.file_conflict_var = tk.StringVar(value="number")
        self.skip_identical_var = tk.BooleanVar(value=True)
        self.write_csv_var = tk.BooleanVar(value=True)

        self.status_var = tk.StringVar(value="请选择根目录和输出目录。")
        self.events: queue.Queue[tuple] = queue.Queue()
        self.cancel_event = threading.Event()
        self._build_ui()
        self._update_controls()
        self.after(100, self._process_events)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Subtitle.TLabel", foreground="#536273")
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))

        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        canvas = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=scrollbar.set)

        main = ttk.Frame(canvas, padding=18)
        window_id = canvas.create_window((0, 0), window=main, anchor="nw")
        main.columnconfigure(1, weight=1)

        def resize_scroll(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def resize_inner(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        main.bind("<Configure>", resize_scroll)
        canvas.bind("<Configure>", resize_inner)

        def mouse_wheel(event) -> None:
            canvas.yview_scroll(int(-event.delta / 120), "units")

        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", mouse_wheel))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))

        ttk.Label(
            main, text=APP_TITLE, style="Title.TLabel"
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Button(
            main, text="应用推荐配置", command=self._apply_recommended
        ).grid(row=0, column=2, sticky="e")
        ttk.Label(
            main,
            text="保留来源层级、冲突可控、支持SHA-256增量跳过，并生成可追溯CSV记录。",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 14))

        ttk.Label(main, text="根目录：").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Entry(main, textvariable=self.source_var).grid(
            row=2, column=1, sticky="ew", padx=8, pady=5
        )
        ttk.Button(main, text="选择…", command=self._choose_source).grid(
            row=2, column=2, pady=5
        )
        ttk.Label(main, text="输出目录：").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Entry(main, textvariable=self.output_var).grid(
            row=3, column=1, sticky="ew", padx=8, pady=5
        )
        ttk.Button(main, text="选择…", command=self._choose_output).grid(
            row=3, column=2, pady=5
        )

        find_box = ttk.LabelFrame(
            main, text="1. 查找需要处理的目录", padding=12,
            style="Section.TLabelframe",
        )
        find_box.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(12, 6))
        find_box.columnconfigure(3, weight=1)
        ttk.Radiobutton(
            find_box, text="按目录层级", variable=self.mode_var, value="level",
            command=self._update_controls,
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(find_box, text="目标：").grid(row=0, column=1, padx=(16, 4))
        self.level_spin = ttk.Spinbox(
            find_box, from_=2, to=99, width=6, textvariable=self.level_var
        )
        self.level_spin.grid(row=0, column=2, sticky="w")
        ttk.Label(find_box, text="级目录（根目录为1级）").grid(
            row=0, column=3, sticky="w", padx=(5, 0)
        )
        ttk.Radiobutton(
            find_box, text="按文件夹名称", variable=self.mode_var, value="name",
            command=self._update_controls,
        ).grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Label(find_box, text="名称：").grid(
            row=1, column=1, padx=(16, 4), pady=(10, 0)
        )
        self.name_entry = ttk.Entry(find_box, textvariable=self.target_name_var)
        self.name_entry.grid(
            row=1, column=2, columnspan=2, sticky="ew", pady=(10, 0)
        )
        ttk.Checkbutton(
            find_box, text="包含命中目录下面的所有更深层文件",
            variable=self.recursive_var,
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(10, 0))

        structure_box = ttk.LabelFrame(
            main, text="2. 输出目录结构", padding=12,
            style="Section.TLabelframe",
        )
        structure_box.grid(row=5, column=0, columnspan=3, sticky="ew", pady=6)
        structure_box.columnconfigure(5, weight=1)
        ttk.Radiobutton(
            structure_box, text="保留相对目录结构（推荐）",
            variable=self.structure_var, value="relative", command=self._update_controls,
        ).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(structure_box, text="保留末尾级数：").grid(
            row=0, column=2, padx=(20, 4)
        )
        self.relative_spin = ttk.Spinbox(
            structure_box, from_=0, to=99, width=5, textvariable=self.relative_levels_var
        )
        self.relative_spin.grid(row=0, column=3)
        ttk.Label(structure_box, text="（0=全部）").grid(row=0, column=4, sticky="w")
        ttk.Label(
            structure_box,
            text="示例：2026-07-31\\Inspect_001 与 2026-08-01\\Inspect_001 会分别保留，不发生冲突。",
            foreground="#476582",
        ).grid(row=0, column=5, sticky="w", padx=(8, 0))

        ttk.Radiobutton(
            structure_box, text="使用上级目录组合命名",
            variable=self.structure_var, value="combined", command=self._update_controls,
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(10, 0))
        ttk.Label(structure_box, text="组合末尾级数：").grid(
            row=1, column=2, padx=(20, 4), pady=(10, 0)
        )
        self.combined_spin = ttk.Spinbox(
            structure_box, from_=1, to=99, width=5, textvariable=self.combined_levels_var
        )
        self.combined_spin.grid(row=1, column=3, pady=(10, 0))
        ttk.Label(structure_box, text="（用 __ 连接）").grid(
            row=1, column=4, sticky="w", pady=(10, 0)
        )

        ttk.Radiobutton(
            structure_box, text="简单名称（兼容旧版）",
            variable=self.structure_var, value="simple", command=self._update_controls,
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self.simple_current = ttk.Radiobutton(
            structure_box, text="命中目录名", variable=self.simple_naming_var,
            value="current", command=self._update_controls,
        )
        self.simple_current.grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.simple_ancestor = ttk.Radiobutton(
            structure_box, text="向上", variable=self.simple_naming_var,
            value="ancestor", command=self._update_controls,
        )
        self.simple_ancestor.grid(row=3, column=1, sticky="e", pady=(8, 0))
        self.ancestor_spin = ttk.Spinbox(
            structure_box, from_=1, to=99, width=5, textvariable=self.ancestor_var
        )
        self.ancestor_spin.grid(row=3, column=2, sticky="w", pady=(8, 0))
        ttk.Label(structure_box, text="级目录名").grid(
            row=3, column=3, sticky="w", pady=(8, 0)
        )
        self.simple_custom = ttk.Radiobutton(
            structure_box, text="自定义", variable=self.simple_naming_var,
            value="custom", command=self._update_controls,
        )
        self.simple_custom.grid(row=4, column=0, sticky="w", pady=(8, 0))
        self.custom_entry = ttk.Entry(structure_box, textvariable=self.custom_name_var)
        self.custom_entry.grid(
            row=4, column=1, columnspan=5, sticky="ew", padx=(8, 0), pady=(8, 0)
        )

        conflict_box = ttk.LabelFrame(
            main, text="3. 同名冲突处理", padding=12,
            style="Section.TLabelframe",
        )
        conflict_box.grid(row=6, column=0, columnspan=3, sticky="ew", pady=6)
        conflict_box.columnconfigure(4, weight=1)
        ttk.Label(conflict_box, text="同名目录：").grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(
            conflict_box, text="自动编号（目录名_2）", variable=self.directory_conflict_var,
            value="number",
        ).grid(row=0, column=1, sticky="w", padx=(8, 18))
        ttk.Radiobutton(
            conflict_box, text="合并同一路径（增量推荐）", variable=self.directory_conflict_var,
            value="merge",
        ).grid(row=0, column=2, sticky="w")

        ttk.Label(conflict_box, text="同名文件：").grid(
            row=1, column=0, sticky="w", pady=(10, 0)
        )
        ttk.Radiobutton(
            conflict_box, text="自动编号", variable=self.file_conflict_var,
            value="number",
        ).grid(row=1, column=1, sticky="w", padx=(8, 18), pady=(10, 0))
        ttk.Radiobutton(
            conflict_box, text="跳过", variable=self.file_conflict_var, value="skip",
        ).grid(row=1, column=2, sticky="w", pady=(10, 0))
        ttk.Radiobutton(
            conflict_box, text="覆盖", variable=self.file_conflict_var,
            value="overwrite",
        ).grid(row=1, column=3, sticky="w", padx=(18, 0), pady=(10, 0))

        ttk.Checkbutton(
            conflict_box,
            text="内容相同则跳过（SHA-256完全一致才跳过，图片不会被无条件跳过）",
            variable=self.skip_identical_var,
        ).grid(row=2, column=0, columnspan=5, sticky="w", pady=(10, 0))
        ttk.Label(
            conflict_box,
            text="判断顺序：先检查内容是否相同；不同内容再执行上方同名文件策略。",
            foreground="#555555",
        ).grid(row=3, column=0, columnspan=5, sticky="w", pady=(6, 0))

        record_box = ttk.LabelFrame(
            main, text="4. 记录与预览", padding=12,
            style="Section.TLabelframe",
        )
        record_box.grid(row=7, column=0, columnspan=3, sticky="ew", pady=6)
        ttk.Checkbutton(
            record_box, text="生成CSV来源记录（推荐）", variable=self.write_csv_var
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            record_box,
            text="CSV记录源文件、目标文件、复制/覆盖/编号/跳过结果、大小、哈希说明和时间。",
            foreground="#555555",
        ).grid(row=1, column=0, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(main)
        buttons.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        buttons.columnconfigure(0, weight=1)
        self.preview_button = ttk.Button(
            buttons, text="预览映射与统计", command=self._preview_matches
        )
        self.preview_button.grid(row=0, column=0, sticky="e", padx=(0, 8))
        self.cancel_button = ttk.Button(
            buttons, text="取消", command=self.cancel_event.set, state="disabled"
        )
        self.cancel_button.grid(row=0, column=1, padx=(0, 8))
        self.start_button = ttk.Button(
            buttons, text="开始提取", command=self._start_extract,
            style="Primary.TButton",
        )
        self.start_button.grid(row=0, column=2)

        self.progress = ttk.Progressbar(main, mode="determinate", maximum=100)
        self.progress.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(14, 5))
        ttk.Label(main, textvariable=self.status_var).grid(
            row=10, column=0, columnspan=3, sticky="w"
        )
        self.log = tk.Text(main, height=9, state="disabled", wrap="word")
        self.log.grid(row=11, column=0, columnspan=3, sticky="nsew", pady=(8, 0))

    def _apply_recommended(self) -> None:
        self.structure_var.set("relative")
        self.relative_levels_var.set(0)
        self.directory_conflict_var.set("merge")
        self.file_conflict_var.set("number")
        self.skip_identical_var.set(True)
        self.write_csv_var.set(True)
        self.recursive_var.set(True)
        self._update_controls()
        self.status_var.set("已应用推荐配置：保留完整相对目录，支持增量与追溯。")

    def _update_controls(self) -> None:
        self.level_spin.configure(
            state="normal" if self.mode_var.get() == "level" else "disabled"
        )
        self.name_entry.configure(
            state="normal" if self.mode_var.get() == "name" else "disabled"
        )
        relative = self.structure_var.get() == "relative"
        combined = self.structure_var.get() == "combined"
        simple = self.structure_var.get() == "simple"
        self.relative_spin.configure(state="normal" if relative else "disabled")
        self.combined_spin.configure(state="normal" if combined else "disabled")
        for widget in (self.simple_current, self.simple_ancestor, self.simple_custom):
            widget.configure(state="normal" if simple else "disabled")
        self.ancestor_spin.configure(
            state="normal"
            if simple and self.simple_naming_var.get() == "ancestor"
            else "disabled"
        )
        self.custom_entry.configure(
            state="normal"
            if simple and self.simple_naming_var.get() == "custom"
            else "disabled"
        )

    def _choose_source(self) -> None:
        selected = filedialog.askdirectory(title="选择根目录")
        if selected:
            self.source_var.set(selected)

    def _choose_output(self) -> None:
        selected = filedialog.askdirectory(title="选择输出目录")
        if selected:
            self.output_var.set(selected)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text + os.linesep)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _read_options(self) -> ExtractOptions:
        try:
            level = int(self.level_var.get())
            relative_levels = int(self.relative_levels_var.get())
            combined_levels = int(self.combined_levels_var.get())
            ancestor = int(self.ancestor_var.get())
        except (tk.TclError, ValueError) as exc:
            raise ValueError("目录级数必须填写整数。") from exc
        return ExtractOptions(
            source=Path(self.source_var.get().strip()),
            output=Path(self.output_var.get().strip()),
            mode=self.mode_var.get(),
            target_level=level,
            target_name=self.target_name_var.get().strip(),
            recursive_files=self.recursive_var.get(),
            structure_mode=self.structure_var.get(),
            relative_keep_levels=relative_levels,
            combined_levels=combined_levels,
            simple_naming=self.simple_naming_var.get(),
            ancestor_steps=ancestor,
            custom_name=self.custom_name_var.get().strip(),
            directory_conflict=self.directory_conflict_var.get(),
            file_conflict=self.file_conflict_var.get(),
            skip_identical=self.skip_identical_var.get(),
            write_csv=self.write_csv_var.get(),
        )

    def _validated_options(self) -> ExtractOptions:
        if not self.source_var.get().strip() or not self.output_var.get().strip():
            raise ValueError("请先选择根目录和输出目录。")
        options = self._read_options()
        validate_options(options)
        return options

    def _start_extract(self) -> None:
        try:
            options = self._validated_options()
        except (ValueError, OSError) as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return
        self.cancel_event.clear()
        self.start_button.configure(state="disabled")
        self.preview_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress["value"] = 0
        self.status_var.set("正在扫描目录和文件……")
        self._append_log("开始处理……")
        threading.Thread(target=self._worker, args=(options,), daemon=True).start()

    def _preview_matches(self) -> None:
        try:
            options = self._validated_options()
            targets = find_target_folders(options)
            plans = plan_output_folders(targets, options)
            excluded = (
                options.output.resolve()
                if is_within(options.output.resolve(), options.source.resolve())
                else None
            )
            file_counts = [
                len(files_in_folder(plan.target, options.recursive_files, excluded))
                for plan in plans
            ]
        except (ValueError, OSError) as exc:
            messagebox.showerror(APP_TITLE, str(exc))
            return

        unique_outputs = {path_key(plan.output_folder) for plan in plans}
        output = options.output.resolve()
        existing_conflicts = sum(
            1
            for target in targets
            if (output / desired_relative_output(target, options)).exists()
        )
        lines: list[str] = []
        for plan, count in zip(plans, file_counts):
            if len(lines) >= 300:
                break
            try:
                source_relative = plan.target.resolve().relative_to(options.source.resolve())
            except (OSError, ValueError):
                source_relative = plan.target
            try:
                output_relative = plan.output_folder.relative_to(output)
            except ValueError:
                output_relative = plan.output_folder
            lines.append(f"{source_relative}  →  {output_relative}  （{count}个文件）")

        window = tk.Toplevel(self)
        window.title("映射与统计预览")
        window.geometry("950x620")
        window.minsize(720, 450)
        frame = ttk.Frame(window, padding=14)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        file_policy_text = {
            "skip": "跳过",
            "overwrite": "覆盖",
            "number": "自动编号",
        }[options.file_conflict]
        summary = (
            f"命中目录：{len(targets)} 个    文件：{sum(file_counts)} 个    "
            f"最终输出目录：{len(unique_outputs)} 个    "
            f"已存在的目标目录：{existing_conflicts} 个\n"
            f"目录策略：{'合并' if options.directory_conflict == 'merge' else '自动编号'}；"
            f"文件策略：{file_policy_text}；"
            f"SHA-256相同跳过：{'是' if options.skip_identical else '否'}。"
        )
        ttk.Label(frame, text=summary, wraplength=900).grid(
            row=0, column=0, sticky="w", pady=(0, 8)
        )
        text_box = tk.Text(frame, wrap="none")
        text_box.grid(row=1, column=0, sticky="nsew")
        y_scroll = ttk.Scrollbar(frame, orient="vertical", command=text_box.yview)
        y_scroll.grid(row=1, column=1, sticky="ns")
        x_scroll = ttk.Scrollbar(frame, orient="horizontal", command=text_box.xview)
        x_scroll.grid(row=2, column=0, sticky="ew")
        text_box.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        text_box.insert(
            "1.0",
            "\n".join(lines) if lines else "没有找到符合条件的目录。",
        )
        text_box.configure(state="disabled")

    def _worker(self, options: ExtractOptions) -> None:
        def report(current: int, total: int, detail: str) -> None:
            self.events.put(("progress", current, total, detail))
        try:
            result = extract_files(options, report, self.cancel_event.is_set)
            self.events.put(("done", result, str(options.output.resolve())))
        except Exception as exc:
            self.events.put(("fatal", str(exc)))

    def _process_events(self) -> None:
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    _, current, total, detail = event
                    self.progress["value"] = current / total * 100 if total else 0
                    self.status_var.set(f"正在处理：{current}/{total}")
                    self._append_log(detail)
                elif event[0] == "done":
                    _, result, output = event
                    self.start_button.configure(state="normal")
                    self.preview_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    cancelled = self.cancel_event.is_set()
                    if not cancelled:
                        self.progress["value"] = 100
                    summary = (
                        f"匹配目录：{result.matched_folders} 个\n"
                        f"扫描文件：{result.total_files} 个\n"
                        f"成功写入：{result.copied_files} 个\n"
                        f"其中覆盖：{result.overwritten_files} 个，自动编号：{result.renamed_files} 个\n"
                        f"内容相同跳过：{result.identical_skipped} 个\n"
                        f"同名冲突跳过：{result.conflict_skipped} 个\n"
                        f"失败：{len(result.errors)} 个\n"
                        f"输出：{output}"
                    )
                    if result.csv_path:
                        summary += f"\nCSV记录：{result.csv_path}"
                    if result.errors:
                        self._append_log("失败项目：")
                        for error in result.errors:
                            self._append_log(error)
                    if cancelled:
                        self.status_var.set("已取消，已完成的文件和CSV记录会保留。")
                        messagebox.showwarning(APP_TITLE, "任务已取消。\n\n" + summary)
                    elif result.matched_folders == 0:
                        self.status_var.set("完成，但没有找到符合条件的目录。")
                        messagebox.showwarning(APP_TITLE, "没有找到符合条件的目录。")
                    else:
                        self.status_var.set(
                            f"完成：写入 {result.copied_files}，"
                            f"内容相同跳过 {result.identical_skipped}。"
                        )
                        messagebox.showinfo(APP_TITLE, "处理完成。\n\n" + summary)
                elif event[0] == "fatal":
                    self.start_button.configure(state="normal")
                    self.preview_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    self.status_var.set("处理失败。")
                    self._append_log("错误：" + event[1])
                    messagebox.showerror(APP_TITLE, "处理失败：\n" + event[1])
        except queue.Empty:
            pass
        self.after(100, self._process_events)


if __name__ == "__main__":
    ExtractorApp().mainloop()
