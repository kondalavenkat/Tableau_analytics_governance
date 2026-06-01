from abc import ABC, abstractmethod
from pathlib import Path


class SourceAdapter(ABC):
    source_type = "base"

    @abstractmethod
    def list_workbooks(self, location: str) -> list[str]:
        raise NotImplementedError


class LocalFolderSourceAdapter(SourceAdapter):
    source_type = "local_folder"

    def list_workbooks(self, location: str) -> list[str]:
        root = Path(location)
        if not root.exists():
            raise FileNotFoundError(f"Source path does not exist: {location}")
        if not root.is_dir():
            raise ValueError(f"Source path is not a directory: {location}")
        patterns = ("*.twb", "*.twbx", "*.tds", "*.tdsx")
        files: list[str] = []
        for pattern in patterns:
            files.extend(str(p) for p in root.rglob(pattern))
        return sorted(set(files))


class TableauServerCloudSourceAdapter(SourceAdapter):
    source_type = "tableau_server_cloud"

    def list_workbooks(self, location: str) -> list[str]:
        raise NotImplementedError(
            "Tableau Server/Cloud connector is planned but not yet implemented."
        )


class SourceAdapterRegistry:
    def __init__(self):
        self._adapters = {
            LocalFolderSourceAdapter.source_type: LocalFolderSourceAdapter(),
            TableauServerCloudSourceAdapter.source_type: TableauServerCloudSourceAdapter(),
        }

    def get(self, source_type: str) -> SourceAdapter:
        if source_type not in self._adapters:
            raise ValueError(f"Unsupported source type: {source_type}")
        return self._adapters[source_type]
