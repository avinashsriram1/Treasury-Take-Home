"""
Build a local test dataset from the public TTB COLA registry.

This script is intentionally separate from the FastAPI runtime. It is for
developer testing: it downloads public COLA application fields and affixed label
images, then writes both a raw CSV and a batch-compatible JSON manifest.

Usage:
  python scripts/scrape_cola_dataset.py --count 25
  python scripts/scrape_cola_dataset.py --date-from 01/01/2026 --date-to 06/24/2026
  python scripts/scrape_cola_dataset.py --out-dir data/cola_smoke --delay 0.7
"""

from __future__ import annotations

import argparse
import csv
import html
import http.cookiejar
import json
import re
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BASE = "https://ttbonline.gov/colasonline"
SEARCH = f"{BASE}/publicSearchColasBasicProcess.do?action=search"
SORT = f"{BASE}/publicPageBasicCola.do?action=sort&sortcol=ttbId&order=desc"
NEXT = f"{BASE}/publicPageBasicCola.do?action=page&pgfcn=nextset"
PRINTABLE = f"{BASE}/viewColaDetails.do?action=publicFormDisplay&ttbid={{ttbid}}"

RAW_COLUMNS = [
    "ttb_id",
    "completed_date",
    "product_id",
    "label",
    "source_of_product",
    "product_type",
    "brand_name",
    "fanciful_name",
    "applicant_name_address",
    "serial_number",
    "net_contents",
    "alcohol_content",
    "class_type_description",
    "status",
    "date_issued",
    "label_images",
    "image_files",
    "source_url",
]


@dataclass(frozen=True)
class ColaRow:
    values: dict[str, str]

    def manifest_product(self) -> dict[str, Any]:
        images = [image.strip() for image in self.values.get("image_files", "").split(";") if image.strip()]
        class_type = self.values.get("class_type_description") or self.values.get("product_type") or None
        label = self.values.get("brand_name") or self.values.get("ttb_id")
        return {
            "product_id": self.values["product_id"],
            "label": label,
            "brand_name": self.values.get("brand_name") or None,
            "class_type": class_type,
            "alcohol_content": self.values.get("alcohol_content") or None,
            "net_contents": self.values.get("net_contents") or None,
            "bottler": self.values.get("applicant_name_address") or None,
            "country": None,
            "images": images,
            "notes": "Generated from public COLA registry for verifier testing.",
            "source_url": self.values.get("source_url"),
        }


def make_opener(*, insecure_tls: bool = False) -> urllib.request.OpenerDirector:
    cookie_jar = http.cookiejar.CookieJar()
    context = ssl.create_default_context()
    if insecure_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar),
        urllib.request.HTTPSHandler(context=context),
    )
    opener.addheaders = [("User-Agent", "Treasury-Take-Home-V3-Test-Scraper/1.0")]
    return opener


def fetch_text(
    opener: urllib.request.OpenerDirector,
    url: str,
    data: str | bytes | None = None,
    retries: int = 3,
) -> str:
    for attempt in range(retries):
        try:
            body = data.encode("utf-8") if isinstance(data, str) else data
            with opener.open(url, data=body, timeout=45) as response:
                return response.read().decode("latin-1", errors="replace")
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return ""


def fetch_bytes(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    referer: str | None = None,
    retries: int = 3,
) -> tuple[str | None, bytes | None]:
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url)
            if referer:
                request.add_header("Referer", referer)
            with opener.open(request, timeout=45) as response:
                return response.headers.get("Content-Type", ""), response.read()
        except Exception:
            if attempt == retries - 1:
                return None, None
            time.sleep(2 * (attempt + 1))
    return None, None


def clean(fragment: str | None) -> str:
    if not fragment:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", ", ", fragment)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(\s*,\s*)+", ", ", text)
    return text.strip(" ,")


def data_after(page: str, name: str, *, exact: bool = False) -> str:
    if exact:
        match = re.search(
            rf'class="(?:bold)?label"[^>]*>\s*{name}\s*</div>',
            page,
            re.I,
        )
    else:
        match = re.search(
            rf'<div class="(?:bold)?label"[^>]*>(?:(?!</div>).)*?{name}',
            page,
            re.I | re.S,
        )
    if not match:
        return ""
    data = re.search(r'<div class="data"[^>]*>(.*?)</div>', page[match.end() :], re.I | re.S)
    return clean(data.group(1)) if data else ""


def checked_alt(page: str, prefix: str) -> str:
    checked = []
    pattern = re.compile(rf'<input[^>]*alt="{prefix}:\s*([^"]+)"[^>]*>', re.I)
    for match in pattern.finditer(page):
        if "checked" in match.group(0).lower():
            checked.append(match.group(1).strip())
    return "; ".join(checked)


def parse_label_images(page: str) -> str:
    labels = []
    pattern = re.compile(
        r"Image Type:\s*</p>\s*(.*?)\s*<br>.*?Actual Dimensions:\s*(.*?)<br>",
        re.I | re.S,
    )
    for match in pattern.finditer(page):
        image_type = clean(match.group(1))
        dimensions = clean(match.group(2))
        labels.append(f"{image_type} ({dimensions})" if dimensions else image_type)
    return " | ".join(labels)


def parse_printable(page: str, ttb_id: str, completed_date: str = "") -> ColaRow:
    row = {column: "" for column in RAW_COLUMNS}
    row["ttb_id"] = ttb_id
    row["completed_date"] = completed_date
    row["product_id"] = f"cola-{ttb_id}"
    row["source_url"] = PRINTABLE.format(ttbid=ttb_id)
    row["source_of_product"] = checked_alt(page, "Source of Product")
    row["product_type"] = checked_alt(page, "Type of Product")
    row["brand_name"] = data_after(page, "BRAND NAME")
    row["fanciful_name"] = data_after(page, "FANCIFUL NAME")
    row["applicant_name_address"] = data_after(page, "NAME AND ADDRESS")
    row["serial_number"] = data_after(page, "SERIAL NUMBER")
    row["net_contents"] = data_after(page, "NET CONTENTS")
    row["alcohol_content"] = data_after(page, "ALCOHOL CONTENT")
    row["class_type_description"] = data_after(page, "CLASS/TYPE DESCRIPTION")
    row["status"] = data_after(page, "STATUS")
    row["date_issued"] = data_after(page, "DATE ISSUED")
    row["label_images"] = parse_label_images(page)
    row["label"] = row["brand_name"] or row["fanciful_name"] or ttb_id
    return ColaRow(row)


def parse_listing(page: str) -> list[tuple[str, str]]:
    rows = []
    for row_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", page, re.S | re.I):
        block = row_match.group(1)
        id_match = re.search(r"ttbid=(\d{14})", block)
        if not id_match:
            continue
        date_match = re.search(r"(\d{2}/\d{2}/\d{4})", html.unescape(block))
        rows.append((id_match.group(1), date_match.group(1) if date_match else ""))
    seen = set()
    unique = []
    for ttb_id, completed_date in rows:
        if ttb_id not in seen:
            seen.add(ttb_id)
            unique.append((ttb_id, completed_date))
    return unique


def collect_ids(
    opener: urllib.request.OpenerDirector,
    *,
    count: int,
    date_from: str,
    date_to: str,
    delay: float,
) -> dict[str, str]:
    form = urllib.parse.urlencode(
        {
            "searchCriteria.dateCompletedFrom": date_from,
            "searchCriteria.dateCompletedTo": date_to,
            "searchCriteria.productNameSearchType": "C",
            "searchCriteria.productOrFancifulName": "",
            "searchCriteria.classTypeFrom": "",
            "searchCriteria.classTypeTo": "",
            "searchCriteria.originCode": "",
        }
    )
    fetch_text(opener, SEARCH, data=form)
    fetch_text(opener, SORT)
    time.sleep(delay)

    collected: dict[str, str] = {}
    page = fetch_text(opener, NEXT)
    while len(collected) < count:
        rows = parse_listing(page)
        if not rows:
            break
        for ttb_id, completed_date in rows:
            collected.setdefault(ttb_id, completed_date)
            if len(collected) >= count:
                break
        time.sleep(delay)
        page = fetch_text(opener, NEXT)
    return dict(list(collected.items())[:count])


def download_images(
    opener: urllib.request.OpenerDirector,
    *,
    ttb_id: str,
    page: str,
    images_dir: Path,
) -> list[str]:
    saved = []
    referer = PRINTABLE.format(ttbid=ttb_id)
    srcs = re.findall(r'<img[^>]*src="([^"]*publicViewAttachment\.do\?[^"]*)"', page, re.I)
    for index, src in enumerate(srcs, 1):
        src = html.unescape(src)
        filename_match = re.search(r"filename=([^&]*)", src)
        filetype_match = re.search(r"filetype=([^&\"]*)", src)
        if not filename_match:
            continue
        filename = urllib.parse.unquote(filename_match.group(1))
        filetype = filetype_match.group(1) if filetype_match else "l"
        url = f"{BASE}/publicViewAttachment.do?" + urllib.parse.urlencode({"filename": filename, "filetype": filetype})
        content_type, data = fetch_bytes(opener, url, referer=referer)
        if not data or not content_type or "image" not in content_type:
            continue
        extension = image_extension(content_type, filename)
        output_name = f"{ttb_id}_{index}{extension}"
        (images_dir / output_name).write_bytes(data)
        saved.append(output_name)
    return saved


def image_extension(content_type: str, filename: str) -> str:
    media_type = content_type.split(";")[0].strip().lower()
    return {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/tiff": ".tif",
    }.get(media_type, Path(filename).suffix or ".jpg")


def write_outputs(out_dir: Path, rows: list[ColaRow]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "application_data.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=RAW_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow(row.values)

    manifest = {
        "source": "TTB public COLA registry",
        "purpose": "testing",
        "products": [row.manifest_product() for row in rows if row.values.get("image_files")],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a COLA label test dataset.")
    parser.add_argument("--count", type=int, default=25)
    parser.add_argument("--date-from", default="01/01/2026")
    parser.add_argument("--date-to", default="06/24/2026")
    parser.add_argument("--delay", type=float, default=0.7)
    parser.add_argument("--out-dir", default="data/cola_testing")
    parser.add_argument("--insecure-tls", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    images_dir = out_dir / "label_images"
    images_dir.mkdir(parents=True, exist_ok=True)
    opener = make_opener(insecure_tls=args.insecure_tls)
    ids = collect_ids(
        opener,
        count=args.count,
        date_from=args.date_from,
        date_to=args.date_to,
        delay=args.delay,
    )

    rows: list[ColaRow] = []
    for index, (ttb_id, completed_date) in enumerate(ids.items(), 1):
        page = fetch_text(opener, PRINTABLE.format(ttbid=ttb_id))
        row = parse_printable(page, ttb_id, completed_date)
        image_files = download_images(opener, ttb_id=ttb_id, page=page, images_dir=images_dir)
        row.values["image_files"] = "; ".join(f"label_images/{name}" for name in image_files)
        rows.append(row)
        print(f"[{index}/{len(ids)}] {ttb_id} {row.values['brand_name']!r} {len(image_files)} image(s)")
        time.sleep(args.delay)

    write_outputs(out_dir, rows)
    print(f"Wrote {out_dir / 'application_data.csv'}")
    print(f"Wrote {out_dir / 'manifest.json'}")
    print(f"Images are in {images_dir}")


if __name__ == "__main__":
    main()
