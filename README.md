<!-- 本文件说明生产缺陷分析软件的安装、输入数据、界面流程、输出结果、测试和发布方法。 -->
# MEA 5S 缺陷规律与工艺关联分析

本项目是一套完全本地运行的 Windows 桌面软件，用于从成对 A/E 图片中提取缺陷、发现空间与时间规律、生成预警，并分析工艺参数与缺陷之间的统计关系。程序不上传图片、不调用云服务，也不依赖独立显卡。

## 软件界面

界面采用六步工作流：

1. 新建任务：选择产品清单、图片根目录、结果目录和分析参数。
2. 数据检查：检查字段、产品序号、空值及图片路径。
3. 执行分析：后台提取缺陷并显示进度、资源使用、预计时间和实时告警。
4. 结果概览：查看规律、预警、缺陷共现和序列关系，支持筛选、排序、搜索和导出。
5. 关联分析：分析数值工艺参数与缺陷之间的相关性、效应量、区间缺陷率和模型重要性。
6. 图片复核：查看 A图、E图、差异图和Mask，支持检测框、同步缩放拖动、缺陷导航及局部原图。

窗口会适配常见办公电脑分辨率和 Windows DPI 缩放，并记忆窗口、路径、表格列宽及复核布局。

## 输入数据

产品 CSV 每行代表一个产品，至少需要 `global_order`、`camera`、`a_image_path`（兼容 `v_image_path`）和 `e_image_path`。推荐同时提供 `order_code`、`dmc_raw`、`product_id`、`batch`、`machine`、`line`、`recipe` 和 `production_timestamp`。

相对图片路径默认相对于项目目录解析，也可在界面指定图片根目录。A/E图片必须为8位、宽高一致并严格对齐；A图可为灰度图，E图为彩色图且AOI缺陷轮廓应以红色标记。

工艺参数 CSV 应包含 `product_id`、`order_code`、`dmc_raw` 或 `global_order` 之一进行精确关联。没有共同产品键时，可使用 `production_timestamp` 或 `timestamp` 按界面容差就近匹配。其他数值列作为工艺参数参与分析。统计关联不代表因果关系。

## 从GitHub部署到公司电脑

GitHub仓库只保存源码、配置、测试和部署脚本，不包含图片、产品CSV、工艺参数、分析结果、虚拟环境、wheel或EXE。生产数据应放在仓库外部，并在界面中选择。

```powershell
git clone https://github.com/MIZI-XX-XIAO/mizi.git
cd mizi
.\setup_company.bat
.\run_gui.bat
```

`setup_company.bat`要求64位Python 3.12，创建项目自己的`.venv`，并从当前pip配置或环境变量`PIP_INDEX_URL`指定的公司镜像安装依赖。脚本不会删除或覆盖不兼容的现有虚拟环境，也不会读取或上传生产数据。

更新稳定源码：

```powershell
git pull --ff-only
.\setup_company.bat
.\run_gui.bat
```

如果本机存在`data/dataset_realistic/products.csv`，界面仍会将其作为默认产品清单；GitHub克隆环境没有示例数据时，该输入框保持空白。

## 开发与测试

开发电脑使用Python 3.12并安装测试依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -X utf8 -m pip install -r requirements-dev.txt
$env:QT_QPA_PLATFORM="offscreen"
.\.venv\Scripts\python.exe -X utf8 -m pytest -p no:cacheprovider -q
```

普通测试使用运行时生成的临时A/E图片，不需要仓库携带数据。若需运行真实图片端到端测试，将`MEA5S_REAL_DATA_ROOT`设置为包含`products.csv`的本地数据集目录：

```powershell
$env:MEA5S_REAL_DATA_ROOT="D:\本地数据\dataset_realistic"
.\.venv\Scripts\python.exe -X utf8 -m pytest tests\test_real_images_e2e.py -q
```

## 任务输出

每次任务创建独立的“任务名+时间”结果目录，不覆盖历史结果。主要文件包括：

- `extracted_defects.csv`、`spatial_clusters.csv`
- `discovered_patterns.csv`、`alerts.csv`
- `analysis_summary.json`、`analysis_config_snapshot.yaml`
- `task_manifest.json`、`visualizations/`
- `defect_cooccurrence.csv`、`defect_transitions.csv`

执行工艺关联后还会生成：

- `process_parameter_metrics.csv`
- `process_parameter_binned_rates.csv`
- `process_model_importance.csv`
- `process_relationship_summary.json`

取消任务会保留部分提取结果；失败任务保留错误编号、状态和 traceback。应用滚动日志位于当前用户的 `LOCALAPPDATA/MEA5SDefectAnalysis/logs/`。

GitHub Actions在Windows和Python 3.12环境中执行无生产数据的依赖安装、分析服务、CSV写入和Qt离屏测试。真实数据不会上传到GitHub Actions。

## 离线安装

联网电脑运行 `python scripts/prepare_offline_dependencies.py`。公司电脑创建虚拟环境后执行：

```powershell
python -X utf8 -m pip install --no-index --find-links wheels -r requirements-gui.lock.txt
```

## 便携版发布

```powershell
python -m pip install -r requirements-build.txt
python scripts/build_portable.py --clean
```

将整个 `dist/MEA5S缺陷分析/` 目录复制到公司电脑，运行其中的 `MEA5S缺陷分析.exe`，不能只复制单独的 EXE。发布包无需公司电脑安装 Python 或授予管理员权限，`release_manifest.json` 保存发布文件SHA-256。

发布验收可使用 `--validate-images products.csv --image-root 图片路径根目录 --report 报告.json`，让便携版实际解码首组A/E图片并输出四个复核场景的检查结果。
