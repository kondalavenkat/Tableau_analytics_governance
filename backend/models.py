from pydantic import BaseModel, Field


class SourceScanRequest(BaseModel):
    source_type: str = Field(default="local_folder")
    path: str


class WorkbookScanResult(BaseModel):
    workbook_name: str
    source_path: str
    datasource: str = "Unknown"
    query_name: str
    query_hash: str | None = None
    type: str = "Custom SQL"
    original_temp_query: str
    raw_initial_sql: str | None = None
    converted_permanent_query: str
    setup_sql: str = ""
    runtime_sql: str = ""
    runtime_temp_definitions: dict[str, str] = Field(default_factory=dict)
    runtime_cte_prefix: str = "perm_"
    targets: list[dict] = Field(default_factory=list)
    related_runtime_targets: list[dict] = Field(default_factory=list)
    temp_table_tokens: list[str] = Field(default_factory=list)
    status: str
    error_message: str = ""


class ScanSummary(BaseModel):
    source_type: str
    workbooks_scanned: int
    temp_queries_found: int
    converted_queries: int
    failed_queries: int


class WriteBackFromScanRequest(BaseModel):
    workbook_path: str
    selected_queries: list[WorkbookScanResult]
