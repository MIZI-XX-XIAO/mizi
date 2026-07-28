"""本文件构建可复制到公司电脑直接运行的Windows onedir发布包。"""

from __future__ import annotations

from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true", help="删除旧build/dist后重新构建")
    args = parser.parse_args()
    build_dir, dist_dir = ROOT / "build", ROOT / "dist"
    if args.clean:
        for path in (build_dir, dist_dir):
            if path.exists():
                shutil.rmtree(path)
    target = dist_dir / "MEA5S缺陷分析"
    if target.exists():
        raise FileExistsError(f"发布目录已存在：{target}；使用 --clean 显式重建")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(ROOT / "packaging/mea_5s_analysis.spec"),
         "--distpath", str(dist_dir), "--workpath", str(build_dir)],
        cwd=ROOT, check=True,
    )
    files = []
    for path in sorted(item for item in target.rglob("*") if item.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files.append({
            "path": path.relative_to(target).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": digest,
        })
    (target / "release_manifest.json").write_text(
        json.dumps({"python": sys.version, "files": files}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Portable release created: {target}")


if __name__ == "__main__":
    main()
