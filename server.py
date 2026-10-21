"""
Server-side API endpoints for 工作流模型管理
"""

import os
import re
import sys
import glob
import struct
import json
import logging
import asyncio
import datetime
import requests
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from aiohttp import web
from logging.handlers import RotatingFileHandler

import folder_paths
from server import PromptServer

# Get routes from ComfyUI server
routes = PromptServer.instance.routes

# Extension path
EXTENSION_PATH = os.path.dirname(__file__)

# Setup file logging
LOG_FILE = os.path.join(EXTENSION_PATH, 'wmd.log')
_file_handler = None

def setup_file_logging():
    """Setup file logging for the extension"""
    global _file_handler
    try:
        # Create a rotating file handler (max 5MB, keep 3 backups)
        _file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=5*1024*1024,
            backupCount=3,
            encoding='utf-8'
        )
        _file_handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        _file_handler.setFormatter(formatter)

        # Add handler to the root logger
        logging.getLogger().addHandler(_file_handler)
        logging.info("[WMD] File logging initialized: " + LOG_FILE)
    except Exception as e:
        logging.error(f"[WMD] Failed to setup file logging: {e}")

# Initialize file logging
setup_file_logging()

# Settings file path
SETTINGS_FILE = os.path.join(EXTENSION_PATH, 'settings.json')

# Version info
PYPROJECT_FILE = os.path.join(EXTENSION_PATH, 'pyproject.toml')
GITHUB_REPO = "yezhangdeng01/ComfyUI-Workflow-Models-Downloader"
REGISTRY_URL = "https://registry.comfy.org/nodes/comfyui-workflow-model-manager"

def get_installed_version():
    """Get installed version from pyproject.toml"""
    try:
        logging.debug(f"[WMD] Looking for pyproject.toml at: {PYPROJECT_FILE}")
        if not os.path.exists(PYPROJECT_FILE):
            logging.warning(f"[WMD] pyproject.toml not found at: {PYPROJECT_FILE}")
            return "1.8.1"  # Fallback to current version

        with open(PYPROJECT_FILE, 'r', encoding='utf-8') as f:
            content = f.read()
            # Simple regex to extract version
            match = re.search(r'version\s*=\s*"([^"]+)"', content)
            if match:
                version = match.group(1)
                logging.debug(f"[WMD] Found version: {version}")
                return version
            else:
                logging.warning(f"[WMD] Could not find version in pyproject.toml")
    except Exception as e:
        logging.error(f"[WMD] Could not read version from pyproject.toml: {e}")
    return "1.8.1"  # Fallback to current version

def get_latest_version():
    """Get latest version from GitHub releases API"""
    try:
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        response = requests.get(url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            tag = data.get('tag_name', '')
            # Remove 'v' prefix if present
            return tag.lstrip('v')
    except Exception as e:
        logging.debug(f"[WMD] Could not fetch latest version from GitHub: {e}")
    return None

def compare_versions(installed, latest):
    """Compare version strings. Returns True if update is available."""
    if not latest or installed == "unknown":
        return False
    try:
        installed_parts = [int(x) for x in installed.split('.')]
        latest_parts = [int(x) for x in latest.split('.')]
        # Pad with zeros for comparison
        while len(installed_parts) < len(latest_parts):
            installed_parts.append(0)
        while len(latest_parts) < len(installed_parts):
            latest_parts.append(0)
        return latest_parts > installed_parts
    except Exception:
        return False

# Search metadata cache file
# DEPRECATED: search_cache.json - now using model_metadata.json as single source of truth
# SEARCH_CACHE_FILE = os.path.join(EXTENSION_PATH, 'search_cache.json')

# Download history file for persistent tracking
DOWNLOAD_HISTORY_FILE = os.path.join(EXTENSION_PATH, 'download_history.json')

# Tavily search cache file for persistent caching of advanced search results
TAVILY_CACHE_FILE = os.path.join(EXTENSION_PATH, 'tavily_cache.json')

# Download progress tracking
download_progress = {}
download_lock = threading.Lock()
cancelled_downloads = set()  # Track cancelled download IDs

# Download history (persistent)
download_history = []

# Download queue system
download_queue = []  # Queued downloads waiting to start
download_queue_lock = threading.Lock()
max_parallel_downloads = 3  # Default, configurable via settings
active_download_count = 0

# Model aliases file
MODEL_ALIASES_FILE = os.path.join(EXTENSION_PATH, 'metadata', 'model-aliases.json')

# Settings cache
_settings_cache = None

# Fuzzy matching imports
from difflib import SequenceMatcher
import subprocess
import shutil


def load_settings():
    """Load settings from settings.json or ComfyUI's native settings"""
    global _settings_cache
    if _settings_cache is not None:
        return _settings_cache

    default_settings = {
        'huggingface_token': '',
        'civitai_api_key': '',
        'tavily_api_key': '',
        'enable_advanced_search': False,
        'max_parallel_downloads': 3,
        'use_hf_mirror': False,
        'hf_mirror_domain': 'hf-mirror.com'
    }

    # First try extension's own settings file
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r', encoding='utf-8') as f:
                saved = json.load(f)
                # Merge with defaults
                _settings_cache = {**default_settings, **saved}
                return _settings_cache
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Error loading settings: {e}")

    # Fall back to ComfyUI's native settings
    try:
        comfy_settings_path = os.path.join(folder_paths.base_path, 'user', 'default', 'comfy.settings.json')
        if os.path.exists(comfy_settings_path):
            with open(comfy_settings_path, 'r', encoding='utf-8') as f:
                comfy_settings = json.load(f)
                # Map ComfyUI setting keys to our internal keys
                _settings_cache = {
                    'huggingface_token': comfy_settings.get('WorkflowModelsDownloader.HuggingFaceToken', ''),
                    'civitai_api_key': comfy_settings.get('WorkflowModelsDownloader.CivitAIApiKey', ''),
                    'tavily_api_key': comfy_settings.get('WorkflowModelsDownloader.TavilyApiKey', ''),
                    'enable_advanced_search': comfy_settings.get('WorkflowModelsDownloader.EnableAdvancedSearch', False),
                    'max_parallel_downloads': comfy_settings.get('WorkflowModelsDownloader.MaxParallelDownloads', 3)
                }
                logging.info(f"[WMD] Loaded settings from ComfyUI native settings")
                return _settings_cache
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Error loading ComfyUI settings: {e}")

    _settings_cache = default_settings
    return _settings_cache


def save_settings(settings):
    """Save settings to settings.json"""
    global _settings_cache
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump(settings, f, indent=2)
        _settings_cache = settings
        logging.info("[Workflow-Models-Downloader] Settings saved")
        return True
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Error saving settings: {e}")
        return False


# ============================================================================
# HuggingFace Mirror Rewriting
# ============================================================================

def _normalize_endpoint(value):
    """Normalize a mirror value to 'https://domain' with no trailing slash.
    Accepts a bare domain, an https:// endpoint, or an http:// one."""
    if not value:
        return ''
    v = str(value).strip()
    if not v:
        return ''
    v = v.rstrip('/').strip()
    if v.startswith(('http://', 'https://')):
        return v
    return 'https://' + v


def _hf_mirror_endpoint_from_env():
    """Read the HF mirror endpoint from the environment.

    Supported launchers (秋叶绘世 / aki ComfyUI launcher, etc.) start ComfyUI
    with HF_ENDPOINT injected when the user enables 'mirror_huggingface'.
    ComfyUI-Manager reads this; this plugin did not, so model downloads still
    hit huggingface.co directly no matter what the launcher said."""
    for var in ('HF_ENDPOINT', 'HF_MIRROR', 'HF_HUB_ENDPOINT'):
        val = os.environ.get(var, '')
        if val and val.strip():
            return _normalize_endpoint(val)
    return ''


def apply_hf_mirror(url):
    """Rewrite huggingface.co download/API URLs to the configured mirror.

    Priority:
      1. Plugin setting use_hf_mirror=true (with hf_mirror_domain)
      2. HF_ENDPOINT environment variable (set by 秋叶绘世 / aki launcher)
      3. otherwise the URL is returned untouched.
    Returns the original URL when no mirror is configured."""
    try:
        if url is None:
            return url
        settings = load_settings()
        if settings.get('use_hf_mirror', False):
            mirror = _normalize_endpoint(settings.get('hf_mirror_domain') or 'hf-mirror.com')
        else:
            mirror = _hf_mirror_endpoint_from_env()
        if not mirror:
            return url
        if 'https://huggingface.co/' in url:
            return url.replace('https://huggingface.co/', mirror + '/', 1)
        if 'https://hf.co/' in url:
            return url.replace('https://hf.co/', mirror + '/', 1)
        return url
    except Exception:
        return url


def load_download_history():
    """Load download history from file"""
    global download_history
    try:
        if os.path.exists(DOWNLOAD_HISTORY_FILE):
            with open(DOWNLOAD_HISTORY_FILE, 'r', encoding='utf-8') as f:
                download_history = json.load(f)
                logging.info(f"[WMD] Loaded {len(download_history)} download history entries")
                return download_history
    except Exception as e:
        logging.error(f"[WMD] Error loading download history: {e}")
    download_history = []
    return download_history


def save_download_history():
    """Save download history to file"""
    global download_history
    try:
        with open(DOWNLOAD_HISTORY_FILE, 'w', encoding='utf-8') as f:
            json.dump(download_history, f, indent=2)
        return True
    except Exception as e:
        logging.error(f"[WMD] Error saving download history: {e}")
        return False


# Tavily search cache
_tavily_cache = {}

def load_tavily_cache():
    """Load Tavily search cache from file"""
    global _tavily_cache
    try:
        if os.path.exists(TAVILY_CACHE_FILE):
            with open(TAVILY_CACHE_FILE, 'r', encoding='utf-8') as f:
                _tavily_cache = json.load(f)
                logging.info(f"[WMD] Loaded Tavily cache with {len(_tavily_cache)} entries")
                return _tavily_cache
    except Exception as e:
        logging.error(f"[WMD] Error loading Tavily cache: {e}")
    _tavily_cache = {}
    return _tavily_cache


def save_tavily_cache():
    """Save Tavily search cache to file"""
    global _tavily_cache
    try:
        with open(TAVILY_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(_tavily_cache, f, indent=2)
        return True
    except Exception as e:
        logging.error(f"[WMD] Error saving Tavily cache: {e}")
        return False


def get_tavily_cached_result(filename):
    """Get cached Tavily search result for a filename"""
    global _tavily_cache
    return _tavily_cache.get(filename)


def set_tavily_cached_result(filename, data):
    """Cache Tavily search result for a filename"""
    global _tavily_cache
    data['cached_at'] = datetime.datetime.now().isoformat()
    _tavily_cache[filename] = data
    save_tavily_cache()


def add_to_download_history(download_info):
    """Add a download entry to history"""
    global download_history

    # Create history entry
    entry = {
        'id': download_info.get('id', ''),
        'filename': download_info.get('filename', ''),
        'status': download_info.get('status', ''),
        'error': download_info.get('error', ''),
        'total_size': download_info.get('total_size', 0),
        'timestamp': datetime.datetime.now().isoformat(),
        'directory': download_info.get('directory', '')
    }

    # If download completed successfully, invalidate folder cache so the file is discoverable.
    # The recorded directory is a best guess and can disagree with where the file
    # actually landed (curated table override, subfolder in the filename), so
    # clear every candidate folder type — this is a dict delete, it is cheap.
    if entry['status'] == 'completed':
        to_clear = set()
        if entry['directory']:
            to_clear.add(os.path.basename(entry['directory'].rstrip('/\\')))
        hit = _find_file_by_basename(entry.get('filename') or '') if entry.get('filename') else None
        if hit:
            try:
                for name, e in folder_paths.folder_names_and_paths.items():
                    for d in e[0]:
                        if os.path.dirname(hit).lower().startswith(os.path.abspath(d).lower()):
                            to_clear.add(name)
            except Exception:
                pass
        for folder_type in to_clear:
            if not folder_type:
                continue
            invalidate_folder_cache(folder_type)
        if to_clear:
            logging.info(f"[WMD] Download complete, cache invalidated for: {sorted(x for x in to_clear if x)}")

    # Remove any existing entry with same filename to avoid duplicates
    download_history = [h for h in download_history if h.get('filename') != entry['filename']]

    # Add new entry at the beginning
    download_history.insert(0, entry)

    # Keep only last 100 entries
    download_history = download_history[:100]

    save_download_history()


def clear_download_history():
    """Clear all download history"""
    global download_history
    download_history = []
    save_download_history()


def get_huggingface_token():
    """Get HuggingFace token from settings"""
    settings = load_settings()
    return settings.get('huggingface_token', '')


def get_civitai_api_key():
    """Get CivitAI API key from settings (force reload to get latest)"""
    global _settings_cache
    _settings_cache = None  # Force reload to get latest settings
    settings = load_settings()
    key = settings.get('civitai_api_key', '')
    if key:
        logging.debug(f"[WMD] CivitAI API key found (length: {len(key)})")
    else:
        logging.warning("[WMD] CivitAI API key not configured")
    return key


def parse_civitai_urn(urn_string):
    """
    Parse CivitAI URN format: urn:air:other:unknown:civitai:MODEL_ID@VERSION_ID
    Returns (model_id, version_id) tuple or (None, None) if not a valid URN
    """
    if not urn_string or not urn_string.startswith('urn:'):
        return None, None

    # Pattern: urn:air:other:unknown:civitai:MODEL_ID@VERSION_ID
    # Also support: urn:air:MODEL_TYPE:BASE_MODEL:civitai:MODEL_ID@VERSION_ID
    urn_pattern = r'^urn:air:[^:]+:[^:]+:civitai:(\d+)@(\d+)$'
    match = re.match(urn_pattern, urn_string)
    if match:
        return match.group(1), match.group(2)

    return None, None


def civitai_urn_to_download_url(urn_string):
    """
    Convert CivitAI URN to download URL
    Returns download URL or None if not a valid URN
    """
    model_id, version_id = parse_civitai_urn(urn_string)
    if version_id:
        return f"https://civitai.com/api/download/models/{version_id}"
    return None


def is_civitai_urn(value):
    """Check if a value is a CivitAI URN"""
    if not value or not isinstance(value, str):
        return False
    model_id, version_id = parse_civitai_urn(value)
    return model_id is not None and version_id is not None


def get_tavily_api_key():
    """Get Tavily API key from settings"""
    global _settings_cache
    # Force reload settings to get latest key
    _settings_cache = None
    settings = load_settings()
    key = settings.get('tavily_api_key', '')
    logging.info(f"[WMD] Tavily key loaded: {'*' * (len(key) - 4) + key[-4:] if key else 'NOT SET'}")
    return key


def is_advanced_search_enabled():
    """Check if advanced search is enabled"""
    settings = load_settings()
    return settings.get('enable_advanced_search', False) and bool(settings.get('tavily_api_key', ''))


# ============================================================================
# Metadata Cache Functions (redirect to model_metadata.json)
# ============================================================================
# These functions now use model_metadata.json as the single source of truth


def get_cached_metadata(filename):
    """Get cached metadata for a filename from model_metadata.json.

    Curated KNOWN_GOOD_SOURCES overrides any cached (possibly poisoned) URL,
    and the fix is persisted back to model_metadata.json.
    """
    # Import here to avoid circular dependency (load_model_metadata defined later)
    metadata = _get_model_metadata_safe()
    basename = os.path.basename(filename)
    entry = metadata.get(filename) or metadata.get(basename)

    kg = lookup_known_good_source(filename)
    if kg and (not entry or entry.get('url') != kg['url']):
        entry = dict(entry or {})
        entry['filename'] = basename
        entry['url'] = kg['url']
        entry['source'] = 'known_good'
        entry['hf_repo'] = kg.get('hf_repo', '')
        entry['hf_path'] = kg.get('hf_path', '')
        all_metadata = _get_model_metadata_safe()
        all_metadata[basename] = entry
        _save_model_metadata_safe(all_metadata)

    return entry


def save_search_metadata(filename, metadata):
    """Save search metadata for a filename to model_metadata.json"""
    basename = os.path.basename(filename)
    metadata['cached_at'] = datetime.datetime.now().isoformat()

    # Update model_metadata.json
    all_metadata = _get_model_metadata_safe()
    existing = all_metadata.get(basename, {})

    # Merge new metadata (don't overwrite user_url)
    for key, value in metadata.items():
        if key == 'user_url' and existing.get('user_url'):
            continue  # Don't overwrite user-provided URL
        if value is not None and value != '':
            existing[key] = value

    existing['filename'] = basename
    all_metadata[basename] = existing
    _save_model_metadata_safe(all_metadata)


def _get_model_metadata_safe():
    """Safe wrapper to get model metadata (handles import order)"""
    global _model_metadata_cache
    if _model_metadata_cache is not None:
        return _model_metadata_cache
    try:
        model_metadata_file = os.path.join(os.path.dirname(__file__), "model_metadata.json")
        if os.path.exists(model_metadata_file):
            with open(model_metadata_file, 'r', encoding='utf-8') as f:
                _model_metadata_cache = json.load(f)
                return _model_metadata_cache
    except Exception as e:
        logging.error(f"[WMD] Error loading model metadata: {e}")
    _model_metadata_cache = {}
    return _model_metadata_cache


def _save_model_metadata_safe(metadata):
    """Safe wrapper to save model metadata"""
    global _model_metadata_cache
    try:
        model_metadata_file = os.path.join(os.path.dirname(__file__), "model_metadata.json")
        with open(model_metadata_file, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2)
        _model_metadata_cache = metadata
        return True
    except Exception as e:
        logging.error(f"[WMD] Error saving model metadata: {e}")
        return False


# Global cache for model metadata (shared with functions defined later)
_model_metadata_cache = None


def _cache_download_url(filename, url, source, hf_repo=None, hf_path=None, model_name=None, civitai_url=None):
    """Cache URL after successful download for future use"""
    try:
        metadata = {
            'url': url,
            'source': f'download_{source}',
            'hf_repo': hf_repo or '',
            'hf_path': hf_path or '',
            'model_name': model_name or '',
            'civitai_url': civitai_url or '',
            'cached_at': datetime.datetime.now().isoformat(),
            'from_download': True
        }
        save_search_metadata(filename, metadata)
        logging.info(f"[Workflow-Models-Downloader] Cached URL for: {filename}")
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Failed to cache URL: {e}")


logging.info("[Workflow-Models-Downloader] Loading extension...")


# =============================================================================
# METADATA LOADING
# =============================================================================

# Cache for metadata
_model_list_cache = None
_extension_node_map_cache = None


def get_metadata_path():
    """Get path to metadata directory - check multiple locations"""
    # First check in our extension's metadata folder
    local_metadata = os.path.join(EXTENSION_PATH, 'metadata')
    if os.path.exists(local_metadata):
        return local_metadata

    # Check in comfyui_workflow_models_identifier
    identifier_path = os.path.join(os.path.dirname(folder_paths.base_path), 'comfyui_workflow_models_identifier', 'metadata')
    if os.path.exists(identifier_path):
        return identifier_path

    # Check ComfyUI Manager
    manager_path = os.path.join(folder_paths.base_path, 'custom_nodes', 'ComfyUI-Manager')
    if os.path.exists(manager_path):
        return manager_path

    return None


def load_model_list():
    """Load model-list.json from metadata"""
    global _model_list_cache
    if _model_list_cache is not None:
        return _model_list_cache

    metadata_path = get_metadata_path()
    if not metadata_path:
        logging.warning("[Workflow-Models-Downloader] Metadata path not found")
        _model_list_cache = []
        return _model_list_cache

    try:
        model_list_path = os.path.join(metadata_path, 'model-list.json')
        if os.path.exists(model_list_path):
            with open(model_list_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                _model_list_cache = data.get('models', [])
                logging.info(f"[Workflow-Models-Downloader] Loaded {len(_model_list_cache)} models from model-list.json")
                return _model_list_cache
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Error loading model-list.json: {e}")

    _model_list_cache = []
    return _model_list_cache


def load_extension_node_map():
    """Load extension-node-map.json from metadata"""
    global _extension_node_map_cache
    if _extension_node_map_cache is not None:
        return _extension_node_map_cache

    metadata_path = get_metadata_path()
    if not metadata_path:
        _extension_node_map_cache = {}
        return _extension_node_map_cache

    try:
        map_path = os.path.join(metadata_path, 'extension-node-map.json')
        if os.path.exists(map_path):
            with open(map_path, 'r', encoding='utf-8') as f:
                _extension_node_map_cache = json.load(f)
                logging.info(f"[Workflow-Models-Downloader] Loaded {len(_extension_node_map_cache)} extensions from extension-node-map.json")
                return _extension_node_map_cache
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Error loading extension-node-map.json: {e}")

    _extension_node_map_cache = {}
    return _extension_node_map_cache


def lookup_model_in_model_list(filename):
    """Look up model info from model-list.json by filename"""
    models = load_model_list()
    filename_lower = filename.lower()

    for model in models:
        if model.get('filename', '').lower() == filename_lower:
            model_type = model.get('type', '')
            save_path = model.get('save_path', '')

            # Handle 'default' save_path - map to appropriate directory
            if save_path == 'default':
                type_to_dir = {
                    'upscale': 'upscale_models',
                    'TAESD': 'vae_approx',
                    'controlnet': 'controlnet',
                    'checkpoint': 'checkpoints',
                    'lora': 'loras',
                    'vae': 'vae',
                }
                save_path = type_to_dir.get(model_type, 'models')

            return model_type, save_path, model.get('url', ''), model.get('size', '')

    return None, None, None, None


def lookup_node_github_url(node_type):
    """Look up GitHub URL for a node type from extension-node-map.json"""
    node_map = load_extension_node_map()

    for github_url, node_data in node_map.items():
        if isinstance(node_data, list) and len(node_data) > 0:
            node_list = node_data[0] if isinstance(node_data[0], list) else []
            if node_type in node_list:
                return github_url

    return None


# Initialize metadata on load
logging.info("[Workflow-Models-Downloader] Initializing metadata...")
_metadata_path = get_metadata_path()
if _metadata_path:
    logging.info(f"[Workflow-Models-Downloader] Using metadata from: {_metadata_path}")
else:
    logging.warning("[Workflow-Models-Downloader] No metadata found - model detection may be limited")


# =============================================================================
# URL DETECTION - Multi-source lookup
# =============================================================================

# Cache for popular models registry
_popular_models_cache = None

# Cache for API search results
_url_search_cache = {}


def load_popular_models():
    """Load the curated popular-models.json registry"""
    global _popular_models_cache
    if _popular_models_cache is not None:
        return _popular_models_cache

    try:
        popular_path = os.path.join(EXTENSION_PATH, 'metadata', 'popular-models.json')
        if os.path.exists(popular_path):
            with open(popular_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                _popular_models_cache = data.get('models', {})
                logging.info(f"[Workflow-Models-Downloader] Loaded {len(_popular_models_cache)} popular models")
                return _popular_models_cache
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Error loading popular-models.json: {e}")

    _popular_models_cache = {}
    return _popular_models_cache


def lookup_url_in_popular_models(filename):
    """Look up URL from curated popular models registry.

    Matching is done on the BASENAME: workflow references may carry a
    subfolder prefix ("moss\\moss_image_sft_bf16.safetensors") while every
    registry is keyed by the plain file name. Without this a prefixed
    reference silently missed the registry and the row showed "无链接".
    """
    models = load_popular_models()
    base = os.path.basename(filename or '')
    filename_lower = base.lower()

    # Exact match
    if base in models:
        return models[base].get('url', '')

    # Case-insensitive match
    for name, info in models.items():
        if name.lower() == filename_lower:
            return info.get('url', '')

    return None


def lookup_url_in_model_list(filename):
    """Look up URL from model-list.json (STRICT matching only).

    v1.8.1-fix: the old substring fuzzy match ('ae' in 'taeh3' etc.) resolved
    wrong gated repos (e.g. taeh3 -> FLUX.1-schnell/ae.safetensors -> 401)
    and the bad URL got cached forever. Exact match only now.
    """
    models = load_model_list()
    filename_lower = os.path.basename(filename or '').lower()

    # Exact match only
    for model in models:
        model_filename = model.get('filename', '')
        if os.path.basename(model_filename).lower() == filename_lower:
            return model.get('url', '')

    return None


# ============================================================================
# Curated known-good source overrides (fixes bad cached/mismatched URLs).
# filename (lowercase) -> {url, hf_repo, hf_path}
# Checked BEFORE any cache / registry lookup. Extend as needed.
# ============================================================================

KNOWN_GOOD_SOURCES = {
    # taeh3 was wrongly resolved to gated FLUX.1-schnell/ae.safetensors (401)
    'taeh3.safetensors': {
        'url': 'https://huggingface.co/OzzyGT/taeh3/resolve/main/taeh3.safetensors',
        'hf_repo': 'OzzyGT/taeh3',
        'hf_path': 'taeh3.safetensors',
        # KJ ModelPreviewOverrideKJ tiny_vae reads models/vae_approx —
        # without this the scan guessed checkpoints/ and re-downloads
        # duplicated the file there.
        'directory': 'vae_approx',
    },
    # int8 convrot video vae was poisoned by the same FLUX cache entry
    'minimax_h3_video_vae_int8_convrot.safetensors': {
        'url': 'https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae/minimax_h3_video_vae_int8_convrot.safetensors',
        'hf_repo': 'Comfy-Org/MiniMax-H3',
        'hf_path': 'vae/minimax_h3_video_vae_int8_convrot.safetensors',
        'directory': 'vae',
    },
    # real filename matches the workflow (ref2va, not the OCR-mangled 'rel2va').
    # 19.5GB diffusion model: force diffusion_models/ (plugin would guess checkpoints/)
    'minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors': {
        'url': 'https://huggingface.co/smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models/resolve/main/minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors',
        'hf_repo': 'smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models',
        'hf_path': 'minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors',
        'directory': 'diffusion_models',
        'size': 20970379632,
    },
    # alias kept for safety (case variants)
    'minimax_h3_hybrid_fl2va_rel2va_b25-49-int8.safetensors': {
        'url': 'https://huggingface.co/smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models/resolve/main/minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors',
        'hf_repo': 'smhfacct/Minimax-H3-fl2va-ref2va-hybrid-models',
        'hf_path': 'minimax_h3_hybrid_fl2va_ref2va_b25-49-int8.safetensors',
        'directory': 'diffusion_models',
        'size': 20970379632,
    },
    # Civitai 2837571 (tsolful Minimax H3 Turbo LoRAs), version v4 (README-verified name)
    'minimax_h3_dmad_full_dareties_v4_step600_comfy_fro.safetensors': {
        'url': 'https://civitai.com/api/download/models/3391964?fileId=3281483',
        'directory': 'loras',
    },
    # --- H3 companion LoRAs (all verified via Range GET, 2026-10-18) ---
    'h3_combat_v2.safetensors': {
        'url': 'https://huggingface.co/JOKER141/MiniMax-H3-Combat-Base-V2/resolve/main/H3_Combat_V2.safetensors',
        'hf_repo': 'JOKER141/MiniMax-H3-Combat-Base-V2',
        'hf_path': 'H3_Combat_V2.safetensors',
        'directory': 'loras',
    },
    'h3_fight_flow_v1.safetensors': {
        'url': 'https://huggingface.co/JOKER141/H3-Fight-Flow-Fight-Interaction-LoRA/resolve/main/H3_Fight_Flow_V1.safetensors',
        'hf_repo': 'JOKER141/H3-Fight-Flow-Fight-Interaction-LoRA',
        'hf_path': 'H3_Fight_Flow_V1.safetensors',
        'directory': 'loras',
    },
    'h3_speed_slider_1.1.safetensors': {
        'url': 'https://huggingface.co/RunningHubAI/rh-h3-speed-slider-1.1-lora/resolve/main/H3_speed_slider_1.1.safetensors',
        'hf_repo': 'RunningHubAI/rh-h3-speed-slider-1.1-lora',
        'hf_path': 'H3_speed_slider_1.1.safetensors',
        'directory': 'loras',
    },
    'motion_repair_v2.safetensors': {
        'url': 'https://huggingface.co/JOKER141/MiniMax-H3-General-Motion-Continuity-Repair/resolve/main/Motion_Repair_V2.safetensors',
        'hf_repo': 'JOKER141/MiniMax-H3-General-Motion-Continuity-Repair',
        'hf_path': 'Motion_Repair_V2.safetensors',
        'directory': 'loras',
    },
    'mysticxxx_mmh3-v1.safetensors': {
        'url': 'https://civitai.com/api/download/models/3226233?fileId=3108441',
        'directory': 'loras',
    },
    'minimax_h3_latent_upscaler_3d_fp16_v2.safetensors': {
        'url': 'https://huggingface.co/KENIC-1/minimax_h3_latent_upscaler_3d_fp16_V2/resolve/main/minimax_h3_latent_upscaler_3d_fp16_V2.safetensors',
        'hf_repo': 'KENIC-1/minimax_h3_latent_upscaler_3d_fp16_V2',
        'hf_path': 'minimax_h3_latent_upscaler_3d_fp16_V2.safetensors',
        'directory': 'upscale_models',
    },
}


def lookup_known_good_source(filename):
    """Curated known-good source table, keyed by lowercase basename."""
    base = os.path.basename(filename or '').lower()
    if base in KNOWN_GOOD_SOURCES:
        return KNOWN_GOOD_SOURCES[base]
    # also try base without path separators variants
    return KNOWN_GOOD_SOURCES.get((filename or '').lower())


def resolve_model_directory(filename, model_dir=''):
    """Single source of truth for which ComfyUI directory a model belongs in.

    Both the workflow scan (check_model_exists) and the download threads used
    to compute this independently, and the download side could override it via
    KNOWN_GOOD_SOURCES['directory'] while the scan side kept its filename-based
    guess. The result: a file landed in diffusion_models/ but the panel kept
    asking for checkpoints/ and reported it as still missing forever.

    Order: explicit caller directory -> curated table -> filename patterns.
    """
    base = os.path.basename(filename or '')
    if model_dir:
        return model_dir
    kg = lookup_known_good_source(base)
    if kg and kg.get('directory'):
        return kg['directory']
    _type, dir_from_name = identify_model_type_from_filename(base)
    return dir_from_name or 'checkpoints'


# ============================================================================
# ModelScope (魔搭) support — domestic mirror, very fast on CN networks.
# Used as a long-lived generic source, not tied to any single workflow.
# ============================================================================

MODELSCOPE_FILE_API = "https://modelscope.cn/api/v1/models/{repo}/repo/files"
MODELSCOPE_RESOLVE = "https://modelscope.cn/models/{repo}/resolve/master/{path}"

# Known-good namespaces that mirror ComfyUI's official model repos.
# Keys are lowered repo-id fragments, values are the ModelScope repo to try first.
MODELSCOPE_REPO_HINTS = {
    'comfy-org': 'Comfy-Org',
    'minimax': 'Comfy-Org/MiniMax-H3',
    'hunyuan': 'Comfy-Org/HunyuanVideo',
    'wan': 'Wan-AI',
    'qwen': 'qwen',
    'flux': 'Comfy-Org/flux',
    'sd3': 'Comfy-Org/stable-diffusion-3',
    'sdxl': 'Comfy-Org/stable-diffusion-xl',
}


def _modelscope_headers():
    return {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
        'Accept': 'application/json',
        'Referer': 'https://modelscope.cn/',
    }


MODELSCOPE_MAX_ROOTS = 12          # recursion depth cap for directory walk
_MODELSCOPE_LIST_CACHE = {}        # repo -> {path, size} list (in-memory, per process)


def _modelscope_list_dir(repo, root='', timeout=20):
    """List one directory level of a ModelScope repo.

    Returns (files, subdirs). Entries with Size==0 and no extension are treated
    as directories (ModelScope reports dirs as Size 0).
    """
    try:
        url = MODELSCOPE_FILE_API.format(repo=repo)
        params = {'Revision': 'master'}
        if root:
            params['Root'] = root
        r = requests.get(url, params=params, headers=_modelscope_headers(), timeout=timeout)
        if r.status_code != 200:
            return [], []
        data = r.json()
        entries = (data.get('Data') or {}).get('Files') or []
    except Exception as e:
        logging.debug(f"[WMD] ModelScope list failed for {repo}[/{root}]: {e}")
        return [], []

    files, subdirs = [], []
    for f in entries:
        if not isinstance(f, dict) or not f.get('Name'):
            continue
        name = f['Name']
        path = (root + '/' + name) if root else name
        size = f.get('Size') or 0
        # Size 0 + no dot => directory
        if size == 0 and '.' not in name:
            subdirs.append(path)
        else:
            files.append({'path': path, 'size': size})
    return files, subdirs


def modelscope_list_files(repo, timeout=20, max_roots=MODELSCOPE_MAX_ROOTS):
    """Recursively list every file of a ModelScope repo. [{path, size}]"""
    if repo in _MODELSCOPE_LIST_CACHE:
        return _MODELSCOPE_LIST_CACHE[repo]

    all_files, subdirs = _modelscope_list_dir(repo, '', timeout)
    # BFS over subdirectories (dir names come back as plain 'diffusion_models')
    queue = list(subdirs)
    visited = set(queue)
    while queue and len(visited) <= max_roots:
        cur = queue.pop(0)
        files, subs = _modelscope_list_dir(repo, cur, timeout)
        all_files.extend(files)
        for s in subs:
            if s not in visited:
                visited.add(s)
                queue.append(s)

    _MODELSCOPE_LIST_CACHE[repo] = all_files
    logging.debug(f"[WMD] ModelScope {repo}: listed {len(all_files)} files")
    return all_files


def _modelscope_file_to_dir(path):
    """Map a ModelScope repo-relative path to a ComfyUI model directory."""
    p = path.lower()
    table = [
        ('diffusion_models', 'diffusion_models'), ('text_encoders', 'text_encoders'),
        ('vae/', 'vae'), ('vae_approx', 'vae_approx'), ('loras/', 'loras'),
        ('lora/', 'loras'), ('controlnet', 'controlnet'), ('clip_vision', 'clip_vision'),
        ('embeddings', 'embeddings'), ('upscale', 'upscale_models'),
        ('animatediff', 'animatediff_models'), ('controlnet/', 'controlnet'),
        ('ipadapter', 'ipadapter'), ('instantid', 'instantid'), ('pulid', 'pulid'),
        ('sams/', 'sams'), ('insightface', 'insightface/models'),
        ('facerestore', 'facerestore_models'), ('unet/', 'unet'), ('checkpoints', 'checkpoints'),
        ('style_models', 'style_models'), ('model_patches', 'model_patches'),
    ]
    for needle, d in table:
        if needle in p:
            return d
    return None


def search_modelscope(filename, model_dir=None):
    """Search ModelScope for an exact-filename match.

    Returns a list of candidate dicts:
      {url, source:'modelscope', repo, path, directory, size, priority}
    Only exact basename matches are returned — the plugin's contract is to match
    the workflow's model name, not to guess a similar-sounding file.
    """
    base = os.path.basename(filename or '')
    if not base:
        return []

    repos = []
    # 1. explicit KNOWN_GOOD_SOURCES hint
    kg = lookup_known_good_source(filename)
    if kg and kg.get('modelscope_repo'):
        repos.append(kg['modelscope_repo'])
    # 2. dir-aware hints (only namespaces that actually host ComfyUI mirrors)
    name_lower = (base + ' ' + (model_dir or '')).lower()
    for needle, repo in MODELSCOPE_REPO_HINTS.items():
        if needle in name_lower:
            repos.append(repo)
    # 3. broad fallback: Comfy-Org namespace hosts most mirrored repos
    repos.append('Comfy-Org')

    seen, out = set(), []
    for repo in dict.fromkeys(repos):
        for f in modelscope_list_files(repo):
            if os.path.basename(f['path']).lower() == base.lower():
                d = _modelscope_file_to_dir(f['path'])
                if d is None:
                    continue
                out.append({
                    'url': MODELSCOPE_RESOLVE.format(repo=repo, path=f['path']),
                    'source': 'modelscope',
                    'repo': repo,
                    'path': f['path'],
                    'directory': d,
                    'size': f['size'],
                })
                seen.add(repo)
                break
        if repo in seen and len(out) >= 1:
            break
    return out


def probe_url_speed(url, bytes_to_fetch=256 * 1024, timeout=6):
    """Measure download speed of a URL by fetching a small byte range.

    Returns (speed_bytes_per_sec, total_bytes_or_None). Never raises.
    """
    try:
        t0 = time.time()
        with requests.get(url, headers={'Range': f'bytes=0-{bytes_to_fetch - 1}'},
                          stream=True, timeout=timeout, allow_redirects=True) as r:
            if r.status_code not in (200, 206):
                return None, None
            total = r.headers.get('Content-Range')
            total = int(total.split('/')[-1]) if total and '/' in total else None
            n = 0
            for chunk in r.iter_content(262144):
                if not chunk:
                    continue
                n += len(chunk)
                if n >= bytes_to_fetch or time.time() - t0 > timeout:
                    break
        dt = time.time() - t0
        if n <= 0 or dt <= 0:
            return None, None
        return n / dt, total
    except Exception:
        return None, None



# ============================================================================
# Reliable-source scoring (P2)
# ============================================================================
# The panel used to offer a flat list of same-name mirrors and let the user
# guess. Three objective, checkable signals decide which mirror is right:
#
#   1. SIZE  - the remote file size must match the size the workflow
#      expects. Same-name files differ in size when they are different
#      quantisations (int8_convrot vs full bf16), and picking the wrong one
#      yields a file ComfyUI cannot load.
#   2. DIRECTORY - the file's path inside the repo must live in the same
#      ComfyUI folder the workflow expects (diffusion_models/, text_encoders/,
#      ...). A publisher's own repo mirrors the ComfyUI layout; a random
#      re-upload usually drops the file at the repo root.
#   3. OFFICIAL - the repo belongs to a namespace that publishes the
#      ComfyUI-ready layout (Comfy-Org, Kijai, Lightx2v, RunningHubAI).
#
# Every signal is recorded on the candidate so the UI can SHOW the evidence
# instead of asking the user to trust a name.

# Tolerance for the size comparison. Remote metadata and the local
# safetensors header can disagree by a few bytes; 1% covers it.
_SIZE_TOLERANCE = 0.01


def _expected_model_size(filename, model_dir=None):
    """The size this model is expected to have, or None when unknown.

    Sources, in order:
      * the safetensors header of an in-flight .partial file
      * the safetensors header of a (truncated) local file
      * the size the running download reported
      * a cached entry in model_metadata.json

    IMPORTANT: a .partial file's CURRENT byte count is NOT the expected size
    (a 4.6 GB partial of a 9.7 GB model would make every candidate look
    "wrong size"). The safetensors header sits at the very beginning of the
    file and states the full tensor data size, so it gives the right
    number even for a half-downloaded file.
    """
    base = os.path.basename(filename or '')
    if not base:
        return None

    # 1) the in-flight partial file - its header states the total size
    try:
        partial = _find_file_by_basename(base + '.partial')
    except Exception:
        partial = None
    if partial:
        try:
            status, expected = verify_safetensors_file(partial)
            if expected and status in ('truncated', 'ok'):
                return expected
        except Exception:
            pass

    # 2) the header of a truncated / complete local file
    try:
        local = _find_file_by_basename(base)
    except Exception:
        local = None
    if local:
        try:
            status, expected = verify_safetensors_file(local)
            if expected and status in ('truncated', 'ok'):
                return expected
        except Exception:
            pass

    # 3) the size the running download reported (total, not downloaded)
    try:
        with download_lock:
            for prog in download_progress.values():
                if (prog.get('filename') or '').lower().endswith(base.lower()):
                    total = prog.get('total_size') or 0
                    if total:
                        return int(total)
    except Exception:
        pass

    # 4) a cached size from a previous lookup
    try:
        cached = get_cached_metadata(base) or {}
        if cached.get('size'):
            return int(cached['size'])
    except Exception:
        pass

    return None


def _hf_remote_size(url, timeout=10):
    """Remote file size in bytes for a HuggingFace resolve URL.

    Uses HEAD (cheap) and falls back to a 1-byte Range GET for hosts that
    reject HEAD. Returns None when the size cannot be determined.
    """
    if not url:
        return None
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True)
        size = resp.headers.get('X-Linked-Size') or resp.headers.get('Content-Length')
        if size:
            return int(size)
        if resp.status_code not in (200, 206):
            return None
    except Exception:
        pass
    try:
        resp = requests.get(url, headers={'Range': 'bytes=0-0'}, timeout=timeout,
                            allow_redirects=True, stream=True)
        cr = resp.headers.get('Content-Range', '')
        if '/' in cr:
            tail = cr.rsplit('/', 1)[-1].strip()
            if tail.isdigit():
                return int(tail)
        size = resp.headers.get('Content-Length')
        if size and resp.status_code == 200:
            return int(size)
    except Exception:
        pass
    return None


def score_candidates(cands, filename, model_dir=None, expected_size=None):
    """Attach evidence + a score to every candidate, best first.

    Mutates and returns the list. Each candidate gains:
      'size_match'   True/False/None  (None = size unknown, not a mismatch)
      'dir_match'    True/False/None
      'official'     True/False
      'score'        int, higher is better
      'evidence'     human readable list of why this candidate ranked here
    """
    if not cands:
        return cands

    if expected_size is None:
        expected_size = _expected_model_size(filename, model_dir)
    want_dir = (model_dir or resolve_model_directory(filename) or '').lower()

    # Remote sizes are fetched in parallel - one HEAD per candidate.
    urls = [c.get('url', '') for c in cands]
    sizes = {}
    if urls:
        with ThreadPoolExecutor(max_workers=min(8, len(urls))) as pool:
            futs = {pool.submit(_hf_remote_size, u): c for u, c in zip(urls, cands)}
            for fut in as_completed(futs):
                try:
                    sizes[id(futs[fut])] = fut.result()
                except Exception:
                    sizes[id(futs[fut])] = None

    for c in cands:
        score = 0
        evidence = []
        official = False
        repo_l = (c.get('repo') or '').lower()
        for pref in _HF_OFFICIAL_PREFIXES:
            if repo_l.startswith(pref):
                official = True
                break
        c['official'] = official

        # --- signal 1: size ---
        remote = sizes.get(id(c)) or c.get('size') or 0
        c['remote_size'] = remote
        if expected_size and remote:
            delta = abs(remote - expected_size)
            if delta <= max(1, int(expected_size * _SIZE_TOLERANCE)):
                c['size_match'] = True
                score += 100
                evidence.append(f"大小匹配 {format_size(remote)}")
            else:
                c['size_match'] = False
                score -= 100
                evidence.append(f"大小不符 远端 {format_size(remote)}"
                                f" / 期望 {format_size(expected_size)}")
        else:
            c['size_match'] = None

        # --- signal 2: directory layout ---
        path = (c.get('path') or '').lower()
        if want_dir and path:
            head = path.split('/')[0]
            if head == want_dir:
                c['dir_match'] = True
                score += 40
                evidence.append(f"目录匹配 {want_dir}/")
            elif '/' not in path:
                c['dir_match'] = False
                score -= 15
                evidence.append("仓库根目录（无 ComfyUI 目录结构）")
            else:
                c['dir_match'] = False
                evidence.append(f"目录为 {head}/，期望 {want_dir}/")
        else:
            c['dir_match'] = None

        # --- signal 3: official namespace ---
        if official:
            score += 30
            evidence.append("官方发布仓库")

        # --- tie breakers ---
        score += min(10, (c.get('likes') or 0) // 10)
        score += min(5, (c.get('downloads') or 0) // 1000)

        c['score'] = score
        c['evidence'] = evidence

    cands.sort(key=lambda c: -c.get('score', 0))
    return cands


def select_reliable_source(cands, filename, model_dir=None, expected_size=None):
    """Pick the best candidate and return (best, reason) or (None, reason).

    A candidate is only accepted when it is CONFIRMED - i.e. it either
    matches the expected size, or it is an official namespace with a
    directory layout that matches the expected ComfyUI folder. An
    unconfirmed candidate is still returned, but flagged, so the UI can
    present it as a guess instead of a fact.
    """
    if not cands:
        return None, '没有找到任何同名来源'

    scored = score_candidates(cands, filename, model_dir, expected_size)
    best = scored[0]

    confirmed = (best.get('size_match') is True) or (
        best.get('official') and best.get('dir_match') is True)

    if best.get('size_match') is True:
        reason = '大小已校验：' + '；'.join(best.get('evidence') or [])
    elif confirmed:
        reason = '官方源已确认：' + '；'.join(best.get('evidence') or [])
    else:
        reason = '最佳猜测（未完全确认）：' + '；'.join(best.get('evidence') or [])

    best['confirmed'] = confirmed
    return best, reason


def collect_source_candidates(filename, model_dir=None):
    """Collect every candidate download URL for a workflow model filename.

    Order: curated KNOWN_GOOD_SOURCES -> ModelScope -> HF/Civitai registries.
    Each candidate carries its source label so the UI can show provenance.
    """
    cands = []
    base = os.path.basename(filename or '')

    kg = lookup_known_good_source(filename)
    if kg and kg.get('url'):
        cands.append({
            'url': kg['url'], 'source': 'known_good',
            'repo': kg.get('hf_repo', ''), 'directory': kg.get('directory', ''),
            'size': kg.get('size', 0),
        })

    try:
        cands.extend(search_modelscope(filename, model_dir))
    except Exception as e:
        logging.debug(f"[WMD] ModelScope search error: {e}")

    # Every exact-name mirror on HuggingFace. find_model_url() only returns
    # ONE url, so without this the panel can never show the user the choice
    # of mirrors to download from.
    try:
        cands.extend(search_huggingface_candidates(filename, model_dir))
    except Exception as e:
        logging.debug(f"[WMD] HF candidate search error: {e}")

    # Fallback registries, tried AFTER the HF mirrors so that a rich HF
    # record is never shadowed by a bare url-only record. The old order
    # (registry first) made the official repo show up with an empty repo
    # name, because find_model_url()'s record had no repo/path/size.
    url, src = find_model_url(filename, search_apis=True)
    if url:
        cands.append({'url': url, 'source': src or 'search',
                      'directory': model_dir or '', 'size': 0,
                      'repo': '', 'path': '', 'hf_repo': '', 'hf_path': ''})

    # dedupe by url, keep the RICHEST record (repo/path/size/official)
    out, by_url = [], {}
    for c in cands:
        u = c.get('url')
        if not u:
            continue
        prev = by_url.get(u)
        if prev is None:
            by_url[u] = c
            out.append(c)
            continue
        # merge: fill in whatever the new record knows and the old one lacks
        for k in ('repo', 'path', 'hf_repo', 'hf_path', 'directory', 'size',
                  'likes', 'downloads', 'source', 'official'):
            if not prev.get(k) and c.get(k):
                prev[k] = c[k]
    return out


def pick_best_candidate(cands, probe_bytes=1 * 1024 * 1024, top_n=1):
    """Speed-test candidates and rank them fastest-first.

    Runs the probes sequentially (small ranges) — cheap and predictable.
    Returns (ranked_list, best_candidate_or_None). Candidates that fail the
    probe are dropped.
    """
    ranked = []
    if cands:
        with ThreadPoolExecutor(max_workers=min(8, len(cands))) as pool:
            futs = {pool.submit(probe_url_speed, c['url'], probe_bytes): c
                    for c in cands}
            for fut in as_completed(futs):
                c = futs[fut]
                try:
                    speed, total = fut.result()
                except Exception:
                    speed, total = None, None
                if speed and speed > 0:
                    c2 = dict(c)
                    c2['speed_bps'] = speed
                    if total and not c2.get('size'):
                        c2['size'] = total
                    ranked.append(c2)
    ranked.sort(key=lambda x: -x['speed_bps'])
    best = ranked[0] if ranked else None
    return ranked[:max(top_n, 1)], best


def find_model_url_with_sources(filename, model_dir=None, auto_select=True):
    """Resolve a filename to its best download source.

    Returns (best_candidate, all_ranked_candidates).
    - Exact-name matches only; when none exists, this returns (None, [call list])
      and the caller should present the alternatives to the user to choose.
    """
    cands = collect_source_candidates(filename, model_dir)
    if not cands:
        return None, []
    if not auto_select:
        return cands[0], cands

    # Evidence-based pick (size / directory / official namespace). The
    # speed probe is kept as a tie-breaker only, so a slow-but-correct
    # official mirror still wins over a fast-but-wrong repack.
    scored = score_candidates(cands, filename, model_dir)
    best = select_reliable_source(scored, filename, model_dir)[0]
    return best, scored


# Token-level "junk" that never identifies a model family and would make any
# two unrelated filenames look identical.
_SUGGEST_IGNORE_TOKENS = {
    'safetensors', 'ckpt', 'pt', 'pth', 'bin', 'onnx', 'gguf', 'sft',
    'comfy', 'comfyui', 'comyu', 'fro', 'frozen', 'prune', 'pruned',
    'fp16', 'fp32', 'bf16', 'fp8', 'int8', 'w6a8', 'w4a8', 'nvfp4',
    'e4m3fn', 'e5m2', 'scaled', 'convrot', 'emaonly', 'only', 'q4', 'q5',
    'q6', 'q8', 'km', 'q4_0', 'f16', 'v1', 'v2', 'v3', 'v4',
    'x', 'y', 'step', 'steps', 'final', 'latest', 'master', 'main',
    'model', 'models', 'mini', 'small', 'full', 'lite', 'pro', 'ultra',
    'awq', 'gptq', 'gguf', 'tensor', 'split', 'part', 'shard',
    # provenance / packaging words: they describe HOW a file was made, not WHAT
    # model it is, and they differ between the two sources of the same model
    'hybrid', 'merged', 'merge', 'ref2va', 'fl2va', 'rel2va', 'b25',
}


def _name_tokens(name):
    """Meaningful lowercase tokens of a filename (quant/format noise removed)."""
    stem = os.path.splitext(os.path.basename(name or ''))[0]
    toks = re.split(r'[-_.\s/]+', stem.lower())
    return {t for t in toks if t and t not in _SUGGEST_IGNORE_TOKENS and not t.isdigit()}


def suggest_alternative_models(filename, model_dir=None):
    """Suggest downloadable alternatives when no exact-name source exists.

    Uses token overlap (shared identifying tokens such as minimax/h3/fl2va)
    rather than raw string similarity — quantization and provenance tokens like
    'hybrid', 'pruned', 'int8' inflate edit distance and hide real siblings.
    """
    base = os.path.splitext(os.path.basename(filename or ''))[0]
    if not base:
        return []
    want = _name_tokens(filename)
    if not want:
        return []

    out = []
    for repo in ('Comfy-Org/MiniMax-H3', 'Comfy-Org'):
        try:
            files = modelscope_list_files(repo)
        except Exception:
            continue
        for f in files:
            fbase = os.path.basename(f['path'])
            if fbase.lower() == os.path.basename(filename or '').lower():
                continue        # exact name would have matched already
            have = _name_tokens(fbase)
            if not have:
                continue
            shared = want & have
            if not shared:
                continue
            # require a real family: a meaningful share of BOTH token sets is shared
            jaccard = len(shared) / len(want | have)
            containment = len(shared) / min(len(want), len(have))
            if jaccard < 0.4 or containment < 0.6:
                continue
            d = _modelscope_file_to_dir(f['path'])
            if not d:
                continue
            out.append({
                'filename': fbase,
                'url': MODELSCOPE_RESOLVE.format(repo=repo, path=f['path']),
                'source': 'modelscope',
                'repo': repo,
                'path': f['path'],
                'directory': d,
                'size': f['size'],
                'similarity': round(jaccard, 3),
                'match_quality': round(containment, 3),
                'matched_tokens': sorted(shared),
            })
    # best matches first: joint similarity, then how much of the smaller
    # token set is covered (favours candidates the user can actually use)
    # and, when similarity ties (common for quant variants of one family),
    # the file whose size is closest to the wanted file — that is the
    # variant a user can most plausibly drop in as a replacement.
    # Size of the wanted file comes from the curated table / cached metadata
    # (the exact name is by definition absent on ModelScope here)
    want_size = 0
    kg = lookup_known_good_source(filename)
    if kg and kg.get('size'):
        want_size = kg['size']
    out.sort(key=lambda x: (-x['similarity'], -x['match_quality'],
                            abs(x.get('size', 0) - want_size) if want_size else 0))
    seen, uniq = set(), []
    for o in out:
        if o['filename'].lower() not in seen:
            seen.add(o['filename'].lower())
            uniq.append(o)
    return uniq[:12]


# --- HuggingFace candidate search -------------------------------------------
# The previous implementation searched /api/models?search=<full filename stem>
# and then listed /tree/main (top level only). Both assumptions break on the
# way HuggingFace actually stores these files:
#   * /api/models?search= matches REPO NAMES. Repos are named after the model
#     FAMILY ("Comfy-Org/MiniMax-H3"), not after the file
#     ("minimax_h3_fl2va_bf16.safetensors"), so a filename query misses them.
#   * /tree/main without ?recursive=1 returns only the top level, so every
#     file inside loras/ diffusion_models/ text_encoders/ is invisible.
# Fix: derive the family tokens from the filename, query with those, and walk
# the repo tree recursively. Every candidate is confirmed by exact basename.

_HF_CANDIDATE_CACHE = {}
_HF_REPO_FILE_CACHE = {}
_HF_OFFICIAL_PREFIXES = ('comfy-org/', 'kijai/', 'lightx2v/', 'runninghubai/')

# Disk cache for HF candidate lookups. The in-memory dict is lost on every
# ComfyUI restart, which made the first scan after each restart pay the
# full network cost again. The cache is keyed by "<basename>|<dir>" and
# holds only public repo metadata (URL, size, path), so it is safe to keep.
_HF_CANDIDATE_DISK_CACHE = os.path.join(EXTENSION_PATH, 'hf_candidate_cache.json')
_HF_CANDIDATE_DISK_MAX_AGE = 7 * 24 * 3600      # 1 week
_HF_CANDIDATE_DISK_LOCK = threading.Lock()


def _load_hf_disk_cache():
    """Load the on-disk HF candidate cache. Never raises."""
    try:
        if not os.path.exists(_HF_CANDIDATE_DISK_CACHE):
            return {}
        with open(_HF_CANDIDATE_DISK_CACHE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.debug(f"[WMD] HF disk cache load failed: {e}")
        return {}


def _save_hf_disk_cache(cache):
    """Persist the HF candidate cache. Never raises."""
    try:
        tmp = _HF_CANDIDATE_DISK_CACHE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cache, f, indent=1)
        os.replace(tmp, _HF_CANDIDATE_DISK_CACHE)
    except Exception as e:
        logging.debug(f"[WMD] HF disk cache save failed: {e}")


def _hf_candidate_cache_get(cache_key):
    """Cache lookup: memory first, then disk (with age check)."""
    with _HF_CANDIDATE_DISK_LOCK:
        if cache_key in _HF_CANDIDATE_CACHE:
            return _HF_CANDIDATE_CACHE[cache_key]
        disk = _load_hf_disk_cache()
        entry = disk.get(cache_key)
        if not entry:
            return None
        try:
            age = time.time() - float(entry.get('ts', 0))
        except (TypeError, ValueError):
            age = _HF_CANDIDATE_DISK_MAX_AGE + 1
        if age > _HF_CANDIDATE_DISK_MAX_AGE:
            return None
        cands = entry.get('candidates') or []
        _HF_CANDIDATE_CACHE[cache_key] = cands
        return cands


def _hf_candidate_cache_put(cache_key, cands):
    """Cache store: memory + disk."""
    with _HF_CANDIDATE_DISK_LOCK:
        _HF_CANDIDATE_CACHE[cache_key] = cands
        disk = _load_hf_disk_cache()
        disk[cache_key] = {
            'ts': time.time(),
            'candidates': cands,
        }
        # keep the file from growing without bound
        if len(disk) > 2000:
            cutoff = time.time() - _HF_CANDIDATE_DISK_MAX_AGE
            disk = {k: v for k, v in disk.items()
                    if (v.get('ts') or 0) >= cutoff}
        _save_hf_disk_cache(disk)


def _hf_candidate_rank(c):
    """Order candidates: official namespaces first, then popularity.

    Official prefixes are checked case-insensitively; 'comfy-org/' covers the
    publisher namespace Comfy uses for official ComfyUI-ready repos
    (e.g. Comfy-Org/MOSS-Image), which is exactly the repo a workflow
    author means.
    """
    official = 0
    repo_l = (c.get('repo') or '').lower()
    for i, pref in enumerate(_HF_OFFICIAL_PREFIXES):
        if repo_l.startswith(pref):
            official = len(_HF_OFFICIAL_PREFIXES) - i
            break
    return (-official, -(c.get('likes') or 0), -(c.get('downloads') or 0))


_HF_VERSION_TOKEN_RE = re.compile(r'^v?\d+(\.\d+)*$')


def _hf_family_tokens(filename):
    """Identifying tokens of a filename, used to build repo search queries.

    Drops quantisation/packaging noise (_SUGGEST_IGNORE_TOKENS), plain version
    numbers (v0.1, v2) and tokens that START with a digit (4step, 32b, 3d).
    Tokens that merely CONTAIN a digit are kept on purpose: they are often
    the most identifying part of a model family ("h3", "fl2va", "qwen3vl").
    """
    stem = os.path.splitext(os.path.basename(filename or ''))[0]
    toks = re.split(r'[-_.\s/]+', stem.lower())
    out, seen = [], set()
    for t in toks:
        if not t or t in _SUGGEST_IGNORE_TOKENS:
            continue
        if t[0].isdigit():
            continue
        if _HF_VERSION_TOKEN_RE.match(t):
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _hf_search_queries(filename):
    """Repo search queries for a filename.

    HF's /api/models?search= is a SUBSTRING match on the repo name, so the
    useful query is a run of ADJACENT identifying tokens ("minimax_h3"), not
    the whole filename stem and not a prefix either - the family name can
    sit anywhere in the file name
    (qwen3vl_32b_minimax_h3_int8_convrot -> "minimax_h3").
    """
    toks = _hf_family_tokens(filename)
    if not toks:
        return []

    queries = []
    def add(q):
        if q and q not in queries:
            queries.append(q)

    # adjacent windows, most specific first
    n = len(toks)
    for size in (2, 3):
        for i in range(0, n - size + 1):
            add('_'.join(toks[i:i + size]))
    # single tokens as a last resort (broad)
    for t in toks:
        add(t)
    # hyphenated variants of the windows (repo names commonly use '-')
    base = list(queries)
    for q in base:
        add(q.replace('_', '-'))
    return queries


def _hf_repos_containing(query, target_basename, limit=40):
    """Repos whose file list contains target_basename exactly.
    Returns [{repo, likes, downloads}] (discovery only, no file path yet)."""
    try:
        hf_api = apply_hf_mirror('https://huggingface.co')
        url = (f"{hf_api}/api/models?search={urllib.parse.quote(query)}"
               f"&limit={limit}&full=true")
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return []
        repos = resp.json()
    except Exception as e:
        logging.debug(f"[WMD] HF repo search failed for '{query}': {e}")
        return []
    out = []
    for rp in (repos if isinstance(repos, list) else []):
        rid = rp.get('id', '')
        if not rid:
            continue
        for sib in (rp.get('siblings') or []):
            rfn = sib.get('rfilename') or ''
            if rfn and os.path.basename(rfn) == target_basename:
                out.append({
                    'repo': rid,
                    'likes': rp.get('likes', 0) or 0,
                    'downloads': rp.get('downloads', 0) or 0,
                })
                break
    return out


def _hf_repo_file_info(repo_id, target_basename):
    """{path, size} for target_basename inside repo (recursive tree), or None."""
    key = (repo_id, target_basename)
    if key in _HF_REPO_FILE_CACHE:
        return _HF_REPO_FILE_CACHE[key]
    info = None
    try:
        hf_api = apply_hf_mirror('https://huggingface.co')
        url = f"{hf_api}/api/models/{urllib.parse.quote(repo_id)}/tree/main?recursive=1"
        resp = requests.get(url, timeout=20)
        if resp.status_code == 200:
            for t in resp.json():
                if t.get('type') != 'file':
                    continue
                path = t.get('path', '')
                if os.path.basename(path) == target_basename:
                    info = {'path': path, 'size': t.get('size', 0) or 0}
                    break
    except Exception as e:
        logging.debug(f"[WMD] HF tree failed for {repo_id}: {e}")
    _HF_REPO_FILE_CACHE[key] = info
    return info


def _hf_fulltext_repos(target_basename, limit=20):
    """Repos mentioning the filename, via the HF full-text index."""
    try:
        hf_api = apply_hf_mirror('https://huggingface.co')
        url = (f"{hf_api}/api/search/full-text?q={urllib.parse.quote(target_basename)}"
               f"&type=model&limit={limit}")
        resp = requests.get(url, timeout=20)
        if resp.status_code != 200:
            return []
        hits = resp.json().get('hits', [])
    except Exception as e:
        logging.debug(f"[WMD] HF full-text search failed for {target_basename}: {e}")
        return []
    out, seen = [], set()
    for h in hits:
        rid = h.get('name')
        if rid and rid not in seen:
            seen.add(rid)
            out.append(rid)
    return out


def search_huggingface_candidates(filename, model_dir=None):
    """Every exact-name download candidate on HuggingFace for `filename`.

    Returns [{url, source, repo, path, directory, size, hf_repo, hf_path}],
    best first (official namespaces, then popularity). The panel lists these
    so the user can pick which mirror to download from - nothing is fetched
    until the user clicks download.
    """
    global _HF_CANDIDATE_CACHE

    base = os.path.basename(filename or '')
    if not base:
        return []

    cache_key = f"{base}|{model_dir or ''}"
    cached = _hf_candidate_cache_get(cache_key)
    if cached is not None:
        return cached

    found = {}   # repo -> {repo, likes, downloads}
    queries = _hf_search_queries(base)

    # Stage 1: family-token repo search, all queries in parallel.
    if queries:
        with ThreadPoolExecutor(max_workers=min(8, len(queries))) as pool:
            for res in pool.map(lambda q: _hf_repos_containing(q, base), queries):
                for r in res:
                    found.setdefault(r['repo'], r)

    # Stage 2: full-text search. Community repos are often named after
    # something else entirely ("Minimax_h3_latent_Upscaler", "selflift-bundle"),
    # so no family-token query reaches them. The full-text index does.
    if len(found) < 3:
        for rid in _hf_fulltext_repos(base):
            if rid not in found:
                found[rid] = {'repo': rid, 'likes': 0, 'downloads': 0}

    # Stage 3: confirm the exact file inside every candidate repo, in parallel.
    repos = list(found.values())
    infos = {}
    if repos:
        with ThreadPoolExecutor(max_workers=min(8, len(repos))) as pool:
            futs = {pool.submit(_hf_repo_file_info, r['repo'], base): r['repo']
                    for r in repos}
            for fut in as_completed(futs):
                info = fut.result()
                if info:
                    infos[futs[fut]] = info

    cands = []
    for r in repos:
        info = infos.get(r['repo'])
        if not info:
            continue
        path = info['path']
        directory = model_dir or _modelscope_file_to_dir(path) or resolve_model_directory(base)
        cands.append({
            'url': f"https://huggingface.co/{r['repo']}/resolve/main/{path}",
            'source': 'huggingface_api',
            'repo': r['repo'],
            'path': path,
            'directory': directory,
            'size': info['size'],
            'hf_repo': r['repo'],
            'hf_path': path,
            'likes': r.get('likes', 0),
            'downloads': r.get('downloads', 0),
        })

    cands.sort(key=_hf_candidate_rank)
    _hf_candidate_cache_put(cache_key, cands)
    logging.info(f"[WMD] HF candidates for {base}: {len(cands)} "
                 f"({', '.join(c['repo'] for c in cands[:5])})")
    return cands


def search_huggingface_api(filename):
    """Search HuggingFace for an exact-name model file. Returns the best URL."""
    global _url_search_cache

    cache_key = f"hf_{filename}"
    if cache_key in _url_search_cache:
        return _url_search_cache[cache_key]

    url = None
    try:
        cands = search_huggingface_candidates(filename)
        if cands:
            url = cands[0]['url']
            logging.info(f"[Workflow-Models-Downloader] Found {filename} on HuggingFace: {cands[0]['repo']}")
    except Exception as e:
        logging.debug(f"[Workflow-Models-Downloader] HuggingFace API search failed: {e}")

    _url_search_cache[cache_key] = url
    return url


def search_civitai_api(filename):
    """Search CivitAI API for a model file"""
    global _url_search_cache

    cache_key = f"civit_{filename}"
    if cache_key in _url_search_cache:
        return _url_search_cache[cache_key]

    try:
        # Search by filename
        filename_base = os.path.splitext(filename)[0]
        # Remove common suffixes for better search
        search_name = re.sub(r'[-_]?(fp16|fp8|bf16|e4m3fn|scaled|pruned|emaonly).*', '', filename_base, flags=re.IGNORECASE)

        search_url = f"https://civitai.com/api/v1/models?query={urllib.parse.quote(search_name)}&limit=5"

        response = requests.get(search_url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            items = data.get('items', [])

            for item in items:
                model_versions = item.get('modelVersions', [])
                for version in model_versions:
                    files = version.get('files', [])
                    for file_info in files:
                        file_name = file_info.get('name', '')
                        if file_name.lower() == filename.lower():
                            url = file_info.get('downloadUrl', '')
                            if url:
                                _url_search_cache[cache_key] = url
                                logging.info(f"[Workflow-Models-Downloader] Found {filename} on CivitAI")
                                return url

    except Exception as e:
        logging.debug(f"[Workflow-Models-Downloader] CivitAI API search failed: {e}")

    _url_search_cache[cache_key] = None
    return None


def search_tavily_api(filename):
    """Search using Tavily API for model download URLs"""
    global _url_search_cache

    cache_key = f"tavily_{filename}"
    if cache_key in _url_search_cache:
        return _url_search_cache[cache_key]

    tavily_key = get_tavily_api_key()
    if not tavily_key:
        return None

    try:
        # Build search query focused on finding download URLs
        filename_base = os.path.splitext(filename)[0]
        # Clean up common suffixes for better search
        search_name = re.sub(r'[-_]?(fp16|fp8|bf16|e4m3fn|scaled|pruned|emaonly).*', '', filename_base, flags=re.IGNORECASE)

        search_query = f"{search_name} safetensors download huggingface OR civitai"

        url = "https://api.tavily.com/search"
        payload = {
            "api_key": tavily_key,
            "query": search_query,
            "search_depth": "advanced",
            "include_domains": ["huggingface.co", "civitai.com", "github.com"],
            "max_results": 10
        }

        response = requests.post(url, json=payload, timeout=15)

        if response.status_code == 200:
            data = response.json()
            results = data.get('results', [])

            # Look for direct download URLs in results
            for result in results:
                result_url = result.get('url', '')
                content = result.get('content', '').lower()
                title = result.get('title', '').lower()

                # Check if this looks like a model page or download link
                if 'huggingface.co' in result_url:
                    # Try to construct download URL from HuggingFace page
                    # Pattern: https://huggingface.co/{repo}/blob/main/{file}
                    hf_pattern = r'huggingface\.co/([^/]+/[^/]+)(?:/(?:blob|tree)/[^/]+)?'
                    match = re.search(hf_pattern, result_url)
                    if match:
                        repo = match.group(1)
                        # Check if filename is mentioned in content or title
                        if filename.lower() in content or filename_base.lower() in content:
                            # Try to find the file in this repo
                            try:
                                files_url = f"https://huggingface.co/api/models/{repo}/tree/main"
                                files_response = requests.get(files_url, timeout=10)
                                if files_response.status_code == 200:
                                    files = files_response.json()
                                    for file_info in files:
                                        file_path = file_info.get('path', '')
                                        if file_path.endswith('.safetensors') or file_path.endswith('.ckpt'):
                                            # Check if filename matches
                                            if filename.lower() in file_path.lower() or filename_base.lower() in file_path.lower():
                                                download_url = f"https://huggingface.co/{repo}/resolve/main/{file_path}"
                                                _url_search_cache[cache_key] = {
                                                    'url': download_url,
                                                    'source': 'tavily_huggingface',
                                                    'repo': repo,
                                                    'tavily_result': result
                                                }
                                                logging.info(f"[Workflow-Models-Downloader] Tavily found {filename} on HuggingFace: {repo}")
                                                return _url_search_cache[cache_key]
                            except Exception:
                                pass

                elif 'civitai.com' in result_url:
                    # Extract model ID from CivitAI URL
                    civit_pattern = r'civitai\.com/models/(\d+)'
                    match = re.search(civit_pattern, result_url)
                    if match:
                        model_id = match.group(1)
                        # Get model info from CivitAI API
                        try:
                            api_url = f"https://civitai.com/api/v1/models/{model_id}"
                            api_response = requests.get(api_url, timeout=10)
                            if api_response.status_code == 200:
                                model_data = api_response.json()
                                model_versions = model_data.get('modelVersions', [])
                                for version in model_versions:
                                    files = version.get('files', [])
                                    for file_info in files:
                                        file_name = file_info.get('name', '')
                                        if filename.lower() in file_name.lower() or filename_base.lower() in file_name.lower():
                                            download_url = file_info.get('downloadUrl', '')
                                            if download_url:
                                                _url_search_cache[cache_key] = {
                                                    'url': download_url,
                                                    'source': 'tavily_civitai',
                                                    'model_name': model_data.get('name', ''),
                                                    'civitai_url': result_url,
                                                    'tavily_result': result
                                                }
                                                logging.info(f"[Workflow-Models-Downloader] Tavily found {filename} on CivitAI")
                                                return _url_search_cache[cache_key]
                        except Exception:
                            pass

            # If no direct match found, return the most relevant result info
            if results:
                _url_search_cache[cache_key] = {
                    'url': None,
                    'results': results[:5],  # Return top 5 for user to choose
                    'source': 'tavily_suggestions'
                }
                return _url_search_cache[cache_key]

    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Tavily API search failed: {e}")

    _url_search_cache[cache_key] = None
    return None


def find_model_url(filename, search_apis=False):
    """
    Try to find download URL for a model using multiple sources:
    1. Curated known-good overrides
    2. Popular models registry (curated list)
    3. model-list.json (ComfyUI Manager, exact match only)
    4. HuggingFace API search (if search_apis=True)
    5. CivitAI API search (if search_apis=True)
    """
    # 0. Curated known-good table (beats everything, fixes stale/poisoned entries)
    kg = lookup_known_good_source(filename)
    if kg and kg.get('url'):
        return kg['url'], 'known_good'

    # 1. Check popular models registry
    url = lookup_url_in_popular_models(filename)
    if url:
        return url, 'popular_models'

    # 2. Check model-list.json with fuzzy matching
    url = lookup_url_in_model_list(filename)
    if url:
        return url, 'model_list'

    # 3. Search HuggingFace API
    if search_apis:
        url = search_huggingface_api(filename)
        if url:
            return url, 'huggingface_api'

        # 4. Search CivitAI API
        url = search_civitai_api(filename)
        if url:
            return url, 'civitai_api'

    return None, None


# =============================================================================
# MODEL TYPE DETECTION
# =============================================================================

SUPPORTED_MODEL_EXTENSIONS = {'.ckpt', '.pt', '.pt2', '.bin', '.pth', '.safetensors', '.pkl', '.sft', '.gguf', '.onnx'}

# Filename patterns for model type detection
# Patterns are checked in order - first match wins
# This must match exactly with parse_workflow_models.py
FILENAME_TYPE_PATTERNS = [
    # VAE Approximation (TAESD)
    (r'tae[sf]\d*[_-]?(decoder|encoder)', 'TAESD', 'vae_approx'),

    # Latent upscalers (before generic upscale)
    (r'latent.*upsampl|upsampl.*latent', 'Latent Upsampler', 'latent_upscale_models'),

    # Text encoders (T5 variants)
    (r't5[-_]?xxl|umt5|t5[-_]?xl|t5[-_]?base', 'Text Encoder (T5)', 'text_encoders'),
    (r'long[-_]?clip', 'Long CLIP', 'text_encoders'),

    # CLIP Vision (before generic CLIP)
    (r'clip.*vision|sigclip.*vision|vision.*clip', 'CLIP Vision', 'clip_vision'),

    # CLIP models
    (r'^clip[-_]|clip[-_][lg]|openclip', 'CLIP', 'clip'),

    # Flux specific
    (r'^flux.*dev|flux.*schnell|^flux1|flux[-_]', 'Flux UNET', 'diffusion_models'),
    (r'^ae\.safetensors$', 'VAE (Flux)', 'vae'),

    # MOSS-Image: a FLUX.2-latent single-stream DiT. The DiT checkpoints are
    # diffusion models, the Qwen3.5 text encoder is a text encoder. Must sit
    # BEFORE the generic ".safetensors -> checkpoints" fallback, which would
    # otherwise send the 10 GB DiT to models/checkpoints/ where no loader
    # ever looks for it.
    (r'^moss[_-]?image', 'MOSS-Image DiT', 'diffusion_models'),

    # Qwen-family text encoders (Qwen3.5 / Qwen3-VL / Qwen-Image encoders)
    (r'^qwen.*(convrot|int8|bf16|fp8|w4a8|gptq)|qwen3\.5', 'Text Encoder (Qwen)', 'text_encoders'),

    # VAE (check before video models since video models can have _vae suffix)
    (r'_vae_|_vae\.|^vae[-_]|[-_]vae\.|vae[-_]?fp', 'VAE', 'vae'),

    # LoRA (check BEFORE video models - distill_lora, refinement_lora, etc. are LoRAs even with video model names)
    (r'lora|locon|dora', 'LoRA', 'loras'),

    # Video/Diffusion models (check patterns from VIDEO_MODEL_PATTERNS)
    (r'hunyuan|longcat|wanvideo|wan_|cosmos|cogvideo|mochi|ltxv|ltx[-_]?video', 'Video Diffusion Model', 'diffusion_models'),
    (r'framepack|stable[-_]?cascade|cascade[-_]', 'Video/Image Diffusion Model', 'diffusion_models'),
    (r'dynamicrafter|tooncrafter|animate', 'Animation Model', 'diffusion_models'),
    (r'svd|video[-_]?diffusion|i2v|t2v|ti2v', 'Video Diffusion Model', 'diffusion_models'),

    # ControlNet and adapters
    (r'controlnet|control[-_]?net|cn[-_]', 'ControlNet', 'controlnet'),
    (r't2i[-_]?adapter|adapter[-_]?t2i', 'T2I-Adapter', 'controlnet'),
    (r'ipadapter|ip[-_]?adapter', 'IP-Adapter', 'ipadapter'),

    # Face/Identity models
    (r'instantid', 'InstantID', 'instantid'),
    (r'pulid', 'PuLID', 'pulid'),
    (r'photomaker', 'PhotoMaker', 'photomaker'),
    (r'insightface|antelopev2|buffalo', 'InsightFace', 'insightface/models'),
    (r'gfpgan|codeformer|facerestorer', 'Face Restore', 'facerestore_models'),

    # Segmentation models
    (r'sam[-_]?2\.1|sam2\.1', 'SAM 2.1', 'sams'),
    (r'sam[-_]?2|sam2', 'SAM 2', 'sams'),
    (r'^sam[-_]|segment[-_]?anything', 'SAM', 'sams'),
    (r'grounding[-_]?dino', 'GroundingDINO', 'groundingdino'),
    (r'yolo|ultralytics', 'Ultralytics', 'ultralytics'),

    # VAE
    (r'vae|variational', 'VAE', 'vae'),

    # Upscalers
    (r'esrgan|realesrgan|swinir|upscale|4x[-_]|2x[-_]|rgt', 'Upscaler', 'upscale_models'),

    # AnimateDiff. 'mm' only counts when it starts an AnimateDiff-style filename
    # (mm_sd_v15, mm_sdxl...); a bare 'mm' otherwise swallows unrelated names
    # like MysticXXX_MMH3 and sends a LoRA to animatediff_models/.
    (r'animatediff|motion[-_]?module|mm[-_]?sd|mm[-_]?sdxl', 'AnimateDiff', 'animatediff_models'),
    (r'motion[-_]?lora', 'Motion LoRA', 'animatediff_motion_lora'),

    # Depth models
    (r'depth[-_]?anything|depthanything', 'Depth Anything', 'depthanything'),
    (r'depth[-_]?pro|ml[-_]?depth', 'Depth Pro', 'depth/ml-depth-pro'),

    # Embeddings
    (r'embedding|textual[-_]?inversion', 'Embedding', 'embeddings'),

    # SDXL checkpoints
    (r'sdxl|sd[-_]?xl|xl[-_]turbo', 'Checkpoint (SDXL)', 'checkpoints'),

    # SD 1.5/2.x checkpoints
    (r'sd[-_]?1\.?5|sd15|v1[-_]?5', 'Checkpoint (SD1.5)', 'checkpoints'),
    (r'sd[-_]?2\.?1|sd21|v2[-_]?1', 'Checkpoint (SD2.1)', 'checkpoints'),

    # SD3
    (r'sd[-_]?3|sd3', 'Checkpoint (SD3)', 'checkpoints'),

    # Inpainting
    (r'inpaint', 'Inpaint Model', 'checkpoints'),
]

# URL path to directory mapping
# Node-type -> model directory. This is ComfyUI's own loading rule: what
# matters is not where a URL path or a filename suggests the file should go,
# but which folder the loader node actually reads from at run time
# (LoraLoaderModelOnly reads loras/, VAELoader reads vae/, ...). Loader nodes
# not listed here fall through to the URL/keyword heuristics.
NODE_TYPE_DIRECTORIES = {
    'LoraLoader': 'loras',
    'LoraLoaderModelOnly': 'loras',
    'VAELoader': 'vae',
    'UNETLoader': 'diffusion_models',
    'UnetLoaderGGUF': 'diffusion_models',
    'CLIPLoader': 'text_encoders',
    'CLIPLoaderGGUF': 'text_encoders',
    'DualCLIPLoader': 'text_encoders',
    'TripleCLIPLoader': 'text_encoders',
    'QuadrupleCLIPLoader': 'text_encoders',
    'CLIPVisionLoader': 'clip_vision',
    'ControlNetLoader': 'controlnet',
    'DiffControlNetLoader': 'controlnet',
    'UpscaleModelLoader': 'upscale_models',
    'CheckpointLoaderSimple': 'checkpoints',
    'CheckpointLoader': 'checkpoints',
    'unCLIPCheckpointLoader': 'checkpoints',
    'ImageOnlyCheckpointLoader': 'checkpoints',
    'StyleModelLoader': 'style_models',
    'HypernetworkLoader': 'hypernetworks',
    'GLIGENLoader': 'gligen',
    'IPAdapterModelLoader': 'ipadapter',
    'InsightFaceLoader': 'insightface/models',
    'SAMLoader': 'sams',
    'UltralyticsDetectorProvider': 'ultralytics',
}

URL_DIRECTORY_HINTS = {
    '/diffusion_models/': 'diffusion_models',
    '/latent_upscale_models/': 'latent_upscale_models',
    '/text_encoders/': 'text_encoders',
    '/clip_vision/': 'clip_vision',
    '/controlnet/': 'controlnet',
    '/checkpoints/': 'checkpoints',
    '/loras/': 'loras',
    '/vae/': 'vae',
    '/vae_approx/': 'vae_approx',
    '/upscale_models/': 'upscale_models',
    '/embeddings/': 'embeddings',
    '/animatediff_models/': 'animatediff_models',
    '/animatediff_motion_lora/': 'animatediff_motion_lora',
    '/ipadapter/': 'ipadapter',
    '/insightface/': 'insightface/models',
    '/instantid/': 'instantid',
    '/photomaker/': 'photomaker',
    '/sams/': 'sams',
    '/ultralytics/': 'ultralytics',
    '/groundingdino/': 'groundingdino',
    '/facerestore_models/': 'facerestore_models',
    '/facedetection/': 'facedetection',
    '/depthanything/': 'depthanything',
    '/hypernetworks/': 'hypernetworks',
    '/gligen/': 'gligen',
    '/style_models/': 'style_models',
    '/unet/': 'unet',
    '/pulid/': 'pulid',
}


def identify_model_type_from_filename(model_name):
    """Identify model type from filename patterns"""
    # A workflow reference may carry a subfolder ("moss\\model.safetensors").
    # Every registry (model-list.json, the filename patterns) is keyed by the
    # bare filename, so match on the basename - otherwise the pattern
    # anchors (^moss_image, ^qwen...) never fire and the generic
    # ".safetensors -> checkpoints" fallback sends the file to the wrong
    # folder.
    base = os.path.basename(model_name.replace('\\', '/'))
    model_lower = base.lower()

    # Skip GGUF/LLM files
    if model_lower.endswith('.gguf'):
        return None, None

    # First check model-list.json for exact match (from metadata)
    list_type, list_dir, list_url, list_size = lookup_model_in_model_list(base)
    if list_type and list_dir:
        return list_type, list_dir

    # Check against filename patterns
    for pattern, model_type, directory in FILENAME_TYPE_PATTERNS:
        if re.search(pattern, model_lower):
            return model_type, directory

    # Default fallback by extension
    ext = os.path.splitext(base)[1].lower()
    if ext in {'.safetensors', '.ckpt'}:
        return 'Checkpoint', 'checkpoints'
    elif ext == '.onnx':
        return 'ONNX Model', 'onnx'
    elif ext in SUPPORTED_MODEL_EXTENSIONS:
        return 'Model', 'models'
    else:
        return None, None


def calculate_file_hash(filepath, algorithm='sha256'):
    """Calculate SHA256 hash of a file (first 10MB for speed)"""
    import hashlib
    hash_obj = hashlib.new(algorithm)

    try:
        with open(filepath, 'rb') as f:
            # Read first 10MB for quick hash (CivitAI uses full file hash though)
            # For accurate lookup, we need the full hash
            while chunk := f.read(8192 * 1024):  # 8MB chunks
                hash_obj.update(chunk)
        return hash_obj.hexdigest()
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Hash calculation error: {e}")
        return None


def lookup_civitai_by_hash(file_hash):
    """Look up model on CivitAI by SHA256 hash"""
    if not file_hash:
        return None

    try:
        url = f"https://civitai.com/api/v1/model-versions/by-hash/{file_hash}"
        response = requests.get(url, timeout=15)

        if response.status_code == 200:
            data = response.json()

            # Extract relevant info
            model_info = {
                'model_name': data.get('model', {}).get('name', 'Unknown'),
                'model_type': data.get('model', {}).get('type', 'Unknown'),
                'version_name': data.get('name', ''),
                'model_id': data.get('modelId'),
                'version_id': data.get('id'),
                'download_url': None,
                'original_filename': None
            }

            # Find download URL from files
            files = data.get('files', [])
            for f in files:
                hashes = f.get('hashes', {})
                if hashes.get('SHA256', '').lower() == file_hash.lower():
                    model_info['download_url'] = f.get('downloadUrl')
                    model_info['original_filename'] = f.get('name')
                    break

            # Fallback to first file if hash match not found
            if not model_info['download_url'] and files:
                model_info['download_url'] = files[0].get('downloadUrl')
                model_info['original_filename'] = files[0].get('name')

            return model_info

        return None
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] CivitAI hash lookup error: {e}")
        return None


def find_model_file_path(target_dir, filename):
    """Find the full path to a model file, checking all configured model paths including extra_model_paths.yaml"""

    dirs_to_check = [target_dir]
    if target_dir in EQUIVALENT_DIRECTORIES:
        dirs_to_check = EQUIVALENT_DIRECTORIES[target_dir]

    for check_dir in dirs_to_check:
        # Get all configured paths for this folder type (includes extra_model_paths.yaml)
        try:
            all_paths = folder_paths.get_folder_paths(check_dir)
        except Exception:
            # Fallback to default models_dir if folder type not found
            all_paths = [os.path.join(folder_paths.models_dir, check_dir)]

        for base_path in all_paths:
            # Check exact path
            model_path = os.path.join(base_path, filename)
            if os.path.exists(model_path):
                return model_path

            # Search subdirectories
            if os.path.exists(base_path):
                for root, dirs, files in os.walk(base_path):
                    if filename in files:
                        return os.path.join(root, filename)

    return None


def extract_huggingface_info(url):
    """Extract HuggingFace repo and filename from URL"""
    if not url or 'huggingface.co' not in url:
        return None, None

    # Clean URL
    url = url.split(')')[0].replace('\\n', '').replace('\n', '').strip()

    # Pattern: https://huggingface.co/{repo}/resolve/{branch}/{path/to/file}
    pattern = r'huggingface\.co/([^/]+/[^/]+)/(?:resolve|blob)/[^/]+/(.+?)(?:\?|$)'
    match = re.search(pattern, url)

    if match:
        repo = match.group(1)
        filepath = match.group(2)
        return repo, filepath

    return None, None


# ComfyUI equivalent directories - these share the same search path
EQUIVALENT_DIRECTORIES = {
    'text_encoders': ['clip', 'text_encoders'],
    'clip': ['clip', 'text_encoders'],
    'unet': ['unet', 'diffusion_models'],
    'diffusion_models': ['unet', 'diffusion_models'],
}

# Model format alternatives - used to find compatible alternatives
MODEL_FORMAT_PATTERNS = [
    # (pattern_to_match, alternative_suffixes_to_try)
    (r'(.+?)[-_]?fp32\.safetensors$', ['fp16', 'bf16', 'fp8', 'fp8_e4m3fn', 'gguf']),
    (r'(.+?)[-_]?fp16\.safetensors$', ['fp32', 'bf16', 'fp8', 'fp8_e4m3fn', 'gguf']),
    (r'(.+?)[-_]?bf16\.safetensors$', ['fp16', 'fp32', 'fp8', 'fp8_e4m3fn', 'gguf']),
    (r'(.+?)[-_]?fp8[-_]?e4m3fn\.safetensors$', ['fp16', 'bf16', 'fp8', 'fp32']),
    (r'(.+?)[-_]?fp8\.safetensors$', ['fp16', 'bf16', 'fp8_e4m3fn', 'fp32']),
    (r'(.+?)\.gguf$', ['safetensors', 'fp16.safetensors', 'bf16.safetensors']),
    (r'(.+?)[-_]?Q\d+.*\.gguf$', ['safetensors', 'fp16.safetensors', 'gguf']),
    (r'(.+?)\.safetensors$', ['fp16.safetensors', 'bf16.safetensors', 'fp8.safetensors', 'gguf']),
]


def find_model_alternatives(filename, target_dir):
    """Find alternative versions of a model (different quantizations/formats)"""
    alternatives = []
    filename_lower = filename.lower()
    base_name = None

    # Try to extract base name from filename
    for pattern, alt_suffixes in MODEL_FORMAT_PATTERNS:
        match = re.match(pattern, filename_lower)
        if match:
            base_name = match.group(1)
            break

    if not base_name:
        # Try simple extension removal
        base_name = os.path.splitext(filename_lower)[0]
        # Remove common suffixes
        for suffix in ['_fp16', '_fp32', '_bf16', '_fp8', '-fp16', '-fp32', '-bf16', '-fp8']:
            if base_name.endswith(suffix):
                base_name = base_name[:-len(suffix)]
                break

    # Get list of directories to check
    dirs_to_check = [target_dir]
    if target_dir in EQUIVALENT_DIRECTORIES:
        dirs_to_check = EQUIVALENT_DIRECTORIES[target_dir]

    for check_dir in dirs_to_check:
        try:
            available_files = folder_paths.get_filename_list(check_dir)

            for available_file in available_files:
                available_lower = available_file.lower()
                available_base = os.path.splitext(os.path.basename(available_lower))[0]

                # Remove common suffixes for comparison
                for suffix in ['_fp16', '_fp32', '_bf16', '_fp8', '-fp16', '-fp32', '-bf16', '-fp8', '_fp8_e4m3fn']:
                    if available_base.endswith(suffix):
                        available_base = available_base[:-len(suffix)]
                        break

                # Check if this could be an alternative
                if available_file.lower() != filename_lower:
                    # Check if base names are similar
                    if base_name in available_base or available_base in base_name:
                        # Get full path and size
                        full_path = folder_paths.get_full_path(check_dir, available_file)
                        size_str = None
                        if full_path and os.path.exists(full_path):
                            try:
                                size_bytes = os.path.getsize(full_path)
                                size_mb = size_bytes / (1024 * 1024)
                                if size_mb >= 1024:
                                    size_str = f"{size_mb/1024:.2f} GB"
                                else:
                                    size_str = f"{size_mb:.1f} MB"
                            except Exception:
                                pass

                        # Determine format type
                        format_type = 'unknown'
                        if 'gguf' in available_lower:
                            format_type = 'GGUF'
                        elif 'fp16' in available_lower:
                            format_type = 'FP16'
                        elif 'bf16' in available_lower:
                            format_type = 'BF16'
                        elif 'fp8' in available_lower:
                            format_type = 'FP8'
                        elif 'fp32' in available_lower:
                            format_type = 'FP32'
                        elif available_lower.endswith('.safetensors'):
                            format_type = 'SafeTensors'

                        alternatives.append({
                            'filename': available_file,
                            'directory': check_dir,
                            'format': format_type,
                            'size': size_str
                        })
        except Exception as e:
            logging.debug(f"[WMD] Error finding alternatives in {check_dir}: {e}")

    return alternatives[:5]  # Return top 5 alternatives


def invalidate_folder_cache(folder_type):
    """Invalidate ComfyUI's folder cache for a specific folder type so new files are discovered"""
    try:
        # Handle subdirectories: extract base folder type from paths like "loras/subfolder"
        folder_type_normalized = folder_type.replace('\\', '/')
        base_folder_type = folder_type_normalized.split('/')[0]

        # Clear the cache for this folder type
        if hasattr(folder_paths, 'filename_list_cache'):
            dirs_to_clear = [base_folder_type]
            if base_folder_type in EQUIVALENT_DIRECTORIES:
                dirs_to_clear = EQUIVALENT_DIRECTORIES[base_folder_type]

            for ft in dirs_to_clear:
                if ft in folder_paths.filename_list_cache:
                    del folder_paths.filename_list_cache[ft]
                    logging.debug(f"[WMD] Invalidated folder cache for: {ft}")
    except Exception as e:
        logging.debug(f"[WMD] Could not invalidate cache: {e}")


# ---- runtime node introspection: authoritative model folder per node type ----
# A node's file-picker combo options ARE folder_paths.get_filename_list(X) as
# evaluated by the node itself at run time - the only ground truth for "which
# folder does this node read". Deriving the directory from workflows/filenames/
# URLs can only ever guess; asking the node class cannot be wrong.
_NODE_COMBO_CACHE = {'t': 0.0, 'map': {}}


def _node_combo_options(node_type):
    """[(input_name, [option strings)] for a node type's file-picker combos. Cached 30s."""
    now = time.time()
    cache = _NODE_COMBO_CACHE
    if now - cache['t'] > 30.0:
        cache['map'], cache['t'] = {}, now
    if node_type in cache['map']:
        return cache['map'][node_type]
    combos = []
    try:
        import nodes as comfy_nodes
        cls = comfy_nodes.NODE_CLASS_MAPPINGS.get(node_type)
        it = getattr(cls, 'INPUT_TYPES', None) if cls else None
        res = None
        if it:
            try:
                res = it()
            except TypeError:
                res = it(cls)
        if isinstance(res, dict):
            for section in ('required', 'optional'):
                for iname, spec in (res.get(section) or {}).items():
                    if not isinstance(spec, (list, tuple)) or len(spec) < 2:
                        continue
                    opts = spec[0]
                    extra = spec[1] if isinstance(spec[1], dict) else {}
                    # classic style: (['a.safetensors', ...], {...})
                    # io.Combo style: ('COMBO', {'options': [...]})
                    if isinstance(opts, str) and opts.upper() in ('COMBO', 'STRING'):
                        opts = extra.get('options')
                    if not isinstance(opts, (list, tuple)):
                        continue
                    strs = [o for o in opts if isinstance(o, str)]
                    if strs:
                        combos.append((iname, strs))
    except Exception as e:
        logging.debug(f"[WMD] INPUT_TYPES probe failed for {node_type}: {e}")
    cache['map'][node_type] = combos
    return combos


def _folder_from_options(options):
    """Reverse-map a combo option list to the model folder it was built from.
    Returns (folder_type, containment_score)."""
    file_opts = {o for o in options
                 if os.path.splitext(o)[1].lower() in SUPPORTED_MODEL_EXTENSIONS}
    if not file_opts:
        return None, 0.0
    best, score = None, 0.0
    for name in folder_paths.folder_names_and_paths:
        try:
            fl = set(folder_paths.get_filename_list(name))
        except Exception:
            continue
        if not fl:
            continue
        s = len(file_opts & fl) / len(file_opts)
        if s > score:
            best, score = name, s
    return best, score


def authoritative_folder_for(filename, node_type):
    """(folder, 'visible'|'missing') from the node's own option lists, or None.

    'visible' - the file appears in the node's picker (that folder is correct).
    'missing' - the picker clearly maps to folder X (>=60% of options live
    there, same extension as the target) and the file is NOT in it: placing
    the file in X is the only way this node can see it.
    """
    if not filename or not node_type:
        return None
    base = os.path.basename(filename)
    ext = os.path.splitext(base)[1].lower()
    fallback = None
    for _iname, opts in _node_combo_options(node_type):
        for o in opts:
            if o == filename or os.path.basename(o) == base:
                folder, _s = _folder_from_options(opts)
                return (folder, 'visible') if folder else None
        folder, score = _folder_from_options(opts)
        if folder and score >= 0.6:
            same_ext = any(os.path.splitext(o)[1].lower() == ext for o in opts)
            if same_ext and fallback is None:
                fallback = (folder, 'missing')
    return fallback


_BASENAME_INDEX = {'built_at': 0.0, 'map': {}}


def verify_safetensors_file(path):
    """Check a .safetensors file for truncation using its own header.

    A safetensors file starts with an 8-byte little-endian header length
    followed by a JSON map whose tensor offsets describe the data region, so
    the file states its own expected total size: 8 + header_len + max(offset
    end). A download that died mid-stream (old versions had no resume) leaves
    a final-name file that passes naive "exists" checks but is garbage to
    ComfyUI. Returns (status, expected_bytes):
      'ok'        complete file
      'truncated' header valid, file shorter than header promises
      'corrupt'   header unreadable/insane (also covers non-safetensors)
      'unknown'   not a .safetensors name - no cheap structural check
    """
    if not path or not os.path.isfile(path):
        return 'unknown', None
    # A download in flight is written to "<name>.safetensors.partial" - its
    # header is intact and states the FULL size, so the check works on it
    # too (that is how the expected size of a half-downloaded model is
    # known). Accept any name that CONTAINS .safetensors.
    if '.safetensors' not in os.path.basename(path).lower():
        return 'unknown', None
    try:
        size = os.path.getsize(path)
        if size < 8:
            return 'corrupt', None
        with open(path, 'rb') as f:
            header_len = struct.unpack('<Q', f.read(8))[0]
            # Real headers are KB-scale; MB-scale means a garbage/shifted file
            if header_len == 0 or header_len > 32 * 1024 * 1024 or 8 + header_len > size:
                return 'corrupt', None
            header = json.loads(f.read(header_len))
        expected = 8 + header_len
        for key, meta in header.items():
            if key == '__metadata__' or not isinstance(meta, dict):
                continue
            ends = meta.get('data_offsets')
            if isinstance(ends, list) and ends:
                expected = max(expected, 8 + header_len + int(ends[-1]))
        if size < expected:
            return 'truncated', expected
        return 'ok', expected
    except Exception as e:
        logging.debug(f"[WMD] safetensors verify failed for {path}: {e}")
        return 'corrupt', None


def _find_file_by_basename(filename):
    """Locate a file by basename anywhere under every configured model folder.

    Compliments check_model_exists' directory-scoped lookup: when the download
    side and the scan side disagree about the target folder (or the user moved
    a file by hand) the directory-scoped lookup reports "missing" forever even
    though ComfyUI would happily load the file. Results are cached in-process
    and rebuilt at most once every 10s, and only on the miss path, so the fast
    path (file in the expected folder) never pays for it.
    Returns the full path or None.
    """
    base = os.path.basename(filename or '')
    if not base:
        return None
    now = time.time()
    idx = _BASENAME_INDEX
    if now - idx['built_at'] > 10.0:
        index, folders = {}, []
        try:
            # folder_names_and_paths is ComfyUI's own registry: name -> [dirs, exts]
            folders = [d for e in folder_paths.folder_names_and_paths.values() for d in e[0]]
        except Exception as e:
            logging.debug(f"[WMD] Could not enumerate model folders: {e}")
            return None
        for folder in folders:
            if not os.path.isdir(folder):
                continue
            for root, dirs, files in os.walk(folder):
                dirs[:] = [d for d in dirs if d not in ('.git',)]
                for f in files:
                    key = f.lower()
                    if key not in index or len(os.path.join(root, f)) < len(index[key]):
                        index[key] = os.path.join(root, f)
        idx['map'], idx['built_at'] = index, now
    return idx['map'].get(base.lower())


def check_model_exists(target_dir, filename):
    """Check if model file exists using ComfyUI's folder_paths system.
    This ensures we match exactly what ComfyUI can find and load."""

    # Handle subdirectories: "loras/subfolder" -> folder_type="loras", subpath="subfolder"
    # Normalize path separators
    target_dir_normalized = target_dir.replace('\\', '/')
    parts = target_dir_normalized.split('/')
    folder_type = parts[0]  # e.g., "loras"
    subpath = '/'.join(parts[1:]) if len(parts) > 1 else ''  # e.g., "qwen-image-lightning"

    # Build the full relative filename as ComfyUI sees it
    if subpath:
        relative_filename = f"{subpath}/{filename}"
    else:
        relative_filename = filename

    # Get list of directories to check (including equivalent ones)
    dirs_to_check = [folder_type]
    if folder_type in EQUIVALENT_DIRECTORIES:
        dirs_to_check = EQUIVALENT_DIRECTORIES[folder_type]

    for check_dir in dirs_to_check:
        try:
            # Use ComfyUI's get_filename_list which returns all files ComfyUI can find
            # This respects extra_model_paths.yaml and subdirectory structure
            available_files = folder_paths.get_filename_list(check_dir)

            # Check both the relative filename (with subpath) and just the filename
            filenames_to_check = [relative_filename, filename]
            for fname in filenames_to_check:
                if fname in available_files:
                    # Found it - get the full path to check size
                    full_path = folder_paths.get_full_path(check_dir, fname)
                    if full_path and os.path.exists(full_path):
                        try:
                            size_bytes = os.path.getsize(full_path)
                            size_mb = size_bytes / (1024 * 1024)
                            if size_mb >= 1024:
                                return True, f"{size_mb/1024:.2f} GB"
                            else:
                                return True, f"{size_mb:.1f} MB"
                        except Exception:
                            return True, None
        except Exception as e:
            # Fallback: folder type might not exist in ComfyUI
            logging.debug(f"[WMD] Could not check {check_dir}: {e}")
            continue

    # Global fallback: the file may exist somewhere other than the directory
    # the scan expected. This is the normal case when the curated table forces
    # a directory that the filename patterns would never guess (e.g. the 19.5GB
    # hybrid model -> diffusion_models while the scan said checkpoints), and
    # it also covers any file the user placed manually elsewhere.
    global_hit = _find_file_by_basename(filename)
    if global_hit:
        try:
            size_bytes = os.path.getsize(global_hit)
            size_mb = size_bytes / (1024 * 1024)
            size_str = f"{size_mb/1024:.2f} GB" if size_mb >= 1024 else f"{size_mb:.1f} MB"
            logging.info(
                f"[WMD] {filename} found at {global_hit} (different folder than '{target_dir}'); treating as installed"
            )
            return True, size_str
        except Exception:
            return True, None

    # Fallback: Also check by direct file path in case ComfyUI cache is stale
    try:
        if subpath:
            direct_path = os.path.join(folder_paths.models_dir, folder_type, subpath, filename)
        else:
            direct_path = os.path.join(folder_paths.models_dir, folder_type, filename)

        if os.path.exists(direct_path):
            size_bytes = os.path.getsize(direct_path)
            size_mb = size_bytes / (1024 * 1024)
            if size_mb >= 1024:
                return True, f"{size_mb/1024:.2f} GB"
            else:
                return True, f"{size_mb:.1f} MB"
    except Exception as e:
        logging.debug(f"[WMD] Direct path check failed: {e}")

    return False, None


def check_model_integrity(target_dir, filename):
    """check_model_exists + structural verification for .safetensors.

    An old crash-interrupted download can leave a final-name file that is
    truncated: it passes the naive exists check, the panel shows EXISTS,
    download is never offered, and the workflow dies at run time. The file's
    own safetensors header states its expected size, so verify after exists.
    Returns (exists, size_str, incomplete, expected_bytes).
    """
    exists, size_str = check_model_exists(target_dir, filename)
    if not exists:
        return False, size_str, False, None
    path = _find_file_by_basename(filename) or folder_paths.get_full_path(target_dir, filename)
    status, expected = verify_safetensors_file(path)
    if status in ('truncated', 'corrupt'):
        logging.warning(
            f"[WMD] {filename} at {path} is {status} "
            f"({os.path.getsize(path):,} of {expected:,} bytes) - reporting MISSING so it re-downloads"
        )
        return False, size_str, True, expected
    return True, size_str, False, expected


def scan_workflow_for_models(workflow_json):
    """Scan workflow JSON for model references"""
    if isinstance(workflow_json, str):
        try:
            workflow_data = json.loads(workflow_json)
        except Exception:
            workflow_data = {}
        content = workflow_json
    else:
        workflow_data = workflow_json
        content = json.dumps(workflow_json)

    # Skip if not a dict (e.g., index files that are lists)
    if not isinstance(workflow_data, dict):
        return []

    # First, extract models from node properties (the proper way)
    # ComfyUI stores model info in node.properties.models array
    node_models = {}  # filename -> {url, directory, node_type}

    # Collect all nodes including from subgraphs
    all_nodes = list(workflow_data.get('nodes', []))

    # Also check subgraph definitions for nested nodes
    definitions = workflow_data.get('definitions', {})
    subgraphs = definitions.get('subgraphs', [])
    for subgraph in subgraphs:
        subgraph_nodes = subgraph.get('nodes', [])
        all_nodes.extend(subgraph_nodes)

    for node in all_nodes:
        node_type = node.get('type', '')
        properties = node.get('properties', {})
        models_list = properties.get('models', [])

        for model_info in models_list:
            if isinstance(model_info, dict):
                name = model_info.get('name', '')
                url = model_info.get('url', '')
                directory = model_info.get('directory', '')

                if name and name not in node_models:
                    node_models[name] = {
                        'url': url,
                        'directory': directory,
                        'node_type': node_type
                    }

    # Also check widgets_values for model filenames
    for node in all_nodes:
        node_type = node.get('type', '')
        widgets_values = node.get('widgets_values', [])

        for value in widgets_values:
            if isinstance(value, str) and any(value.endswith(ext) for ext in ['.safetensors', '.ckpt', '.pt', '.pth', '.bin', '.onnx']):
                # Skip GGUF
                if value.lower().endswith('.gguf'):
                    continue
                if value not in node_models:
                    node_models[value] = {
                        'url': '',
                        'directory': '',
                        'node_type': node_type
                    }

    # Find model filenames via regex (fallback for markdown notes, etc.)
    model_pattern = r'([\w\-\.%]+\.(?:safetensors|ckpt|pt|pth|bin|onnx))'
    model_files_raw = re.findall(model_pattern, content)

    # Clean and deduplicate, decode URL-encoded names
    model_files = set()
    model_name_map = {}  # Map decoded name -> original name (for URL matching)

    # First, add all models from node properties (these are authoritative)
    for model_name in node_models.keys():
        if model_name and not model_name.lower().endswith('.gguf'):
            model_files.add(model_name)
            model_name_map[model_name] = model_name

    for model in model_files_raw:
        cleaned = model.strip()
        if cleaned and cleaned[0].isalnum():
            # Skip GGUF files
            if not cleaned.lower().endswith('.gguf'):
                # Decode URL-encoded characters (%2D -> -, %20 -> space, etc.)
                try:
                    decoded = urllib.parse.unquote(cleaned)
                except Exception:
                    decoded = cleaned

                model_files.add(decoded)
                model_name_map[decoded] = cleaned  # Keep original for URL matching

    # Drop regex "ghost" rows. The raw-text regex above only matches a bare
    # filename ("moss_image_sft_bf16.safetensors"), but a real node reference
    # carries a subfolder ("moss\\moss_image_sft_bf16.safetensors"). Both land
    # in model_files, so the same model appears twice - and the bare copy gets
    # a guessed directory ("checkpoints" for a .safetensors fallback) that
    # points the download at the wrong folder. Keep only the qualified
    # reference when both exist.
    by_basename = {}
    for m in model_files:
        by_basename.setdefault(os.path.basename(m).lower(), []).append(m)
    ghost_basenames = set()
    for base, variants in by_basename.items():
        if len(variants) < 2:
            continue
        qualified = [v for v in variants if os.path.dirname(v.replace('\\', '/'))]
        if qualified:
            ghost_basenames.add(base)
    if ghost_basenames:
        before = len(model_files)
        model_files = {m for m in model_files
                       if os.path.basename(m).lower() not in ghost_basenames
                       or os.path.dirname(m.replace('\\', '/'))}
        if len(model_files) != before:
            logging.info(
                f"[WMD] Dropped {before - len(model_files)} ghost model row(s): "
                f"bare filename duplicated by a folder-qualified reference"
            )

    # Find download URLs via regex
    url_pattern = r'(https?://(?:huggingface\.co|civitai\.com|github\.com)[^\s"\'<>\)]+)'
    urls = re.findall(url_pattern, content)

    # Clean URLs
    cleaned_urls = []
    for url in urls:
        url = url.split(')')[0].replace('\\n', '').replace('\n', '').strip()
        if url:
            cleaned_urls.append(url)

    # Match models with URLs - check both decoded and original (URL-encoded) names
    model_url_map = {}
    for model in model_files:
        # First check if we have URL from node properties
        if model in node_models and node_models[model].get('url'):
            model_url_map[model] = node_models[model]['url']
            continue

        original_name = model_name_map.get(model, model)

        for url in cleaned_urls:
            # Check decoded name in URL
            if model in url:
                model_url_map[model] = url
                break
            # Check original (possibly URL-encoded) name in URL
            if original_name in url:
                model_url_map[model] = url
                break
            # Check URL-encoded version of decoded name
            encoded_model = urllib.parse.quote(model, safe='')
            if encoded_model in url:
                model_url_map[model] = url
                break
            # Check without extension
            model_base = model.replace('.safetensors', '').replace('.ckpt', '')
            if model_base in url or urllib.parse.quote(model_base, safe='') in url:
                model_url_map[model] = url
                break

    # Build results
    models_data = []
    for model in sorted(model_files):
        url = model_url_map.get(model, '')

        # Get model type and directory from filename patterns
        model_type, target_dir = identify_model_type_from_filename(model)

        if model_type is None:
            continue  # Skip unknown types

        # Check if this model was found in node properties (authoritative source)
        from_node_properties = model in node_models

        # Priority 0: runtime node introspection. The node class's own
        # file-picker option list is literally get_filename_list(folder)
        # evaluated by the node - ground truth for where this file must live.
        # Beats even properties.models.directory (author-recorded, can be
        # stale/wrong, e.g. animatediff_models on a LoraLoaderModelOnly file).
        probe_node_type = node_models.get(model, {}).get('node_type', '')
        auth = authoritative_folder_for(model, probe_node_type) if probe_node_type else None
        auth_dir = auth[0] if (auth and auth[0]) else ''
        if auth_dir and target_dir != auth_dir:
            logging.info(f"[WMD] {model}: node {probe_node_type} reads from '{auth_dir}' ({auth[1]}); overriding scan directory '{target_dir}'")

        # First priority: runtime truth wins over everything
        if auth_dir:
            target_dir = auth_dir
        elif from_node_properties and node_models[model].get('directory'):
            target_dir = node_models[model]['directory']
        else:
            # Second priority: the loader node type - ComfyUI's own rule for
            # where a file must sit, because that is the folder the node reads
            # at run time (LoraLoaderModelOnly -> loras/, VAELoader -> vae/).
            # Beats URL keywords: a Civitai URL mentioning "animatediff" must
            # not send a LoRA attached to LoraLoaderModelOnly into
            # animatediff_models/ where the node would never look.
            nt_dir = NODE_TYPE_DIRECTORIES.get(
                node_models.get(model, {}).get('node_type', ''))
            # Third priority: Check URL for directory hints
            hinted = ''
            if not nt_dir and url:
                url_lower = url.lower()
                for url_path, directory in URL_DIRECTORY_HINTS.items():
                    if url_path.lower() in url_lower:
                        hinted = directory
                        break
            if not hinted:
                # No hint anywhere -> curated table FIRST (it knows where files
                # really live, e.g. hybrid -> diffusion_models), then filename
                # rules. Runs before the exists-check below so that the scan and
                # the download thread agree on where this file belongs.
                hinted = resolve_model_directory(model)
            if not target_dir or target_dir in ('', 'checkpoints', 'models'):
                # An unqualified guess (or the checkpoints fallback from a
                # generic extension match) loses to a positive hint.
                target_dir = nt_dir or hinted

        # Check for cached search metadata first
        cached_metadata = get_cached_metadata(model)

        # If no URL found, try to find one from our registries (not API search - that's manual)
        url_source = None
        if not url:
            # First check cache for previously downloaded/found URLs
            if cached_metadata and cached_metadata.get('url'):
                url = cached_metadata['url']
                url_source = cached_metadata.get('source', 'cached')
            else:
                found_url, url_source = find_model_url(model, search_apis=False)
                if found_url:
                    url = found_url

        # Extract HuggingFace info
        hf_repo, hf_path = extract_huggingface_info(url)

        # If we got URL from cache, also get hf_repo/hf_path from cache
        if cached_metadata and not hf_repo:
            hf_repo = cached_metadata.get('hf_repo', '')
            hf_path = cached_metadata.get('hf_path', '')

        # Check if model exists (with structural integrity verification:
        # truncated safetensors are reported as missing, see check_model_integrity)
        exists, local_size, incomplete, expected_bytes = check_model_integrity(target_dir, model)

        # Skip models found only via regex that have no URL and don't exist locally
        # These are likely false positives from markdown notes or comments
        if not from_node_properties and not url and not exists:
            continue

        # Get node type from node_models if available
        node_type = node_models.get(model, {}).get('node_type', '')

        # Determine source
        if hf_repo:
            source = 'HuggingFace'
        elif url and 'civitai.com' in url:
            source = 'CivitAI'
        elif url_source:
            source = url_source
        else:
            source = 'Unknown'

        # Find alternatives if model doesn't exist
        alternatives = []
        if not exists:
            alternatives = find_model_alternatives(model, target_dir)

        # List every exact-name download mirror. The panel shows these so the
        # user can choose which one to fetch - the plugin never downloads
        # without an explicit click. Only looked up for models that are
        # missing AND have no known url yet (the ones that used to say
        # "无链接"); results are cached so re-scanning is cheap.
        # NO network work here. A scan used to block for ~10s because
        # every missing model triggered a live HF lookup. The frontend now
        # resolves each missing model in the background through
        # /workflow-models/resolve-source, so the table appears instantly.
        mirrors = []
        needs_resolve = (not exists) and (not url)

        models_data.append({
            'filename': model,
            'type': model_type,
            'directory': target_dir,
            'full_path': os.path.join(folder_paths.models_dir, target_dir, model),
            # Where the file ACTUALLY lives (subdirectory-transparent search);
            # empty when missing. UI uses this for the "open folder" button.
            # Also resolved for incomplete files so the folder button can
            # reveal the truncated remnant the user may want to delete.
            'actual_path': (_find_file_by_basename(model) or '') if (exists or incomplete) else '',
            'exists': exists,
            # truncated/corrupt file on disk: exists was True but unusable
            'incomplete': incomplete,
            'expected_size': expected_bytes,
            'local_size': local_size or '',
            'source': source,
            'url': url,
            'hf_repo': hf_repo or '',
            'hf_path': hf_path or '',
            'node_type': node_type,
            'url_source': url_source or ('workflow' if url else None),
            'search_metadata': cached_metadata,
            'alternatives': alternatives,
            # True when this row still needs a source lookup (the frontend
            # fires resolve-source for it, in the background)
            'needs_resolve': needs_resolve,
            # every exact-name mirror the user can pick from
            'mirrors': [{
                'url': m.get('url', ''),
                'repo': m.get('repo', ''),
                'path': m.get('path', ''),
                'directory': m.get('directory', ''),
                'size': m.get('size', 0),
                'size_str': format_size(m.get('size', 0)) if m.get('size') else '',
                'source': m.get('source', ''),
                'likes': m.get('likes', 0),
                'downloads': m.get('downloads', 0),
                'official': m.get('official', False),
                'size_match': m.get('size_match'),
                'dir_match': m.get('dir_match'),
                'score': m.get('score', 0),
                'evidence': m.get('evidence', []),
                'confirmed': m.get('confirmed', False),
            } for m in mirrors]
        })

        # Save URL to model_metadata.json if found (so Local Browser can see it)
        if url and not cached_metadata:
            save_search_metadata(model, {
                'url': url,
                'source': url_source or ('workflow' if url else None),
                'hf_repo': hf_repo or '',
                'hf_path': hf_path or '',
                'model_type': model_type,
                'directory': target_dir
            })

    return models_data


# =============================================================================
# API ENDPOINTS
# =============================================================================

@routes.post("/workflow-models/source-candidates")
async def get_source_candidates(request):
    """List every candidate download source for a workflow model, ranked by speed.

    Body: {filename, directory?, auto_select?}
    auto_select=true  -> probes each candidate's speed and ranks fastest-first
    auto_select=false -> returns the candidate list without probing

    When the workflow filename has NO exact-name source anywhere, `best` is null
    and `suggestions` carries same-family alternatives for the user to pick from.
    """
    try:
        data = await request.json()
        filename = data.get('filename', '')
        directory = data.get('directory', '')
        auto_select = data.get('auto_select', True)

        if not filename:
            return web.json_response({'error': '未提供文件名'}, status=400)

        best, ranked = find_model_url_with_sources(filename, directory, auto_select)

        out = {
            'success': True,
            'filename': filename,
            'best': best,
            'candidates': ranked if ranked else collect_source_candidates(filename, directory),
        }

        # No exact-name match anywhere -> offer same-family alternatives
        if not best:
            out['suggestions'] = suggest_alternative_models(filename, directory)
        else:
            out['suggestions'] = []

        return web.json_response(out)
    except Exception as e:
        logging.error(f"[WMD] source-candidates error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/resolve-source")
async def resolve_source(request):
    """Resolve the reliable download source for ONE model.

    Body: {filename, directory?}

    Runs the full candidate search, scores every candidate on objective
    evidence (size match, directory layout, official namespace), picks the
    best one and persists it. The frontend calls this in parallel for each
    missing model, so the workflow scan itself stays instant.

    Returns:
      {success, best: {...}, candidates: [...], reason, confirmed}
    """
    try:
        data = await request.json()
        filename = data.get('filename', '')
        directory = data.get('directory', '')
        if not filename:
            return web.json_response({'error': '未提供文件名'}, status=400)

        cands = collect_source_candidates(filename, directory)
        if not cands:
            return web.json_response({
                'success': True,
                'filename': filename,
                'best': None,
                'candidates': [],
                'reason': '没有找到任何同名来源',
                'confirmed': False,
            })

        best, reason = select_reliable_source(cands, filename, directory)

        # Remember it: the next scan shows a plain download button and
        # needs no lookup at all.
        if best and best.get('url'):
            save_search_metadata(filename, {
                'url': best['url'],
                'source': 'auto_verified',
                'hf_repo': best.get('repo') or best.get('hf_repo') or '',
                'hf_path': best.get('path') or best.get('hf_path') or '',
                'directory': directory or best.get('directory', ''),
                'size': best.get('remote_size') or best.get('size') or 0,
                'verified': bool(best.get('confirmed')),
                'reason': reason,
            })

        def _pack(m):
            return {
                'url': m.get('url', ''),
                'repo': m.get('repo', ''),
                'path': m.get('path', ''),
                'directory': m.get('directory', ''),
                'size': m.get('size', 0),
                'size_str': format_size(m.get('size', 0)) if m.get('size') else '',
                'source': m.get('source', ''),
                'likes': m.get('likes', 0),
                'downloads': m.get('downloads', 0),
                'official': m.get('official', False),
                'size_match': m.get('size_match'),
                'dir_match': m.get('dir_match'),
                'score': m.get('score', 0),
                'evidence': m.get('evidence', []),
                'confirmed': m.get('confirmed', False),
            }

        return web.json_response({
            'success': True,
            'filename': filename,
            'best': _pack(best) if best else None,
            'candidates': [_pack(c) for c in cands],
            'reason': reason,
            'confirmed': bool(best.get('confirmed')) if best else False,
        })
    except Exception as e:
        logging.error(f"[WMD] resolve-source error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/find-mirrors")
async def find_mirrors(request):
    """List every exact-name download mirror for a workflow model.

    Body: {filename, directory?}

    Returns the candidate list WITHOUT downloading anything - the panel shows
    the mirrors (repo, size, source) and the user picks which one to fetch.
    """
    try:
        data = await request.json()
        filename = data.get('filename', '')
        directory = data.get('directory', '')
        if not filename:
            return web.json_response({'error': '未提供文件名'}, status=400)

        cands = collect_source_candidates(filename, directory)

        out = []
        for c in cands:
            size = c.get('size', 0) or 0
            out.append({
                'url': c.get('url', ''),
                'source': c.get('source', ''),
                'repo': c.get('repo', '') or c.get('hf_repo', ''),
                'path': c.get('path', '') or c.get('hf_path', ''),
                'directory': c.get('directory', '') or directory,
                'size': size,
                'size_str': format_size(size) if size else '',
                'likes': c.get('likes', 0) or 0,
                'downloads': c.get('downloads', 0) or 0,
                'hf_repo': c.get('hf_repo', '') or c.get('repo', ''),
                'hf_path': c.get('hf_path', '') or c.get('path', ''),
                'official': c.get('official', False),
                'size_match': c.get('size_match'),
                'dir_match': c.get('dir_match'),
                'score': c.get('score', 0),
                'evidence': c.get('evidence', []),
                'confirmed': c.get('confirmed', False),
            })

        return web.json_response({
            'success': True,
            'filename': filename,
            'candidates': out,
        })
    except Exception as e:
        logging.error(f"[WMD] find-mirrors error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/suggest-alternatives")
async def suggest_alternatives_endpoint(request):
    """Same-family model alternatives when the exact filename has no source."""
    try:
        data = await request.json()
        filename = data.get('filename', '')
        directory = data.get('directory', '')
        if not filename:
            return web.json_response({'error': '未提供文件名'}, status=400)
        return web.json_response({
            'success': True,
            'filename': filename,
            'suggestions': suggest_alternative_models(filename, directory),
        })
    except Exception as e:
        logging.error(f"[WMD] suggest-alternatives error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/scan")
async def scan_workflow(request):
    """Scan the provided workflow JSON for models"""
    try:
        data = await request.json()
        workflow = data.get('workflow', {})

        if not workflow:
            return web.json_response({'error': '未提供工作流'}, status=400)

        models = scan_workflow_for_models(workflow)

        # Calculate summary
        total = len(models)
        existing = sum(1 for m in models if m['exists'])
        missing = total - existing
        hf_models = [m for m in models if m['source'] == 'HuggingFace']

        return web.json_response({
            'success': True,
            'models': models,
            'summary': {
                'total': total,
                'existing': existing,
                'missing': missing,
                'hf_downloadable': len([m for m in hf_models if not m['exists']])
            }
        })
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Scan error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/directories")
async def get_available_directories(request):
    """Get available model directories including extra_model_paths"""
    try:
        # Standard model folder types (curated list)
        all_types = set([
            'checkpoints', 'clip', 'clip_vision', 'controlnet', 'diffusion_models',
            'embeddings', 'gligen', 'hypernetworks', 'ipadapter', 'loras',
            'style_models', 'text_encoders', 'unet', 'upscale_models', 'vae',
            'photomaker', 'instantid', 'pulid', 'sams', 'animatediff_models',
            'ultralytics', 'mmdets', 'onnx', 'reactor', 'facerestore_models',
            'facedetection', 'liveportrait', 'inpaint', 'xlabs', 'LLM',
            'llm_gguf', 'CogVideo', 'blip'
        ])

        # Folders to exclude (not model folders)
        EXCLUDED_FOLDERS = {
            'custom_nodes', 'configs', 'fonts', 'kjnodes_fonts', 'web', 'js',
            'user', 'input', 'output', 'temp', 'models', 'pycache'
        }

        # Add custom folder types, excluding non-model folders
        if hasattr(folder_paths, 'folder_names_and_paths'):
            for folder_type in folder_paths.folder_names_and_paths.keys():
                if folder_type.lower() in EXCLUDED_FOLDERS:
                    continue
                if any(x in folder_type.lower() for x in ['pycache', '_cache', 'config', 'font']):
                    continue
                all_types.add(folder_type)

        available = []
        for folder_type in all_types:
            try:
                paths = folder_paths.get_folder_paths(folder_type)
                if paths:
                    available.append({
                        'name': folder_type,
                        'paths': paths,
                        'has_extra_paths': len(paths) > 1
                    })
            except Exception:
                # Folder type not registered, skip
                pass

        # Sort alphabetically
        available.sort(key=lambda x: x['name'])

        return web.json_response({
            'success': True,
            'directories': available
        })
    except Exception as e:
        logging.error(f"[WMD] Get directories error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/debug-paths")
async def debug_model_paths(request):
    """Debug endpoint to show all model paths ComfyUI knows about"""
    try:
        paths_info = {}
        folder_types = ['checkpoints', 'loras', 'vae', 'controlnet', 'clip', 'text_encoders',
                        'diffusion_models', 'unet', 'embeddings', 'upscale_models', 'clip_vision']

        for folder_type in folder_types:
            try:
                paths = folder_paths.get_folder_paths(folder_type)
                file_count = len(folder_paths.get_filename_list(folder_type))
                paths_info[folder_type] = {
                    'paths': paths,
                    'file_count': file_count
                }
            except Exception as e:
                paths_info[folder_type] = {
                    'paths': [],
                    'file_count': 0,
                    'error': str(e)
                }

        return web.json_response({
            'success': True,
            'models_dir': folder_paths.models_dir,
            'folder_paths': paths_info
        })
    except Exception as e:
        logging.error(f"[WMD] Debug paths error: {e}")
        return web.json_response({'error': str(e)}, status=500)


# Track which models have been used in workflows (for unused detection)
used_models_tracking = {}  # filename -> { last_used: timestamp, workflows: [list of workflow names] }

# Cache file path
USAGE_CACHE_FILE = os.path.join(os.path.dirname(__file__), "usage_cache.json")


def load_usage_cache():
    """Load usage tracking from persistent cache"""
    global used_models_tracking
    try:
        if os.path.exists(USAGE_CACHE_FILE):
            with open(USAGE_CACHE_FILE, 'r', encoding='utf-8') as f:
                used_models_tracking = json.load(f)
            logging.info(f"[WMD] Loaded usage cache with {len(used_models_tracking)} models")
    except Exception as e:
        logging.error(f"[WMD] Error loading usage cache: {e}")
        used_models_tracking = {}


def save_usage_cache():
    """Save usage tracking to persistent cache"""
    global used_models_tracking
    try:
        with open(USAGE_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(used_models_tracking, f, indent=2)
        logging.info(f"[WMD] Saved usage cache with {len(used_models_tracking)} models")
    except Exception as e:
        logging.error(f"[WMD] Error saving usage cache: {e}")


# Load cache on module import
load_usage_cache()

# Initialize max_parallel_downloads from settings
def _init_parallel_downloads():
    global max_parallel_downloads
    try:
        settings = load_settings()
        max_parallel_downloads = settings.get('max_parallel_downloads', 3)
        logging.info(f"[WMD] Max parallel downloads set to: {max_parallel_downloads}")
    except Exception as e:
        logging.debug(f"[WMD] Could not load max_parallel_downloads from settings: {e}")

_init_parallel_downloads()


def extract_models_from_workflow(workflow_data):
    """Extract model filenames from a workflow JSON"""
    models = set()

    # Skip if not a dict (e.g., index files that are lists)
    if not isinstance(workflow_data, dict):
        return models

    def extract_from_node(node):
        if isinstance(node, dict):
            widgets = node.get('widgets_values', [])
            if widgets:
                for val in widgets:
                    if isinstance(val, str) and (val.endswith('.safetensors') or val.endswith('.ckpt') or
                                                   val.endswith('.pt') or val.endswith('.pth') or
                                                   val.endswith('.bin') or val.endswith('.gguf')):
                        models.add(val)

    # Handle both graph format and API format
    nodes = workflow_data.get('nodes', [])
    if isinstance(nodes, list):
        for node in nodes:
            extract_from_node(node)

    # Also check 'workflow' key if present (embedded in PNG metadata)
    if 'workflow' in workflow_data:
        nested = workflow_data['workflow']
        if isinstance(nested, dict) and 'nodes' in nested:
            for node in nested.get('nodes', []):
                extract_from_node(node)

    return models


@routes.post("/workflow-models/track-usage")
async def track_model_usage(request):
    """Track which models are used in the current workflow"""
    global used_models_tracking

    try:
        data = await request.json()
        models = data.get('models', [])
        workflow_name = data.get('workflow_name', 'current')

        timestamp = time.time()
        for model in models:
            filename = model.get('filename', '')
            if filename:
                if filename not in used_models_tracking:
                    used_models_tracking[filename] = {'last_used': timestamp, 'workflows': []}
                else:
                    used_models_tracking[filename]['last_used'] = timestamp

                # Add workflow to list if not already there
                workflows = used_models_tracking[filename].get('workflows', [])
                if workflow_name and workflow_name not in workflows:
                    workflows.append(workflow_name)
                    used_models_tracking[filename]['workflows'] = workflows[-10:]  # Keep last 10

        # Save to persistent cache
        save_usage_cache()

        return web.json_response({'success': True, 'tracked': len(models)})
    except Exception as e:
        logging.error(f"[WMD] Track usage error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/unused")
async def get_unused_models(request):
    """Get list of installed models that haven't been used recently"""
    global used_models_tracking

    try:
        # Get all installed models
        all_models = []
        folder_types = ['checkpoints', 'loras', 'vae', 'controlnet', 'clip', 'text_encoders',
                        'diffusion_models', 'unet', 'embeddings', 'upscale_models']

        for folder_type in folder_types:
            try:
                files = folder_paths.get_filename_list(folder_type)
                for filename in files:
                    full_path = folder_paths.get_full_path(folder_type, filename)
                    size_str = None
                    modified_time = None

                    if full_path and os.path.exists(full_path):
                        try:
                            stat = os.stat(full_path)
                            size_bytes = stat.st_size
                            size_mb = size_bytes / (1024 * 1024)
                            if size_mb >= 1024:
                                size_str = f"{size_mb/1024:.2f} GB"
                            else:
                                size_str = f"{size_mb:.1f} MB"
                            modified_time = stat.st_mtime
                        except Exception:
                            pass

                    # Check if model was used (handle both old and new format)
                    usage_info = used_models_tracking.get(filename)
                    if isinstance(usage_info, dict):
                        is_used = True
                        last_used = usage_info.get('last_used')
                        workflows = usage_info.get('workflows', [])
                    elif usage_info is not None:
                        # Old format (just timestamp)
                        is_used = True
                        last_used = usage_info
                        workflows = []
                    else:
                        is_used = False
                        last_used = None
                        workflows = []

                    all_models.append({
                        'filename': filename,
                        'type': folder_type,
                        'size': size_str,
                        'modified': modified_time,
                        'last_used': last_used,
                        'is_used': is_used,
                        'workflows': workflows
                    })
            except Exception:
                pass

        # Sort by unused first, then by size (largest first)
        unused_models = [m for m in all_models if not m['is_used']]
        unused_models.sort(key=lambda x: -(x.get('modified') or 0))

        return web.json_response({
            'success': True,
            'total_models': len(all_models),
            'unused_count': len(unused_models),
            'unused_models': unused_models,
            'tracked_count': len(used_models_tracking)
        })
    except Exception as e:
        logging.error(f"[WMD] Unused models error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/scan-all-workflows")
async def scan_all_workflows(request):
    """Scan all workflow files in a directory to build usage cache"""
    global used_models_tracking

    try:
        data = await request.json()
        directory = data.get('directory', '')

        # Default to ComfyUI user directory
        if not directory:
            directory = os.path.join(folder_paths.base_path, 'user', 'default', 'workflows')
            if not os.path.exists(directory):
                # Try output directory
                directory = os.path.join(folder_paths.base_path, 'output')

        if not os.path.exists(directory):
            return web.json_response({
                'error': f'目录不存在：{directory}',
                'default_path': directory
            }, status=400)

        # Find all JSON files
        json_files = glob.glob(os.path.join(directory, '**', '*.json'), recursive=True)
        logging.info(f"[WMD] Scanning {len(json_files)} workflow files in {directory}")

        scanned = 0
        errors = 0
        models_found = set()
        timestamp = time.time()

        for filepath in json_files:
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    workflow_data = json.load(f)

                # Extract models from workflow
                workflow_models = extract_models_from_workflow(workflow_data)
                workflow_name = os.path.basename(filepath)

                for model in workflow_models:
                    models_found.add(model)
                    if model not in used_models_tracking:
                        used_models_tracking[model] = {'last_used': timestamp, 'workflows': []}

                    workflows = used_models_tracking[model].get('workflows', [])
                    if workflow_name not in workflows:
                        workflows.append(workflow_name)
                        used_models_tracking[model]['workflows'] = workflows[-10:]  # Keep last 10
                    used_models_tracking[model]['last_used'] = timestamp

                scanned += 1
            except Exception as e:
                logging.debug(f"[WMD] Error scanning {filepath}: {e}")
                errors += 1

        # Save to persistent cache
        save_usage_cache()

        return web.json_response({
            'success': True,
            'directory': directory,
            'workflows_scanned': scanned,
            'workflows_errored': errors,
            'models_found': len(models_found),
            'total_tracked': len(used_models_tracking)
        })
    except Exception as e:
        logging.error(f"[WMD] Scan all workflows error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/clear-cache")
async def clear_usage_cache(request):
    """Clear the usage tracking cache"""
    global used_models_tracking

    try:
        used_models_tracking = {}

        # Delete cache file
        if os.path.exists(USAGE_CACHE_FILE):
            os.remove(USAGE_CACHE_FILE)
            logging.info("[WMD] Usage cache cleared and file deleted")

        return web.json_response({
            'success': True,
            'message': '使用缓存已清理'
        })
    except Exception as e:
        logging.error(f"[WMD] Clear cache error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/cache-info")
async def get_cache_info(request):
    """Get information about the usage cache"""
    global used_models_tracking

    try:
        cache_size = 0
        if os.path.exists(USAGE_CACHE_FILE):
            cache_size = os.path.getsize(USAGE_CACHE_FILE)

        # Get default workflow directories
        default_dirs = []
        user_workflows = os.path.join(folder_paths.base_path, 'user', 'default', 'workflows')
        if os.path.exists(user_workflows):
            default_dirs.append(user_workflows)

        output_dir = os.path.join(folder_paths.base_path, 'output')
        if os.path.exists(output_dir):
            default_dirs.append(output_dir)

        return web.json_response({
            'success': True,
            'tracked_models': len(used_models_tracking),
            'cache_file': USAGE_CACHE_FILE,
            'cache_size_bytes': cache_size,
            'default_directories': default_dirs
        })
    except Exception as e:
        logging.error(f"[WMD] Cache info error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/check-exists")
async def check_model_exists_endpoint(request):
    """Check if a model exists in a specific directory"""
    try:
        data = await request.json()
        filename = data.get('filename')
        directory = data.get('directory')

        if not filename or not directory:
            return web.json_response({'error': '缺少文件名或目录'}, status=400)

        exists, size, incomplete, expected_bytes = check_model_integrity(directory, filename)

        return web.json_response({
            'exists': exists,
            'size': size or '',
            'incomplete': incomplete,
            'expected_size': expected_bytes
        })
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Check exists error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/search-url")
async def search_model_url(request):
    """Search for model URL using HuggingFace and CivitAI APIs"""
    try:
        data = await request.json()
        filename = data.get('filename')

        if not filename:
            return web.json_response({'error': '缺少文件名'}, status=400)

        # Search with API calls enabled
        url, source = find_model_url(filename, search_apis=True)

        if url:
            # Extract HuggingFace info if applicable
            hf_repo, hf_path = extract_huggingface_info(url)

            return web.json_response({
                'success': True,
                'url': url,
                'source': source,
                'hf_repo': hf_repo or '',
                'hf_path': hf_path or ''
            })
        else:
            return web.json_response({
                'success': False,
                'message': '未找到该模型的链接'
            })
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Search URL error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/list-workflows")
async def list_workflows(request):
    """List all workflow files from default workflow directories"""
    try:
        workflows = []
        seen_paths = set()

        # Default workflow directories
        workflow_dirs = []

        user_workflows = os.path.join(folder_paths.base_path, 'user', 'default', 'workflows')
        if os.path.exists(user_workflows):
            workflow_dirs.append(('user/default/workflows', user_workflows))

        root_workflows = os.path.join(folder_paths.base_path, 'workflows')
        if os.path.exists(root_workflows):
            workflow_dirs.append(('workflows', root_workflows))

        # Scan directories
        for base_name, dir_path in workflow_dirs:
            for root, dirs, files in os.walk(dir_path):
                for file in files:
                    if file.endswith('.json'):
                        full_path = os.path.join(root, file)

                        # Skip if already seen (deduplicate)
                        norm_full = os.path.normpath(full_path)
                        if norm_full in seen_paths:
                            continue
                        seen_paths.add(norm_full)

                        rel_path = os.path.relpath(full_path, dir_path)
                        try:
                            stat = os.stat(full_path)
                            workflows.append({
                                'name': file,
                                'path': full_path,
                                'relative_path': rel_path,
                                'folder': base_name,
                                'size': stat.st_size,
                                'modified': stat.st_mtime
                            })
                        except Exception as e:
                            logging.debug(f"[WMD] Error reading workflow stats: {e}")

        # Sort by modified time (newest first)
        workflows.sort(key=lambda x: x.get('modified', 0), reverse=True)

        return web.json_response({
            'success': True,
            'workflows': workflows
        })
    except Exception as e:
        logging.error(f"[WMD] List workflows error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/parse-workflow")
async def parse_workflow(request):
    """Parse a workflow file and extract model information with details"""
    try:
        data = await request.json()
        workflow_path = data.get('path')

        if not workflow_path or not os.path.exists(workflow_path):
            return web.json_response({'error': '无效的工作流路径'}, status=400)

        # Read and parse workflow
        with open(workflow_path, 'r', encoding='utf-8') as f:
            workflow_content = f.read()

        try:
            workflow_data = json.loads(workflow_content)
        except Exception:
            return web.json_response({'error': '工作流文件 JSON 无效'}, status=400)

        logging.debug(f"[WMD] Parsing workflow: {workflow_path}")

        # Use the same scan_workflow_for_models function as the main Workflow Model tab
        # This already returns all the data we need including existence check, URLs, and alternatives
        scanned_models = scan_workflow_for_models(workflow_content)

        # Map node_type to node_class for consistency with frontend
        models = []
        for model_info in scanned_models:
            model_info['node_class'] = model_info.get('node_type', '')
            models.append(model_info)

        return web.json_response({
            'success': True,
            'models': models,
            'workflow_name': os.path.basename(workflow_path)
        })
    except json.JSONDecodeError as e:
        return web.json_response({'error': f'Invalid JSON: {e}'}, status=400)
    except Exception as e:
        logging.error(f"[WMD] Parse workflow error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/advanced-search")
async def advanced_search(request):
    """Search for model URL using Tavily API (advanced search)"""
    try:
        data = await request.json()
        filename = data.get('filename')

        if not filename:
            return web.json_response({'error': '缺少文件名'}, status=400)

        # Check if Tavily is configured
        if not get_tavily_api_key():
            return web.json_response({
                'success': False,
                'error': '未配置 Tavily API 密钥'
            }, status=400)

        # Check cache first
        cached = get_cached_metadata(filename)
        if cached and cached.get('url'):
            logging.info(f"[Workflow-Models-Downloader] Using cached result for: {filename}")
            return web.json_response({
                'success': True,
                'url': cached['url'],
                'source': cached.get('source', 'cached'),
                'hf_repo': cached.get('hf_repo', ''),
                'hf_path': cached.get('hf_path', ''),
                'model_name': cached.get('model_name', ''),
                'civitai_url': cached.get('civitai_url', ''),
                'from_cache': True,
                'metadata': cached
            })

        # Search with Tavily
        result = search_tavily_api(filename)

        if result:
            if result.get('url'):
                # Direct URL found
                hf_repo, hf_path = extract_huggingface_info(result['url'])

                # Build metadata to cache
                metadata = {
                    'url': result['url'],
                    'source': result.get('source', 'tavily'),
                    'hf_repo': hf_repo or result.get('repo', ''),
                    'hf_path': hf_path or '',
                    'model_name': result.get('model_name', ''),
                    'civitai_url': result.get('civitai_url', ''),
                    'search_method': 'tavily_advanced'
                }

                # Save to cache
                save_search_metadata(filename, metadata)

                return web.json_response({
                    'success': True,
                    'url': result['url'],
                    'source': result.get('source', 'tavily'),
                    'hf_repo': hf_repo or result.get('repo', ''),
                    'hf_path': hf_path or '',
                    'model_name': result.get('model_name', ''),
                    'civitai_url': result.get('civitai_url', ''),
                    'metadata': metadata
                })
            elif result.get('results'):
                # Return suggestions for user to choose
                suggestions = []
                for r in result['results']:
                    suggestions.append({
                        'title': r.get('title', ''),
                        'url': r.get('url', ''),
                        'snippet': r.get('content', '')[:200] if r.get('content') else ''
                    })

                # Cache suggestions too
                metadata = {
                    'suggestions': suggestions,
                    'search_method': 'tavily_suggestions'
                }
                save_search_metadata(filename, metadata)

                return web.json_response({
                    'success': True,
                    'suggestions': suggestions,
                    'message': '未找到直链，以下是一些相关结果'
                })

        return web.json_response({
            'success': False,
            'message': '未找到该模型的结果'
        })
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Advanced search error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/search-alternatives")
async def search_alternatives(request):
    """Search for model alternatives, sources, and GGUF versions using Tavily API"""
    try:
        data = await request.json()
        search_name = data.get('filename')  # Already extracted filename from frontend
        original_filename = data.get('original_filename', search_name)  # Full path for caching
        search_gguf = data.get('search_gguf', False)

        if not search_name:
            return web.json_response({'error': '缺少文件名'}, status=400)

        logging.info(f"[WMD] Search name: {search_name}, Original: {original_filename}")

        tavily_key = get_tavily_api_key()
        if not tavily_key:
            return web.json_response({
                'success': False,
                'error': '未配置 Tavily API 密钥'
            }, status=400)

        # Use tavily package
        try:
            from tavily import TavilyClient
        except ImportError:
            return web.json_response({
                'success': False,
                'error': '未安装 Tavily 包，请运行：pip install tavily-python'
            }, status=400)

        client = TavilyClient(tavily_key)

        results = []

        # Single comprehensive search using just the filename (no path)
        try:
            response = client.search(
                query=search_name,
                search_depth="advanced",
                max_results=10
            )

            for r in response.get('results', []):
                results.append({
                    'title': r.get('title', ''),
                    'url': r.get('url', ''),
                    'content': (r.get('content', '') or '')[:300],
                    'score': r.get('score', 0)
                })

            logging.info(f"[WMD] Found {len(results)} results")

        except Exception as e:
            logging.error(f"[WMD] Tavily search error: {e}")
            return web.json_response({
                'success': False,
                'error': str(e)
            }, status=500)

        result_data = {
            'success': True,
            'results': results,
            'query': search_name
        }

        # Cache the results using original filename as key
        set_tavily_cached_result(original_filename, result_data)

        return web.json_response(result_data)

    except Exception as e:
        logging.error(f"[WMD] Search alternatives error: {e}")
        return web.json_response({'error': str(e)}, status=500)


async def fetch_download_url_from_page(url, filename):
    """Fetch a page and try to find the actual download URL for the filename"""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as response:
                if response.status != 200:
                    return None

                html = await response.text()
                filename_lower = filename.lower()

                # For HuggingFace blob pages, convert to resolve URL
                if 'huggingface.co' in url and '/blob/' in url:
                    download_url = url.replace('/blob/', '/resolve/')
                    return download_url

                # For HuggingFace tree pages, look for the file link
                if 'huggingface.co' in url and '/tree/' in url:
                    # Look for links containing the filename
                    pattern = rf'href="([^"]*{re.escape(filename)}[^"]*)"'
                    matches = re.findall(pattern, html, re.IGNORECASE)
                    for match in matches:
                        if '/blob/' in match or '/resolve/' in match:
                            full_url = match if match.startswith('http') else f"https://huggingface.co{match}"
                            return full_url.replace('/blob/', '/resolve/')

                # For CivitAI model pages, try to find download link
                if 'civitai.com' in url:
                    # Look for model version ID
                    version_match = re.search(r'modelVersionId[=:](\d+)', html)
                    if version_match:
                        version_id = version_match.group(1)
                        return f"https://civitai.com/api/download/models/{version_id}"

                    # Look for download button/link
                    download_match = re.search(r'href="(/api/download/models/\d+[^"]*)"', html)
                    if download_match:
                        return f"https://civitai.com{download_match.group(1)}"

                # For GitHub releases, look for asset links
                if 'github.com' in url and '/releases/' in url:
                    pattern = rf'href="([^"]*releases/download[^"]*{re.escape(filename)}[^"]*)"'
                    matches = re.findall(pattern, html, re.IGNORECASE)
                    if matches:
                        match = matches[0]
                        return match if match.startswith('http') else f"https://github.com{match}"

    except Exception as e:
        logging.error(f"[WMD] Error fetching page {url}: {e}")

    return None


@routes.post("/workflow-models/extract-source")
async def extract_source_from_results(request):
    """Extract the best download source from search results and update model metadata"""
    try:
        data = await request.json()
        filename = data.get('filename')  # The model filename (just name, no path)
        original_filename = data.get('original_filename', filename)  # Full path for metadata key
        results = data.get('results', [])
        model_type = data.get('model_type', 'unknown')
        directory = data.get('directory', '')

        if not filename or not results:
            return web.json_response({'success': False, 'error': '缺少文件名或搜索结果'}, status=400)

        logging.info(f"[WMD] Extracting source for: {filename} from {len(results)} results")

        best_source = None
        best_score = 0
        filename_lower = filename.lower()
        filename_base = filename.rsplit('.', 1)[0].lower()  # Remove extension

        for result in results:
            url = result.get('url', '')
            title = result.get('title', '')
            content = result.get('content', '')
            score = 0

            # Skip non-relevant URLs
            if not url:
                continue

            url_lower = url.lower()
            url_decoded = urllib.parse.unquote(url_lower)

            # HIGHEST PRIORITY: Exact filename in URL (direct download link)
            if filename_lower in url_decoded or filename_lower in url_lower:
                score += 200  # Very high score for exact match
                logging.info(f"[WMD] Exact filename match in URL: {url} (+200)")
            elif filename_base in url_decoded or filename_base in url_lower:
                score += 150  # High score for base name match
                logging.info(f"[WMD] Base filename match in URL: {url} (+150)")

            # Check if URL ends with the filename (strongest indicator of direct link)
            if url_decoded.endswith(filename_lower) or url_lower.endswith(filename_lower):
                score += 100
                logging.info(f"[WMD] URL ends with filename: {url} (+100)")

            # Prioritize known model hosting sites
            if 'huggingface.co' in url_lower:
                score += 30
                if '/resolve/' in url_lower:
                    score += 50  # Direct download link
                elif '/blob/' in url_lower:
                    score += 40  # File viewer (can convert to download)
                elif '/tree/' in url_lower:
                    score += 10  # Directory listing
            elif 'civitai.com' in url_lower:
                score += 25
                if '/models/' in url_lower:
                    score += 20
                if 'modelVersionId' in url_lower or '/api/' in url_lower:
                    score += 30  # API/download link
            elif 'github.com' in url_lower:
                score += 15
                if '/releases/' in url_lower:
                    score += 25

            # Filename in content is a weak signal
            if filename_lower in (content or '').lower():
                score += 10

            # Penalize non-download pages
            if '/discussions/' in url_lower or '/issues/' in url_lower:
                score -= 50
            if '/wiki/' in url_lower or '/readme' in url_lower:
                score -= 30
            if 'reddit.com' in url_lower or 'twitter.com' in url_lower or 'x.com' in url_lower:
                score -= 100
            if 'youtube.com' in url_lower or 'medium.com' in url_lower:
                score -= 80

            logging.info(f"[WMD] Score for {url[:80]}...: {score}")

            if score > best_score:
                best_score = score
                best_source = {
                    'url': url,
                    'title': title,
                    'score': score
                }

        if not best_source or best_score < 30:
            return web.json_response({
                'success': False,
                'message': '搜索结果中没有可靠来源'
            })

        # Extract metadata from the best source URL
        url = best_source['url']

        # Try to fetch the actual download URL from the page
        logging.info(f"[WMD] Visiting page to extract download URL: {url}")
        actual_download_url = await fetch_download_url_from_page(url, filename)
        if actual_download_url:
            logging.info(f"[WMD] Found actual download URL: {actual_download_url}")
            url = actual_download_url

        metadata = {
            'url': url,
            'source': 'tavily_search',
            'model_type': model_type,
            'directory': directory,
            'filename': filename,
            'search_score': best_score,
            'original_search_url': best_source['url']  # Keep track of original
        }

        # Parse HuggingFace URLs
        if 'huggingface.co' in url:
            # https://huggingface.co/owner/repo/resolve/main/path/to/file.safetensors
            # https://huggingface.co/owner/repo/blob/main/path/to/file.safetensors
            hf_match = re.search(r'huggingface\.co/([^/]+/[^/]+)(?:/(?:resolve|blob)/[^/]+)?(?:/(.+))?', url)
            if hf_match:
                metadata['hf_repo'] = hf_match.group(1)
                if hf_match.group(2):
                    metadata['hf_path'] = hf_match.group(2)
                # Construct direct download URL
                if metadata.get('hf_path'):
                    metadata['url'] = f"https://huggingface.co/{metadata['hf_repo']}/resolve/main/{metadata['hf_path']}"
                else:
                    # Try to construct URL with filename
                    metadata['url'] = f"https://huggingface.co/{metadata['hf_repo']}/resolve/main/{filename}"
                    metadata['hf_path'] = filename

        # Parse CivitAI URLs
        elif 'civitai.com' in url:
            civit_match = re.search(r'civitai\.com/models/(\d+)', url)
            if civit_match:
                metadata['civitai_model_id'] = civit_match.group(1)
                metadata['civitai_url'] = url

        # Save to model metadata
        save_search_metadata(filename, metadata)

        logging.info(f"[WMD] Extracted source for {filename}: {metadata.get('url')} (score: {best_score})")

        return web.json_response({
            'success': True,
            'metadata': metadata,
            'score': best_score
        })

    except Exception as e:
        logging.error(f"[WMD] Extract source error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/save-model-source")
async def save_model_source(request):
    """Manually save a source URL for a model"""
    try:
        data = await request.json()
        filename = data.get('filename')
        url = data.get('url')
        model_type = data.get('model_type', 'unknown')
        directory = data.get('directory', '')

        if not filename or not url:
            return web.json_response({'success': False, 'error': '缺少文件名或链接'}, status=400)

        logging.info(f"[WMD] Saving source for {filename}: {url}")

        metadata = {
            'url': url,
            'source': 'manual',
            'model_type': model_type,
            'directory': directory,
            'filename': filename
        }

        # Parse HuggingFace URLs
        if 'huggingface.co' in url:
            hf_match = re.search(r'huggingface\.co/([^/]+/[^/]+)(?:/(?:resolve|blob)/[^/]+)?(?:/(.+))?', url)
            if hf_match:
                metadata['hf_repo'] = hf_match.group(1)
                if hf_match.group(2):
                    metadata['hf_path'] = hf_match.group(2)
                # Construct direct download URL
                if '/blob/' in url:
                    metadata['url'] = url.replace('/blob/', '/resolve/')

        # Parse CivitAI URLs
        elif 'civitai.com' in url:
            civit_match = re.search(r'civitai\.com/models/(\d+)', url)
            if civit_match:
                metadata['civitai_model_id'] = civit_match.group(1)
                metadata['civitai_url'] = url

        # Save to model metadata
        save_search_metadata(filename, metadata)

        return web.json_response({
            'success': True,
            'metadata': metadata
        })

    except Exception as e:
        logging.error(f"[WMD] Save source error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/tavily-cache/{filename}")
async def get_tavily_cache(request):
    """Get cached Tavily search results for a filename"""
    try:
        filename = request.match_info['filename']
        filename = urllib.parse.unquote(filename)

        cached = get_tavily_cached_result(filename)
        if cached:
            return web.json_response(cached)
        else:
            return web.json_response({
                'success': False,
                'message': '无缓存结果'
            })

    except Exception as e:
        logging.error(f"[WMD] Get Tavily cache error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/tavily-cache-all")
async def get_tavily_cache_all(request):
    """Get all cached Tavily search results"""
    global _tavily_cache
    return web.json_response({
        'success': True,
        'cache': _tavily_cache
    })


@routes.get("/workflow-models/search-cache/{filename}")
async def get_search_cache(request):
    """Get cached search metadata for a filename"""
    try:
        filename = request.match_info['filename']
        # URL decode the filename
        filename = urllib.parse.unquote(filename)

        cached = get_cached_metadata(filename)
        if cached:
            return web.json_response({
                'success': True,
                'metadata': cached
            })
        else:
            return web.json_response({
                'success': False,
                'message': '未找到缓存元数据'
            })
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Get search cache error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/lookup-hash")
async def lookup_by_hash(request):
    """Look up model on CivitAI by calculating file hash"""
    try:
        data = await request.json()
        filename = data.get('filename')
        directory = data.get('directory')

        if not filename or not directory:
            return web.json_response({'error': '缺少文件名或目录'}, status=400)

        # Find the model file
        filepath = find_model_file_path(directory, filename)

        if not filepath:
            return web.json_response({
                'success': False,
                'message': '本地未找到模型文件'
            })

        # Calculate hash (this may take time for large files)
        logging.info(f"[Workflow-Models-Downloader] Calculating hash for: {filename}")
        file_hash = calculate_file_hash(filepath)

        if not file_hash:
            return web.json_response({
                'success': False,
                'message': '计算文件哈希失败'
            })

        logging.info(f"[Workflow-Models-Downloader] Hash: {file_hash[:16]}... Looking up on CivitAI")

        # Look up on CivitAI
        model_info = lookup_civitai_by_hash(file_hash)

        if model_info and model_info.get('download_url'):
            return web.json_response({
                'success': True,
                'hash': file_hash,
                'model_name': model_info.get('model_name'),
                'model_type': model_info.get('model_type'),
                'version_name': model_info.get('version_name'),
                'original_filename': model_info.get('original_filename'),
                'url': model_info.get('download_url'),
                'model_id': model_info.get('model_id'),
                'civitai_url': f"https://civitai.com/models/{model_info.get('model_id')}" if model_info.get('model_id') else None,
                'source': 'civitai_hash'
            })
        else:
            return web.json_response({
                'success': False,
                'hash': file_hash,
                'message': 'CivitAI 上没有该模型'
            })

    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Hash lookup error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/download-url")
async def download_from_url(request):
    """Download a model from a direct URL or CivitAI URN"""
    try:
        data = await request.json()

        url = data.get('url')
        filename = data.get('filename')
        target_dir = data.get('directory')

        if not all([url, filename, target_dir]):
            return web.json_response({'error': '缺少必填字段（url、filename、directory）'}, status=400)

        # Check if URL is actually a CivitAI URN and convert it
        if is_civitai_urn(url):
            civitai_url = civitai_urn_to_download_url(url)
            if civitai_url:
                logging.info(f"[Workflow-Models-Downloader] Converted CivitAI URN to download URL: {url} -> {civitai_url}")
                url = civitai_url
            else:
                return web.json_response({'error': '无效的 CivitAI URN 格式'}, status=400)

        # Generate download ID
        download_id = f"direct_{filename}".replace('/', '_').replace('\\', '_')

        # Check if already downloading
        with download_lock:
            if download_id in download_progress and download_progress[download_id].get('status') == 'downloading':
                return web.json_response({'error': '该文件正在下载中'}, status=400)

            download_progress[download_id] = {
                'status': 'starting',
                'progress': 0,
                'filename': filename,
                'total_size': 0,
                'downloaded': 0
            }

        # Start download in background thread
        thread = threading.Thread(
            target=_download_from_url_thread,
            args=(download_id, url, filename, target_dir)
        )
        thread.start()

        return web.json_response({
            'success': True,
            'download_id': download_id,
            'message': f'开始下载 {filename}'
        })
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Download URL error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/download")
async def download_model(request):
    """Start downloading a model from HuggingFace"""
    try:
        data = await request.json()

        hf_repo = data.get('hf_repo')
        hf_path = data.get('hf_path')
        filename = data.get('filename')
        target_dir = data.get('directory')
        force = data.get('force', False)  # Force re-download even if exists

        if not all([hf_repo, filename, target_dir]):
            return web.json_response({'error': '缺少必填字段'}, status=400)

        # Generate download ID
        download_id = f"{hf_repo}/{filename}".replace('/', '_')

        # Check if already downloading
        with download_lock:
            if download_id in download_progress and download_progress[download_id].get('status') == 'downloading':
                return web.json_response({'error': '该文件正在下载中'}, status=400)

            download_progress[download_id] = {
                'status': 'starting',
                'progress': 0,
                'filename': filename,
                'total_size': 0,
                'downloaded': 0
            }

        # Start download in background thread
        thread = threading.Thread(
            target=_download_model_thread,
            args=(download_id, hf_repo, hf_path or filename, filename, target_dir)
        )
        thread.start()

        return web.json_response({
            'success': True,
            'download_id': download_id,
            'message': f'开始下载 {filename}'
        })
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Download start error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/progress/{download_id}")
async def get_download_progress(request):
    """Get download progress for a specific download"""
    download_id = request.match_info['download_id']

    with download_lock:
        if download_id in download_progress:
            return web.json_response(download_progress[download_id])
        else:
            return web.json_response({'error': '未找到该下载任务'}, status=404)


def _win_open_in_explorer(path):
    """Reveal `path` in a FOREGROUND Explorer window, selecting the file.

    Why not `explorer /select,`: Windows 11 only lets the foreground app
    bring new windows to front. The ComfyUI server is a background
    process, so spawning explorer.exe merely blinks the taskbar icon.
    SHOpenFolderAndSelectItems routes activation through Explorer itself
    (the shell is exempt from the foreground lock), so the window both
    opens AND selects the file. Returns True on success.
    """
    try:
        import ctypes
        VP = ctypes.c_void_p
        shell32 = ctypes.WinDLL('shell32')
        # Shell API needs COM on this thread (aiohttp worker has none).
        # S_OK  -> we own the init and must uninit;
        # S_FALSE -> already init (don't uninit); RPC_E_CHANGED_MODE
        # (0x80010106, MTA) -> still usable for shell calls, skip uninit.
        hr_init = ctypes.windll.ole32.CoInitializeEx(None, 0)
        if hr_init not in (0, 1):
            hr_init = ctypes.windll.ole32.CoInitializeEx(None, 2)  # retry MTA
        own_com = (hr_init == 0)
        _sigs = (
            ('SHParseDisplayName', ctypes.c_long,
             [ctypes.c_wchar_p, VP, ctypes.POINTER(VP), ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]),
            ('ILClone', VP, [VP]),
            ('ILRemoveLastID', ctypes.c_int, [VP]),
            ('ILFindChild', VP, [VP, VP]),
            ('SHOpenFolderAndSelectItems', ctypes.c_long, [VP, ctypes.c_uint, ctypes.POINTER(VP), ctypes.c_uint32]),
            ('ILFree', None, [VP]),
        )
        for _fn, _res, _args in _sigs:
            _f = getattr(shell32, _fn)
            _f.restype, _f.argtypes = _res, _args

        pidl = VP()
        attr = ctypes.c_uint32(0)
        if shell32.SHParseDisplayName(os.path.abspath(path), None, ctypes.byref(pidl), 0, ctypes.byref(attr)) != 0 or not pidl:
            return False
        folder = None
        ok = False
        try:
            # MSDN pattern: clone -> strip last id = parent folder;
            # ILFindChild points at the file id INSIDE `pidl` (not owned).
            folder = shell32.ILClone(pidl)
            if folder and shell32.ILRemoveLastID(folder):
                child = shell32.ILFindChild(folder, pidl)
                if child:
                    apidl = (VP * 1)(child)
                    ok = shell32.SHOpenFolderAndSelectItems(folder, 1, apidl, 0) == 0
            if not ok:
                # Fall back to opening the containing folder (no selection)
                ok = shell32.SHOpenFolderAndSelectItems(pidl, 0, None, 0) == 0
        finally:
            shell32.ILFree(pidl)
            if folder:
                shell32.ILFree(folder)
            if own_com:
                ctypes.windll.ole32.CoUninitialize()
        return ok
    except Exception as e:
        logging.debug(f"[WMD] Shell-API reveal failed: {e}")
        return False


@routes.post("/workflow-models/open-folder")
async def open_folder(request):
    """Open the model's folder in the OS file manager, selecting the file.

    Restricted to paths inside ComfyUI's models tree (same guard style as
    delete_model) so the endpoint can't be used to reveal arbitrary files.
    """
    try:
        data = await request.json()
        path = data.get('path', '')
        if not path or not os.path.exists(path):
            return web.json_response({'error': '路径不存在'}, status=404)
        path = os.path.abspath(path)

        allowed_roots = [os.path.abspath(folder_paths.models_dir)]
        try:
            for e in folder_paths.folder_names_and_paths.values():
                allowed_roots.extend(os.path.abspath(d) for d in e[0])
        except Exception:
            pass
        if not any(path.startswith(root + os.sep) or path == root for root in allowed_roots):
            return web.json_response({'error': '访问被拒绝：路径不在 models 目录内'}, status=403)

        if os.name == 'nt':
            if not _win_open_in_explorer(path):
                # Fallback: classic explorer /select (known to only blink the
                # taskbar icon under Windows 11 foreground-lock)
                subprocess.Popen(['explorer', '/select,', path.replace('/', '\\')])
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', '-R', path])
        else:
            subprocess.Popen(['xdg-open', os.path.dirname(path)])
        return web.json_response({'ok': True, 'path': path})
    except Exception as e:
        logging.error(f"[WMD] open-folder failed: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/progress")
async def get_all_progress(request):
    """Get all download progress"""
    with download_lock:
        return web.json_response(dict(download_progress))


@routes.post("/workflow-models/cancel/{download_id}")
async def cancel_download(request):
    """Cancel a download"""
    download_id = request.match_info['download_id']

    with download_lock:
        if download_id in download_progress:
            cancelled_downloads.add(download_id)
            download_progress[download_id]['status'] = 'cancelled'
            logging.info(f"[Workflow-Models-Downloader] Cancelled download: {download_id}")
            return web.json_response({'success': True, 'message': '下载已取消'})
        else:
            return web.json_response({'error': '未找到该下载任务'}, status=404)


@routes.get("/workflow-models/download-history")
async def get_download_history_endpoint(request):
    """Get download history"""
    global download_history
    if not download_history:
        load_download_history()
    return web.json_response({
        'success': True,
        'history': download_history
    })


@routes.post("/workflow-models/clear-download-history")
async def clear_download_history_endpoint(request):
    """Clear download history"""
    clear_download_history()
    return web.json_response({
        'success': True,
        'message': '下载历史已清空'
    })


@routes.post("/workflow-models/delete-history-item")
async def delete_history_item_endpoint(request):
    """Delete a single item from download history"""
    global download_history
    try:
        data = await request.json()
        filename = data.get('filename')
        if filename:
            download_history = [h for h in download_history if h.get('filename') != filename]
            save_download_history()
            return web.json_response({'success': True})
        return web.json_response({'error': '缺少文件名'}, status=400)
    except Exception as e:
        return web.json_response({'error': str(e)}, status=500)


def _download_model_thread(download_id, hf_repo, hf_path, filename, target_dir):
    """Background thread to download a model"""
    try:
        from huggingface_hub import hf_hub_download

        # Normalize path separators for the OS
        target_dir_normalized = target_dir.replace('/', os.sep).replace('\\', os.sep)
        target_path = os.path.join(folder_paths.models_dir, target_dir_normalized)

        # Create directory if it doesn't exist
        try:
            os.makedirs(target_path, exist_ok=True)
            logging.info(f"[Workflow-Models-Downloader] Target directory: {target_path}")
        except Exception as dir_error:
            logging.error(f"[Workflow-Models-Downloader] Failed to create directory {target_path}: {dir_error}")
            raise

        with download_lock:
            download_progress[download_id]['status'] = 'downloading'

        # Get HuggingFace token if available
        hf_token = get_huggingface_token()
        headers = {}
        if hf_token:
            headers['Authorization'] = f'Bearer {hf_token}'

        # Normalize filename path separators and create subdirectories if needed
        filename_normalized = filename.replace('/', os.sep).replace('\\', os.sep)
        dest_file = os.path.join(target_path, filename_normalized)
        dest_dir = os.path.dirname(dest_file)
        if dest_dir and not os.path.exists(dest_dir):
            os.makedirs(dest_dir, exist_ok=True)

        url = f"https://huggingface.co/{hf_repo}/resolve/main/{hf_path}"

        # Curated known-good override wins over the requested repo
        kg = lookup_known_good_source(filename)
        if kg and kg.get('url'):
            url = kg['url']
            if kg.get('hf_repo'):
                hf_repo, hf_path = kg['hf_repo'], kg.get('hf_path', '')
            # Force the correct model directory (plugin's filename regex is unreliable:
            # it files the 19.5GB hybrid diffusion model under checkpoints/)
            kg_dir = kg.get('directory')
            if kg_dir:
                target_path = os.path.join(folder_paths.models_dir, kg_dir)
                os.makedirs(target_path, exist_ok=True)
                dest_file = os.path.join(target_path, filename_normalized)
                logging.info(f"[Workflow-Models-Downloader] Forced directory for {filename}: {kg_dir}")
            logging.info(f"[Workflow-Models-Downloader] Using known-good source for {filename}: {url}")
        else:
            # Verify the cached/resolved entry actually matches this repo;
            # if model_metadata.json holds a stale/wrong URL, prefer the
            # caller's repo (from the workflow panel) over the cache.
            cached = get_cached_metadata(filename)
            if cached and cached.get('url') and extract_huggingface_info(cached['url'])[0] != hf_repo:
                logging.warning(f"[Workflow-Models-Downloader] Cached URL repo mismatch for {filename} "
                                f"({extract_huggingface_info(cached['url'])[0]} != {hf_repo}); using workflow repo")

        # Resume-capable download with automatic retry (shared engine)
        success, error = _resume_download_core(url, dest_file, download_id, headers)

        if error == "Cancelled":
            cancelled_downloads.discard(download_id)
            return

        if not success:
            raise requests.exceptions.RequestException(error or "Download failed")

        with download_lock:
            download_progress[download_id]['status'] = 'completed'
            download_progress[download_id]['progress'] = 100

        # Save to model_metadata.json (single source of truth)
        hf_url = f"https://huggingface.co/{hf_repo}/resolve/main/{hf_path}"
        metadata = load_model_metadata()
        metadata[filename] = {
            'filename': filename,
            'url': hf_url,
            'url_source': 'download',
            'source': 'huggingface',
            'hf_repo': hf_repo,
            'hf_path': hf_path,
            'type': target_dir,
            'downloaded_at': time.strftime('%Y-%m-%dT%H:%M:%S')
        }
        save_model_metadata(metadata)

        # Also keep in download history for UI
        add_to_download_history({
            'id': download_id,
            'filename': filename,
            'status': 'completed',
            'total_size': download_progress.get(download_id, {}).get('total_size', 0),
            'directory': target_dir,
            'url': hf_url,
            'hf_repo': hf_repo,
            'hf_path': hf_path
        })

        logging.info(f"[Workflow-Models-Downloader] Downloaded: {filename}")

    except requests.exceptions.HTTPError as e:
        error_msg = str(e)
        if e.response is not None:
            status_code = e.response.status_code
            if status_code in [401, 403]:
                if 'huggingface.co' in url:
                    error_msg = f"Unauthorized (HTTP {status_code}): HuggingFace token required. Go to File > Settings > 工作流模型管理 to configure your HuggingFace token. Get one at https://huggingface.co/settings/tokens"
                else:
                    error_msg = f"Unauthorized (HTTP {status_code}): Authentication required for this model."
            elif status_code == 404:
                error_msg = f"Model not found (HTTP 404): The file may have been moved or deleted."
        logging.error(f"[Workflow-Models-Downloader] Download error: {error_msg}")
        with download_lock:
            download_progress[download_id]['status'] = 'error'
            download_progress[download_id]['error'] = error_msg
        # Add to download history
        add_to_download_history({
            'id': download_id,
            'filename': filename,
            'status': 'error',
            'error': error_msg,
            'directory': target_dir
        })
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Download error: {e}")
        with download_lock:
            download_progress[download_id]['status'] = 'error'
            download_progress[download_id]['error'] = str(e)
        # Add to download history
        add_to_download_history({
            'id': download_id,
            'filename': filename,
            'status': 'error',
            'error': str(e),
            'directory': target_dir
        })
    finally:
        # Clean up cancelled flag
        cancelled_downloads.discard(download_id)


def _download_from_url_thread(download_id, url, filename, target_dir):
    """Background thread to download a model from direct URL"""
    try:
        # Normalize path separators for the OS
        target_dir_normalized = target_dir.replace('/', os.sep).replace('\\', os.sep)
        target_path = os.path.join(folder_paths.models_dir, target_dir_normalized)

        # Create directory if it doesn't exist
        try:
            os.makedirs(target_path, exist_ok=True)
            logging.info(f"[Workflow-Models-Downloader] Target directory: {target_path}")
        except Exception as dir_error:
            logging.error(f"[Workflow-Models-Downloader] Failed to create directory {target_path}: {dir_error}")
            raise

        with download_lock:
            download_progress[download_id]['status'] = 'downloading'

        # Normalize filename path separators and create subdirectories if needed
        filename_normalized = filename.replace('/', os.sep).replace('\\', os.sep)
        dest_file = os.path.join(target_path, filename_normalized)
        # Create parent directory if filename contains subdirectories
        dest_dir = os.path.dirname(dest_file)
        if dest_dir and not os.path.exists(dest_dir):
            os.makedirs(dest_dir, exist_ok=True)

        # Prepare headers based on URL source
        headers = {}
        if 'civitai.com' in url:
            civitai_key = get_civitai_api_key()
            logging.info(f"[WMD] CivitAI download - key configured: {bool(civitai_key)}")
            if civitai_key:
                # CivitAI uses token as query parameter
                if '?' in url:
                    url = f"{url}&token={civitai_key}"
                else:
                    url = f"{url}?token={civitai_key}"
                logging.info(f"[WMD] CivitAI URL with token: {url[:80]}...")
            else:
                logging.warning("[WMD] CivitAI download attempted without API key!")
        elif 'huggingface.co' in url:
            hf_token = get_huggingface_token()
            if hf_token:
                headers['Authorization'] = f'Bearer {hf_token}'

        # Curated known-good override wins over a stale resolved URL
        kg = lookup_known_good_source(filename)
        if kg and kg.get('url'):
            url = kg['url']
            logging.info(f"[Workflow-Models-Downloader] Using known-good source for {filename}: {url}")
            kg_dir = kg.get('directory')
            if kg_dir:
                target_path = os.path.join(folder_paths.models_dir, kg_dir)
                os.makedirs(target_path, exist_ok=True)
                dest_file = os.path.join(target_path, filename_normalized)
                logging.info(f"[Workflow-Models-Downloader] Forced directory for {filename}: {kg_dir}")

        # Resume-capable download with automatic retry (shared engine)
        success, error = _resume_download_core(url, dest_file, download_id, headers)

        # Handle cancellation / failure after engine returns
        if error == "Cancelled":
            cancelled_downloads.discard(download_id)
            return

        if not success:
            raise requests.exceptions.RequestException(error or "Download failed")

        with download_lock:
            download_progress[download_id]['status'] = 'completed'
            download_progress[download_id]['progress'] = 100

        # Save to model_metadata.json (single source of truth)
        clean_url = url.split('?')[0] if 'civitai.com' in url else url
        source = 'civitai' if 'civitai.com' in url else ('huggingface' if 'huggingface.co' in url else 'direct')
        hf_repo, hf_path = extract_huggingface_info(url)

        metadata = load_model_metadata()
        entry = {
            'filename': filename,
            'url': clean_url,
            'url_source': 'download',
            'source': source,
            'type': target_dir,
            'downloaded_at': time.strftime('%Y-%m-%dT%H:%M:%S')
        }
        if hf_repo:
            entry['hf_repo'] = hf_repo
            entry['hf_path'] = hf_path
        if 'civitai.com' in url:
            # Try to extract model ID
            match = re.search(r'/models/(\d+)', url)
            if match:
                entry['civitai_model_id'] = match.group(1)
        metadata[filename] = entry
        save_model_metadata(metadata)

        # Also keep in download history for UI
        add_to_download_history({
            'id': download_id,
            'filename': filename,
            'status': 'completed',
            'total_size': download_progress.get(download_id, {}).get('total_size', 0),
            'directory': target_dir,
            'url': clean_url,
            'source': source
        })

        logging.info(f"[Workflow-Models-Downloader] Downloaded from URL: {filename}")

    except requests.exceptions.HTTPError as e:
        error_msg = str(e)
        if e.response is not None:
            status_code = e.response.status_code
            if status_code in [401, 403]:
                if 'huggingface.co' in url:
                    error_msg = f"Unauthorized (HTTP {status_code}): HuggingFace token required. Go to File > Settings > 工作流模型管理 to configure your HuggingFace token. Get one at https://huggingface.co/settings/tokens"
                elif 'civitai.com' in url:
                    error_msg = f"Unauthorized (HTTP {status_code}): CivitAI API key required. Go to File > Settings > 工作流模型管理 to configure your CivitAI API key."
                else:
                    error_msg = f"Unauthorized (HTTP {status_code}): Authentication required for this model."
            elif status_code == 404:
                error_msg = f"Model not found (HTTP 404): The file may have been moved or deleted."
        logging.error(f"[Workflow-Models-Downloader] URL download error: {error_msg}")
        with download_lock:
            download_progress[download_id]['status'] = 'error'
            download_progress[download_id]['error'] = error_msg
        # Add to download history
        add_to_download_history({
            'id': download_id,
            'filename': filename,
            'status': 'error',
            'error': error_msg,
            'directory': target_dir
        })
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] URL download error: {e}")
        with download_lock:
            download_progress[download_id]['status'] = 'error'
            download_progress[download_id]['error'] = str(e)
        # Add to download history
        add_to_download_history({
            'id': download_id,
            'filename': filename,
            'status': 'error',
            'error': str(e),
            'directory': target_dir
        })
    finally:
        # Clean up cancelled flag
        cancelled_downloads.discard(download_id)


@routes.get("/workflow-models/version")
async def get_version_endpoint(request):
    """Get extension version and check for updates"""
    installed = get_installed_version()
    latest = get_latest_version()
    update_available = compare_versions(installed, latest)

    return web.json_response({
        'installed': installed,
        'latest': latest,
        'update_available': update_available,
        'github_url': f"https://github.com/{GITHUB_REPO}",
        'registry_url': REGISTRY_URL
    })


@routes.get("/workflow-models/settings")
async def get_settings(request):
    """Get current settings (with masked tokens)"""
    try:
        settings = load_settings()
        # Return masked versions for display
        masked = {
            'huggingface_token': mask_token(settings.get('huggingface_token', '')),
            'civitai_api_key': mask_token(settings.get('civitai_api_key', '')),
            'tavily_api_key': mask_token(settings.get('tavily_api_key', '')),
            'huggingface_token_set': bool(settings.get('huggingface_token', '')),
            'civitai_api_key_set': bool(settings.get('civitai_api_key', '')),
            'tavily_api_key_set': bool(settings.get('tavily_api_key', '')),
            'enable_advanced_search': settings.get('enable_advanced_search', False),
            'max_parallel_downloads': settings.get('max_parallel_downloads', 3),
            'use_hf_mirror': settings.get('use_hf_mirror', False),
            'hf_mirror_domain': settings.get('hf_mirror_domain', 'hf-mirror.com')
        }
        return web.json_response(masked)
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Get settings error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/settings")
async def update_settings(request):
    """Update settings"""
    try:
        data = await request.json()
        current = load_settings()

        # Only update if new value is provided (not empty or masked)
        if 'huggingface_token' in data:
            token = data['huggingface_token']
            if token and not token.startswith('***'):
                current['huggingface_token'] = token
            elif token == '':
                current['huggingface_token'] = ''

        if 'civitai_api_key' in data:
            key = data['civitai_api_key']
            if key and not key.startswith('***'):
                current['civitai_api_key'] = key
            elif key == '':
                current['civitai_api_key'] = ''

        if 'tavily_api_key' in data:
            key = data['tavily_api_key']
            if key and not key.startswith('***'):
                current['tavily_api_key'] = key
            elif key == '':
                current['tavily_api_key'] = ''

        if 'enable_advanced_search' in data:
            current['enable_advanced_search'] = bool(data['enable_advanced_search'])

        if 'use_hf_mirror' in data:
            current['use_hf_mirror'] = bool(data['use_hf_mirror'])

        if 'hf_mirror_domain' in data:
            dom = str(data['hf_mirror_domain'] or '').strip()
            if dom:
                current['hf_mirror_domain'] = dom

        if 'max_parallel_downloads' in data:
            try:
                val = int(data['max_parallel_downloads'])
                current['max_parallel_downloads'] = max(1, min(10, val))  # Clamp between 1-10
                # Update the global variable too
                global max_parallel_downloads
                max_parallel_downloads = current['max_parallel_downloads']
            except (ValueError, TypeError):
                pass

        if save_settings(current):
            return web.json_response({'success': True})
        else:
            return web.json_response({'error': '保存设置失败'}, status=500)
    except Exception as e:
        logging.error(f"[Workflow-Models-Downloader] Update settings error: {e}")
        return web.json_response({'error': str(e)}, status=500)


def mask_token(token):
    """Mask a token for display, showing only first 4 and last 4 characters"""
    if not token:
        return ''
    if len(token) <= 8:
        return '*' * len(token)
    return token[:4] + '*' * (len(token) - 8) + token[-4:]


# ============================================================================
# Model Browser Endpoints
# ============================================================================

def format_size(size_bytes):
    """Format file size in human-readable format"""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


# ============================================================================
# Node Metadata Database (Second Master - Single Source of Truth for Nodes)
# ============================================================================
# Hybrid approach:
# 1. Bulk fetch from ComfyUI Registry during scan
# 2. Dynamic lookup as fallback for unknown nodes
# 3. Also imports from ComfyUI Manager's extension-node-map.json

NODE_METADATA_FILE = os.path.join(os.path.dirname(__file__), "node_metadata.json")
_node_metadata_cache = None

# ComfyUI Registry API endpoints
COMFY_REGISTRY_API = "https://api.comfy.org"
COMFY_REGISTRY_LIST_NODES = f"{COMFY_REGISTRY_API}/comfy-nodes"
COMFY_REGISTRY_GET_NODE = f"{COMFY_REGISTRY_API}/comfy-nodes/{{node_name}}/node"


def load_node_metadata():
    """Load the node metadata database"""
    global _node_metadata_cache
    if _node_metadata_cache is not None:
        return _node_metadata_cache
    try:
        if os.path.exists(NODE_METADATA_FILE):
            with open(NODE_METADATA_FILE, 'r', encoding='utf-8') as f:
                _node_metadata_cache = json.load(f)
                return _node_metadata_cache
    except Exception as e:
        logging.error(f"[WMD] Error loading node metadata: {e}")
    _node_metadata_cache = {}
    return _node_metadata_cache


def save_node_metadata(metadata):
    """Save the node metadata database"""
    global _node_metadata_cache
    try:
        with open(NODE_METADATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(metadata, f, indent=2)
        _node_metadata_cache = metadata
        return True
    except Exception as e:
        logging.error(f"[WMD] Error saving node metadata: {e}")
        return False


def get_node_info(node_type):
    """Get metadata for a node type from local database"""
    metadata = load_node_metadata()
    return metadata.get(node_type)


def fetch_node_from_registry(node_name):
    """Dynamic lookup: Fetch single node info from ComfyUI Registry API"""
    try:
        url = COMFY_REGISTRY_GET_NODE.format(node_name=node_name)
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return {
                'node_type': node_name,
                'name': data.get('name'),
                'description': data.get('description'),
                'author': data.get('author'),
                'repository': data.get('repository'),
                'license': data.get('license'),
                'category': data.get('category'),
                'downloads': data.get('downloads'),
                'rating': data.get('rating'),
                'latest_version': data.get('latest_version', {}).get('version') if data.get('latest_version') else None,
                'source': 'comfy_registry',
                'fetched_at': time.strftime('%Y-%m-%dT%H:%M:%S')
            }
        elif response.status_code == 404:
            return None
    except Exception as e:
        logging.debug(f"[WMD] Registry lookup failed for {node_name}: {e}")
    return None


def fetch_bulk_nodes_from_registry(page=1, page_size=100):
    """Bulk fetch: Get paginated list of nodes from ComfyUI Registry"""
    try:
        url = f"{COMFY_REGISTRY_LIST_NODES}?page={page}&pageSize={page_size}"
        response = requests.get(url, timeout=30)
        if response.status_code == 200:
            data = response.json()
            return data.get('comfy_nodes', []), data.get('total', 0)
    except Exception as e:
        logging.error(f"[WMD] Registry bulk fetch failed: {e}")
    return [], 0


@routes.post("/workflow-models/scan-node-metadata")
async def scan_node_metadata(request):
    """Scan and build node metadata from multiple sources:
    1. ComfyUI Registry API (bulk fetch)
    2. ComfyUI Manager's extension-node-map.json
    """
    try:
        data = await request.json() if request.body_exists else {}
        force_rescan = data.get('force', False)
        include_registry = data.get('include_registry', True)

        # Load existing metadata
        existing_metadata = load_node_metadata()
        new_metadata = dict(existing_metadata) if not force_rescan else {}

        from_registry = 0
        from_extension_map = 0
        updated = 0

        # Source 1: ComfyUI Registry API (bulk fetch with pagination)
        if include_registry:
            logging.info("[WMD] Fetching nodes from ComfyUI Registry...")
            page = 1
            page_size = 100
            total_fetched = 0

            while True:
                nodes, total = fetch_bulk_nodes_from_registry(page, page_size)
                if not nodes:
                    break

                for node in nodes:
                    node_name = node.get('comfy_node_name')
                    if not node_name:
                        continue

                    # Skip if already exists with better data (unless force)
                    if not force_rescan and node_name in existing_metadata:
                        existing = existing_metadata[node_name]
                        # Keep existing if it has more info
                        if existing.get('github_url') or existing.get('repository'):
                            new_metadata[node_name] = existing
                            continue

                    entry = {
                        'node_type': node_name,
                        'category': node.get('category'),
                        'description': node.get('description'),
                        'function': node.get('function'),
                        'input_types': node.get('input_types'),
                        'return_types': node.get('return_types'),
                        'deprecated': node.get('deprecated', False),
                        'experimental': node.get('experimental', False),
                        'source': 'comfy_registry',
                        'scanned_at': time.strftime('%Y-%m-%dT%H:%M:%S')
                    }

                    new_metadata[node_name] = entry
                    from_registry += 1
                    total_fetched += 1

                # Check if we've fetched all
                if total_fetched >= total or len(nodes) < page_size:
                    break
                page += 1

            logging.info(f"[WMD] Fetched {from_registry} nodes from ComfyUI Registry")

        # Source 2: ComfyUI Manager's extension-node-map.json (adds GitHub URLs)
        logging.info("[WMD] Importing from extension-node-map.json...")
        extension_map = load_extension_node_map()

        for github_url, node_data in extension_map.items():
            if not isinstance(node_data, list) or len(node_data) < 1:
                continue

            # First element is list of node types
            node_list = node_data[0] if isinstance(node_data[0], list) else []

            # Second element might be extension info
            extension_info = node_data[1] if len(node_data) > 1 else {}
            extension_name = extension_info.get('title', '') if isinstance(extension_info, dict) else ''

            for node_type in node_list:
                if not node_type:
                    continue

                # Get or create entry
                entry = new_metadata.get(node_type, {'node_type': node_type})

                # Add/update GitHub info
                entry['github_url'] = github_url
                entry['extension_name'] = extension_name

                # Check if installed
                try:
                    repo_name = github_url.rstrip('/').split('/')[-1]
                    custom_nodes_path = os.path.join(folder_paths.base_path, 'custom_nodes', repo_name)
                    entry['installed'] = os.path.exists(custom_nodes_path)
                except Exception:
                    pass

                # Update source if this is new info
                if entry.get('source') != 'comfy_registry':
                    entry['source'] = 'extension_node_map'
                else:
                    entry['source'] = 'comfy_registry+extension_map'

                entry['scanned_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')

                if node_type not in new_metadata:
                    from_extension_map += 1
                else:
                    updated += 1

                new_metadata[node_type] = entry

        # Save updated metadata
        save_node_metadata(new_metadata)

        return web.json_response({
            'success': True,
            'from_registry': from_registry,
            'from_extension_map': from_extension_map,
            'updated': updated,
            'total': len(new_metadata)
        })
    except Exception as e:
        logging.error(f"[WMD] Scan node metadata error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/node-metadata")
async def get_all_node_metadata(request):
    """Get the node metadata database"""
    try:
        metadata = load_node_metadata()
        return web.json_response({
            'success': True,
            'metadata': metadata,
            'count': len(metadata)
        })
    except Exception as e:
        logging.error(f"[WMD] Get node metadata error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/node-info")
async def get_node_info_endpoint(request):
    """Get metadata for a specific node type with dynamic fallback"""
    try:
        node_type = request.query.get('node_type', '')
        if not node_type:
            return web.json_response({'error': '需要 node_type'}, status=400)

        # 1. Check local database first
        info = get_node_info(node_type)

        # 2. If not found, try ComfyUI Manager's extension-node-map
        if not info:
            github_url = lookup_node_github_url(node_type)
            if github_url:
                info = {
                    'node_type': node_type,
                    'github_url': github_url,
                    'source': 'extension_node_map_live'
                }

        # 3. If still not found, try dynamic lookup from ComfyUI Registry
        if not info:
            registry_info = fetch_node_from_registry(node_type)
            if registry_info:
                info = registry_info
                # Save to local database for future lookups
                metadata = load_node_metadata()
                metadata[node_type] = info
                save_node_metadata(metadata)

        return web.json_response({
            'success': True,
            'info': info,
            'found': info is not None
        })
    except Exception as e:
        logging.error(f"[WMD] Get node info error: {e}")
        return web.json_response({'error': str(e)}, status=500)


# ============================================================================
# Model Metadata Scanner
# ============================================================================
# Uses _model_metadata_cache and functions defined earlier in the file

MODEL_METADATA_FILE = os.path.join(os.path.dirname(__file__), "model_metadata.json")


def load_model_metadata():
    """Load the master model metadata file (uses shared cache)"""
    return _get_model_metadata_safe()


def save_model_metadata(metadata):
    """Save the master model metadata file (uses shared cache)"""
    return _save_model_metadata_safe(metadata)


def extract_safetensors_metadata(file_path):
    """Extract metadata from a safetensors file header"""
    try:
        with open(file_path, 'rb') as f:
            # Read header size (first 8 bytes, little-endian)
            header_size_bytes = f.read(8)
            if len(header_size_bytes) < 8:
                return None
            header_size = int.from_bytes(header_size_bytes, 'little')

            # Sanity check - header shouldn't be too large
            if header_size > 10 * 1024 * 1024:  # 10MB max
                return None

            # Read header JSON
            header_json = f.read(header_size)
            header = json.loads(header_json.decode('utf-8'))

            # Extract __metadata__ section if present
            metadata = header.get('__metadata__', {})
            return metadata
    except Exception as e:
        logging.debug(f"[WMD] Error reading safetensors metadata from {file_path}: {e}")
        return None


def extract_model_info_from_metadata(metadata, filename):
    """Extract useful info (URLs, source, etc.) from safetensors metadata"""
    if not metadata:
        return None

    info = {
        'filename': filename,
        'source': None,
        'url': None,
        'model_name': None,
        'base_model': None,
        'description': None,
        'civitai_model_id': None,
        'civitai_version_id': None,
        'hf_repo': None,
    }

    # Check for CivitAI metadata
    if 'modelId' in metadata or 'ss_civitai_model_id' in metadata:
        model_id = metadata.get('modelId') or metadata.get('ss_civitai_model_id')
        version_id = metadata.get('versionId') or metadata.get('ss_civitai_version_id')
        if model_id:
            info['civitai_model_id'] = str(model_id)
            info['source'] = 'civitai'
            if version_id:
                info['civitai_version_id'] = str(version_id)
                info['url'] = f"https://civitai.com/models/{model_id}?modelVersionId={version_id}"
            else:
                info['url'] = f"https://civitai.com/models/{model_id}"

    # Check for HuggingFace metadata
    if 'hf_repo' in metadata or 'ss_hf_repo' in metadata:
        hf_repo = metadata.get('hf_repo') or metadata.get('ss_hf_repo')
        if hf_repo:
            info['hf_repo'] = hf_repo
            info['source'] = 'huggingface'
            info['url'] = f"https://huggingface.co/{hf_repo}"

    # Check for direct URL in metadata
    if 'ss_model_url' in metadata:
        info['url'] = metadata['ss_model_url']
    elif 'model_url' in metadata:
        info['url'] = metadata['model_url']
    elif 'source_url' in metadata:
        info['url'] = metadata['source_url']

    # Extract model name
    info['model_name'] = (
        metadata.get('ss_output_name') or
        metadata.get('ss_sd_model_name') or
        metadata.get('model_name') or
        metadata.get('name')
    )

    # Extract base model info
    info['base_model'] = (
        metadata.get('ss_base_model_version') or
        metadata.get('ss_sd_model_hash') or
        metadata.get('base_model')
    )

    # Extract description
    info['description'] = metadata.get('description') or metadata.get('ss_training_comment')

    # Only return if we found something useful
    if info['url'] or info['model_name'] or info['civitai_model_id'] or info['hf_repo']:
        return info
    return None


@routes.post("/workflow-models/find-model-url")
async def find_and_save_model_url(request):
    """Search for model URL using HuggingFace and CivitAI APIs, save to metadata.

    Search order:
    1. HuggingFace API
    2. CivitAI API (fallback)
    """
    try:
        data = await request.json()
        filename = data.get('filename')

        if not filename:
            return web.json_response({'error': '需要文件名'}, status=400)

        basename = os.path.basename(filename)

        # Check if already has URL in metadata
        metadata = load_model_metadata()
        existing = metadata.get(basename, {})
        if existing.get('url') or existing.get('user_url'):
            return web.json_response({
                'success': True,
                'url': existing.get('url') or existing.get('user_url'),
                'source': existing.get('url_source', 'existing'),
                'already_found': True
            })

        # Search APIs (HuggingFace first, then CivitAI)
        url, source = find_model_url(basename, search_apis=True)

        if url:
            # Save to model_metadata.json
            entry = metadata.get(basename, {'filename': basename})
            entry['url'] = url
            entry['url_source'] = source
            entry['searched_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')

            # Extract additional info from URL
            if 'huggingface.co' in url:
                entry['source'] = 'huggingface'
                hf_repo, hf_path = extract_huggingface_info(url)
                if hf_repo:
                    entry['hf_repo'] = hf_repo
                    entry['hf_path'] = hf_path
            elif 'civitai.com' in url:
                entry['source'] = 'civitai'
                match = re.search(r'/models/(\d+)', url)
                if match:
                    entry['civitai_model_id'] = match.group(1)

            metadata[basename] = entry
            save_model_metadata(metadata)

            return web.json_response({
                'success': True,
                'url': url,
                'source': source,
                'saved': True
            })
        else:
            return web.json_response({
                'success': False,
                'message': 'HuggingFace 或 CivitAI 上未找到链接'
            })
    except Exception as e:
        logging.error(f"[WMD] Find model URL error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/scan-model-metadata")
async def scan_model_metadata(request):
    """Scan all installed models and build comprehensive metadata database.

    Sources (in priority order for URLs):
    1. User-provided URLs (preserved, never overwritten)
    2. Safetensors embedded metadata
    3. ComfyUI Manager's model-list.json
    4. HuggingFace/CivitAI API search (if search_apis=True)
    """
    try:
        data = await request.json() if request.body_exists else {}
        force_rescan = data.get('force', False)

        # Load existing metadata
        existing_metadata = load_model_metadata()
        new_metadata = dict(existing_metadata) if not force_rescan else {}

        scanned = 0
        updated = 0
        from_safetensors = 0
        from_model_list = 0
        errors = 0

        # Valid model file extensions
        MODEL_EXTENSIONS = {'.safetensors', '.ckpt', '.pt', '.pth', '.bin', '.onnx'}

        # Known model folder types
        model_types = set([
            'checkpoints', 'loras', 'vae', 'controlnet', 'clip', 'clip_vision',
            'text_encoders', 'diffusion_models', 'unet', 'embeddings',
            'upscale_models', 'hypernetworks', 'gligen', 'style_models',
            'ipadapter', 'instantid', 'photomaker', 'pulid'
        ])

        # Add custom folder types from folder_paths
        if hasattr(folder_paths, 'folder_names_and_paths'):
            for folder_type in folder_paths.folder_names_and_paths.keys():
                if folder_type.lower() not in ['custom_nodes', 'configs', 'fonts', 'web', 'js', 'user', 'input', 'output', 'temp', 'models', 'pycache']:
                    model_types.add(folder_type)

        for folder_type in model_types:
            try:
                files = folder_paths.get_filename_list(folder_type)

                for filename in files:
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in MODEL_EXTENSIONS:
                        continue

                    basename = os.path.basename(filename)

                    # Get existing entry (preserve user data)
                    existing_entry = existing_metadata.get(basename, {})
                    user_url = existing_entry.get('user_url')  # Preserve user-provided URL

                    # Skip if already has complete data (unless force)
                    if not force_rescan and basename in existing_metadata:
                        entry = existing_metadata[basename]
                        if entry.get('url') or entry.get('user_url'):
                            new_metadata[basename] = entry
                            continue

                    try:
                        full_path = folder_paths.get_full_path(folder_type, filename)
                        if not full_path or not os.path.exists(full_path):
                            continue

                        scanned += 1

                        # Start with basic info
                        info = {
                            'filename': basename,
                            'type': folder_type,
                            'path': full_path,
                            'scanned_at': time.strftime('%Y-%m-%dT%H:%M:%S')
                        }

                        # Preserve user-provided URL (highest priority)
                        if user_url:
                            info['user_url'] = user_url

                        # Source 1: Try to extract from safetensors metadata
                        if ext == '.safetensors':
                            st_metadata = extract_safetensors_metadata(full_path)
                            if st_metadata:
                                st_info = extract_model_info_from_metadata(st_metadata, basename)
                                if st_info:
                                    # Merge safetensors info
                                    for key in ['url', 'source', 'model_name', 'base_model', 'description',
                                                'civitai_model_id', 'civitai_version_id', 'hf_repo']:
                                        if st_info.get(key):
                                            info[key] = st_info[key]
                                    info['url_source'] = 'safetensors_metadata'
                                    from_safetensors += 1

                        # Source 2: If no URL yet, check ComfyUI Manager's model-list.json
                        if not info.get('url'):
                            ml_type, ml_dir, ml_url, ml_size = lookup_model_in_model_list(basename)
                            if ml_url:
                                info['url'] = ml_url
                                info['url_source'] = 'model_list'
                                from_model_list += 1
                                # Determine source from URL
                                if 'huggingface.co' in ml_url:
                                    info['source'] = 'huggingface'
                                    hf_repo, hf_path = extract_huggingface_info(ml_url)
                                    if hf_repo:
                                        info['hf_repo'] = hf_repo
                                elif 'civitai.com' in ml_url:
                                    info['source'] = 'civitai'

                        # Only save if we found something useful
                        if info.get('url') or info.get('user_url') or info.get('model_name'):
                            new_metadata[basename] = info
                            updated += 1
                        else:
                            # Still save basic info so we know we scanned it
                            new_metadata[basename] = info

                    except Exception as e:
                        errors += 1
                        logging.debug(f"[WMD] Error scanning {filename}: {e}")
            except Exception as e:
                logging.debug(f"[WMD] Error scanning folder {folder_type}: {e}")

        # Save updated metadata
        save_model_metadata(new_metadata)

        return web.json_response({
            'success': True,
            'scanned': scanned,
            'updated': updated,
            'from_safetensors': from_safetensors,
            'from_model_list': from_model_list,
            'errors': errors,
            'total': len(new_metadata)
        })
    except Exception as e:
        logging.error(f"[WMD] Scan metadata error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/model-metadata")
async def get_all_model_metadata(request):
    """Get the master model metadata"""
    try:
        metadata = load_model_metadata()
        return web.json_response({
            'success': True,
            'metadata': metadata,
            'count': len(metadata)
        })
    except Exception as e:
        logging.error(f"[WMD] Get metadata error: {e}")
        return web.json_response({'error': str(e)}, status=500)


# DEPRECATED: user_model_urls.json - user URLs now stored in model_metadata.json


@routes.get("/workflow-models/model-url")
async def get_model_url(request):
    """Get URL info for a model from model_metadata.json (single source of truth)"""
    try:
        filename = request.query.get('filename', '')
        if not filename:
            return web.json_response({'error': '需要文件名'}, status=400)

        basename = os.path.basename(filename)
        result = {
            'filename': filename,
            'url': None,
            'user_url': None,
            'source': None,
            'model_name': None,
            'base_model': None
        }

        # Check model_metadata.json (single source of truth)
        model_metadata = load_model_metadata()

        # Try multiple matching strategies
        meta = None
        # 1. Exact match with full filename
        if filename in model_metadata:
            meta = model_metadata[filename]
        # 2. Match by basename
        elif basename in model_metadata:
            meta = model_metadata[basename]
        # 3. Try case-insensitive match
        else:
            filename_lower = basename.lower()
            for key in model_metadata:
                if key.lower() == filename_lower or os.path.basename(key).lower() == filename_lower:
                    meta = model_metadata[key]
                    break

        if meta:
            result['url'] = meta.get('url')
            result['user_url'] = meta.get('user_url')
            result['source'] = meta.get('url_source') or meta.get('source')
            result['model_name'] = meta.get('model_name')
            result['base_model'] = meta.get('base_model')
            result['hf_repo'] = meta.get('hf_repo')
            result['civitai_model_id'] = meta.get('civitai_model_id')
            result['description'] = meta.get('description')
            result['model_type'] = meta.get('model_type') or meta.get('type')
            result['directory'] = meta.get('directory')
            result['note'] = meta.get('note')
            result['found_in_metadata'] = True
            result['metadata_source'] = 'runtime_cache'
        else:
            # Check popular-models.json (curated list)
            popular_models = load_popular_models()
            popular_meta = popular_models.get(basename)
            if not popular_meta:
                # Try case-insensitive match
                for key, val in popular_models.items():
                    if key.lower() == basename.lower():
                        popular_meta = val
                        break

            if popular_meta:
                result['url'] = popular_meta.get('url')
                result['source'] = 'popular_models'
                result['model_type'] = popular_meta.get('type')
                result['directory'] = popular_meta.get('directory')
                result['note'] = popular_meta.get('note')
                result['found_in_metadata'] = True
                result['metadata_source'] = 'popular_models'
            else:
                # Check model-list.json (ComfyUI Manager)
                model_list = load_model_list()
                model_list_meta = None
                for model_info in model_list:
                    model_filename = model_info.get('filename', '')
                    if model_filename.lower() == basename.lower():
                        model_list_meta = model_info
                        break

                if model_list_meta:
                    result['url'] = model_list_meta.get('url')
                    result['source'] = 'model_list'
                    result['model_type'] = model_list_meta.get('type')
                    result['model_name'] = model_list_meta.get('name')
                    result['base_model'] = model_list_meta.get('base')
                    result['description'] = model_list_meta.get('description')
                    result['found_in_metadata'] = True
                    result['metadata_source'] = 'model_list'
                else:
                    result['found_in_metadata'] = False
                    result['metadata_count'] = len(model_metadata)

        return web.json_response(result)
    except Exception as e:
        logging.error(f"[WMD] Get model URL error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/save-model-url")
async def save_model_url(request):
    """Save a user-provided URL for a model to model_metadata.json"""
    try:
        data = await request.json()
        filename = data.get('filename', '')
        url = data.get('url', '')

        if not filename:
            return web.json_response({'error': '需要文件名'}, status=400)
        if not url:
            return web.json_response({'error': '需要链接'}, status=400)

        basename = os.path.basename(filename)

        # Update model_metadata.json (single source of truth)
        metadata = load_model_metadata()

        # Get or create entry
        entry = metadata.get(basename, {'filename': basename})

        # Set user-provided URL (highest priority)
        entry['user_url'] = url
        entry['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')

        # Also extract info from URL
        if 'huggingface.co' in url:
            entry['source'] = 'huggingface'
            hf_repo, hf_path = extract_huggingface_info(url)
            if hf_repo:
                entry['hf_repo'] = hf_repo
        elif 'civitai.com' in url:
            entry['source'] = 'civitai'
            # Try to extract model ID from URL
            match = re.search(r'/models/(\d+)', url)
            if match:
                entry['civitai_model_id'] = match.group(1)

        metadata[basename] = entry

        if save_model_metadata(metadata):
            logging.info(f"[WMD] Saved user URL for {basename}: {url}")
            return web.json_response({'success': True})
        else:
            return web.json_response({'error': '保存失败'}, status=500)
    except Exception as e:
        logging.error(f"[WMD] Save model URL error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/hf-readme")
async def get_hf_readme(request):
    """Fetch README from a HuggingFace repo"""
    try:
        url = request.query.get('url', '')
        if not url or 'huggingface.co' not in url:
            return web.json_response({'error': '需要有效的 HuggingFace 链接'}, status=400)

        # Extract repo from URL (e.g., https://huggingface.co/owner/repo/...)
        match = re.search(r'huggingface\.co/([^/]+/[^/]+)', url)
        if not match:
            return web.json_response({'readme': None, 'error': '无法从链接解析仓库'})

        repo_id = match.group(1)

        # Fetch README from HuggingFace API
        readme_url = f"https://huggingface.co/{repo_id}/raw/main/README.md"

        try:
            response = requests.get(readme_url, timeout=10)
            if response.status_code == 200:
                readme_content = response.text

                # Simple markdown to HTML conversion for display
                # Convert headers
                readme_html = readme_content
                readme_html = re.sub(r'^### (.+)$', r'<h4>\1</h4>', readme_html, flags=re.MULTILINE)
                readme_html = re.sub(r'^## (.+)$', r'<h3>\1</h3>', readme_html, flags=re.MULTILINE)
                readme_html = re.sub(r'^# (.+)$', r'<h2>\1</h2>', readme_html, flags=re.MULTILINE)
                # Convert bold/italic
                readme_html = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', readme_html)
                readme_html = re.sub(r'\*(.+?)\*', r'<em>\1</em>', readme_html)
                # Convert links
                readme_html = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2" target="_blank">\1</a>', readme_html)
                # Convert line breaks
                readme_html = readme_html.replace('\n\n', '</p><p>')
                readme_html = f'<p>{readme_html}</p>'
                # Limit length
                if len(readme_html) > 10000:
                    readme_html = readme_html[:10000] + '... <em>(truncated)</em>'

                return web.json_response({'readme': readme_html, 'repo': repo_id})
            else:
                return web.json_response({'readme': None, 'error': '未找到 README'})
        except requests.exceptions.RequestException as e:
            return web.json_response({'readme': None, 'error': f'Failed to fetch: {str(e)}'})

    except Exception as e:
        logging.error(f"[WMD] HF README error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/installed")
async def get_installed_models(request):
    """Get all installed models with metadata"""
    try:
        models = []

        # Valid model file extensions
        MODEL_EXTENSIONS = {'.safetensors', '.ckpt', '.pt', '.pth', '.bin', '.gguf', '.sft'}

        # Known model folder types (curated list)
        model_types = set([
            'checkpoints', 'loras', 'vae', 'controlnet', 'clip', 'clip_vision',
            'text_encoders', 'diffusion_models', 'unet', 'embeddings',
            'upscale_models', 'hypernetworks', 'gligen', 'style_models',
            'ipadapter', 'instantid', 'photomaker', 'pulid', 'sams',
            'depthanything', 'groundingdino', 'insightface', 'animatediff_models',
            'vae_approx', 'ultralytics', 'mmdets', 'onnx', 'reactor', 'facerestore_models',
            'facedetection', 'liveportrait', 'layerstyle', 'rembg', 'segment_anything',
            'inpaint', 'prompt_expansion', 'xlabs', 'LLM', 'Joy_caption', 'Florence2',
            'llm_gguf', 'CogVideo', 'TIPO', 'blip', 'nsfw_detector', 'mediapipe'
        ])

        # Folders to exclude (not model folders)
        EXCLUDED_FOLDERS = {
            'custom_nodes', 'configs', 'fonts', 'kjnodes_fonts', 'web', 'js',
            'user', 'input', 'output', 'temp', 'models', 'pycache'
        }

        # Add custom folder types from folder_paths, but only if they look like model folders
        if hasattr(folder_paths, 'folder_names_and_paths'):
            for folder_type in folder_paths.folder_names_and_paths.keys():
                # Skip excluded folders
                if folder_type.lower() in EXCLUDED_FOLDERS:
                    continue
                # Skip if folder name contains certain patterns
                if any(x in folder_type.lower() for x in ['pycache', '_cache', 'config', 'font']):
                    continue
                model_types.add(folder_type)

        for folder_type in model_types:
            try:
                files = folder_paths.get_filename_list(folder_type)

                for filename in files:
                    try:
                        # Only include files with model extensions
                        ext = os.path.splitext(filename)[1].lower()
                        if ext not in MODEL_EXTENSIONS:
                            continue

                        full_path = folder_paths.get_full_path(folder_type, filename)
                        if full_path and os.path.exists(full_path):
                            stat = os.stat(full_path)
                            models.append({
                                'filename': filename,
                                'type': folder_type,
                                'path': full_path,
                                'size': stat.st_size,
                                'modified': stat.st_mtime,
                                'size_human': format_size(stat.st_size)
                            })
                    except Exception as e:
                        logging.debug(f"[WMD] Error processing {filename}: {e}")
                        continue
            except Exception as e:
                logging.debug(f"[WMD] Error scanning {folder_type}: {e}")
                continue

        return web.json_response({'models': models})
    except Exception as e:
        logging.error(f"[WMD] Error getting installed models: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/model/metadata")
async def get_model_metadata(request):
    """Get detailed metadata for a model file"""
    try:
        path = request.query.get('path', '')
        if not path:
            return web.json_response({'error': '需要路径'}, status=400)

        if not os.path.exists(path):
            return web.json_response({'error': '文件不存在'}, status=404)

        # Security check - ensure path is within models directories
        models_dir = folder_paths.models_dir
        if not os.path.abspath(path).startswith(os.path.abspath(models_dir)):
            # Also check extra_model_paths
            valid_path = False
            for folder_type in ['checkpoints', 'loras', 'vae', 'controlnet']:
                try:
                    for base_path in folder_paths.get_folder_paths(folder_type):
                        if os.path.abspath(path).startswith(os.path.abspath(base_path)):
                            valid_path = True
                            break
                except Exception:
                    pass
                if valid_path:
                    break

            if not valid_path:
                return web.json_response({'error': '访问被拒绝'}, status=403)

        stat = os.stat(path)
        filename = os.path.basename(path)

        # Determine model type from directory structure
        model_type = 'unknown'
        for folder_type in ['checkpoints', 'loras', 'vae', 'controlnet', 'clip', 'embeddings', 'upscale_models']:
            try:
                for base_path in folder_paths.get_folder_paths(folder_type):
                    if path.startswith(base_path):
                        model_type = folder_type
                        break
            except Exception:
                pass

        metadata = {
            'filename': filename,
            'path': path,
            'type': model_type,
            'size': stat.st_size,
            'size_human': format_size(stat.st_size),
            'modified': stat.st_mtime,
            'hash': None  # Optional: compute hash on request
        }

        return web.json_response(metadata)
    except Exception as e:
        logging.error(f"[WMD] Error getting model metadata: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/model/delete")
async def delete_model(request):
    """Delete a model file"""
    try:
        data = await request.json()
        path = data.get('path', '')

        if not path:
            return web.json_response({'error': '需要路径'}, status=400)

        if not os.path.exists(path):
            return web.json_response({'error': '文件不存在'}, status=404)

        # Security check - ensure path is within models directories
        models_dir = folder_paths.models_dir
        valid_path = os.path.abspath(path).startswith(os.path.abspath(models_dir))

        if not valid_path:
            # Also check extra_model_paths
            for folder_type in ['checkpoints', 'loras', 'vae', 'controlnet', 'clip', 'embeddings', 'upscale_models']:
                try:
                    for base_path in folder_paths.get_folder_paths(folder_type):
                        if os.path.abspath(path).startswith(os.path.abspath(base_path)):
                            valid_path = True
                            break
                except Exception:
                    pass
                if valid_path:
                    break

        if not valid_path:
            return web.json_response({'error': '访问被拒绝：路径不在 models 目录内'}, status=403)

        # Ensure it's a file, not a directory
        if not os.path.isfile(path):
            return web.json_response({'error': '路径不是文件'}, status=400)

        # Delete the file
        os.remove(path)
        logging.info(f"[WMD] Deleted model: {path}")

        return web.json_response({'success': True})
    except Exception as e:
        logging.error(f"[WMD] Error deleting model: {e}")
        return web.json_response({'error': str(e)}, status=500)


# ============================================================================
# Fuzzy Matching System
# ============================================================================

def load_model_aliases():
    """Load model aliases from model-aliases.json"""
    try:
        if os.path.exists(MODEL_ALIASES_FILE):
            with open(MODEL_ALIASES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"[WMD] Error loading model aliases: {e}")

    # Default aliases structure
    return {
        "aliases": {},
        "patterns": [
            {"pattern": r"(.+)[-_]?fp16\.safetensors$", "base": "$1.safetensors"},
            {"pattern": r"(.+)[-_]?fp8\.safetensors$", "base": "$1.safetensors"},
            {"pattern": r"(.+)[-_]?bf16\.safetensors$", "base": "$1.safetensors"},
            {"pattern": r"(.+)[-_]?q8_0\.safetensors$", "base": "$1.safetensors"},
            {"pattern": r"(.+)[-_]?pruned\.safetensors$", "base": "$1.safetensors"},
            {"pattern": r"(.+)[-_]?ema\.safetensors$", "base": "$1.safetensors"}
        ]
    }


def resolve_model_alias(filename):
    """Check if filename is an alias for a known model and return canonical name"""
    aliases_data = load_model_aliases()
    filename_lower = filename.lower()

    # Direct alias lookup
    for canonical, alias_list in aliases_data.get('aliases', {}).items():
        if filename_lower in [a.lower() for a in alias_list]:
            return canonical

    # Pattern matching (for quantization variants)
    for pattern_def in aliases_data.get('patterns', []):
        match = re.match(pattern_def['pattern'], filename, re.IGNORECASE)
        if match:
            base = pattern_def['base'].replace('$1', match.group(1))
            return base

    return filename  # No alias found


def fuzzy_match_model(filename, threshold=0.70):
    """Find similar models with confidence scores"""
    matches = []
    base_name = os.path.splitext(filename)[0].lower()

    # First, check if this is an alias
    canonical = resolve_model_alias(filename)
    if canonical != filename:
        # Found via alias - this is a 100% match for the canonical name
        matches.append({
            'matched_name': canonical,
            'confidence': 100,
            'match_type': 'alias',
            'url': None  # Will be looked up separately
        })

    # Search in model-list.json
    model_list = load_model_list()
    for model in model_list:
        model_filename = model.get('filename', '')
        model_base = os.path.splitext(model_filename)[0].lower()

        # Exact match
        if model_base == base_name:
            matches.append({
                'filename': model_filename,
                'url': model.get('url', ''),
                'confidence': 100,
                'match_type': 'exact',
                'source': 'model_list'
            })
            continue

        # Fuzzy match using SequenceMatcher
        ratio = SequenceMatcher(None, base_name, model_base).ratio()
        if ratio >= threshold:
            matches.append({
                'filename': model_filename,
                'url': model.get('url', ''),
                'confidence': int(ratio * 100),
                'match_type': 'fuzzy',
                'source': 'model_list'
            })

    # Search in popular-models.json
    popular_models = load_popular_models()
    for model_name, model_info in popular_models.items():
        model_base = os.path.splitext(model_name)[0].lower()

        # Exact match
        if model_base == base_name:
            matches.append({
                'filename': model_name,
                'url': model_info.get('url', ''),
                'confidence': 100,
                'match_type': 'exact',
                'source': 'popular_models'
            })
            continue

        # Fuzzy match
        ratio = SequenceMatcher(None, base_name, model_base).ratio()
        if ratio >= threshold:
            matches.append({
                'filename': model_name,
                'url': model_info.get('url', ''),
                'confidence': int(ratio * 100),
                'match_type': 'fuzzy',
                'source': 'popular_models'
            })

    # Search in search_cache.json for previously found models
    search_cache = load_search_cache()
    for cached_name, cached_info in search_cache.items():
        cached_base = os.path.splitext(cached_name)[0].lower()

        # Exact match
        if cached_base == base_name:
            matches.append({
                'filename': cached_name,
                'url': cached_info.get('url', ''),
                'confidence': 100,
                'match_type': 'exact',
                'source': 'search_cache'
            })
            continue

        # Fuzzy match
        ratio = SequenceMatcher(None, base_name, cached_base).ratio()
        if ratio >= threshold:
            matches.append({
                'filename': cached_name,
                'url': cached_info.get('url', ''),
                'confidence': int(ratio * 100),
                'match_type': 'fuzzy',
                'source': 'search_cache'
            })

    # Remove duplicates (keep highest confidence)
    seen = {}
    for match in matches:
        key = match.get('filename', match.get('matched_name', ''))
        if key not in seen or match['confidence'] > seen[key]['confidence']:
            seen[key] = match

    # Sort by confidence descending
    unique_matches = list(seen.values())
    unique_matches.sort(key=lambda x: x['confidence'], reverse=True)

    return unique_matches[:5]  # Top 5 matches


@routes.post("/workflow-models/fuzzy-match")
async def fuzzy_match_endpoint(request):
    """Find similar models using fuzzy matching"""
    try:
        data = await request.json()
        filename = data.get('filename')
        threshold = data.get('threshold', 0.70)

        if not filename:
            return web.json_response({'error': '缺少文件名'}, status=400)

        matches = fuzzy_match_model(filename, threshold)

        return web.json_response({
            'success': True,
            'filename': filename,
            'matches': matches
        })
    except Exception as e:
        logging.error(f"[WMD] Fuzzy match error: {e}")
        return web.json_response({'error': str(e)}, status=500)


# ============================================================================
# URL Analysis for Raw Downloads
# ============================================================================

def guess_model_type_from_filename(filename, url_path=''):
    """Guess the model type and directory from filename and URL path"""
    filename_lower = filename.lower()
    url_path_lower = url_path.lower() if url_path else ''

    # Combine filename and URL path for checking
    # URL path often contains folder hints like /loras/, /vae/, /diffusion_models/
    check_text = filename_lower + ' ' + url_path_lower

    # Check for specific patterns (order matters - more specific first)
    if any(x in check_text for x in ['/loras/', '/lora/', '_lora', '-lora', 'lora_']):
        return 'lora', 'loras'
    elif any(x in check_text for x in ['/vae/', '_vae', '-vae', 'vae_']):
        return 'VAE', 'vae'
    elif any(x in check_text for x in ['/controlnet/', 'controlnet', 'control_v', 'control-']):
        return 'ControlNet', 'controlnet'
    elif any(x in check_text for x in ['/clip/', '/text_encoder/', 'clip_', 'text_encoder']):
        return 'CLIP', 'text_encoders'
    elif any(x in check_text for x in ['/diffusion_model/', '/diffusion_models/', '/unet/', 'unet_', 'diffusion_model']):
        return 'diffusion_model', 'diffusion_models'
    elif any(x in check_text for x in ['/upscale/', 'upscale', 'esrgan', 'realesrgan', '4x_', '2x_']):
        return 'Upscaler', 'upscale_models'
    elif any(x in check_text for x in ['/embedding/', 'embedding_', '.pt']):
        return 'Embedding', 'embeddings'
    elif any(x in check_text for x in ['/ipadapter/', 'ipadapter', 'ip_adapter', 'ip-adapter']):
        return 'IPAdapter', 'ipadapter'
    elif any(x in check_text for x in ['/inpaint/', 'inpaint']):
        return 'Inpaint', 'inpaint'
    elif any(x in check_text for x in ['/checkpoint/', '/checkpoints/']):
        return 'Checkpoint', 'checkpoints'
    elif filename_lower.endswith('.safetensors') or filename_lower.endswith('.ckpt'):
        # Default to checkpoint for .safetensors/.ckpt files
        return 'Checkpoint', 'checkpoints'
    else:
        return 'Unknown', 'checkpoints'


@routes.post("/workflow-models/analyze-url")
async def analyze_url_endpoint(request):
    """Analyze a URL to determine filename, model type, and suggested directory"""
    try:
        data = await request.json()
        url = data.get('url', '').strip()

        if not url:
            return web.json_response({'error': '缺少链接'}, status=400)

        filename = None
        size = None
        source = 'Direct URL'
        original_urn = None

        # Check if it's a CivitAI URN and convert to download URL
        if is_civitai_urn(url):
            original_urn = url
            model_id, version_id = parse_civitai_urn(url)
            url = civitai_urn_to_download_url(url)
            source = 'CivitAI URN'
            logging.info(f"[WMD] Converted URN to URL: {original_urn} -> {url}")

            # Try to get model info from CivitAI API using version ID
            try:
                api_url = f"https://civitai.com/api/v1/model-versions/{version_id}"
                api_response = requests.get(api_url, timeout=10)
                if api_response.status_code == 200:
                    version_data = api_response.json()
                    files = version_data.get('files', [])
                    if files:
                        # Get the primary file (usually the first/largest)
                        primary_file = files[0]
                        filename = primary_file.get('name', '')
                        file_size = primary_file.get('sizeKB', 0)
                        if file_size:
                            size_mb = file_size / 1024
                            if size_mb >= 1024:
                                size = f"{size_mb/1024:.2f} GB"
                            else:
                                size = f"{size_mb:.1f} MB"
            except Exception as e:
                logging.warning(f"[WMD] Could not fetch CivitAI version info: {e}")

        # Determine source and extract info
        elif 'huggingface.co' in url:
            source = 'HuggingFace'
            # Extract filename from HF URL
            # Format: huggingface.co/repo/model/resolve/main/path/to/file.safetensors
            hf_match = re.search(r'huggingface\.co/([^/]+/[^/]+)(?:/resolve/[^/]+)?/(.+?)(?:\?|$)', url)
            if hf_match:
                path = hf_match.group(2)
                filename = path.split('/')[-1]
                # Clean up query params from filename
                if '?' in filename:
                    filename = filename.split('?')[0]

        elif 'civitai.com' in url:
            source = 'CivitAI'
            # CivitAI URLs are complex - try to get info from API or headers
            # For now, we'll get filename from Content-Disposition header

        # If we don't have a filename yet, try HEAD request
        if not filename:
            try:
                headers = {}
                if 'huggingface.co' in url:
                    hf_token = get_huggingface_token()
                    if hf_token:
                        headers['Authorization'] = f'Bearer {hf_token}'
                elif 'civitai.com' in url:
                    civitai_key = get_civitai_api_key()
                    if civitai_key:
                        if '?' in url:
                            url = f"{url}&token={civitai_key}"
                        else:
                            url = f"{url}?token={civitai_key}"

                response = requests.head(url, headers=headers, allow_redirects=True, timeout=15)

                # Try Content-Disposition header
                cd = response.headers.get('Content-Disposition', '')
                if 'filename=' in cd:
                    # Parse filename from header
                    match = re.search(r'filename[*]?=["\']?([^"\';\n]+)', cd)
                    if match:
                        filename = match.group(1).strip()
                        # Handle UTF-8 encoded filenames
                        if filename.startswith("UTF-8''"):
                            filename = urllib.parse.unquote(filename[7:])

                # Get size from Content-Length
                content_length = response.headers.get('Content-Length')
                if content_length:
                    size_bytes = int(content_length)
                    size_mb = size_bytes / (1024 * 1024)
                    if size_mb >= 1024:
                        size = f"{size_mb/1024:.2f} GB"
                    else:
                        size = f"{size_mb:.1f} MB"

            except Exception as e:
                logging.warning(f"[WMD] Could not fetch URL headers: {e}")

        # If still no filename, extract from URL path
        if not filename:
            parsed = urllib.parse.urlparse(url)
            path = urllib.parse.unquote(parsed.path)
            filename = path.split('/')[-1]
            # Remove query params
            if '?' in filename:
                filename = filename.split('?')[0]

        # If filename is empty or just an extension, generate a name
        if not filename or filename.startswith('.'):
            filename = f"model_{int(asyncio.get_event_loop().time())}.safetensors"

        # Get URL path for type detection (includes folder structure hints)
        parsed_url = urllib.parse.urlparse(url)
        url_path = urllib.parse.unquote(parsed_url.path)

        # Guess model type and directory from both filename AND URL path
        model_type, suggested_dir = guess_model_type_from_filename(filename, url_path)

        return web.json_response({
            'success': True,
            'url': url,
            'filename': filename,
            'model_type': model_type,
            'suggested_directory': suggested_dir,
            'size': size,
            'source': source
        })

    except Exception as e:
        logging.error(f"[WMD] Analyze URL error: {e}")
        return web.json_response({'error': str(e)}, status=500)


# ============================================================================
# aria2 Integration with Resume Support
# ============================================================================

def check_aria2_available():
    """Check if aria2c is installed and available"""
    try:
        aria2_path = shutil.which('aria2c')
        if aria2_path:
            result = subprocess.run([aria2_path, '--version'], capture_output=True, timeout=5)
            return result.returncode == 0
    except Exception:
        pass
    return False


def _download_with_aria2(url, dest_path, download_id, headers=None):
    """Download using aria2c with resume support"""
    global active_download_count

    try:
        aria2_path = shutil.which('aria2c')
        if not aria2_path:
            return False, "aria2c not found"

        dest_dir = os.path.dirname(dest_path)
        dest_file = os.path.basename(dest_path)

        cmd = [
            aria2_path,
            '--dir=' + dest_dir,
            '--out=' + dest_file,
            '--continue=true',           # Resume support
            '--max-connection-per-server=4',
            '--split=4',
            '--min-split-size=1M',
            '--file-allocation=none',
            # Windows: CRL revocation check is offline on many machines and
            # aborts every aria2 TLS handshake without this flag
            '--check-certificate=false',
            '--console-log-level=error',
            '--summary-interval=1',
            apply_hf_mirror(url)
        ]

        if headers:
            for key, value in headers.items():
                cmd.append(f'--header={key}: {value}')

        with download_lock:
            download_progress[download_id]['status'] = 'downloading'
            download_progress[download_id]['method'] = 'aria2'

        # Run aria2c process
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True
        )

        # Monitor process
        while True:
            # Check for cancellation
            if download_id in cancelled_downloads:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
                return False, "Cancelled"

            # Check if process completed
            poll = process.poll()
            if poll is not None:
                break

            # Try to read progress (aria2c outputs progress to stderr)
            try:
                time.sleep(0.5)

                # Check file size for progress
                if os.path.exists(dest_path):
                    current_size = os.path.getsize(dest_path)
                    with download_lock:
                        download_progress[download_id]['downloaded'] = current_size
                        total = download_progress[download_id].get('total_size', 0)
                        if total > 0:
                            download_progress[download_id]['progress'] = int((current_size / total) * 100)
            except Exception:
                pass

        # Check result
        if process.returncode == 0:
            with download_lock:
                download_progress[download_id]['status'] = 'completed'
                download_progress[download_id]['progress'] = 100
            return True, None
        else:
            stderr = process.stderr.read() if process.stderr else 'Unknown error'
            return False, stderr

    except Exception as e:
        return False, str(e)
    finally:
        with download_queue_lock:
            active_download_count = max(0, active_download_count - 1)


def _resume_download_core(url, dest_path, download_id, headers=None, max_retries=0):
    """Resume-capable streaming download with automatic retry/backoff.

    - Writes to <dest>.partial, resumes via HTTP Range after interruptions.
    - Retries recoverable network errors, resuming from the bytes already on
      disk (this is what makes big models survive flaky connections instead
      of restarting from zero).
    - max_retries=0 (default) means UNLIMITED: on a link that drops every few
      hundred MB a fixed budget just forces the user to restart by hand, and
      every retry is cheap because the bytes are already there. The only way
      this gives up is a URL that makes no progress at all (see the guard
      below) or a non-retryable HTTP status.
    - Respects cancellation via cancelled_downloads.
    - HuggingFace URLs are rewritten to the configured mirror when enabled.
    Returns (success: bool, error: str|None).
    """
    url = apply_hf_mirror(url)
    partial_path = dest_path + '.partial'
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    # Seed resume from a truncated final-name file. Old crash-interrupted
    # downloads left complete-looking-but-short files at dest_path; when the
    # panel (integrity check) flags them missing and the user re-downloads,
    # rename the remnant to .partial so Range resume continues it instead of
    # throwing away e.g. 108MB of a 20.97GB model. Only seed for truncated
    # files; a corrupt header (shifted/garbage content) is not trustworthy
    # byte-for-byte prefix, so drop it and start clean.
    if not os.path.exists(partial_path) and os.path.exists(dest_path):
        status, expected = verify_safetensors_file(dest_path)
        if status == 'truncated':
            try:
                os.rename(dest_path, partial_path)
                logging.info(f"[WMD] Seeding resume from truncated file: {partial_path} "
                             f"({os.path.getsize(partial_path):,} of {expected:,} bytes)")
            except Exception as e:
                logging.warning(f"[WMD] Could not seed .partial from truncated file: {e}")
        elif status == 'corrupt':
            try:
                os.remove(dest_path)
                logging.info(f"[WMD] Removed corrupt file before fresh download: {dest_path}")
            except Exception as e:
                logging.warning(f"[WMD] Could not remove corrupt file: {e}")

    attempt = 0
    last_error = None
    # Unlimited retries need an escape hatch: a URL that never adds a single
    # byte to the partial file (dead link, server permanently refusing Range)
    # would otherwise loop forever. 5 consecutive zero-progress failures after
    # the startup grace period = hopeless. Any real advance resets the tally,
    # so a slow-but-alive link is never abandoned.
    progress_watermark = 0
    consecutive_stalls = 0
    startup_grace_until = time.time() + 120.0   # first 2 min get a free pass

    # Unlimited: loop on a condition that is always true; the only exits are
    # success, cancellation, non-retryable HTTP status, or the zero-progress
    # stall guard below. (while attempt <= max_retries with max_retries=0
    # would have meant exactly ONE attempt - the opposite of unlimited.)
    while True:
        resume_byte = 0
        if os.path.exists(partial_path):
            resume_byte = os.path.getsize(partial_path)

        # get-or-create: this thread may run standalone (direct tests, manual
        # resume) without a download_model endpoint having registered the id.
        # A bare subscript would raise KeyError and kill the whole download.
        with download_lock:
            prog = download_progress.setdefault(download_id, {})

        req_headers = dict(headers or {})
        if resume_byte > 0:
            req_headers['Range'] = f'bytes={resume_byte}-'
            logging.info(f"[WMD] Attempt {attempt + 1}: resuming {os.path.basename(dest_path)} from byte {resume_byte}")

        response = None
        try:
            response = requests.get(url, stream=True, timeout=(15, 60), allow_redirects=True, headers=req_headers)

            # If resume was rejected, server sent full body from 0 -> restart file
            if resume_byte > 0 and response.status_code == 200:
                logging.info("[WMD] Server ignored Range header, restarting from scratch")
                resume_byte = 0
                response = requests.get(url, stream=True, timeout=(15, 60), allow_redirects=True, headers=dict(headers or {}))

            if response.status_code in (401, 403):
                # Auth problems are not retryable
                return False, f"Unauthorized (HTTP {response.status_code}): this model needs a HuggingFace token with access to the repo (gated model). Configure it in Settings > 工作流模型管理."
            if response.status_code == 404:
                return False, "Model not found (HTTP 404): The file may have been moved or deleted."
            response.raise_for_status()

            total_size = int(response.headers.get('content-length', 0))
            if resume_byte > 0:
                total_size += resume_byte  # Add already downloaded bytes

            with download_lock:
                prog['total_size'] = total_size
                prog['status'] = 'downloading'
                prog['method'] = 'native_resume'
                prog.pop('error', None)
                # track start time + bytes for speed/ETA (per attempt window)
                prog['attempt_started_at'] = time.time()
                prog['attempt_start_bytes'] = resume_byte

            mode = 'ab' if resume_byte > 0 else 'wb'
            downloaded = resume_byte
            last_speed_update = time.time()
            last_speed_bytes = resume_byte

            with open(partial_path, mode) as f:
                # Read from the RAW stream instead of iter_content().
                #
                # iter_content() raises IncompleteRead when the CDN drops the
                # connection mid-transfer, and it DISCARDS the bytes that
                # were already buffered - so a 10GB file over a flaky link
                # restarted from the last good point every 60-180MB, over
                # and over. Reading the raw stream and tolerating a short
                # read keeps every byte that did arrive: the file grows
                # continuously and the retry (if any) resumes near where
                # the connection actually stopped.
                raw = response.raw
                while True:
                    if download_id in cancelled_downloads:
                        return False, "Cancelled"
                    try:
                        chunk = raw.read(1024 * 1024, decode_content=True)
                    except Exception as e:
                        # Connection died mid-read. Keep what we have; the
                        # completeness check below decides whether to retry.
                        last_error = f"read interrupted: {e}"
                        logging.warning(
                            f"[WMD] Read interrupted at {downloaded:,} bytes: {e}")
                        break
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    now = time.time()
                    # update speed every ~2s to keep polling cheap
                    if now - last_speed_update >= 2:
                        speed_bps = (downloaded - last_speed_bytes) / (now - last_speed_update)
                        with download_lock:
                            prog['speed_bps'] = int(speed_bps)
                            el = now - (prog.get('attempt_started_at') or now)
                            el_bytes = downloaded - (prog.get('attempt_start_bytes') or downloaded)
                            if el > 0:
                                prog['avg_speed_bps'] = int(el_bytes / el)
                            prog['progress'] = min(99, int(downloaded / total_size * 100)) if total_size else prog.get('progress', 0)
                        last_speed_update, last_speed_bytes = now, downloaded
                    else:
                        with download_lock:
                            prog['downloaded'] = downloaded
                            if total_size > 0:
                                prog['progress'] = min(99, int((downloaded / total_size) * 100))
                try:
                    f.flush()
                    os.fsync(f.fileno())
                except Exception:
                    pass

            # Completeness check: stream ended early without raising
            if total_size > 0 and downloaded < total_size:
                raise IOError(f"Incomplete: got {downloaded}/{total_size} bytes")

            # Rename partial to final
            if os.path.exists(dest_path):
                os.remove(dest_path)
            os.rename(partial_path, dest_path)

            with download_lock:
                prog['status'] = 'completed'
                prog['progress'] = 100
            return True, None

        except Exception as e:
            last_error = str(e)
            if isinstance(e, requests.exceptions.HTTPError) and e.response is not None and e.response.status_code in (401, 403, 404):
                return False, last_error
            attempt += 1
            is_cancel = 'Cancelled' in (last_error or '')
            if not is_cancel:
                snap = os.path.getsize(partial_path) if os.path.exists(partial_path) else 0
                if snap > progress_watermark:
                    progress_watermark, consecutive_stalls = snap, 0
                else:
                    consecutive_stalls += 1
                    if consecutive_stalls >= 5 and time.time() > startup_grace_until:
                        logging.warning(
                            f"[WMD] {os.path.basename(dest_path)}: {consecutive_stalls} consecutive failures "
                            f"with zero bytes added (stuck at {snap} B); giving up"
                        )
                        return False, f"Download stalled: {last_error} (partial file kept at {snap} bytes)"
            # max_retries<=0 -> unlimited retries (the working default).
            if max_retries > 0 and attempt > max_retries:
                break
            # A link that keeps moving bytes is alive: retry almost instantly
            # instead of waiting out a full exponential curve (10 drops on a
            # 19.5GB file would otherwise burn ~4 minutes just sleeping).
            if progress_watermark > 0 and consecutive_stalls == 0:
                backoff = 1
            else:
                backoff = min(30, 2 ** attempt)
            logging.warning(
                f"[WMD] Download interrupted ({last_error}); retry in {backoff}s "
                f"(attempt {attempt}{'/' + str(max_retries) if max_retries > 0 else ''}, partial kept for resume)"
            )
            time.sleep(backoff)
        finally:
            try:
                if response is not None:
                    response.close()
            except Exception:
                pass

    return False, last_error or "Download failed"


def _download_native_with_resume(url, dest_path, download_id, headers=None):
    """Download using requests with resume support (.partial file tracking).
    Thin wrapper kept for the queue worker; handles the concurrency counter."""
    global active_download_count
    try:
        return _resume_download_core(url, dest_path, download_id, headers)
    finally:
        with download_queue_lock:
            active_download_count = max(0, active_download_count - 1)


# ============================================================================
# Download Queue System
# ============================================================================

download_queue_worker_running = False


def start_download_queue_worker():
    """Start the background download queue worker if not already running"""
    global download_queue_worker_running

    if download_queue_worker_running:
        return

    download_queue_worker_running = True
    worker_thread = threading.Thread(target=_download_queue_worker, daemon=True)
    worker_thread.start()
    logging.info("[WMD] Download queue worker started")


def _download_queue_worker():
    """Background thread that processes download queue"""
    global download_queue_worker_running, active_download_count

    while download_queue_worker_running:
        try:
            with download_queue_lock:
                # Check if we can start a new download
                current_max = max_parallel_downloads
                if current_max == 0:  # 0 means unlimited
                    can_start = len(download_queue) > 0
                else:
                    can_start = active_download_count < current_max and len(download_queue) > 0

                if can_start:
                    next_download = download_queue.pop(0)
                    active_download_count += 1

                    # Start download in separate thread
                    thread = threading.Thread(
                        target=_process_queued_download,
                        args=(next_download,),
                        daemon=True
                    )
                    thread.start()
        except Exception as e:
            logging.error(f"[WMD] Queue worker error: {e}")

        time.sleep(0.5)


def _process_queued_download(download_info):
    """Process a download from the queue"""
    global active_download_count

    download_id = download_info['download_id']
    url = download_info['url']
    dest_path = download_info['dest_path']
    headers = download_info.get('headers', {})
    filename = download_info.get('filename', '')

    try:
        with download_lock:
            download_progress[download_id] = {
                'status': 'starting',
                'progress': 0,
                'filename': filename,
                'total_size': 0,
                'downloaded': 0,
                'queued': False
            }

        # Prefer aria2 when available (multi-connection), fall back to the
        # resume engine with unlimited retries. Both write to the same final
        # path, so a swap between them never loses already-downloaded bytes.
        # aria2's own --continue=true handles its resume.
        aria2_available = check_aria2_available()

        if aria2_available:
            success, error = _download_with_aria2(url, dest_path, download_id, headers)
        else:
            success, error = _download_native_with_resume(url, dest_path, download_id, headers)

        # aria2 exit != 0 is frequently a transient network drop; the resume
        # engine's unlimited retry + Range makes it recover without the user.
        if not success and error != "Cancelled" and aria2_available:
            partial = dest_path + '.partial'
            has_partial = os.path.exists(partial) and os.path.getsize(partial) > 0
            logging.warning(
                f"[WMD] aria2 failed for {filename} ({error}); retrying with resume engine "
                f"(partial file: {'yes' if has_partial else 'no'})"
            )
            if not has_partial:
                # aria2 writes the destination directly, so the resume engine
                # can only continue a file it already produced. If aria2 left
                # nothing behind, seed the .partial from whatever exists.
                if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
                    try:
                        os.rename(dest_path, partial)
                    except Exception as e:
                        logging.debug(f"[WMD] Could not seed .partial from aria2 output: {e}")
            success, error = _download_native_with_resume(url, dest_path, download_id, headers)

        if not success and error != "Cancelled":
            with download_lock:
                download_progress[download_id]['status'] = 'error'
                download_progress[download_id]['error'] = error
            # Add to download history
            add_to_download_history({
                'id': download_id,
                'filename': filename,
                'status': 'error',
                'error': error,
                'directory': os.path.dirname(dest_path)
            })

        # Cache URL on success
        if success:
            source = 'civitai' if 'civitai.com' in url else ('huggingface' if 'huggingface.co' in url else 'direct')
            hf_repo, hf_path = extract_huggingface_info(url)
            _cache_download_url(filename, url, source, hf_repo=hf_repo, hf_path=hf_path)
            # Add to download history
            add_to_download_history({
                'id': download_id,
                'filename': filename,
                'status': 'completed',
                'total_size': download_progress.get(download_id, {}).get('total_size', 0),
                'directory': os.path.dirname(dest_path)
            })
            logging.info(f"[WMD] Download completed: {filename}")

    except Exception as e:
        logging.error(f"[WMD] Queued download error: {e}")
        with download_lock:
            download_progress[download_id]['status'] = 'error'
            download_progress[download_id]['error'] = str(e)
        # Add to download history
        add_to_download_history({
            'id': download_id,
            'filename': filename,
            'status': 'error',
            'error': str(e),
            'directory': os.path.dirname(dest_path) if dest_path else ''
        })
    finally:
        with download_queue_lock:
            active_download_count = max(0, active_download_count - 1)
        cancelled_downloads.discard(download_id)


@routes.post("/workflow-models/queue-download")
async def queue_download_endpoint(request):
    """Add a download to the queue"""
    try:
        data = await request.json()

        url = data.get('url')
        filename = data.get('filename')
        target_dir = data.get('directory')

        if not all([url, filename, target_dir]):
            return web.json_response({'error': '缺少必填字段'}, status=400)

        # Check if URL is actually a CivitAI URN and convert it
        if is_civitai_urn(url):
            civitai_url = civitai_urn_to_download_url(url)
            if civitai_url:
                logging.info(f"[WMD] Converted CivitAI URN to download URL: {url} -> {civitai_url}")
                url = civitai_url
            else:
                return web.json_response({'error': '无效的 CivitAI URN 格式'}, status=400)

        # Generate download ID
        download_id = f"queued_{filename}_{int(asyncio.get_event_loop().time() * 1000)}".replace('/', '_').replace('\\', '_')

        # Prepare headers
        headers = {}
        if 'civitai.com' in url:
            civitai_key = get_civitai_api_key()
            if civitai_key:
                if '?' in url:
                    url = f"{url}&token={civitai_key}"
                else:
                    url = f"{url}?token={civitai_key}"
        elif 'huggingface.co' in url:
            hf_token = get_huggingface_token()
            if hf_token:
                headers['Authorization'] = f'Bearer {hf_token}'

        # Normalize path
        target_dir_normalized = target_dir.replace('/', os.sep).replace('\\', os.sep)
        dest_path = os.path.join(folder_paths.models_dir, target_dir_normalized, filename)

        # Create directory
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        # Add to queue
        download_info = {
            'download_id': download_id,
            'url': url,
            'dest_path': dest_path,
            'filename': filename,
            'headers': headers
        }

        with download_queue_lock:
            download_queue.append(download_info)

        # Initialize progress tracking
        with download_lock:
            download_progress[download_id] = {
                'status': 'queued',
                'progress': 0,
                'filename': filename,
                'total_size': 0,
                'downloaded': 0,
                'queued': True
            }

        # Ensure queue worker is running
        start_download_queue_worker()

        return web.json_response({
            'success': True,
            'download_id': download_id,
            'queued': True,
            'message': f'已将 {filename} 加入下载队列'
        })

    except Exception as e:
        logging.error(f"[WMD] Queue download error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.get("/workflow-models/queue-status")
async def get_queue_status(request):
    """Get download queue status"""
    try:
        with download_queue_lock:
            queue_count = len(download_queue)
            active = active_download_count

        return web.json_response({
            'queued': queue_count,
            'active': active,
            'max_parallel': max_parallel_downloads,
            'aria2_available': check_aria2_available()
        })
    except Exception as e:
        logging.error(f"[WMD] Queue status error: {e}")
        return web.json_response({'error': str(e)}, status=500)


@routes.post("/workflow-models/settings/parallel")
async def set_parallel_downloads(request):
    """Set max parallel downloads"""
    global max_parallel_downloads

    try:
        data = await request.json()
        value = data.get('max_parallel', 3)

        # Validate value
        if not isinstance(value, int) or value < 0 or value > 50:
            return web.json_response({'error': '无效取值（须为 0-50）'}, status=400)

        max_parallel_downloads = value

        # Save to settings
        settings = load_settings()
        settings['max_parallel_downloads'] = value
        save_settings(settings)

        logging.info(f"[WMD] Max parallel downloads set to: {value}")

        return web.json_response({
            'success': True,
            'max_parallel': value
        })
    except Exception as e:
        logging.error(f"[WMD] Set parallel downloads error: {e}")
        return web.json_response({'error': str(e)}, status=500)


# Initialize max_parallel from settings on load
def _init_parallel_setting():
    global max_parallel_downloads
    settings = load_settings()
    max_parallel_downloads = settings.get('max_parallel_downloads', 3)

_init_parallel_setting()

# Load Tavily search cache on startup
load_tavily_cache()

logging.info("[Workflow-Models-Downloader] Extension loaded successfully")
