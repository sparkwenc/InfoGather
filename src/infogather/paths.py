import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
CHECKOUT_ROOT = PACKAGE_DIR.parents[1]
PACKAGE_CONFIG_PATH = PACKAGE_DIR / "config.toml"

if (CHECKOUT_ROOT / "pyproject.toml").is_file():
    DEFAULT_DB_PATH = CHECKOUT_ROOT / "data" / "entries.db"
    DEFAULT_CONFIG_PATH = CHECKOUT_ROOT / "conf" / "config.toml"
else:
    data_home = Path(
        os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share"
    )
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    )
    user_config = config_home / "infogather" / "config.toml"
    DEFAULT_DB_PATH = data_home / "infogather" / "entries.db"
    DEFAULT_CONFIG_PATH = (
        user_config if user_config.is_file() else PACKAGE_CONFIG_PATH
    )

WEB_DIR = PACKAGE_DIR / "web"
