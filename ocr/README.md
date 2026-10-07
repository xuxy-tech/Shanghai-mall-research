# 大众点评商场录屏 OCR 管线

本目录是录屏采集的长期工作入口。日常流程只有三步：把规范命名的视频放入 `input_videos/`，运行批处理脚本，到 `outputs/` 查看同名结果目录。

## 目录结构

```text
ocr/
├─ input_videos/          # 唯一的视频投放入口
├─ outputs/               # 每座商场的 CSV、审计和证据图
├─ scripts/
│  ├─ analyze_video.py    # OCR、卡片解析、去重和召回审计
│  ├─ run_single.ps1      # 单视频运行与调试入口
│  └─ run_batch.ps1       # 日常批量处理入口
├─ tests/                 # 解析和去重单元测试
├─ models/cache/          # PaddleOCR 模型缓存
└─ README.md
```

`input_videos/` 和 `outputs/` 是业务数据，`scripts/` 和 `tests/` 是程序。不要再创建 `runs/v1/v2/v3` 一类目录。

## 视频命名

统一格式：

```text
城市__商场名__录制日期.mp4
```

例如：

```text
上海__五角场合生汇__20260824.mp4
上海__百联又一城__20260825.mp4
```

分隔符必须是连续两个下划线，日期使用八位 `YYYYMMDD`。批处理脚本会把中间字段作为 `mall_name`，并生成同名输出目录。

## 日常批处理

双击 `ocr/run_batch.bat`，或在项目根目录运行：

```text
ocr/run_batch.bat
```

启动器会检查专属 Python 环境和输入视频，随后在可见窗口中运行批处理并持续显示 OCR 进度。运行结束或发生错误时窗口会暂停，按任意键后才关闭。

也可以在 PowerShell 中直接运行：

在项目根目录运行：

```powershell
& '.\ocr\scripts\run_batch.ps1'
```

脚本扫描 `input_videos/*.mp4`：

- 命名不合规的视频会跳过并提示；
- 已存在 `merchant_cards.csv` 的视频默认跳过；
- 新视频输出到 `outputs/<视频文件名>/`；
- 需要明确重跑时使用 `-Force`，它会替换该视频原有的同名输出目录。

自定义输入和输出目录：

```powershell
& '.\ocr\scripts\run_batch.ps1' `
  -InputDir 'D:\mall_videos' `
  -OutputDir 'D:\mall_results'
```

## 单视频运行与调试

```powershell
& '.\ocr\scripts\run_single.ps1' `
  -Video '.\ocr\input_videos\上海__五角场合生汇__20260824.mp4' `
  -MallName '五角场合生汇'
```

未指定 `-OutputDir` 时，结果自动进入与视频同名的 `outputs/` 子目录。

短片冒烟测试建议写到系统临时目录，避免污染正式输出：

```powershell
& '.\ocr\scripts\run_single.ps1' `
  -Video '.\ocr\input_videos\上海__五角场合生汇__20260824.mp4' `
  -MallName '五角场合生汇' `
  -OutputDir "$env:TEMP\mall_ocr_smoke" `
  -MaxSeconds 20 `
  -SaveDebug
```

`-SaveDebug` 会额外生成 `raw_ocr/`。修改解析规则后可以复用这些 token：

```powershell
& '.\ocr\scripts\run_single.ps1' `
  -Video '.\ocr\input_videos\上海__五角场合生汇__20260824.mp4' `
  -MallName '五角场合生汇' `
  -OutputDir "$env:TEMP\mall_ocr_reparse" `
  -ReuseRawOcrDir "$env:TEMP\mall_ocr_smoke\raw_ocr"
```

复用时必须保持原视频、截取时长、关键帧间隔和画面差异阈值一致；完整视频的 token 不能直接与另一组短片关键帧混用。

已保存的 `run_summary.json` 记录的是采集当时的绝对路径；目录迁移后这些历史字段可能指向旧位置，当前程序仍以本目录中的 CSV 和配置路径读取数据。

## 输出说明

每个商场目录固定包含：

- `merchant_cards.csv`：跨帧去重后的候选商户；
- `research_merchants.csv`：排除明确新店和明确暂无评分后的研究用商户；
- `card_observations.csv`：全部逐帧观测，用于审计误并和漏检；
- `recall_audit.json`：逐关键帧的标题、锚点、匹配和缺字段统计；
- `run_summary.json`：本次运行汇总；
- `evidence/`：每个去重候选的证据截图。

只有使用 `-SaveDebug` 时才会出现 `raw_ocr/`。正式批处理默认不保存它。

## 环境与测试

项目统一使用根目录 `environment.yml` 定义的 `mall-analysis` Conda 环境。

```powershell
conda env update --name mall-analysis --file '.\environment.yml' --prune
```

运行测试：

```powershell
conda run --no-capture-output -n mall-analysis python -m unittest discover `
  -s '.\ocr\tests' -v
```

默认使用 `PP-OCRv5_mobile_rec`。`PP-OCRv5_server_rec` 只建议用于短片或单张疑难卡片复核。

## 录制建议

- 固定竖屏、系统字体和页面缩放；
- 每次滑动约屏幕高度的 60%–70%；
- 滑动后短暂停顿，前后保留约30%重叠；
- 从列表顶部录到明确底部；
- 每座商场单独保存一个视频；
- 避免通知、悬浮窗和过快的惯性滑动。

管线采用召回优先策略。缺评分、艺术字和单次观测不会被直接删除，而会通过 `review_status` 和 `review_reason` 进入复核层。楼层属于可选辅助字段：识别到时继续参与去重，未识别到时不单独触发人工复核。
只有卡片固定信息区明确出现“新店”或“暂无评分/暂无星级”等标识时，才会设置 `research_eligible=false`；普通 OCR 漏评分仍保留在研究表并进入复核。
