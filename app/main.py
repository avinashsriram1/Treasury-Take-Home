from __future__ import annotations

import asyncio
import csv
import io
import json
import math
import time
import uuid
from typing import Annotated

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.models import (
    BatchJob,
    BatchMetrics,
    CorrectionRecord,
    ExpectedFields,
    ExtractionResult,
    ProcessingMode,
    UploadedImage,
)
from app.services.azure_foundry_extractor import AzureFoundryExtractionError, extract_with_azure_foundry
from app.services.llm_extractor import OpenRouterExtractionError, extract_with_openrouter
from app.services.local_ocr import extract_with_tesseract
from app.services.verifier import verify_extraction

app = FastAPI(title="Treasury Take Home V3", version="3.1.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

jobs: dict[str, BatchJob] = {}
corrections: list[CorrectionRecord] = []

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
MANIFEST_NAMES = {"manifest.csv", "manifest.json"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/health")
async def health() -> dict:
    config_error = settings.free_route_config_error
    using_foundry = settings.llm_provider == "azure_foundry"
    return {
        "status": "ok",
        "v3": True,
        "processing_mode": settings.processing_mode,
        "llm": {
            "available": (
                bool(settings.azure_foundry_endpoint and settings.azure_foundry_api_key)
                if using_foundry
                else bool(settings.openrouter_api_key) and config_error is None
            ),
            "provider": settings.llm_provider,
            "configured_model": settings.azure_foundry_deployment if using_foundry else settings.openrouter_model,
            "foundry_endpoint": settings.azure_foundry_endpoint if using_foundry else None,
            "fallback_models": list(settings.openrouter_fallback_models),
            "fallbacks_enabled": settings.openrouter_enable_fallbacks,
            "timeout_seconds": (
                settings.azure_foundry_request_timeout_seconds
                if using_foundry
                else settings.openrouter_request_timeout_seconds
            ),
            "connect_timeout_seconds": (
                settings.azure_foundry_connect_timeout_seconds
                if using_foundry
                else settings.openrouter_connect_timeout_seconds
            ),
            "max_image_long_edge": settings.openrouter_max_image_long_edge,
            "jpeg_quality": settings.openrouter_jpeg_quality,
            "max_tokens": settings.azure_foundry_max_output_tokens if using_foundry else settings.openrouter_max_tokens,
            "free_route_required": settings.openrouter_require_free,
            "paid_fallback_allowed": settings.openrouter_allow_paid_fallback,
            "free_route_config_error": None if using_foundry else config_error,
        },
        "batch": {
            "llm_parallelism": settings.llm_batch_parallelism,
            "local_ocr_parallelism": settings.local_ocr_batch_parallelism,
        },
        "local_ocr": {"enabled": settings.allow_local_ocr},
        "security": {"raw_extraction_visible": settings.show_raw_extraction},
    }


@app.get("/api/openapi.json")
async def openapi_json() -> dict:
    return app.openapi()


@app.post("/api/verify")
async def verify(
    images: Annotated[list[UploadFile], File(alias="images[]")],
    manifest: Annotated[UploadFile | None, File()] = None,
    brand_name: Annotated[str | None, Form()] = None,
    class_type: Annotated[str | None, Form()] = None,
    alcohol_content: Annotated[str | None, Form()] = None,
    net_contents: Annotated[str | None, Form()] = None,
    bottler: Annotated[str | None, Form()] = None,
    country: Annotated[str | None, Form()] = None,
    product_id: Annotated[str | None, Form()] = None,
    processing_mode: Annotated[str | None, Form()] = None,
    debug_mode: Annotated[bool, Form()] = False,
):
    expected = expected_from_values(brand_name, class_type, alcohol_content, net_contents, bottler, country)
    payloads, recovered_manifest = await read_uploads(images)
    manifest = manifest or recovered_manifest
    if manifest is not None:
        products = await parse_manifest(manifest, payloads, expected)
        if len(products) != 1:
            raise HTTPException(
                status_code=400, detail="single verification manifest must resolve to exactly one product"
            )
        product = products[0]
        if len(product["images"]) > settings.max_images_per_product:
            raise HTTPException(
                status_code=400,
                detail=f"at most {settings.max_images_per_product} images are supported per product",
            )
        return await verify_product(
            product_id=product_id or product["product_id"],
            label=product.get("label"),
            expected=product["expected"],
            images=product["images"],
            processing_mode=parse_mode(processing_mode),
            debug_mode=debug_mode,
        )
    if len(payloads) > settings.max_images_per_product:
        raise HTTPException(
            status_code=400,
            detail=f"at most {settings.max_images_per_product} images are supported per product",
        )
    return await verify_product(
        product_id=product_id or str(uuid.uuid4()),
        label=payloads[0].filename if payloads else None,
        expected=expected,
        images=payloads,
        processing_mode=parse_mode(processing_mode),
        debug_mode=debug_mode,
    )


@app.post("/api/batch/jobs")
@app.post("/api/batch/jobs/", include_in_schema=False)
async def create_batch_job(
    background_tasks: BackgroundTasks,
    images: Annotated[list[UploadFile], File(alias="images[]")],
    manifest: Annotated[UploadFile | None, File()] = None,
    brand_name: Annotated[str | None, Form()] = None,
    class_type: Annotated[str | None, Form()] = None,
    alcohol_content: Annotated[str | None, Form()] = None,
    net_contents: Annotated[str | None, Form()] = None,
    bottler: Annotated[str | None, Form()] = None,
    country: Annotated[str | None, Form()] = None,
    processing_mode: Annotated[str | None, Form()] = None,
    debug_mode: Annotated[bool, Form()] = False,
) -> dict[str, str]:
    payloads, recovered_manifest = await read_uploads(images)
    manifest = manifest or recovered_manifest
    default_expected = expected_from_values(brand_name, class_type, alcohol_content, net_contents, bottler, country)
    products = await parse_manifest(manifest, payloads, default_expected)
    if not products:
        raise HTTPException(status_code=400, detail="no products could be built from the uploaded images/manifest")
    job_id = str(uuid.uuid4())
    jobs[job_id] = BatchJob(
        job_id=job_id,
        status="queued",
        total=len(products),
        completed=0,
        counts={"pass": 0, "review": 0, "fail": 0},
        results=[],
        errors=[],
    )
    background_tasks.add_task(process_batch, job_id, products, parse_mode(processing_mode), debug_mode)
    return {"job_id": job_id}


@app.get("/api/batch/jobs/{job_id}")
@app.get("/api/batch/jobs/{job_id}/", include_in_schema=False)
async def get_batch_job(job_id: str) -> BatchJob:
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="batch job not found")
    return jobs[job_id]


@app.get("/api/batch/jobs/{job_id}/export.csv")
async def export_batch(job_id: str) -> StreamingResponse:
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="batch job not found")
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["product_id", "label", "verdict", "processing_mode", "latency_ms", "issues"])
    for result in jobs[job_id].results:
        issues = [check.field for check in result.fields.values() if check.status.value not in {"pass", "not_checked"}]
        if result.government_warning.status.value != "pass":
            issues.insert(0, "government_warning")
        writer.writerow(
            [
                result.product_id,
                result.label or "",
                result.verdict.value,
                result.processing_mode.value,
                result.latency_ms,
                "; ".join(issues),
            ]
        )
    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="ttb-v3-batch.csv"'},
    )


@app.post("/api/corrections")
async def add_correction(record: CorrectionRecord) -> dict[str, bool]:
    corrections.append(record)
    return {"ok": True}


def expected_from_values(
    brand_name: str | None,
    class_type: str | None,
    alcohol_content: str | None,
    net_contents: str | None,
    bottler: str | None,
    country: str | None,
) -> ExpectedFields:
    return ExpectedFields(
        brand_name=clean_form_value(brand_name),
        class_type=clean_form_value(class_type),
        alcohol_content=clean_form_value(alcohol_content),
        net_contents=clean_form_value(net_contents),
        bottler=clean_form_value(bottler),
        country=clean_form_value(country),
    )


def clean_form_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


async def read_uploads(files: list[UploadFile]) -> tuple[list[UploadedImage], UploadFile | None]:
    if not files:
        raise HTTPException(status_code=400, detail="at least one image is required")
    payloads: list[UploadedImage] = []
    recovered_manifest: UploadFile | None = None
    for index, file in enumerate(files):
        filename = file.filename or f"image-{index + 1}.png"
        ext = upload_extension(filename)
        basename = filename.replace("\\", "/").split("/")[-1].lower()
        if ext in {".csv", ".json"} and basename in MANIFEST_NAMES:
            if recovered_manifest is not None:
                raise HTTPException(status_code=400, detail="only one manifest file can be uploaded")
            recovered_manifest = file
            continue
        if ext not in IMAGE_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"unsupported upload in images field: {filename}")
        payloads.append(
            UploadedImage(
                image_id=f"img-{len(payloads) + 1}",
                filename=filename,
                content_type=file.content_type,
                content=await file.read(),
            )
        )
    if not payloads:
        raise HTTPException(status_code=400, detail="at least one label image is required")
    return payloads, recovered_manifest


def upload_extension(filename: str) -> str:
    normalized = filename.replace("\\", "/").split("/")[-1].lower()
    return "." + normalized.rsplit(".", 1)[-1] if "." in normalized else ""


async def verify_product(
    *,
    product_id: str,
    label: str | None,
    expected: ExpectedFields,
    images: list[UploadedImage],
    processing_mode: ProcessingMode,
    debug_mode: bool,
):
    started = time.perf_counter()
    try:
        if processing_mode == ProcessingMode.local_ocr:
            if not settings.allow_local_ocr:
                raise HTTPException(status_code=400, detail="local OCR mode is disabled")
            extraction = await extract_with_tesseract(images)
        elif settings.llm_provider == "azure_foundry":
            extraction = await extract_with_azure_foundry(images, expected)
        elif settings.llm_provider == "openrouter":
            extraction = await extract_with_openrouter(images, expected)
        else:
            raise AzureFoundryExtractionError(f"Unsupported LLM_PROVIDER: {settings.llm_provider}")
    except (OpenRouterExtractionError, AzureFoundryExtractionError) as exc:
        provider = settings.llm_provider
        model = settings.azure_foundry_deployment if provider == "azure_foundry" else settings.openrouter_model
        extraction = ExtractionResult(
            fields={},
            raw_text=getattr(exc, "raw_output", ""),
            confidence=0.0,
            notes=[f"LLM extraction failed: {exc}"],
            provider=provider,
            model_used=model,
        )
    latency_ms = int((time.perf_counter() - started) * 1000)
    return verify_extraction(
        product_id=product_id,
        label=label,
        expected=expected,
        extraction=extraction,
        processing_mode=processing_mode,
        image_count=len(images),
        latency_ms=latency_ms,
        show_raw=settings.show_raw_extraction and debug_mode,
    )


async def process_batch(
    job_id: str,
    products: list[dict],
    processing_mode: ProcessingMode,
    debug_mode: bool,
) -> None:
    job = jobs[job_id]
    job.status = "running"
    started = time.perf_counter()
    queue: asyncio.Queue[dict | None] = asyncio.Queue()
    for product in products:
        queue.put_nowait(product)
    worker_count = min(
        len(products),
        settings.local_ocr_batch_parallelism
        if processing_mode == ProcessingMode.local_ocr
        else settings.llm_batch_parallelism,
    )
    for _ in range(worker_count):
        queue.put_nowait(None)

    async def worker() -> None:
        while True:
            product = await queue.get()
            try:
                if product is None:
                    return
                result = await verify_product(
                    product_id=product["product_id"],
                    label=product.get("label"),
                    expected=product["expected"],
                    images=product["images"],
                    processing_mode=processing_mode,
                    debug_mode=debug_mode,
                )
                job.results.append(result)
                job.counts[result.verdict.value] += 1
            except Exception as exc:  # noqa: BLE001
                job.errors.append(str(exc))
            finally:
                if product is not None:
                    job.completed += 1
                    job.metrics = compute_batch_metrics(job.results, started)
                queue.task_done()

    await asyncio.gather(*(worker() for _ in range(worker_count)))
    job.results.sort(key=lambda result: {"fail": 0, "review": 1, "pass": 2}[result.verdict.value])
    job.metrics = compute_batch_metrics(job.results, started)
    job.status = "complete" if not job.errors else "failed"


def compute_batch_metrics(results: list, started: float) -> BatchMetrics:
    if not results:
        return BatchMetrics()
    latencies = sorted(result.latency_ms for result in results)
    average = int(sum(latencies) / len(latencies))
    p50 = percentile(latencies, 0.50)
    p95 = percentile(latencies, 0.95)
    slowest = max(results, key=lambda result: result.latency_ms)
    elapsed_minutes = max((time.perf_counter() - started) / 60.0, 1 / 60000)
    images = sum(result.image_count for result in results)
    return BatchMetrics(
        average_latency_ms=average,
        p50_latency_ms=p50,
        p95_latency_ms=p95,
        slowest_product_id=slowest.product_id,
        slowest_latency_ms=slowest.latency_ms,
        throughput_images_per_minute=round(images / elapsed_minutes, 2),
    )


def percentile(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    index = min(len(values) - 1, math.ceil(len(values) * fraction) - 1)
    return values[index]


async def parse_manifest(
    manifest: UploadFile | None,
    images: list[UploadedImage],
    default_expected: ExpectedFields,
) -> list[dict]:
    matcher = ImageMatcher(images)
    if manifest is None:
        return [
            {
                "product_id": str(uuid.uuid4()),
                "label": image.filename,
                "expected": default_expected,
                "images": [image],
            }
            for image in images
        ]

    content = (await manifest.read()).decode("utf-8-sig")
    if (manifest.filename or "").lower().endswith(".json"):
        data = json.loads(content)
        rows = data.get("products", data) if isinstance(data, dict) else data
    else:
        rows = list(csv.DictReader(io.StringIO(content)))

    products = []
    for row in rows:
        image_refs = image_refs_from_row(row)
        matched = [matcher.match(ref) for ref in image_refs]
        matched = [image for image in matched if image is not None]
        if not matched:
            continue
        products.append(
            {
                "product_id": str(row.get("product_id") or uuid.uuid4()),
                "label": row.get("label") or row.get("brand_name") or matched[0].filename,
                "expected": ExpectedFields(
                    brand_name=clean_form_value(row.get("brand_name")) or default_expected.brand_name,
                    class_type=clean_form_value(row.get("class_type")) or default_expected.class_type,
                    alcohol_content=clean_form_value(row.get("alcohol_content")) or default_expected.alcohol_content,
                    net_contents=clean_form_value(row.get("net_contents")) or default_expected.net_contents,
                    bottler=clean_form_value(row.get("bottler")) or default_expected.bottler,
                    country=clean_form_value(row.get("country")) or default_expected.country,
                ),
                "images": matched[: settings.max_images_per_product],
            }
        )
    return products


class ImageMatcher:
    def __init__(self, images: list[UploadedImage]) -> None:
        self.by_exact = {normalize_path(image.filename): image for image in images}
        by_base: dict[str, list[UploadedImage]] = {}
        for image in images:
            by_base.setdefault(manifest_basename(image.filename), []).append(image)
        self.by_base = by_base

    def match(self, ref: str) -> UploadedImage | None:
        exact = self.by_exact.get(normalize_path(ref))
        if exact:
            return exact
        candidates = self.by_base.get(manifest_basename(ref), [])
        if len(candidates) > 1:
            raise HTTPException(status_code=400, detail=f"ambiguous image filename in manifest: {ref}")
        return candidates[0] if candidates else None


def image_refs_from_row(row: dict) -> list[str]:
    refs: list[str] = []
    for key in ["image", "filename", "file", "front_image", "back_image"]:
        if row.get(key):
            refs.append(str(row[key]))
    for key in ["images", "files", "image_names", "imageNames"]:
        value = row.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    refs.append(str(item.get("image") or item.get("filename") or item.get("file") or item))
                else:
                    refs.append(str(item))
        elif isinstance(value, str):
            refs.extend(part.strip() for part in value.replace(",", ";").split(";") if part.strip())
    return refs


def normalize_path(value: str) -> str:
    return value.replace("\\", "/").strip().lower()


def manifest_basename(value: str) -> str:
    return normalize_path(value).split("/")[-1]


def parse_mode(value: str | None) -> ProcessingMode:
    if value == ProcessingMode.local_ocr.value:
        return ProcessingMode.local_ocr
    return ProcessingMode.llm

