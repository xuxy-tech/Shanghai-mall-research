"""一键快速分析：确定性分析和 CLR-PCA 基线，不重新拟合贝叶斯模型。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ANALYSIS_ROOT = PROJECT_ROOT / "analysis"
OUTPUTS_ROOT = ANALYSIS_ROOT / "outputs"
CURRENT = OUTPUTS_ROOT / "current"
MASTER = PROJECT_ROOT / "data" / "master_stores.csv"
CONFIG = ANALYSIS_ROOT / "config" / "feature_config.json"
SCRIPTS = ANALYSIS_ROOT / "scripts"
console = Console()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def locate_metadata() -> Path | None:
    candidates = [CURRENT / "tables" / "core" / "mall_metadata.csv", CURRENT / "mall_metadata.csv"]
    return next((path for path in candidates if path.exists()), None)


def checks() -> list[tuple[str, bool, str]]:
    modules = ["numpy", "rich"]
    rows = [
        ("Conda 环境", "mall-analysis" in str(Path(sys.prefix)).lower(), sys.prefix),
        ("商户主表", MASTER.exists(), str(MASTER)),
        ("分析配置", CONFIG.exists(), str(CONFIG)),
        ("区位缓存", locate_metadata() is not None, str(locate_metadata() or "缺少商场坐标")),
    ]
    rows.extend((f"依赖 {name}", importlib.util.find_spec(name) is not None, name) for name in modules)
    return rows


def show_checks() -> bool:
    table = Table(title="快速分析启动检查")
    table.add_column("项目")
    table.add_column("状态")
    table.add_column("说明")
    for label, ok, detail in checks():
        table.add_row(label, "[green]通过[/green]" if ok else "[red]失败[/red]", detail)
    console.print(table)
    return all(ok for _, ok, _ in checks())


def run_stage(index: int, total: int, title: str, command: list[str]) -> float:
    console.rule(f"[bold cyan][{index}/{total}] {title}")
    started = time.perf_counter()
    result = subprocess.run(command, cwd=PROJECT_ROOT, env=os.environ.copy(), check=False)
    elapsed = time.perf_counter() - started
    if result.returncode:
        raise RuntimeError(f"{title}失败，退出码 {result.returncode}")
    console.print(f"[green]完成[/green] · {elapsed:.1f}s")
    return elapsed


def publish(staging: Path) -> None:
    next_dir = OUTPUTS_ROOT / ".current_next"
    backup = OUTPUTS_ROOT / ".current_backup"
    for path in (next_dir, backup):
        if path.exists():
            shutil.rmtree(path)
    shutil.copytree(staging, next_dir)
    if CURRENT.exists():
        CURRENT.rename(backup)
    try:
        next_dir.rename(CURRENT)
    except Exception:
        if backup.exists() and not CURRENT.exists():
            backup.rename(CURRENT)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def run_pipeline() -> None:
    if not show_checks():
        raise RuntimeError("启动检查未通过")
    total = 5
    timings: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix="quick_", dir=OUTPUTS_ROOT) as temp_name:
        staging = Path(temp_name)
        metadata = locate_metadata()
        if metadata:
            shutil.copy2(metadata, staging / "mall_metadata.csv")
        python = sys.executable
        timings["构建特征"] = run_stage(1, total, "构建商场特征", [python, "-u", str(SCRIPTS / "build_features.py"), "--output-dir", str(staging), "--metadata-input", str(staging / "mall_metadata.csv")])
        timings["结构分析"] = run_stage(2, total, "结构、原型与聚类对照", [python, "-u", str(SCRIPTS / "run_analysis.py"), "--output-dir", str(staging)])
        timings["敏感性"] = run_stage(3, total, "品牌定义敏感性", [python, "-u", str(SCRIPTS / "brand_sensitivity.py"), "--output-dir", str(staging)])
        timings["成分基线"] = run_stage(4, total, "CLR-PCA 与 Aitchison 快速基线", [python, "-u", str(SCRIPTS / "compositional_analysis.py"), "--skip-lnm", "--output-dir", str(staging)])
        timings["汇总结果"] = run_stage(5, total, "生成商场综合表并整理结果", [python, "-u", str(SCRIPTS / "package_results.py"), "--output-dir", str(staging)])
        manifest = {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "mode": "quick-clr-pca",
            "python": sys.version,
            "environment": sys.prefix,
            "input": {"path": str(MASTER), "sha256": sha256(MASTER)},
            "config": {"path": str(CONFIG), "sha256": sha256(CONFIG)},
            "timings_seconds": {key: round(value, 3) for key, value in timings.items()},
            "note": "快速模式不重新拟合贝叶斯 LNM；潜在轴来自 CLR-PCA 基线。",
        }
        (staging / "machine" / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        publish(staging)
    console.print(f"\n[bold green]快速分析完成[/bold green]：{CURRENT / 'tables' / 'core' / 'mall_profiles.csv'}")


def main() -> int:
    parser = argparse.ArgumentParser(description="一键快速分析管线")
    parser.add_argument("--check", action="store_true", help="只检查环境和输入，不运行分析")
    args = parser.parse_args()
    try:
        if args.check:
            return 0 if show_checks() else 1
        run_pipeline()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[bold red]失败：[/bold red]{type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
