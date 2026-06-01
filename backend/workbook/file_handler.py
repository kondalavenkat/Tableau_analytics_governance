import os
import zipfile
import shutil
import tempfile
import logging

logger = logging.getLogger(__name__)


def process_file_input(file_path: str) -> dict:
    """
    Analyzes the input file path.
    Returns:
        {
          "twb": absolute path to the main XML file (.twb or .tds),
          "datasources": list of absolute paths to additional .tds / .tdsx files,
          "root_dir": extraction root used for relative XML source tracking,
          "temp_dir": TemporaryDirectory object for cleanup (or None),
        }
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")

    file_ext = os.path.splitext(file_path)[1].lower()

    if file_ext in (".twb", ".tds"):
        logger.info("Processing %s directly: %s", file_ext, file_path)
        return {
            "twb": file_path,
            "datasources": [],
            "root_dir": os.path.dirname(file_path),
            "temp_dir": None,
        }

    if file_ext in (".twbx", ".tdsx"):
        target_xml_ext = ".twb" if file_ext == ".twbx" else ".tds"
        logger.info("Unzipping %s: %s", file_ext, file_path)
        temp_dir = tempfile.TemporaryDirectory()
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                zf.extractall(temp_dir.name)

            twb_file = None
            datasources = []

            for root, _, files in os.walk(temp_dir.name):
                for f in files:
                    full_path = os.path.join(root, f)
                    if f.lower().endswith(target_xml_ext) and twb_file is None:
                        twb_file = full_path
                    elif f.lower().endswith((".tds", ".tdsx")):
                        datasources.append(full_path)

            if not twb_file:
                if datasources:
                    twb_file = datasources.pop(0)
                else:
                    raise ValueError(
                        f"No {target_xml_ext} or .tds file found inside {file_ext} archive."
                    )

            logger.info(
                "Primary XML: %s, %d datasources", twb_file, len(datasources)
            )
            return {
                "twb": twb_file,
                "datasources": datasources,
                "root_dir": temp_dir.name,
                "temp_dir": temp_dir,
            }

        except zipfile.BadZipFile:
            temp_dir.cleanup()
            raise ValueError(f"Invalid {file_ext} file: {file_path}")
        except Exception:
            temp_dir.cleanup()
            raise

    raise ValueError(
        f"Unsupported file format: {file_ext}. Expected .twb, .twbx, .tds, or .tdsx"
    )
