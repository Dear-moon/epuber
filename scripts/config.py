"""
统一配置读取。优先级: config.json > 环境变量 > 默认值。
所有脚本导入此模块即可获取配置，无需各自解析。
"""
import json, os, sys
from pathlib import Path

_CONFIG = None
_SKILL_ROOT = Path(__file__).resolve().parent.parent


def _find_config():
    """Find config.json, checking skill root and cwd."""
    candidates = [
        _SKILL_ROOT / "config.json",
        Path.cwd() / "config.json",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def _load_config():
    """Load config.json once, cache result."""
    global _CONFIG
    if _CONFIG is not None:
        return _CONFIG

    config_path = _find_config()
    if config_path:
        with open(config_path, 'r', encoding='utf-8') as f:
            _CONFIG = json.load(f)
    else:
        _CONFIG = {}
    return _CONFIG


def get(key, default=None):
    """Get a config value by dotted key path, e.g. 'lightnovel.refresh_token'.

    Priority: config.json > environment variable > default.
    Environment variable names are derived by uppercasing and replacing . with _:
      'lightnovel.refresh_token' -> LIGHTNOVEL_REFRESH_TOKEN
    """
    env_key = key.upper().replace('.', '_')
    env_val = os.environ.get(env_key)
    if env_val:
        return env_val

    cfg = _load_config()
    val = cfg
    for part in key.split('.'):
        if isinstance(val, dict) and part in val:
            val = val[part]
        else:
            return default
    return val


def get_fetch_dir():
    """Get fetch directory, creating it if needed."""
    d = get('fetch_dir', None)
    if d:
        p = Path(d)
    else:
        p = Path.cwd() / "fetch"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_edge_path():
    """Get Edge browser path. Auto-detect on Windows if not configured."""
    path = get('edge_path', None)
    if path and Path(path).exists():
        return path

    # Auto-detect on Windows
    if sys.platform == 'win32':
        candidates = [
            r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
            r'C:\Program Files\Microsoft\Edge\Application\msedge.exe',
        ]
        # Also check Local AppData (user install)
        local = os.environ.get('LOCALAPPDATA', '')
        if local:
            candidates.insert(0, Path(local) / 'Microsoft' / 'Edge' / 'Application' / 'msedge.exe')

        for c in candidates:
            if Path(str(c)).exists():
                return str(c)

    return path or 'msedge'  # fallback to PATH


def get_refresh_token():
    """Get lightnovel.app refresh token. Returns None if not configured."""
    token = get('lightnovel.refresh_token', None)
    if token and token != 'YOUR_REFRESH_TOKEN_HERE':
        return token
    return None
