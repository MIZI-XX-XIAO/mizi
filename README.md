<!-- 本文件说明生产缺陷分析软件的安装、输入数据、界面流程、输出结果、测试和发布方法。 -->
# MEA多工站缺陷规律与工艺关联分析

本项目是一套完全本地运行的 Windows 桌面软件，覆盖 3-5、5-7 和 Conveyor 的18个工站。程序按工站和 Ident No. 自动组织多视图图片，从主图对提取缺陷，并分析Excel工艺/质量数据与图片缺陷的统计关系。程序不上传生产数据。

## 软件界面

界面采用七个工作模块：

1. 新建任务：选择工站、Excel工作簿、图片根目录、结果目录，并在数据检查后按AOI/VI来源、缺陷代码、分析模块和工艺参数限定本次分析目标。
2. 数据检查：校验Excel的Location(s)，并检查DMC匹配、多视图覆盖、重复图和缺图。
3. 执行分析：后台提取缺陷并显示进度、资源使用、预计时间和实时告警。
4. 结果概览：分别查看代码规律、图片空间规律或二者联合规律，支持AOI/VI来源、单个或多个缺陷代码筛选。
5. Excel分析：读取测试工作簿，统计State、Tolerance超差、判定冲突、趋势和分组质量。
6. 关联分析：按综合证据分优先展示Top 10重点发现，并分析数值工艺参数与图片/AOI/VI缺陷之间的线性、非线性、阈值和双参数交互关系。
7. 图片复核：查看 A图、E图、差异图和Mask，支持检测框、同步缩放拖动、缺陷导航及局部原图。

窗口会适配常见办公电脑分辨率和 Windows DPI 缩放，并记忆窗口、路径、表格列宽及复核布局。

“指定缺陷代码分析”支持同时选择多个代码，各代码分别建立目标组和工艺关联结果；未出现目标代码的产品仍保留为完整生产序列和正常对照。只选择代码时序规律或工艺参数关联时可以不提供图片目录，程序直接执行纯Excel目标分析。

## 输入数据

公司日常使用不需要产品 CSV。图片文件名按 `Ident No.+YYYYMMDD+图片代码` 解析，例如 `376W...004AK20250624DA.png`。程序根据所选工站自动识别5S的`DA/DC/DE/DX/DY`、5X的`EA/EB/EC/EE/EX/EY`、7S的`FA/FC/FE/FX/FY`和7X的`GA/GC/GE/GX/GY`。现有算法使用A/E视图主对，其他视图同时建档。

下载的Excel与图片可以放在不同目录，程序按 Ident No. 精确重新汇合。Excel有Location(s)且与界面工站不一致时会阻止任务；Excel有记录但缺图时保留记录并报告覆盖率，只跳过该产品的图片算法。

工艺参数 CSV 应包含 `product_id`、`order_code`、`dmc_raw` 或 `global_order` 之一进行精确关联。没有共同产品键时，可使用 `production_timestamp` 或 `timestamp` 按界面容差就近匹配。其他数值列作为工艺参数参与分析。统计关联不代表因果关系。

Excel质量分析支持`.xlsx`和`.xlsm`，并按工站分为WP、AOI、VI三种分析档。WP/AOI保留`Result.*`与`Tolerance`重算；AOI额外统计`AOIFailureCode`；VI在没有数值容差时统计Block code、Document Version、fail1、Failures area/code、Result.From和StationNo。

缺陷证据采用三层独立建模：AOI的`Result.AOIFailureCode`取首个数字段前4位；VI只读取`MS0335all`，`NOK`使用`Failure code`/`Failures code`作为缺陷代码，`OTHERS`使用`BlockCode`作为封存代码（个别导出把5050写入Failures code时仍以OTHERS状态判定为封存）。VI复合代码按分隔后的每个数字段分别取末4位，例如`189997_189998_`解析为`9997;9998`。图片层独立分析固定点、周期、连续异常和水平轨迹。VI人工代码不会覆盖AOI或图片结论。时序规律按同一工站的真实`production_order`计算；图片任务导航使用独立的`task_order`，缺图或过站事件不明确的产品不计为“周期缺失”。

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

旧版`products.csv`仅保留在高级设置中用于历史数据集和开发验收，公司任务不再依赖该文件。

## 开发与测试

开发电脑使用 `uv` 管理Python 3.12、项目`.venv`和锁定依赖：

```powershell
uv python install 3.12
uv sync --frozen
$env:QT_QPA_PLATFORM="offscreen"
uv run --frozen python -X utf8 -m pytest -p no:cacheprovider -q
```

`pyproject.toml`是人工维护的依赖入口，`uv.lock`是权威锁文件。`requirements.txt`、
`requirements-dev.txt`和`requirements-build.txt`仅用于兼容pip部署，由`uv export`生成，不能手工编辑。
开发时可使用`uv run --frozen python -X utf8 scripts/start_gui.py`启动GUI；公司电脑仍使用
`setup_company.bat`和`run_gui.bat`，无需安装uv。

普通测试使用运行时生成的临时A/E图片，不需要仓库携带数据。若需运行真实图片端到端测试，将`MEA5S_REAL_DATA_ROOT`设置为包含`products.csv`的本地数据集目录：

```powershell
$env:MEA5S_REAL_DATA_ROOT="D:\本地数据\dataset_realistic"
uv run --frozen python -X utf8 -m pytest tests\test_real_images_e2e.py -q
```

## 任务输出

每次任务创建独立的“任务名+时间”结果目录，不覆盖历史结果。主要文件包括：

- `extracted_defects.csv`、`spatial_clusters.csv`
- `discovered_patterns.csv`、`alerts.csv`
- `normalized_defect_codes.csv`、`defect_code_catalog_snapshot.csv`
- `code_patterns.csv`、`spatial_trajectories.csv`
- `code_spatial_associations.csv`、`code_label_conflicts.csv`
- `station_attribution.csv`
- `analysis_summary.json`、`analysis_config_snapshot.yaml`
- `task_manifest.json`、`analysis_selection.json`、`visualizations/`
- `defect_cooccurrence.csv`、`defect_transitions.csv`

执行工艺关联后还会生成：

- `process_parameter_metrics.csv`
- `process_parameter_binned_rates.csv`
- `process_model_importance.csv`
- `process_nonlinear_importance.csv`、`process_nonlinear_effects.csv`
- `process_risk_curves.csv`
- `process_interactions.csv`、`process_model_validation.csv`
- `association_findings.csv`、`association_findings.json`
- `process_joined.csv`（纯Excel目标任务）
- `process_relationship_summary.json`

执行Excel质量分析会生成：

- `excel_analysis_summary.json`、`excel_standardized_data.csv`
- `excel_parameter_statistics.csv`、`excel_tolerance_violations.csv`
- `excel_judgement_conflicts.csv`、`excel_group_quality.csv`
- `excel_data_quality.csv`、`visualizations/quality_trend.png`
- `excel_categorical_statistics.csv`（AOI/VI分类失效统计）

图片任务还会保存`station_product_index.csv`和`station_source_issues.csv`，用于追溯Excel记录、多视图路径、缺图和重复图。

取消任务会保留部分提取结果；失败任务保留错误编号、状态和 traceback。应用滚动日志位于当前用户的 `LOCALAPPDATA/MEA5SDefectAnalysis/logs/`。

GitHub Actions在Windows和Python 3.12环境中使用uv校验锁文件及pip导出文件，并执行无生产数据的分析服务、CSV写入和Qt离屏测试。真实数据不会上传到GitHub Actions。

## 一键提交到GitHub

双击仓库根目录的 `一键提交到GitHub.bat`，输入 commit 信息并确认后，脚本会：

1. 排除 `__pycache__`、`.pyc` 和分析输出等生成文件。
2. 列出本次准备提交的文件，等待确认。
3. 检查远端 `main` 是否包含本地尚未同步的提交，避免覆盖他人改动。
4. 提交并直接非强制推送到 `origin/main`，不创建新分支或Pull Request。
5. 如果运行前位于其他分支，推送成功后自动将本地切回 `main`。

也可以在PowerShell中运行：

```powershell
.\scripts\publish_to_github.ps1 -Message "feat: update analysis UI"
```

仅检查将要提交的内容而不修改仓库：

```powershell
.\scripts\publish_to_github.ps1 -Message "dry run" -DryRun
```

## 离线安装

联网开发电脑先执行`uv sync --frozen`，再运行
`uv run --frozen python scripts/prepare_offline_dependencies.py`。脚本直接读取由uv锁定导出的
`requirements.txt`并生成wheel及SHA-256清单。公司电脑创建虚拟环境后执行：

```powershell
python -X utf8 -m pip install --no-index --find-links wheels -r requirements.txt
```

## 便携版发布

```powershell
uv sync --frozen --group build
uv run --frozen --group build python scripts/build_portable.py --clean
```

将整个 `dist/MEA5S缺陷分析/` 目录复制到公司电脑，运行其中的 `MEA5S缺陷分析.exe`，不能只复制单独的 EXE。发布包无需公司电脑安装 Python 或授予管理员权限，`release_manifest.json` 保存发布文件SHA-256。

## MES工作簿下载

在“新建任务”的“Excel工作簿（可选）”右侧点击“从MES下载…”，设置开始/结束时间、可选的10位型号及保存目录。程序会启动Firefox访问OIS Portal，完成全部查询和初步整理后自动把生成的工作簿填回任务。公司账号可以临时输入，密码仅在本次下载期间保存在内存中，不写入设置或源码；留空时可在Firefox中手动登录。

该功能需要公司网络、Firefox，以及发布包内的 `MES/firefoxdriver/geckodriver.exe`。同一天的不同时间范围会写入不同目录和文件名，避免覆盖已有下载。

## 公司图片网站下载

在“新建任务”中点击“从公司网站自动下载图片…”，或者在MES下载完成后点击“继续下载图片”。程序会从MES工作簿提取25位 `Ident No.`，使用Edge打开公司图片网站，并在直接输入页逐行填写DMC后分批下载图片，不使用网站的Excel导入入口。读取工作簿后会按5S/5X/7S/7X AOI站列出产品号并默认全选，可搜索后勾选、清空或反选，也可以粘贴以换行、逗号、分号或空格分隔的DMC清单来替换当前选择；粘贴项会与工作簿校验，同一DMC出现在多个AOI站时会匹配所有对应站。

- 默认每批80个产品号，最大不能超过100个。
- 图片代码和原图/压缩图由每次任务明确选择；“去除7层返工站”默认开启。
- 单张缺失或异常导致整批失败时，程序会自动重试并按产品号、图片代码递归拆分，保留其他正常图片。
- 下载完成后校验ZIP和TIFF，将D/E/F/G系列分别放入5S/5X/7S/7X目录，并自动回填新建任务。
- 每个网站批次的原始ZIP都会改成包含批次、图片族、产品数、图片代码和质量的唯一文件名，长期保存在任务目录的`archives`文件夹；重新运行和异常重试只追加新ZIP，不覆盖或删除已有归档。
- 分类目录中已有同名图片时，相同内容直接复用，内容不同则放入`_quarantine`隔离，不覆盖先前图片。
- 异常项写入 `image_download_issues.csv` 和 `image_download_summary.xlsx`；可通过“仅重试异常项…”继续失败内容。
- LDAP密码仅在本次后台任务内存中使用，不保存到配置、日志或任务清单。
- 产品号只取自 `MS03106`、`MS03206`、`MS03301`、`MS03302` 四个AOI工站；专用页签缺失时按工站位置兜底。
- 多工站任务按D/E/F/G图片族独立分批，不会把一个AOI工站的产品号交叉请求到其他工站。
- 每个任务保存 `image_download_run.log`，记录网站识别数量、批次状态和原始报错，便于现场排查。

图片网站依赖公司网络、企业证书及登录权限。便携版应携带与公司Edge主版本一致的 `image_downloader/msedgedriver.exe`；未携带时程序会尝试通过Selenium Manager解析驱动。
程序使用 `%LOCALAPPDATA%/MEA5SDefectAnalysis/edge-image-profile/` 中的软件专用普通Edge配置，
不会占用日常Edge配置。首次使用需在Edge中完成公司认证；如出现Windows Security系统窗口，
请手动输入凭据，认证成功后程序会自动继续并在后续任务中复用登录状态。

发布验收可使用 `--validate-images products.csv --image-root 图片路径根目录 --report 报告.json`，让便携版实际解码首组A/E图片并输出四个复核场景的检查结果。
