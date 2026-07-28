"""本文件验证所有人工维护的代码、配置和说明文件均以用途说明开头。"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_maintained_text_files_have_purpose_headers() -> None:
    expected: dict[Path, tuple[str, ...]] = {}
    for folder in ("src", "gui", "scripts", "tests"):
        for path in (PROJECT_ROOT / folder).glob("*.py"):
            expected[path] = ('"""本文件',)
    for path in (PROJECT_ROOT / "config").glob("*.yaml"):
        expected[path] = ("# 本文件",)
    for path in (PROJECT_ROOT / ".github/workflows").glob("*.yml"):
        expected[path] = ("# 本文件",)
    for path in (PROJECT_ROOT / "resources/styles").glob("*.qss"):
        expected[path] = ("/* 本文件",)
    for path in (PROJECT_ROOT / "packaging").glob("*.spec"):
        expected[path] = ('"""本文件',)
    expected.update({
        PROJECT_ROOT / ".gitattributes": ("# 本文件",),
        PROJECT_ROOT / ".gitignore": ("# 本文件",),
        PROJECT_ROOT / "README.md": ("<!-- 本文件",),
        PROJECT_ROOT / "requirements.txt": ("# 本文件",),
        PROJECT_ROOT / "requirements-dev.txt": ("# 本文件",),
        PROJECT_ROOT / "requirements-gui.lock.txt": ("# 本文件",),
        PROJECT_ROOT / "requirements-build.txt": ("# Build-time",),
        PROJECT_ROOT / "setup_company.bat": ("@rem 本文件",),
        PROJECT_ROOT / "run_gui.bat": ("@rem 本文件",),
    })
    problems = []
    for path, prefixes in expected.items():
        first_line = path.read_text(encoding="utf-8-sig").splitlines()[0]
        if not first_line.startswith(prefixes):
            problems.append(f"{path.relative_to(PROJECT_ROOT)}: {first_line}")
    assert not problems, "文件头用途说明缺失：\n" + "\n".join(problems)
