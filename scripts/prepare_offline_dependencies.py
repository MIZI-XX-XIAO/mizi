"""本文件按uv导出的锁定依赖下载Windows离线wheel并生成SHA-256校验清单。"""

import argparse
import hashlib
import json
import os
import struct
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare offline wheels for the current Python/Windows environment.")
    parser.add_argument("--requirements", type=Path, default=PROJECT_ROOT / "requirements.txt")
    parser.add_argument("--wheels", type=Path, default=PROJECT_ROOT / "wheels")
    parser.add_argument("--manifest-only", action="store_true", help="不下载，只重新计算现有离线文件校验清单。")
    args = parser.parse_args()
    if sys.version_info[:2] != (3, 12) or struct.calcsize("P") * 8 != 64:
        raise RuntimeError("离线依赖必须使用64位Python 3.12准备。")
    requirements = args.requirements.resolve()
    if not requirements.is_file():
        raise FileNotFoundError(f"依赖锁定文件不存在：{requirements}")
    args.wheels.mkdir(parents=True, exist_ok=True)
    if any(args.wheels.iterdir()) and not args.manifest_only:
        raise FileExistsError("wheels目录不是空目录；为避免混入旧版本，请选择新的空目录。")
    if not args.manifest_only:
        environment = dict(os.environ)
        environment["PYTHONUTF8"] = "1"
        subprocess.run([sys.executable, "-m", "pip", "download", "-r", str(requirements), "-d", str(args.wheels)],
                       check=True, env=environment)
    files = []
    for path in sorted(args.wheels.iterdir()):
        if path.is_file() and path.name != "offline_manifest.json":
            files.append({"name": path.name, "bytes": path.stat().st_size,
                          "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    manifest = {
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "python": sys.version,
        "requirements": {
            "name": requirements.name,
            "sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
        },
        "files": files,
    }
    (args.wheels / "offline_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Offline dependencies prepared: {args.wheels}")


if __name__ == "__main__":
    main()
