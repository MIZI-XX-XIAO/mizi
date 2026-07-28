@rem 本文件从项目虚拟环境启动Qt软件，并在Python或依赖缺失时给出公司电脑部署提示。
@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] 未找到.venv，请先运行setup_company.bat。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import cv2, pandas, PySide6, yaml" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] 运行依赖不完整，请重新运行setup_company.bat。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -X utf8 "scripts\start_gui.py"
if errorlevel 1 (
  echo [ERROR] 软件异常退出，请查看界面提示或本地应用日志。
  pause
  exit /b 1
)
endlocal
exit /b 0
