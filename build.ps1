$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $ProjectRoot

python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "图片人工复核分类工具" `
    --collect-all PIL `
    main.py

Write-Host "构建完成：$ProjectRoot\dist\图片人工复核分类工具.exe"

