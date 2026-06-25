from __future__ import annotations

import asyncio
import csv
import io
import json
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
    CorrectionRecord,
    ExpectedFields,
    ExtractionResult,
    ProcessingMode,
    UploadedImage,
)
from app.services.llm_extractor import OpenRouterExtractionError, extract_with_openrouter
from app.services.local_ocr import extract_with_tesseract
from app.services.verifier import verify_extraction

app = FastAPI(title="Treasury Take Home V3", version="3.0.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

jobs: dict[str, BatchJob] = {}
corrections: list[CorrectionRecord] = []


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "v3": True,
        "processing_mode": settings.processing_mode,
        "llm": {
            "available": bool(settings.openrouter_api_key),
            "provider": "openrouter",
            "model": settings.openrouter_model,
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
    expected = ExpectedFields(
        brand_name=brand_name,
        class_type=class_type,
        alcohol_content=alcohol_content,
        net_contents=net_contents,
        bottler=bottler,
        country=country,
    )
    payloads = await read_images(images)
    return await verify_product(
        product_id=product_id or str(uuid.uuid4()),
        label=None,
        expected=expected,
        images=payloads,
        processing_mode=parse_mode(processing_mode),
        debug_mode=debug_mode,
    )


@app.post("/api/batch/jobs")
async def create_batch_job(
    background_tasks: BackgroundTasks,
    images: Annotated[list[UploadFile], File(alias="images[]")],
    manifest: Annotated[UploadFile | None, File()] = None,
    processing_mode: Annotated[str | None, Form()] = None,
    debug_mode: Annotated[bool, Form()] = False,
) -> dict[str, str]:
    payloads = await read_images(images)
    products = await parse_manifest(manifest, payloads)
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
    background_tasks.add_task(
        process_batch, job_id, products, parse_mode(processing_mode), debug_mode
    )
    return {"job_id": job_id}


@app.get("/api/batch/jobs/{job_id}")
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
        issues = [check.field for check in result.fields.values() if check.status.value != "pass"]
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


async def read_images(files: list[UploadFile]) -> list[UploadedImage]:
    if not files:
        raise HTTPException(status_code=400, detail="at least one image is required")
    if len(files) > settings.max_images_per_product:
        raise HTTPException(
            status_code=400,
            detail=f"at most {settings.max_images_per_product} images are supported per product",
        )
    payloads = []
    for index, file in enumerate(files):
        payloads.append(
            UploadedImage(
                image_id=f"img-{index + 1}",
                filename=file.filename or f"image-{index + 1}.png",
                content_type=file.content_type,
                content=await file.read(),
            )
        )
    return payloads


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
    extraction: ExtractionResult
    try:
        if processing_mode == ProcessingMode.local_ocr:
            if not settings.allow_local_ocr:
                raise HTTPException(status_code=400, detail="local OCR mode is disabled")
            extraction = await extract_with_tesseract(images)
        else:
            extraction = await extract_with_openrouter(images, expected)
    except OpenRouterExtractionError as exc:
        extraction = ExtractionResult(
            fields={},
            raw_text="",
            confidence=0.0,
            notes=[f"LLM extraction failed: {exc}"],
            provider="openrouter",
            model_used=settings.openrouter_model,
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
    semaphore = asyncio.Semaphore(settings.batch_parallelism)

    async def run_product(product: dict) -> None:
        async with semaphore:
            try:
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
                job.completed += 1

    await asyncio.gather(*(run_product(product) for product in products))
    job.results.sort(key=lambda result: {"fail": 0, "review": 1, "pass": 2}[result.verdict.value])
    job.status = "complete" if not job.errors else "failed"


async def parse_manifest(manifest: UploadFile | None, images: list[UploadedImage]) -> list[dict]:
    by_name = {image.filename.lower(): image for image in images}
    if manifest is None:
        return [
            {
                "product_id": str(uuid.uuid4()),
                "label": image.filename,
                "expected": ExpectedFields(),
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
        matched = [
            by_name[ref.lower().split("/")[-1].split("\\")[-1]]
            for ref in image_refs
            if ref.lower().split("/")[-1].split("\\")[-1] in by_name
        ]
        if not matched:
            continue
        products.append(
            {
                "product_id": str(row.get("product_id") or uuid.uuid4()),
                "label": row.get("label") or row.get("brand_name"),
                "expected": ExpectedFields(
                    brand_name=row.get("brand_name"),
                    class_type=row.get("class_type"),
                    alcohol_content=row.get("alcohol_content"),
                    net_contents=row.get("net_contents"),
                    bottler=row.get("bottler"),
                    country=row.get("country"),
                ),
                "images": matched[: settings.max_images_per_product],
            }
        )
    return products


def image_refs_from_row(row: dict) -> list[str]:
    refs: list[str] = []
    for key in ["image", "filename", "file", "front_image", "back_image"]:
        if row.get(key):
            refs.append(str(row[key]))
    for key in ["images", "files", "image_names", "imageNames"]:
        value = row.get(key)
        if isinstance(value, list):
            for item in value:
                refs.append(
                    str(item.get("image") or item.get("filename") or item.get("file") or item)
                )
        elif isinstance(value, str):
            refs.extend(part.strip() for part in value.split(";") if part.strip())
    return refs


def parse_mode(value: str | None) -> ProcessingMode:
    if value == ProcessingMode.local_ocr.value:
        return ProcessingMode.local_ocr
    return ProcessingMode.llm
