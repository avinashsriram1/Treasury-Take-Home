from __future__ import annotations

import csv
import json
import zipfile
from pathlib import Path

from scripts.generate_cola_fixtures import load_products, write_fixture_set


def make_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    image_dir = source / "label_images"
    image_dir.mkdir(parents=True)
    (image_dir / "one.jpg").write_bytes(b"one")
    (image_dir / "two.jpg").write_bytes(b"two")
    rows = [
        {
            "ttb_id": "11111111111111",
            "product_id": "cola-11111111111111",
            "label": "North Orchard",
            "brand_name": "North Orchard",
            "class_type_description": "Hard Cider",
            "product_type": "Wine",
            "alcohol_content": "6.2%",
            "net_contents": "355 mL",
            "applicant_name_address": "North Orchard Cidery, Burlington, VT",
            "source_url": "https://example.test/one",
            "image_files": "label_images/one.jpg",
        },
        {
            "ttb_id": "22222222222222",
            "product_id": "cola-22222222222222",
            "label": "Drop of Sunshine",
            "brand_name": "Drop of Sunshine",
            "class_type_description": "Wine",
            "product_type": "Wine",
            "alcohol_content": "12%",
            "net_contents": "750 mL",
            "applicant_name_address": "Example Winery, Lodi, CA",
            "source_url": "https://example.test/two",
            "image_files": "label_images/two.jpg",
        },
    ]
    with (source / "application_data.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    (source / "manifest.json").write_text(
        json.dumps(
            {
                "products": [
                    {"product_id": row["product_id"], "label": row["label"], "images": row["image_files"].split(";")}
                    for row in rows
                ]
            }
        ),
        encoding="utf-8",
    )
    return source


def test_fixture_generator_creates_single_and_batch_packages(tmp_path):
    source = make_source(tmp_path)
    products = load_products(source)
    out_dir = tmp_path / "fixtures"
    write_fixture_set(
        products,
        source_dir=source,
        out_dir=out_dir,
        single_count=1,
        batch_count=1,
        batch_size=2,
        zip_output=True,
    )

    single_dirs = list((out_dir / "single-products").iterdir())
    assert any(path.name.endswith(".zip") for path in single_dirs)
    single_folder = next(path for path in single_dirs if path.is_dir())
    assert (single_folder / "images" / "one.jpg").exists()
    assert (single_folder / "application.json").exists()
    single_manifest = json.loads((single_folder / "manifest.json").read_text(encoding="utf-8"))
    assert single_manifest["products"][0]["brand_name"] == "North Orchard"
    assert single_manifest["products"][0]["images"] == ["images/one.jpg"]

    batch_dir = out_dir / "batch-jobs" / "batch-001"
    assert (batch_dir / "images" / "two.jpg").exists()
    assert (batch_dir / "manifest.json").exists()
    assert (batch_dir / "application_data.csv").exists()
    assert (batch_dir / "batch.zip").exists()
    with zipfile.ZipFile(batch_dir / "batch.zip") as archive:
        assert any(name.endswith("manifest.json") for name in archive.namelist())
