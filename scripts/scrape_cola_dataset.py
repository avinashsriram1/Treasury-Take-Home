from __future__ import annotations

import argparse
import csv
import html
import http.cookiejar
import json
import re
import shutil
import ssl
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COLA_BASE_URL = "https://ttbonline.gov/colasonline"
SEARCH_URL = f"{COLA_BASE_URL}/publicSearchColasBasicProcess.do?action=search"
SORT_URL = f"{COLA_BASE_URL}/publicPageBasicCola.do?action=sort&sortcol=ttbId&order=desc"
NEXT_PAGE_URL = f"{COLA_BASE_URL}/publicPageBasicCola.do?action=page&pgfcn=nextset"
DETAIL_URL = f"{COLA_BASE_URL}/viewColaDetails.do?action=publicFormDisplay&ttbid={{ttb_id}}"
ATTACHMENT_PATH = "publicViewAttachment.do"

APPLICATION_COLUMNS = [
    "product_id",
    "label",
    "brand_name",
    "class_type",
    "alcohol_content",
    "net_contents",
    "bottler",
    "country",
    "images",
    "source_url",
    "ttb_id",
    "completed_date",
]

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
class ColaApplication:
    ttb_id: str
    completed_date: str
    label: str
    source_of_product: str = ""
    product_type: str = ""
    brand_name: str = ""
    fanciful_name: str = ""
    applicant_name_address: str = ""
    serial_number: str = ""
    net_contents: str = ""
    alcohol_content: str = ""
    class_type_description: str = ""
    status: str = ""
    date_issued: str = ""
    label_images: str = ""
    source_url: str = ""

    @property
    def class_type(self) -> str:
        return self.class_type_description or self.product_type

    @property
    def bottler(self) -> str:
        return self.applicant_name_address

    def raw_row(self, image_refs: list[str]) -> dict[str, str]:
        return {
            "ttb_id": self.ttb_id,
            "completed_date": self.completed_date,
            "product_id": f"cola-{self.ttb_id}",
            "label": self.label,
            "source_of_product": self.source_of_product,
            "product_type": self.product_type,
            "brand_name": self.brand_name,
            "fanciful_name": self.fanciful_name,
            "applicant_name_address": self.applicant_name_address,
            "serial_number": self.serial_number,
            "net_contents": self.net_contents,
            "alcohol_content": self.alcohol_content,
            "class_type_description": self.class_type_description,
            "status": self.status,
            "date_issued": self.date_issued,
            "label_images": self.label_images,
            "image_files": ";".join(image_refs),
            "source_url": self.source_url,
        }


@dataclass(frozen=True)
class LabelImageRecord:
    application: ColaApplication
    image_index: int
    source_filename: str
    local_filename: str

    @property
    def product_id(self) -> str:
        return f"cola-{self.application.ttb_id}-image-{self.image_index:02d}"

    @property
    def label(self) -> str:
        return self.application.label or self.product_id

    def application_payload(self) -> dict[str, str | None]:
        return {
            "brand_name": empty_to_none(self.application.brand_name),
            "class_type": empty_to_none(self.application.class_type),
            "alcohol_content": empty_to_none(self.application.alcohol_content),
            "net_contents": empty_to_none(self.application.net_contents),
            "bottler": empty_to_none(self.application.bottler),
            "country": None,
            "source_url": self.application.source_url,
        }

    def manifest_product(self, image_ref: str) -> dict[str, Any]:
        return {
            "product_id": self.product_id,
            "label": self.label,
            **self.application_payload(),
            "images": [image_ref],
            "notes": "Generated from public TTB COLA registry for verifier stress testing.",
        }


# Backward-compatible alias for existing tests/imports.
ColaRow = ColaApplication


def make_opener(*, insecure_tls: bool) -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    context = ssl.create_default_context()
    if insecure_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(context=context),
    )
    opener.addheaders = [
        ("User-Agent", "Treasury-Take-Home-COLA-Fixture-Builder/3.0"),
        ("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
    ]
    return opener


def request_text(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    data: dict[str, str] | None = None,
    retries: int = 3,
) -> str:
    encoded = urllib.parse.urlencode(data).encode("utf-8") if data is not None else None
    for attempt in range(1, retries + 1):
        try:
            with opener.open(url, data=encoded, timeout=45) as response:
                return response.read().decode("latin-1", errors="replace")
        except Exception:
            if attempt == retries:
                raise
            time.sleep(1.5 * attempt)
    return ""


def request_bytes(
    opener: urllib.request.OpenerDirector,
    url: str,
    *,
    referer: str,
    retries: int = 3,
) -> tuple[str, bytes] | None:
    for attempt in range(1, retries + 1):
        try:
            request = urllib.request.Request(url, headers={"Referer": referer})
            with opener.open(request, timeout=45) as response:
                content_type = response.headers.get("Content-Type", "")
                return content_type, response.read()
        except Exception:
            if attempt == retries:
                return None
            time.sleep(1.5 * attempt)
    return None


def collect_recent_ids(
    opener: urllib.request.OpenerDirector,
    *,
    target_products: int,
    date_from: str,
    date_to: str,
    delay: float,
) -> list[tuple[str, str]]:
    search_form = {
        "searchCriteria.dateCompletedFrom": date_from,
        "searchCriteria.dateCompletedTo": date_to,
        "searchCriteria.productNameSearchType": "C",
        "searchCriteria.productOrFancifulName": "",
        "searchCriteria.classTypeFrom": "",
        "searchCriteria.classTypeTo": "",
        "searchCriteria.originCode": "",
    }
    request_text(opener, SEARCH_URL, data=search_form)
    request_text(opener, SORT_URL)
    time.sleep(delay)

    found: dict[str, str] = {}
    while len(found) < target_products:
        page = request_text(opener, NEXT_PAGE_URL)
        rows = parse_listing(page)
        if not rows:
            break
        for ttb_id, completed_date in rows:
            found.setdefault(ttb_id, completed_date)
            if len(found) >= target_products:
                break
        time.sleep(delay)
    return list(found.items())


def parse_listing(page: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for table_row in re.findall(r"<tr[^>]*>.*?</tr>", page, flags=re.I | re.S):
        match = re.search(r"ttbid=(\d{14})", table_row)
        if not match:
            continue
        ttb_id = match.group(1)
        if ttb_id in seen:
            continue
        seen.add(ttb_id)
        date_match = re.search(r"\b\d{2}/\d{2}/\d{4}\b", clean_text(table_row))
        entries.append((ttb_id, date_match.group(0) if date_match else ""))
    return entries


def parse_printable(page: str, ttb_id: str, completed_date: str = "") -> ColaApplication:
    app = ColaApplication(
        ttb_id=ttb_id,
        completed_date=completed_date,
        label=field_value(page, "BRAND NAME") or field_value(page, "FANCIFUL NAME") or ttb_id,
        source_of_product=checked_inputs(page, "Source of Product"),
        product_type=checked_inputs(page, "Type of Product"),
        brand_name=field_value(page, "BRAND NAME"),
        fanciful_name=field_value(page, "FANCIFUL NAME"),
        applicant_name_address=field_value(page, "NAME AND ADDRESS"),
        serial_number=field_value(page, "SERIAL NUMBER"),
        net_contents=field_value(page, "NET CONTENTS"),
        alcohol_content=field_value(page, "ALCOHOL CONTENT"),
        class_type_description=field_value(page, "CLASS/TYPE DESCRIPTION"),
        status=field_value(page, "STATUS"),
        date_issued=field_value(page, "DATE ISSUED"),
        label_images=parse_label_image_summary(page),
        source_url=DETAIL_URL.format(ttb_id=ttb_id),
    )
    return app


def field_value(page: str, label: str) -> str:
    label_pattern = re.compile(
        rf'<div[^>]*class="[^"]*label[^"]*"[^>]*>\s*{re.escape(label)}\s*</div>',
        flags=re.I | re.S,
    )
    match = label_pattern.search(page)
    if not match:
        return ""
    data_match = re.search(r'<div[^>]*class="[^"]*data[^"]*"[^>]*>(.*?)</div>', page[match.end() :], re.I | re.S)
    return clean_text(data_match.group(1)) if data_match else ""


def checked_inputs(page: str, prefix: str) -> str:
    values = []
    for match in re.finditer(r'<input\b[^>]*>', page, flags=re.I | re.S):
        tag = match.group(0)
        if "checked" not in tag.lower():
            continue
        alt = re.search(r'alt="([^"]+)"', tag, flags=re.I)
        if not alt:
            continue
        value = html.unescape(alt.group(1))
        if value.lower().startswith(prefix.lower()):
            values.append(value.split(":", 1)[-1].strip())
    return "; ".join(values)


def parse_label_image_summary(page: str) -> str:
    summaries = []
    pattern = re.compile(r"Image Type:\s*</p>\s*(.*?)\s*<br>.*?Actual Dimensions:\s*(.*?)<br>", re.I | re.S)
    for image_type, dimensions in pattern.findall(page):
        label = clean_text(image_type)
        size = clean_text(dimensions)
        summaries.append(f"{label} ({size})" if size else label)
    return " | ".join(summaries)


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"(?i)<br\s*/?>", ", ", value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"(\s*,\s*)+", ", ", text)
    return text.strip(" ,")


def find_attachment_refs(page: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for src in re.findall(r'<img[^>]+src="([^"]*publicViewAttachment\.do\?[^"]+)"', page, flags=re.I):
        decoded = html.unescape(src)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(decoded).query)
        filename = query.get("filename", [""])[0]
        filetype = query.get("filetype", ["l"])[0] or "l"
        if not filename:
            continue
        key = (filename, filetype)
        if key not in seen:
            seen.add(key)
            refs.append(key)
    return refs


def download_label_images(
    opener: urllib.request.OpenerDirector,
    *,
    application: ColaApplication,
    detail_page: str,
    images_dir: Path,
    remaining: int,
) -> list[LabelImageRecord]:
    records: list[LabelImageRecord] = []
    referer = application.source_url
    for image_index, (remote_filename, filetype) in enumerate(find_attachment_refs(detail_page), 1):
        if len(records) >= remaining:
            break
        attachment_url = f"{COLA_BASE_URL}/{ATTACHMENT_PATH}?" + urllib.parse.urlencode(
            {"filename": remote_filename, "filetype": filetype}
        )
        response = request_bytes(opener, attachment_url, referer=referer)
        if response is None:
            continue
        content_type, body = response
        if "image" not in content_type.lower():
            continue
        filename = f"{application.ttb_id}_{image_index:02d}{guess_extension(content_type, remote_filename)}"
        (images_dir / filename).write_bytes(body)
        records.append(
            LabelImageRecord(
                application=application,
                image_index=image_index,
                source_filename=remote_filename,
                local_filename=filename,
            )
        )
    return records


def guess_extension(content_type: str, source_name: str) -> str:
    media_type = content_type.split(";", 1)[0].strip().lower()
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/tiff": ".tif",
        "image/webp": ".webp",
    }.get(media_type)
    if extension:
        return extension
    suffix = Path(urllib.parse.unquote(source_name)).suffix
    return suffix if suffix else ".jpg"


def scrape_records(
    *,
    count: int,
    date_from: str,
    date_to: str,
    delay: float,
    out_dir: Path,
    insecure_tls: bool,
    max_products_scan: int,
) -> tuple[list[LabelImageRecord], list[dict[str, str]]]:
    raw_download_dir = out_dir / "_downloaded_images"
    reset_dir(raw_download_dir)
    opener = make_opener(insecure_tls=insecure_tls)
    ids = collect_recent_ids(
        opener,
        target_products=max_products_scan,
        date_from=date_from,
        date_to=date_to,
        delay=delay,
    )

    records: list[LabelImageRecord] = []
    raw_rows: list[dict[str, str]] = []
    for ttb_id, completed_date in ids:
        if len(records) >= count:
            break
        detail_page = request_text(opener, DETAIL_URL.format(ttb_id=ttb_id))
        application = parse_printable(detail_page, ttb_id, completed_date)
        new_records = download_label_images(
            opener,
            application=application,
            detail_page=detail_page,
            images_dir=raw_download_dir,
            remaining=count - len(records),
        )
        image_refs = [f"_downloaded_images/{record.local_filename}" for record in new_records]
        raw_rows.append(application.raw_row(image_refs))
        records.extend(new_records)
        print(f"[{len(records):>3}/{count}] {ttb_id} {application.label!r} {len(new_records)} image(s)")
        time.sleep(delay)

    return records[:count], raw_rows


def write_fixture_outputs(
    *,
    records: list[LabelImageRecord],
    raw_rows: list[dict[str, str]],
    out_dir: Path,
    single_count: int,
    zip_output: bool,
) -> None:
    batch_dir = out_dir / f"batch-{len(records):03d}"
    single_dir = out_dir / "single-verification"
    reset_dir(batch_dir)
    reset_dir(single_dir)
    (batch_dir / "images").mkdir(parents=True, exist_ok=True)

    products = []
    csv_rows = []
    for record in records:
        target = batch_dir / "images" / record.local_filename
        shutil.copy2(out_dir / "_downloaded_images" / record.local_filename, target)
        image_ref = f"images/{record.local_filename}"
        product = record.manifest_product(image_ref)
        products.append(product)
        csv_rows.append(csv_row(record, image_ref))

    write_json(batch_dir / "manifest.json", {"products": products})
    write_csv(batch_dir / "application_data.csv", APPLICATION_COLUMNS, csv_rows)
    write_csv(out_dir / "raw_cola_applications.csv", RAW_COLUMNS, raw_rows)
    (batch_dir / "README.md").write_text(
        "# COLA Batch Stress Fixture\n\n"
        f"This folder contains {len(records)} public COLA label image(s). "
        "Use the Batch Jobs workflow: upload the `images/` folder and attach `manifest.json`.\n\n"
        "The manifest maps each image to application fields scraped from the public COLA registry.\n",
        encoding="utf-8",
    )

    for index, record in enumerate(records[:single_count], 1):
        write_single_fixture(record, source_dir=out_dir / "_downloaded_images", root=single_dir, index=index)

    if zip_output:
        zip_directory(batch_dir, batch_dir / f"batch-{len(records):03d}.zip")
        for fixture in single_dir.iterdir():
            if fixture.is_dir():
                zip_directory(fixture, fixture.with_suffix(".zip"))


def write_single_fixture(record: LabelImageRecord, *, source_dir: Path, root: Path, index: int) -> None:
    fixture = root / f"single-{index:03d}-{slugify(record.label)}"
    reset_dir(fixture)
    images_dir = fixture / "images"
    images_dir.mkdir(parents=True, exist_ok=True)
    target = images_dir / record.local_filename
    shutil.copy2(source_dir / record.local_filename, target)
    image_ref = f"images/{target.name}"
    write_json(fixture / "application.json", record.application_payload())
    write_json(fixture / "manifest.json", {"products": [record.manifest_product(image_ref)]})
    (fixture / "source.txt").write_text(record.application.source_url, encoding="utf-8")


def csv_row(record: LabelImageRecord, image_ref: str) -> dict[str, str]:
    payload = record.application_payload()
    return {
        "product_id": record.product_id,
        "label": record.label,
        "brand_name": payload.get("brand_name") or "",
        "class_type": payload.get("class_type") or "",
        "alcohol_content": payload.get("alcohol_content") or "",
        "net_contents": payload.get("net_contents") or "",
        "bottler": payload.get("bottler") or "",
        "country": payload.get("country") or "",
        "images": image_ref,
        "source_url": payload.get("source_url") or "",
        "ttb_id": record.application.ttb_id,
        "completed_date": record.application.completed_date,
    }


def write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def zip_directory(source: Path, target: Path) -> None:
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source.rglob("*"):
            if path == target:
                continue
            archive.write(path, path.relative_to(source.parent))


def reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def slugify(value: Any) -> str:
    text = re.sub(r"[^a-zA-Z0-9]+", "-", str(value).lower()).strip("-")
    return text[:64] or "cola-label"


def empty_to_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def default_date_to() -> str:
    return datetime.now(UTC).strftime("%m/%d/%Y")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build single and batch COLA label fixtures.")
    parser.add_argument("--count", type=int, default=300, help="Number of label images to collect for batch testing.")
    parser.add_argument("--single-count", type=int, default=10, help="Number of single-verification fixtures to write.")
    parser.add_argument("--date-from", default="01/01/2020")
    parser.add_argument("--date-to", default=default_date_to())
    parser.add_argument("--delay", type=float, default=0.5)
    parser.add_argument("--out-dir", default="samples/cola-scale")
    parser.add_argument("--max-products-scan", type=int, default=900)
    parser.add_argument("--zip", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--insecure-tls", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    records, raw_rows = scrape_records(
        count=args.count,
        date_from=args.date_from,
        date_to=args.date_to,
        delay=args.delay,
        out_dir=out_dir,
        insecure_tls=args.insecure_tls,
        max_products_scan=max(args.max_products_scan, args.count),
    )
    if not records:
        raise SystemExit("No label images were downloaded. Try a wider date range or --insecure-tls.")
    write_fixture_outputs(
        records=records,
        raw_rows=raw_rows,
        out_dir=out_dir,
        single_count=min(args.single_count, len(records)),
        zip_output=args.zip,
    )
    print(f"Wrote batch fixture: {out_dir / f'batch-{len(records):03d}'}")
    print(f"Wrote single fixtures: {out_dir / 'single-verification'}")


if __name__ == "__main__":
    main()
