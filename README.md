# 图片人工复核分类工具

这是一个离线 Windows 桌面工具，用于逐张查看图片，并把图片安全地移动到人工确认的分类目录。

## 使用方法

1. 双击 `dist\图片人工复核分类工具.exe`。
2. 选择存放待复核图片的源目录（软件不会扫描子目录）。
3. 填写分类名称并为每个分类选择目标目录，最多 9 个。
4. 点击“开始复核”，用数字键 `1–9` 或分类按钮移动当前图片。
5. `Space` 暂不处理，`Ctrl+Z` 撤销最近一次移动，方向键浏览图片。

目标目录出现同名文件时，软件绝不会覆盖，会让用户选择自动追加序号、跳过或取消。退出时会保存未完成任务，下次启动可以恢复。CSV 复核记录保存在源图片目录，程序错误日志保存在 `%LOCALAPPDATA%\ImageReviewClassifier\logs`。

## 支持格式

JPG、JPEG、PNG、BMP、TIF、TIFF、WEBP。

## 从源码运行

```powershell
python -m pip install -r requirements.txt
python main.py
```

## 测试与打包

```powershell
python -m unittest discover -s tests -v
.\build.ps1
```

打包结果位于 `dist\图片人工复核分类工具.exe`。构建环境为 Python 3.11、Windows 10/11 64 位。
