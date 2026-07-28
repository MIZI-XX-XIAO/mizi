@rem 本文件在公司Windows电脑上检查Python 3.12并创建或复用项目虚拟环境。
@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

echo [1/4] 检查64位Python 3.12...
set "PYTHON_EXE="
set "PYTHON_ARGS="
where py >nul 2>nul
if not errorlevel 1 (
  py -3.12 -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P')*8 == 64 else 1)" >nul 2>nul
  if not errorlevel 1 (
    set "PYTHON_EXE=py"
    set "PYTHON_ARGS=-3.12"
  )
)
if not defined PYTHON_EXE (
  where python >nul 2>nul
  if not errorlevel 1 (
    python -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P')*8 == 64 else 1)" >nul 2>nul
    if not errorlevel 1 set "PYTHON_EXE=python"
  )
)
if not defined PYTHON_EXE (
  echo [ERROR] 未找到64位Python 3.12。请确认 python --version 或 py -3.12 可用。
  goto :failed
)

echo [2/4] 创建或检查虚拟环境...
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import struct,sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) and struct.calcsize('P')*8 == 64 else 1)" >nul 2>nul
  if errorlevel 1 (
    echo [ERROR] 现有.venv不是64位Python 3.12；为保护本地环境，脚本没有删除或覆盖它。
    echo         请人工重命名该目录后重新运行本脚本。
    goto :failed
  )
) else (
  "%PYTHON_EXE%" %PYTHON_ARGS% -m venv ".venv"
  if errorlevel 1 (
    echo [ERROR] 创建.venv失败。
    goto :failed
  )
)

echo [3/4] 从当前pip配置或PIP_INDEX_URL安装依赖...
".venv\Scripts\python.exe" -X utf8 -m pip install -r "requirements.txt"
if errorlevel 1 (
  echo [ERROR] 依赖安装失败。请检查公司PyPI镜像、网络和pip配置。
  echo         可运行 .venv\Scripts\python.exe -m pip config list 查看当前配置。
  goto :failed
)

echo [4/4] 验证核心依赖...
".venv\Scripts\python.exe" -c "import cv2, matplotlib, numpy, pandas, psutil, PySide6, yaml; print('依赖验证通过')"
if errorlevel 1 (
  echo [ERROR] 核心依赖导入失败。
  goto :failed
)

echo [OK] 公司电脑运行环境准备完成。请双击run_gui.bat启动。
exit /b 0

:failed
echo [FAILED] 环境准备未完成，现有数据和虚拟环境未被删除。
pause
exit /b 1
