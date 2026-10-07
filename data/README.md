# 商户主表

把 `ocr/outputs/*/merchant_cards.csv` 汇总为 `master_stores.csv`。OCR 结果只读，排除、分类、名称修正和去重只发生在本目录。修改规则后运行 `run_master.bat` 可重建主表及 `outputs/current/` 下的质量审计表。

`config/pipeline_config.json` 决定输入、输出和处理开关；`category_mapping.csv`、`exclusion_rules.csv`、`brand_aliases.csv` 与 `chain_brands.csv` 分别维护业态、排除、别名和连锁品牌规则。`scripts/build_master.py` 是唯一构建实现，`tests/` 检验关键规则。

在项目根目录运行：

```powershell
.\data\run_master.bat
```

`master_stores.csv` 一行对应一家商场中的一个店铺实例。`outputs/current/BUILD_REPORT.md`、`excluded_records.csv`、`duplicate_audit.csv`、`category_unmapped.csv` 和 `run_manifest.json` 可用于核对排除、去重、缺失分类和输入版本。当前配置中 `exclude_exact_new_store_badge` 与 `exclude_explicit_unrated` 均为 `false`；报告和地图必须以生成主表时实际生效的配置为准。
