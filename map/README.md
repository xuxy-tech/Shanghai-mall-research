# 上海商场交互地图

直接打开 [index.html](index.html)。`data.js` 是已导出的本地数据，页面运行时不调用高德 API；Esri 底图和备用街道底图需要网络。

点面积对应样本内采集到的门店数，颜色混合购物（绿）、美食（红）与其他（蓝）的门店占比。三个通道按各点最大占比统一亮度；三角图例展示映射。聚合点的数字表示商场数，颜色为所含商场占比的等权平均。面积不能解释为建筑面积，颜色不能解释为离散商场类别。

侧栏提供检索、筛选、门店和关联查看。关联页中的品牌 Jaccard 与业态占比余弦相似度是样本内描述性指标。地图观察和数据检验见 [分析报告](../analysis/REPORT.md)。

更新 `data/master_stores.csv` 和 `analysis/outputs/current/` 后，在项目根目录运行：

```powershell
conda run --no-capture-output -n mall-analysis python '.\map\export_data.py'
```

导出读取 `analysis/outputs/current/tables/core/mall_profiles.csv`、`mall_metadata.csv`、`analysis/outputs/current/intermediate/mall_category_counts.csv` 和 `data/master_stores.csv`，并校验商场名、门店计数与坐标。`geocodes.json` 是元数据缺失时的坐标补充；高德 GCJ-02 坐标在导出时转换为 WGS84。
