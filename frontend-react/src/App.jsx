import { useEffect, useMemo, useState } from "react";

const API_BASE = "http://127.0.0.1:8008";
const STORAGE_KEY = "temp-to-perm-ui-state";

const initialSummary = {
  source_type: "upload",
  workbooks_scanned: 0,
  datasources: 0,
  temp_queries_found: 0,
  converted_queries: 0,
  failed_queries: 0,
};

const initialState = {
  sourceType: "local_folder",
  sourcePath: "",
  autoDownloadEnabled: false,
  summary: initialSummary,
  results: [],
  error: "",
  infoMessage: "",
  selectedWorkbookPath: "",
  selectedQueryKeys: [],
};

function buildQueryKey(item) {
  return [item.source_path, item.query_hash || item.query_name, item.type, item.datasource].join("::");
}

function loadSavedState() {
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY);
    if (!raw) return initialState;
    const parsed = JSON.parse(raw);
    return {
      ...initialState,
      ...parsed,
      summary: { ...initialSummary, ...(parsed.summary || {}) },
      results: Array.isArray(parsed.results) ? parsed.results : [],
      selectedQueryKeys: Array.isArray(parsed.selectedQueryKeys) ? parsed.selectedQueryKeys : [],
    };
  } catch {
    return initialState;
  }
}

function getConvertedSqlSections(item) {
  if (item.type === "Initial SQL") {
    return [
      {
        label: "Setup SQL (Initial / One-time SQL)",
        value: item.setup_sql || item.converted_permanent_query,
      },
      {
        label: "Runtime SQL (Custom SQL relation)",
        value: item.runtime_sql,
      },
    ].filter((section) => section.value);
  }

  return [
    {
      label: "Runtime SQL (Custom SQL relation)",
      value: item.runtime_sql || item.converted_permanent_query,
    },
  ].filter((section) => section.value);
}

function getConvertedSqlFallback(item) {
  return item.converted_permanent_query || item.runtime_sql || item.setup_sql || item.error_message || "Conversion failed.";
}

function getStatusLabel(status) {
  if (status === "converted") return "to convert";
  if (status === "failed") return "needs review";
  return status;
}

function PreviewModal({ rows, workbookName, onConfirm, onCancel, downloading }) {
  const [expandedKey, setExpandedKey] = useState(null);

  const converted = rows.filter((row) => row.status === "converted").length;
  const failed = rows.filter((row) => row.status === "failed").length;

  return (
    <div className="modal-backdrop" onClick={onCancel}>
      <div className="modal" onClick={(event) => event.stopPropagation()}>
        <div className="modal-header">
          <div>
            <p className="eyebrow">Preview & Confirm</p>
            <h2>{workbookName}</h2>
          </div>
          <button type="button" className="modal-close" onClick={onCancel}>
            x
          </button>
        </div>

        <div className="modal-chips">
          <span className="chip chip-total">{rows.length} selected</span>
          <span className="chip chip-converted">{converted} temp queries to convert</span>
          {failed > 0 ? <span className="chip chip-failed">{failed} need review</span> : null}
        </div>

        <p className="modal-note">
          Only <strong>temp query rows marked to convert</strong> will be written into the workbook.
          Rows marked for review are shown for reference and skipped during patching.
        </p>

        <div className="modal-query-list">
          {rows.map((item) => {
            const key = buildQueryKey(item);
            const open = expandedKey === key;

            return (
              <div key={key} className={`modal-query-row ${item.status}`}>
                <button
                  type="button"
                  className="modal-query-toggle"
                  onClick={() => setExpandedKey(open ? null : key)}
                >
                  <span className="modal-query-meta">
                    <strong>{item.query_name}</strong>
                    <span>{`Data source: ${item.datasource}`}</span>
                    <span className={`status-chip ${item.status}`}>{getStatusLabel(item.status)}</span>
                  </span>
                  <span className="modal-chevron">{open ? "▲" : "▼"}</span>
                </button>

                {open ? (
                  <div className="modal-sql-diff">
                    <div className="diff-col">
                      <p className="diff-label">Original (temp tables)</p>
                      <pre>{item.original_temp_query}</pre>
                    </div>
                    <div className="diff-col">
                      <p className="diff-label">Prepared Tableau SQL</p>
                      <div className="sql-section-stack">
                        {getConvertedSqlSections(item).map((section) => (
                          <div key={section.label} className="sql-section">
                            <p className="sql-section-label">{section.label}</p>
                            <pre className={item.status === "failed" ? "pre-failed" : ""}>{section.value}</pre>
                          </div>
                        ))}
                        {getConvertedSqlSections(item).length === 0 ? (
                          <pre className={item.status === "failed" ? "pre-failed" : ""}>
                            {getConvertedSqlFallback(item)}
                          </pre>
                        ) : null}
                      </div>
                    </div>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>

        <div className="modal-footer">
          <button type="button" className="secondary-button" onClick={onCancel} disabled={downloading}>
            Cancel
          </button>
          <button type="button" onClick={onConfirm} disabled={downloading || converted === 0}>
            {downloading ? "Preparing Download..." : `Confirm & Download (${converted} queries)`}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const saved = loadSavedState();

  const [sourceType, setSourceType] = useState(saved.sourceType);
  const [sourcePath, setSourcePath] = useState(saved.sourcePath);
  const [autoDownloadEnabled, setAutoDownloadEnabled] = useState(saved.autoDownloadEnabled);
  const [loading, setLoading] = useState(false);
  const [downloadLoading, setDownloadLoading] = useState(false);
  const [error, setError] = useState("");
  const [infoMessage, setInfoMessage] = useState("");
  const [summary, setSummary] = useState(saved.summary);
  const [results, setResults] = useState(saved.results);
  const [selectedWorkbookPath, setSelectedWorkbookPath] = useState(saved.selectedWorkbookPath);
  const [selectedQueryKeys, setSelectedQueryKeys] = useState(saved.selectedQueryKeys);
  const [showPreview, setShowPreview] = useState(false);

  const workbookOptions = useMemo(() => {
    const byPath = new Map();
    results.forEach((item) => {
      if (!byPath.has(item.source_path)) {
        byPath.set(item.source_path, {
          source_path: item.source_path,
          workbook_name: item.workbook_name,
          total: 0,
          converted: 0,
          failed: 0,
        });
      }
      const workbook = byPath.get(item.source_path);
      workbook.total += 1;
      if (item.status === "converted") workbook.converted += 1;
      if (item.status === "failed") workbook.failed += 1;
    });
    return Array.from(byPath.values());
  }, [results]);

  const activeWorkbookPath = selectedWorkbookPath || workbookOptions[0]?.source_path || "";

  const workbookRows = useMemo(
    () => results.filter((item) => item.source_path === activeWorkbookPath),
    [results, activeWorkbookPath],
  );

  const selectedRows = useMemo(
    () => workbookRows.filter((item) => selectedQueryKeys.includes(buildQueryKey(item))),
    [workbookRows, selectedQueryKeys],
  );

  const activeWbStats = useMemo(() => {
    const selected = selectedRows.length;
    const selectedConverted = selectedRows.filter((row) => row.status === "converted").length;
    const selectedFailed = selectedRows.filter((row) => row.status === "failed").length;
    return { selected, selectedConverted, selectedFailed };
  }, [selectedRows]);

  const activeWorkbook = workbookOptions.find((item) => item.source_path === activeWorkbookPath);

  useEffect(() => {
    if (!activeWorkbookPath && workbookOptions.length > 0) {
      setSelectedWorkbookPath(workbookOptions[0].source_path);
    }
  }, [activeWorkbookPath, workbookOptions]);

  useEffect(() => {
    const validKeys = new Set(workbookRows.map(buildQueryKey));
    setSelectedQueryKeys((current) => current.filter((key) => validKeys.has(key)));
  }, [activeWorkbookPath, workbookRows]);

  useEffect(() => {
    window.sessionStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        sourceType,
        sourcePath,
        autoDownloadEnabled,
        summary,
        results,
        selectedWorkbookPath,
        selectedQueryKeys,
      }),
    );
  }, [sourceType, sourcePath, autoDownloadEnabled, summary, results, selectedWorkbookPath, selectedQueryKeys]);

  function clearState() {
    setSourceType(initialState.sourceType);
    setSourcePath(initialState.sourcePath);
    setAutoDownloadEnabled(initialState.autoDownloadEnabled);
    setSummary(initialSummary);
    setResults([]);
    setError("");
    setInfoMessage("");
    setSelectedWorkbookPath("");
    setSelectedQueryKeys([]);
    setShowPreview(false);
    window.sessionStorage.removeItem(STORAGE_KEY);
  }

  async function handleScanSource(event) {
    event.preventDefault();
    setLoading(true);
    setError("");
    setInfoMessage("");

    try {
      const response = await fetch(`${API_BASE}/scan-source`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ source_type: sourceType, path: sourcePath }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Source scan failed");

      setSummary(data.summary);
      setResults(data.results);

      const firstWorkbookPath = data.results[0]?.source_path || "";
      setSelectedWorkbookPath(firstWorkbookPath);
      setSelectedQueryKeys(
        data.results
          .filter((item) => item.source_path === firstWorkbookPath && item.status === "converted")
          .map(buildQueryKey),
      );

      setInfoMessage(`Scan complete. Found ${data.summary.temp_queries_found} temp-table queries.`);

      const workbookPaths = [...new Set(data.results.map((item) => item.source_path))];
      const autoRows = data.results.filter(
        (item) => item.source_path === firstWorkbookPath && item.status === "converted",
      );

      if (autoDownloadEnabled && workbookPaths.length === 1 && autoRows.length > 0) {
        await executeWriteBack(firstWorkbookPath, autoRows, data.results[0]?.workbook_name);
      }
    } catch (scanError) {
      setError(scanError.message);
    } finally {
      setLoading(false);
    }
  }

  async function executeWriteBack(
    workbookPath = activeWorkbookPath,
    rowsToWrite = selectedRows,
    workbookName = workbookRows[0]?.workbook_name,
  ) {
    setShowPreview(false);
    setDownloadLoading(true);
    setError("");
    setInfoMessage("");

    try {
      const response = await fetch(`${API_BASE}/write-back-from-scan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          workbook_path: workbookPath,
          selected_queries: rowsToWrite,
        }),
      });

      const contentType = response.headers.get("content-type") || "";

      if (!response.ok) {
        const data = contentType.includes("application/json") ? await response.json() : {};
        throw new Error(data.detail || data.message || "Write-back from scan failed");
      }

      if (contentType.includes("application/json")) {
        const data = await response.json();
        setInfoMessage(data.message || "No selected queries were written back.");
        return;
      }

      const blob = await response.blob();
      const downloadUrl = window.URL.createObjectURL(blob);
      const disposition = response.headers.get("content-disposition") || "";
      const match = disposition.match(/filename=\"?([^\"]+)\"?/i);
      const fileName = (match?.[1] || `prepared_${workbookName || "workbook.twbx"}`).replace(/^converted_/i, "prepared_");

      const anchor = document.createElement("a");
      anchor.href = downloadUrl;
      anchor.download = fileName;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.URL.revokeObjectURL(downloadUrl);

      setInfoMessage(`Prepared workbook downloaded: ${fileName}`);
    } catch (writeBackError) {
      setError(writeBackError.message);
    } finally {
      setDownloadLoading(false);
    }
  }

  function handlePreviewAndDownload() {
    if (!activeWorkbookPath) {
      setError("Choose a workbook from the scanned results first.");
      return;
    }
    if (selectedRows.length === 0) {
      setError("Select at least one query row to write back.");
      return;
    }
    setError("");
    setShowPreview(true);
  }

  function toggleQuery(item) {
    const key = buildQueryKey(item);
    setSelectedQueryKeys((current) =>
      current.includes(key) ? current.filter((value) => value !== key) : [...current, key],
    );
  }

  function selectAllForWorkbook() {
    setSelectedQueryKeys(workbookRows.map(buildQueryKey));
  }

  function clearWorkbookSelection() {
    setSelectedQueryKeys([]);
  }

  function selectConvertedOnly() {
    setSelectedQueryKeys(workbookRows.filter((row) => row.status === "converted").map(buildQueryKey));
  }

  function selectFailedOnly() {
    setSelectedQueryKeys(workbookRows.filter((row) => row.status === "failed").map(buildQueryKey));
  }

  return (
    <div className="app-shell">
      {showPreview ? (
        <PreviewModal
          rows={selectedRows}
          workbookName={workbookRows[0]?.workbook_name || "Workbook"}
          onConfirm={() => executeWriteBack()}
          onCancel={() => setShowPreview(false)}
          downloading={downloadLoading}
        />
      ) : null}

      <header className="hero">
        <div>
          <p className="eyebrow">Temp To Permanent Conversion</p>
          <h1>Scan once, select the workbook and query subset, then write back and download.</h1>
          <p className="subtext">
            Source inputs, selected workbook, query selection, and last results stay cached after refresh until you clear them.
          </p>
          <p className="subtext automation-note">
            Automation mode auto-downloads only when the scan finds exactly one workbook with ready query rows.
          </p>
        </div>
        <button type="button" className="secondary-button" onClick={clearState}>
          Clear Inputs And Output
        </button>
      </header>

      <section className="panel">
        <form className="scan-form" onSubmit={handleScanSource}>
          <div className="form-head">
            <div>
              <p className="eyebrow">Source Scan</p>
              <h2>Scan all workbook files from a source</h2>
            </div>
          </div>
          <label>
            Source Type
            <select value={sourceType} onChange={(event) => setSourceType(event.target.value)}>
              <option value="local_folder">Local Folder</option>
              <option value="tableau_server_cloud">Tableau Server / Cloud (next)</option>
            </select>
          </label>
          <label>
            Source Path
            <input
              value={sourcePath}
              onChange={(event) => setSourcePath(event.target.value)}
              placeholder="A:\\workbooks"
            />
          </label>
          <label className="toggle-row">
            <input
              type="checkbox"
              checked={autoDownloadEnabled}
              onChange={(event) => setAutoDownloadEnabled(event.target.checked)}
            />
            <span>Automation mode: auto-download after scan when only one workbook is found</span>
          </label>
          <button type="submit" disabled={loading}>
            {loading ? "Scanning Source..." : "Scan Source"}
          </button>
        </form>
      </section>

      <section className="panel summary-grid">
        <SummaryCard label="Workbooks Scanned" value={summary.workbooks_scanned} />
        <SummaryCard label="Workbook Data Sources" value={summary.datasources} />
        <SummaryCard label="Temp Queries Found" value={summary.temp_queries_found} />
        <SummaryCard label="Temp Queries To Convert" value={summary.converted_queries} accent="green" />
        <SummaryCard label="Failed" value={summary.failed_queries} accent="red" />
      </section>

      {error ? <section className="panel error-box">{error}</section> : null}
      {infoMessage ? <section className="panel info-box">{infoMessage}</section> : null}
      {loading || downloadLoading ? (
        <section className="panel status-box">
          <span className="spinner" />
          {loading ? "Scanning source..." : "Writing back selected queries and preparing download..."}
        </section>
      ) : null}

      <section className="panel">
        <div className="results-header">
          <div>
            <p className="eyebrow">Selection Screen</p>
            <h2>Choose workbook and query rows to apply</h2>
          </div>
          <button
            type="button"
            onClick={handlePreviewAndDownload}
            disabled={downloadLoading || !activeWorkbookPath || selectedRows.length === 0}
          >
            {downloadLoading ? "Preparing Download..." : "Preview & Download"}
          </button>
        </div>

        <div className="selection-toolbar">
          <label className="workbook-picker">
            Workbook
            <select
              value={activeWorkbookPath}
              onChange={(event) => setSelectedWorkbookPath(event.target.value)}
              disabled={workbookOptions.length === 0}
            >
              {workbookOptions.length === 0 ? (
                <option value="">No scanned workbook yet</option>
              ) : (
                workbookOptions.map((workbook) => (
                  <option key={workbook.source_path} value={workbook.source_path}>
                    {workbook.workbook_name}
                  </option>
                ))
              )}
            </select>
          </label>

          <div className="selection-actions">
            <button type="button" className="secondary-button" onClick={selectAllForWorkbook} disabled={workbookRows.length === 0}>
              Select All
            </button>
            <button
              type="button"
              className="secondary-button chip-btn-green"
              onClick={selectConvertedOnly}
              disabled={workbookRows.filter((row) => row.status === "converted").length === 0}
            >
              To Convert Only
            </button>
            <button
              type="button"
              className="secondary-button chip-btn-red"
              onClick={selectFailedOnly}
              disabled={workbookRows.filter((row) => row.status === "failed").length === 0}
            >
              Failed Only
            </button>
            <button type="button" className="secondary-button" onClick={clearWorkbookSelection} disabled={selectedRows.length === 0}>
              Clear
            </button>
          </div>
        </div>

        {activeWorkbook ? (
          <div className="workbook-count-bar">
            <span className="wcb-item">
              <span className="wcb-label">Total</span>
              <strong>{activeWorkbook.total}</strong>
            </span>
            <span className="wcb-divider" />
            <span className="wcb-item">
              <span className="wcb-label">Selected</span>
              <strong>{activeWbStats.selected}</strong>
            </span>
            <span className="wcb-divider" />
            <span className="wcb-item wcb-green">
              <span className="wcb-label">Selected To Convert</span>
              <strong>{activeWbStats.selectedConverted}</strong>
            </span>
            <span className="wcb-divider" />
            <span className="wcb-item wcb-red">
              <span className="wcb-label">Selected Failed</span>
              <strong>{activeWbStats.selectedFailed}</strong>
            </span>
          </div>
        ) : (
          <div className="selection-note">Run a source scan to load workbook choices.</div>
        )}
      </section>

      <section className="panel results-panel">
        <div className="results-header">
          <div>
            <p className="eyebrow">Prepared Query Output</p>
            <h2>Original temp query vs prepared Tableau SQL</h2>
          </div>
          <div className="result-count">{selectedRows.length} selected</div>
        </div>

        {workbookRows.length === 0 ? (
          <div className="empty-state">No temp-table query preparation results yet.</div>
        ) : (
          <table className="results-table">
            <thead>
              <tr>
                <th>Select</th>
                <th>Workbook / Query</th>
                <th>Original Temp Table Query</th>
                <th>Prepared Tableau SQL</th>
              </tr>
            </thead>
            <tbody>
              {workbookRows.map((item, index) => {
                const checked = selectedQueryKeys.includes(buildQueryKey(item));
                return (
                  <tr key={`${item.source_path}-${item.query_name}-${index}`} className={checked ? "row-selected" : ""}>
                    <td className="checkbox-cell">
                      <input type="checkbox" checked={checked} onChange={() => toggleQuery(item)} />
                    </td>
                    <td className="meta-cell">
                      <strong>{item.workbook_name}</strong>
                      <span>{item.query_name}</span>
                      <span>{`Data source: ${item.datasource}`}</span>
                      <span>{item.type}</span>
                    </td>
                    <td>
                      <pre>{item.original_temp_query}</pre>
                    </td>
                    <td>
                      <div className="sql-section-stack">
                        {getConvertedSqlSections(item).map((section) => (
                          <div key={section.label} className="sql-section">
                            <p className="sql-section-label">{section.label}</p>
                            <pre className={item.status === "failed" ? "pre-failed" : ""}>{section.value}</pre>
                          </div>
                        ))}
                        {getConvertedSqlSections(item).length === 0 ? (
                          <pre className={item.status === "failed" ? "pre-failed" : ""}>
                            {getConvertedSqlFallback(item)}
                          </pre>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </section>
    </div>
  );
}

function SummaryCard({ label, value, accent }) {
  return (
    <div className={`summary-card ${accent ? `summary-card-${accent}` : ""}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
