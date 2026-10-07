"""Export the current mall analysis as a static browser data file."""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MAP_ROOT = Path(__file__).resolve().parent
PROFILES = PROJECT_ROOT / "analysis/outputs/current/tables/core/mall_profiles.csv"
METADATA = PROJECT_ROOT / "analysis/outputs/current/tables/core/mall_metadata.csv"
CATEGORY_COUNTS = PROJECT_ROOT / "analysis/outputs/current/intermediate/mall_category_counts.csv"
STORES = PROJECT_ROOT / "data/master_stores.csv"
GEOCODES = MAP_ROOT / "geocodes.json"
OUTPUT = MAP_ROOT / "data.js"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", errors="strict", newline="") as handle:
        return list(csv.DictReader(handle))


def useful_text(*values: str | None) -> str:
    return next((value for value in values if value and value.strip() not in {"[]", "null"}), "")


def offset_lat(lon: float, lat: float) -> float:
    value = -100.0 + 2.0 * lon + 3.0 * lat + 0.2 * lat * lat
    value += 0.1 * lon * lat + 0.2 * math.sqrt(abs(lon))
    value += (20.0 * math.sin(6.0 * lon * math.pi) + 20.0 * math.sin(2.0 * lon * math.pi)) * 2.0 / 3.0
    value += (20.0 * math.sin(lat * math.pi) + 40.0 * math.sin(lat / 3.0 * math.pi)) * 2.0 / 3.0
    value += (160.0 * math.sin(lat / 12.0 * math.pi) + 320.0 * math.sin(lat * math.pi / 30.0)) * 2.0 / 3.0
    return value


def offset_lon(lon: float, lat: float) -> float:
    value = 300.0 + lon + 2.0 * lat + 0.1 * lon * lon
    value += 0.1 * lon * lat + 0.1 * math.sqrt(abs(lon))
    value += (20.0 * math.sin(6.0 * lon * math.pi) + 20.0 * math.sin(2.0 * lon * math.pi)) * 2.0 / 3.0
    value += (20.0 * math.sin(lon * math.pi) + 40.0 * math.sin(lon / 3.0 * math.pi)) * 2.0 / 3.0
    value += (150.0 * math.sin(lon / 12.0 * math.pi) + 300.0 * math.sin(lon / 30.0 * math.pi)) * 2.0 / 3.0
    return value


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    a = 6378245.0
    eccentricity = 0.006693421622965943
    dlat = offset_lat(lon - 105.0, lat - 35.0)
    dlon = offset_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = 1.0 - eccentricity * math.sin(rad_lat) ** 2
    sqrt_magic = math.sqrt(magic)
    dlat = dlat * 180.0 / ((a * (1.0 - eccentricity)) / (magic * sqrt_magic) * math.pi)
    dlon = dlon * 180.0 / (a / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    guess_lon, guess_lat = lon, lat
    for _ in range(5):
        mapped_lon, mapped_lat = wgs84_to_gcj02(guess_lon, guess_lat)
        guess_lon += lon - mapped_lon
        guess_lat += lat - mapped_lat
    return round(guess_lon, 6), round(guess_lat, 6)


def export() -> dict[str, object]:
    profiles = read_csv(PROFILES)
    metadata = {row["mall_name"]: row for row in read_csv(METADATA)}
    counts = {row["mall_name"]: row for row in read_csv(CATEGORY_COUNTS)}
    geocodes = json.loads(GEOCODES.read_text(encoding="utf-8", errors="strict"))["malls"]
    store_rows = read_csv(STORES)
    stores_by_mall: dict[str, list[dict[str, str | float | None]]] = {name: [] for name in metadata}
    brands_by_mall: dict[str, set[str]] = {name: set() for name in metadata}
    for row in store_rows:
        name = row["mall_name"]
        if name not in stores_by_mall:
            raise ValueError(f"Store belongs to unknown mall: {name}")
        brand_id = row["brand_id"]
        brands_by_mall[name].add(brand_id)
        stores_by_mall[name].append({
            "name": useful_text(row["merchant_name_normalized"], row["merchant_name_raw"]),
            "brand": row["canonical_brand_name"],
            "brandId": brand_id,
            "category": row["category_l2"],
            "group": row["category_l1"],
            "floor": row["floor_location"],
            "rating": float(row["rating"]) if row["rating"] else None,
            "price": float(row["avg_price_yuan"]) if row["avg_price_yuan"] else None,
        })
    malls = []

    if set(metadata) != {row["mall_name"] for row in profiles} or set(counts) != set(metadata):
        raise ValueError("Mall names differ across profiles, metadata and category counts")

    for profile in profiles:
        name = profile["mall_name"]
        meta = metadata[name]
        fallback = geocodes.get(name, {})
        source = fallback if not meta["longitude"] or not meta["latitude"] else meta
        if not source.get("longitude") or not source.get("latitude"):
            raise ValueError(f"Missing coordinates: {name}")
        lon, lat = float(source["longitude"]), float(source["latitude"])
        if not (120.8 < lon < 122.2 and 30.5 < lat < 31.9):
            raise ValueError(f"Coordinate outside Shanghai region: {name}")
        wgs_lon, wgs_lat = gcj02_to_wgs84(lon, lat)
        categories = {
            key: int(value)
            for key, value in counts[name].items()
            if key != "mall_name" and int(value) > 0
        }
        store_count = int(profile["store_count"])
        if sum(categories.values()) != store_count:
            raise ValueError(f"Category counts do not sum to store count: {name}")
        if len(stores_by_mall[name]) != store_count:
            raise ValueError(f"Store rows do not match profile count: {name}")
        malls.append({
            "name": name,
            "district": useful_text(source.get("district"), profile.get("district")) or "上海",
            "businessArea": useful_text(meta.get("business_area")),
            "address": useful_text(fallback.get("address")) if source is fallback else "",
            "coordinates": [wgs_lat, wgs_lon],
            "coordinateSource": "Amap Place Text API (2026-10-06)" if fallback and source is fallback else "Amap metadata (2026-08)",
            "storeCount": store_count,
            "brandCount": int(profile["brand_count"]),
            "categoryBreadth": int(profile["l2_breadth"]),
            "retailShare": float(profile["retail_share"]),
            "diningShare": float(profile["dining_share"]),
            "categories": categories,
            "stores": stores_by_mall[name],
        })

    category_totals: dict[str, int] = {}
    for mall in malls:
        for category, count in mall["categories"].items():
            category_totals[category] = category_totals.get(category, 0) + count

    relationships = []
    for index, first in enumerate(malls):
        for second in malls[index + 1:]:
            first_brands = brands_by_mall[first["name"]]
            second_brands = brands_by_mall[second["name"]]
            shared_ids = first_brands & second_brands
            shared_names = sorted({
                row["brand"] for row in first["stores"]
                if row["brandId"] in shared_ids and row["brand"]
            })
            numerator = sum(first["categories"].get(key, 0) * second["categories"].get(key, 0) for key in category_totals)
            norm_first = math.sqrt(sum(value * value for value in first["categories"].values()))
            norm_second = math.sqrt(sum(value * value for value in second["categories"].values()))
            relationships.append({
                "a": first["name"],
                "b": second["name"],
                "sharedCount": len(shared_ids),
                "brandJaccard": round(len(shared_ids) / len(first_brands | second_brands), 6),
                "categoryCosine": round(numerator / (norm_first * norm_second), 6),
                "sharedBrands": shared_names,
            })

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataPeriod": "2026-08",
        "coordinateSystem": "WGS84",
        "mallCount": len(malls),
        "storeCount": sum(mall["storeCount"] for mall in malls),
        "categoryTotals": category_totals,
        "relationships": relationships,
        "malls": malls,
    }
    OUTPUT.write_text(
        "window.MALL_MAP_DATA = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n",
        encoding="utf-8",
        errors="strict",
        newline="\n",
    )
    return payload


if __name__ == "__main__":
    result = export()
    print(f"Exported {result['mallCount']} malls and {result['storeCount']} stores to {OUTPUT}")
