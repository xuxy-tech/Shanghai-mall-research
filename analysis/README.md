# 统计分析与报告

输入是只读的 `data/master_stores.csv`。正式解释见 [REPORT.md](REPORT.md)，交互地图见 [map/index.html](../map/index.html)。地图观察先在报告中提出，再用结果表检验趋势、例外和采样边界。

## 内容

- `config/feature_config.json`：特征定义和输入输出位置。
- `scripts/build_features.py`：商场特征和业态计数。
- `scripts/run_analysis.py`：结构、原型、嵌套性、共址和聚类对照。
- `scripts/brand_sensitivity.py`：品牌身份口径敏感性。
- `scripts/compositional_analysis.py`：CLR-PCA、Aitchison 与贝叶斯 LNM。
- `scripts/package_results.py`：合并商场综合表并整理结果文件。
- `scripts/run_quick_pipeline.py`：前述快速步骤的调度器。
- `outputs/current/tables/core/mall_profiles.csv`：地图使用的商场级综合表。
- `outputs/current/tables/diagnostics/`、`intermediate/`、`machine/`：诊断表、中间矩阵和运行记录。
- `report_assets/`：报告中的地图截图。

## 重建

在项目根目录运行：

```powershell
.\analysis\run_quick.bat --check
.\analysis\run_quick.bat
conda run --no-capture-output -n mall-analysis python '.\map\export_data.py'
```

快速模式重建确定性分析和 CLR-PCA 基线，生成商场综合表；它不拟合贝叶斯 LNM，也不自动更新报告文字。现有报告基于已保存的数据截面和完整模型结果，重跑快速模式后不能把 CLR-PCA 输出直接解释为 LNM 结果。

完整成分模型可使用 `analysis/run_compositional.bat`，默认 ADVI；它运行时间较长，成功后会重新打包商场综合表。若直接运行单个分析脚本，其默认输出为 `outputs/current/` 根部的平面文件，可再运行 `package_results.py --output-dir analysis/outputs/current` 整理。`fetch_mall_metadata.py` 需要 `.env` 中的 `AMAP_WEB_SERVICE_KEY`。

特征与统计检验的具体口径和局限见 [REPORT.md](REPORT.md)。门店数是样本内采集实例数；空间关系、品牌共现和业态相似均是描述性结果。
