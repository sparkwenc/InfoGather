import tomllib
import argparse

from .sources import InfoSources
from .storage import InfoStorage
from .filters import InfoFilters
from .paths import DEFAULT_CONFIG_PATH, DEFAULT_DB_PATH, DEFAULT_OUTPUT_PATH

DEFAULT_CONFIG = DEFAULT_CONFIG_PATH
DEFAULT_OUTPUT = DEFAULT_OUTPUT_PATH


def _cmd_ins(args: argparse.Namespace) -> int:
    with open(args.conf, "rb") as f:
        conf = tomllib.load(f)
    sources = InfoSources(conf)
    with InfoStorage(args.db_path) as storage:
        entries = sources.get_normalized_feeds()
        storage.insert_entries(entries)
    return 0


def _cmd_fav(args: argparse.Namespace) -> int:
    with InfoStorage(args.db_path) as storage:
        for srce_id in args.id:
            stat = storage.favor_entry(args.ty, srce_id, args.fav)
            print(
                f"Updated favored status for {args.ty}:{srce_id} to {args.fav} with {stat}.")
    return 0


def _cmd_exp(args: argparse.Namespace) -> int:
    with InfoStorage(args.db_path) as storage:
        fn = getattr(InfoFilters, args.filter, None)
        if fn is None or not callable(fn):
            raise ValueError(f"unknown filter function: {args.filter}")
        storage.export_entries(args.output, fn)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH,
                        help="SQLite database path")

    subparsers = parser.add_subparsers(dest="command", required=True)

    p_ins = subparsers.add_parser("ins", help="fetch new entries")
    p_ins.add_argument("-c", "--conf", default=DEFAULT_CONFIG,
                       help="configuration file path")
    p_ins.set_defaults(func=_cmd_ins)

    p_fav = subparsers.add_parser("fav", help="set favored entries")
    p_fav.add_argument("-t", "--ty", required=True, help="source type")
    p_fav.add_argument("-i", "--id", required=True, nargs="+",
                       help="source IDs")
    p_fav.add_argument("-f", "--fav", required=True,
                       type=int, choices=[0, 1])
    p_fav.set_defaults(func=_cmd_fav)

    p_exp = subparsers.add_parser("exp", help="export entries to markdown")
    p_exp.add_argument("-o", "--output", default=DEFAULT_OUTPUT,
                       help="output markdown file")
    p_exp.add_argument("-f", "--filter", default="filter_ingestion",
                       help="filter function name")
    p_exp.set_defaults(func=_cmd_exp)

    return parser


def main() -> int:
    args = _build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
