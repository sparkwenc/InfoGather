import os
from pathlib import Path


PACKAGE_DIR = Path(__file__).resolve().parent
CHECKOUT_ROOT = PACKAGE_DIR.parents[1]
EXAMPLE_CONFIG_PATH = PACKAGE_DIR / "example.toml"

if (CHECKOUT_ROOT / "pyproject.toml").is_file():
    DEFAULT_DB_PATH = CHECKOUT_ROOT / "data" / "entries.db"
    checkout_config = CHECKOUT_ROOT / "conf" / "config.toml"
    DEFAULT_CONFIG_PATH = (
        checkout_config
        if checkout_config.is_file()
        else CHECKOUT_ROOT / "conf" / "example.toml"
    )
    DEFAULT_OUTPUT_PATH = CHECKOUT_ROOT / "output" / "feeds.md"
else:
    data_home = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    )
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    )
    user_config = config_home / "infogather" / "config.toml"
    DEFAULT_DB_PATH = data_home / "infogather" / "entries.db"
    DEFAULT_CONFIG_PATH = (
        user_config if user_config.is_file() else EXAMPLE_CONFIG_PATH
    )
    DEFAULT_OUTPUT_PATH = data_home / "infogather" / "feeds.md"

WEB_DIR = PACKAGE_DIR / "web"
