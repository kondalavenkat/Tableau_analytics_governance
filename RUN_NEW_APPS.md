# Run The New Apps

## One-Time Setup (Recommended)
Run from repo root:
`powershell -ExecutionPolicy Bypass -File .\\scripts\\setup_windows.ps1`

## Backend
Start FastAPI backend from repo root:
`.\\.venv\\Scripts\\python.exe -m uvicorn backend.main:app --reload --port 8008`

Backend health check:
`http://127.0.0.1:8008/health`

## Frontend
Open terminal in `A:\\analytics_governance\\frontend-react`

Start dev server:
`npm run dev`

Open:
`http://127.0.0.1:5173`

## Current Scope
- `Scan Source` supports `local_folder`
- `Tableau Server / Cloud` is the next connector, not implemented yet
- UI only shows temp-table queries with:
  - original temp-table query
  - converted permanent-table query
