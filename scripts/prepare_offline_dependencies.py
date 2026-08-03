"""本文件在联网的家庭电脑下载Windows离线依赖并生成版本锁定清单与SHA-256校验清单。"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DIRECT_PACKAGES = [
    "numpy", "opencv-python", "pandas", "openpyxl", "et-xmlfile", "matplotlib", "PyYAML", "pytest", "psutil",
    "PySide6", "PySide6_Addons", "PySide6_Essentials", "shiboken6", "pytest-qt",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare offline wheels for the current Python/Windows environment.")
    parser.add_argument("--requirements", type=Path, default=PROJECT_ROOT / "requirements.txt")
    parser.add_argument("--wheels", type=Path, default=PROJECT_ROOT / "wheels")
    parser.add_argument("--manifest-only", action="store_true", help="不下载，只重新计算现有离线文件校验清单。")
    args = parser.parse_args()
    args.wheels.mkdir(parents=True, exist_ok=True)
    if any(args.wheels.iterdir()) and not args.manifest_only:
        raise FileExistsError("wheels目录不是空目录；为避免混入旧版本，请选择新的空目录。")
    if not args.manifest_only:
        lock = "# 本文件锁定家庭电脑验证过的GUI直接依赖版本，供公司电脑离线复现。\n"
        lock += "\n".join(f"{name}=={version(name)}" for name in DIRECT_PACKAGES) + "\n"
        lock_path = PROJECT_ROOT / "requirements-gui.lock.txt"
        # Windows中文区域的pip可能按GBK读取无BOM文件，因此锁文件显式写为UTF-8 BOM。
        lock_path.write_text(lock, encoding="utf-8-sig")
        environment = dict(os.environ)
        environment["PYTHONUTF8"] = "1"
        subprocess.run([sys.executable, "-m", "pip", "download", "-r", str(lock_path), "-d", str(args.wheels)],
                       check=True, env=environment)
    files = []
    for path in sorted(args.wheels.iterdir()):
        if path.is_file() and path.name != "offline_manifest.json":
            files.append({"name": path.name, "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {"created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "python": sys.version, "files": files}
    (args.wheels / "offline_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Offline dependencies prepared: {args.wheels}")


if __name__ == "__main__":
    main()
