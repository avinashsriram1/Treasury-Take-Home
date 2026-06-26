"""
Package scraped COLA data into upload-ready verifier fixtures.

This utility is intentionally a fixture packager, not a runtime dependency. It
expects output produced by scripts/scrape_cola_dataset.py and creates small
single-product and batch-job folders that mirror the UI upload workflow.

Example:
  python scripts/generate_cola_fixtures.py --source-dir data/cola_testing `
      --single-count 5 --batch-count 2 --batch-size 8 --zip
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

APP_FIELDS = ["brand_name", "class_type", "alcohol_content", "net_contents", "bottler", "country"]


def load_products(source_dir: Path) -> list[dict[str, Any]]:
    manifest_path = source_dir / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"missing {manifest_path}; run scrape_cola_dataset.py first")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    products = data.get("products", data) if isinstance(data, dict) else data
    if not isinstance(products, list):
        raise ValueError("manifest must be a list or contain a products list")

    raw_rows = load_application_rows(source_dir / "application_data.csv")
    enriched = []
    for product in products:
        if not isinstance(product, dict):
            continue
        product_id = str(product.get("product_id") or "").strip()
        raw = raw_rows.get(product_id, {})
        merged = {**raw, **product}
        images = [str(image).strip() for image in merged.get("images", []) if str(image).strip()]
        if not images:
            image_files = str(raw.get("image_files", ""))
            images = [part.strip() for part in image_files.split(";") if part.strip()]
        if not images:
            continue
        merged["images"] = images
        merged["product_id"] = product_id or slugify(merged.get("label") or merged.get("brand_name") or "cola-product")
        enriched.append(merged)
    return enriched


def load_application_rows(path: Path) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    rows: dict[str, dict[str, str]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            product_id = row.get("product_id") or f"cola-{row.get('ttb_id', '')}"
            mapped = {
                "product_id": product_id,
                "label": row.get("label") or row.get("brand_name") or product_id,
                "brand_name": row.get("brand_name") or "",
                "class_type": row.get("class_type_description") or row.get("product_type") or "",
                "alcohol_content": row.get("alcohol_content") or "",
                "net_contents": row.get("net_contents") or "",
                "bottler": row.get("applicant_name_address") or "",
                "country": "",
                "source_url": row.get("source_url") or "",
                "image_files": row.get("image_files") or "",
            }
            rows[product_id] = mapped
    return rows


def write_fixture_set(
    products: list[dict[str, Any]],
    *,
    source_dir: Path,
    out_dir: Path,
    single_count: int,
    batch_count: int,
    batch_size: int,
    zip_output: bool,
) -> None:
    single_root = out_dir / "single-products"
    batch_root = out_dir / "batch-jobs"
    single_root.mkdir(parents=True, exist_ok=True)
    batch_root.mkdir(parents=True, exist_ok=True)

    for product in products[:single_count]:
        write_single_fixture(product, source_dir=source_dir, root=single_root, zip_output=zip_output)

    cursor = single_count
    for batch_index in range(batch_count):
        chunk = products[cursor : cursor + batch_size]
        if not chunk:
            break
        write_batch_fixture(
            chunk, source_dir=source_dir, root=batch_root, batch_index=batch_index + 1, zip_output=zip_output
        )
        cursor += batch_size


def write_single_fixture(product: dict[str, Any], *, source_dir: Path, root: Path, zip_output: bool) -> Path:
    slug = slugify(product.get("label") or product.get("brand_name") or product["product_id"])
    fixture_dir = unique_dir(root / slug)
    images_dir = fixture_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    copied = copy_images(product, source_dir=source_dir, images_dir=images_dir)
    application = application_payload(product)
    (fixture_dir / "application.json").write_text(json.dumps(application, indent=2), encoding="utf-8")
    manifest = {
        "products": [
            {**application, "product_id": product["product_id"], "label": product.get("label"), "images": copied}
        ]
    }
    (fixture_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (fixture_dir / "source.txt").write_text(
        str(product.get("source_url") or "Public TTB COLA registry"), encoding="utf-8"
    )
    if zip_output:
        zip_directory(fixture_dir, fixture_dir.with_suffix(".zip"))
    return fixture_dir


def write_batch_fixture(
    products: list[dict[str, Any]],
    *,
    source_dir: Path,
    root: Path,
    batch_index: int,
    zip_output: bool,
) -> Path:
    fixture_dir = root / f"batch-{batch_index:03d}"
    if fixture_dir.exists():
        shutil.rmtree(fixture_dir)
    images_dir = fixture_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    manifest_products = []
    csv_rows = []
    for product in products:
        copied = copy_images(product, source_dir=source_dir, images_dir=images_dir)
        app = application_payload(product)
        manifest_products.append(
            {**app, "product_id": product["product_id"], "label": product.get("label"), "images": copied}
        )
        csv_rows.append(
            {
                **app,
                "product_id": product["product_id"],
                "label": product.get("label") or product["product_id"],
                "images": ";".join(copied),
            }
        )

    (fixture_dir / "manifest.json").write_text(json.dumps({"products": manifest_products}, indent=2), encoding="utf-8")
    write_application_csv(fixture_dir / "application_data.csv", csv_rows)
    (fixture_dir / "README.md").write_text(
        "# COLA Batch Fixture\n\n"
        "Upload this folder's `images/` contents with `manifest.json` "
        "in the Batch Jobs workflow.\n",
        encoding="utf-8",
    )
    if zip_output:
        zip_directory(fixture_dir, fixture_dir / "batch.zip")
    return fixture_dir


def application_payload(product: dict[str, Any]) -> dict[str, str | None]:
    return {
        "brand_name": empty_to_none(product.get("brand_name")),
        "class_type": empty_to_none(product.get("class_type")),
        "alcohol_content": empty_to_none(product.get("alcohol_content")),
        "net_contents": empty_to_none(product.get("net_contents")),
        "bottler": empty_to_none(product.get("bottler")),
        "country": empty_to_none(product.get("country")),
        "source_url": empty_to_none(product.get("source_url")),
    }


def copy_images(product: dict[str, Any], *, source_dir: Path, images_dir: Path) -> list[str]:
    copied = []
    for ref in product.get("images", []):
        src = source_dir / str(ref)
        if not src.exists():
            src = source_dir / str(ref).replace("\\", "/").split("/")[-1]
        if not src.exists():
            continue
        target = images_dir / src.name
        shutil.copy2(src, target)
        copied.append(f"images/{target.name}")
    return copied


def write_application_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    columns = ["product_id", "label", *APP_FIELDS, "images", "source_url"]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) or "" for column in columns})


def zip_directory(source: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if path == target:
                continue
            archive.write(path, path.relative_to(source.parent))


def unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.name}-{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"could not create unique fixture directory for {path}")


def slugify(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value).strip().lower()).strip("-")
    return text[:70] or "cola-product"


def empty_to_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def main() -> None:
    parser = argparse.ArgumentParser(description="Create upload-ready COLA verifier fixtures from scraped data.")
    parser.add_argument("--source-dir", default="data/cola_testing")
    parser.add_argument("--single-count", type=int, default=5)
    parser.add_argument("--batch-count", type=int, default=2)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--out-dir", default="samples/cola-fixtures")
    parser.add_argument("--zip", action="store_true")
    parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Accepted for workflow compatibility; scraping is done by scrape_cola_dataset.py.",
    )
    parser.add_argument(
        "--insecure-tls",
        action="store_true",
        help="Accepted for workflow compatibility; scraping is done by scrape_cola_dataset.py.",
    )
    args = parser.parse_args()

    source_dir = Path(args.source_dir)
    out_dir = Path(args.out_dir)
    products = load_products(source_dir)
    write_fixture_set(
        products,
        source_dir=source_dir,
        out_dir=out_dir,
        single_count=args.single_count,
        batch_count=args.batch_count,
        batch_size=args.batch_size,
        zip_output=args.zip,
    )
    print(f"Wrote COLA fixtures to {out_dir}")


if __name__ == "__main__":
    main()

