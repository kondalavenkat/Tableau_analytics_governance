import os
import shutil
import tempfile

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .models import SourceScanRequest, WriteBackFromScanRequest
from .services.source_adapters import SourceAdapterRegistry
from .services.temp_query_scan_service import TempQueryScanService

app = FastAPI(title="Temp To Permanent Query Converter API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

adapter_registry = SourceAdapterRegistry()
scan_service = TempQueryScanService()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/scan-workbook")
def scan_workbook(file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".twb", ".twbx", ".tds", ".tdsx")):
        raise HTTPException(status_code=400, detail="Upload a Tableau workbook or datasource file.")
    results = scan_service.scan_uploaded_workbook(file)
    return _build_response("upload", 1, results)


@app.post("/scan-source")
def scan_source(request: SourceScanRequest):
    adapter = adapter_registry.get(request.source_type)
    workbook_paths = adapter.list_workbooks(request.path)

    all_results = []
    for workbook_path in workbook_paths:
        all_results.extend(scan_service.scan_workbook_file(workbook_path))

    return _build_response(request.source_type, len(workbook_paths), all_results)


@app.post("/scan-and-write-back")
def scan_and_write_back(background_tasks: BackgroundTasks, file: UploadFile = File(...)):
    if not file.filename.lower().endswith((".twb", ".twbx")):
        raise HTTPException(status_code=400, detail="Upload a .twb or .twbx file.")

    upload_dir = tempfile.mkdtemp(prefix="twbx_write_back_")
    try:
        input_path = os.path.join(upload_dir, file.filename)
        output_path = os.path.join(upload_dir, "converted_" + file.filename)

        with open(input_path, "wb") as handle:
            shutil.copyfileobj(file.file, handle)

        result = scan_service.scan_and_write_back(input_path, output_path)
        if not result["output_path"]:
            shutil.rmtree(upload_dir, ignore_errors=True)
            return {"message": result["message"], "results": []}

        background_tasks.add_task(shutil.rmtree, upload_dir, True)
        return FileResponse(
            path=output_path,
            filename="converted_" + file.filename,
            media_type="application/octet-stream",
        )
    except Exception as exc:
        shutil.rmtree(upload_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/write-back-from-scan")
def write_back_from_scan(
    background_tasks: BackgroundTasks, request: WriteBackFromScanRequest
):
    workbook_path = request.workbook_path
    if not workbook_path.lower().endswith((".twb", ".twbx")):
        raise HTTPException(status_code=400, detail="Write-back supports only .twb or .twbx files.")

    if not os.path.exists(workbook_path):
        raise HTTPException(status_code=404, detail="Workbook file not found.")

    output_dir = tempfile.mkdtemp(prefix="scan_write_back_")
    try:
        output_path = os.path.join(
            output_dir, "converted_" + os.path.basename(workbook_path)
        )
        result = scan_service.write_back_selected_queries(
            workbook_path,
            [item.model_dump() for item in request.selected_queries],
            output_path,
        )

        if not result["output_path"]:
            shutil.rmtree(output_dir, ignore_errors=True)
            return {"message": result["message"], "results": result["results"]}

        background_tasks.add_task(shutil.rmtree, output_dir, True)
        return FileResponse(
            path=result["output_path"],
            filename=os.path.basename(result["output_path"]),
            media_type="application/octet-stream",
        )
    except Exception as exc:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(exc))


def _build_response(source_type: str, workbooks_scanned: int, results: list[dict]):
    converted = sum(1 for r in results if r["status"] == "converted")
    failed = sum(1 for r in results if r["status"] == "failed")
    datasources = len(
        {
            (r["source_path"], r.get("datasource", "Unknown"))
            for r in results
            if r.get("datasource")
        }
    )
    return {
        "summary": {
            "source_type": source_type,
            "workbooks_scanned": workbooks_scanned,
            "datasources": datasources,
            "temp_queries_found": len(results),
            "converted_queries": converted,
            "failed_queries": failed,
        },
        "results": results,
    }
