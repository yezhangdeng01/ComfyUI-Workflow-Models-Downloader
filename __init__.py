"""
ComfyUI 工作流模型管理
Scans the active workflow for missing models and provides a UI to download them from HuggingFace.

Features:
- Scans active workflow JSON for model references
- Identifies missing models and their target directories
- Shows model information in a modal dialog
- Downloads missing models from HuggingFace with progress tracking
"""

import sys
import importlib.metadata
import os
from pathlib import Path

# Check and install required packages on startup
def check_requirements():
    """Report missing dependencies.

    Dependencies are installed by ComfyUI-Manager (or by the user), never at
    runtime: the Comfy Registry forbids installing packages via subprocess
    during node startup. We only log what is missing so the user knows what
    to install.
    """
    requirements_file = Path(__file__).parent / "requirements.txt"
    if not requirements_file.exists():
        return True

    # Map pip package names to import names
    pip_to_import = {
        "tavily-python": "tavily",
        "huggingface_hub": "huggingface_hub",
        "huggingface-hub": "huggingface_hub",
    }

    missing = []

    with open(requirements_file, 'r') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            pkg_name = line.split('==')[0].split('>=')[0].split('<=')[0].split('<')[0].split('>')[0].strip()
            import_name = pip_to_import.get(pkg_name, pkg_name.replace('-', '_'))

            try:
                importlib.metadata.version(pkg_name)
            except importlib.metadata.PackageNotFoundError:
                # Also try with underscores/hyphens swapped
                alt_name = pkg_name.replace('-', '_') if '-' in pkg_name else pkg_name.replace('_', '-')
                try:
                    importlib.metadata.version(alt_name)
                except importlib.metadata.PackageNotFoundError:
                    missing.append(pkg_name)

    if missing:
        print(
            "[Workflow-Models-Downloader] Missing dependencies: "
            f"{', '.join(missing)}. "
            "Please install them with ComfyUI-Manager, or run: "
            f'"{sys.executable}" -m pip install -r "{requirements_file}"'
        )
        return False

    return True


# Report missing dependencies before any other imports
check_requirements()

# Add the extension directory to path for imports
extension_path = os.path.dirname(__file__)
if extension_path not in sys.path:
    sys.path.insert(0, extension_path)

# Import and register the server routes
from . import server

WEB_DIRECTORY = "js"
NODE_CLASS_MAPPINGS = {}
__all__ = ['NODE_CLASS_MAPPINGS', 'WEB_DIRECTORY']
