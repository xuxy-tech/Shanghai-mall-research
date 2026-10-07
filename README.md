# 上海商场业态图谱

基于大众点评商场门店列表录屏，整理 61 家上海商场的 14,779 个店铺实例，并用交互地图和分析报告探索业态结构。这里的“规模”指样本内采集到的门店数，不代表建筑面积或完整商户总量。

## 项目结构

| 目录 | 用途 | 主要入口 |
| --- | --- | --- |
| `ocr/` | 录屏识别、逐商场原始卡片与复核数据 | `ocr/run_batch.bat` |
| `data/` | 排除、分类、去重和统一商户主表 | `data/run_master.bat` |
| `map/` | 当前交互地图，支持商场检索、筛选与关系查看 | [打开地图](map/index.html) |
| `analysis/` | 统计检验、敏感性分析、结果表与正式报告 | [阅读报告](analysis/REPORT.md) |

数据流：`ocr/outputs/*/merchant_cards.csv` → `data/master_stores.csv` → `analysis/outputs/current/` → `map/data.js`。OCR 结果只读；规则修订在 `data/` 层重建。

## 运行

环境定义在 `environment.yml`。首次创建：

```powershell
conda env create --file '.\environment.yml'
```

更新门店数据时，依次运行 `data/run_master.bat`、`analysis/run_quick.bat`，最后导出地图：

```powershell
conda run --no-capture-output -n mall-analysis python '.\map\export_data.py'
```

`map/index.html` 可直接在浏览器打开；底图需要网络。`analysis/run_quick.bat` 使用 CLR-PCA 快速基线，不重新拟合贝叶斯模型。完整模型入口和分析口径见 [analysis/README.md](analysis/README.md)。

`analysis/REPORT.md` 是独立的阶段性解释文档，包含地图截图、数据检验、例外与采样边界；重跑脚本不会自动改写其中的结论。提交新的数据截面前，需要重新核对报告数字和截图。
