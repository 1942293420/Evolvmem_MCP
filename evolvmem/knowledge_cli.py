"""Local owner entrypoint for the editable knowledge management skill.

python -m evolvmem.knowledge_cli GET items --json '{"project":"evolvmem"}'
python -m evolvmem.knowledge_cli POST items/123/assign --file request.json
"""
import argparse
import json
from pathlib import Path
import sys

from evolvmem.config import Config
from evolvmem.context_service import ContextService
from evolvmem.knowledge_api import dispatch
from evolvmem.web_server import _context_mode


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('method', choices=['GET', 'POST'])
    parser.add_argument('route', help='projects, items, items/ID, rules, preview, organize, batch, items/ID/{update,assign,transition}')
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument('--json', default='{}')
    inputs.add_argument('--file', type=Path, help='UTF-8 JSON request; use a file for multiline content')
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    config = Config.from_file()
    if args.data_dir:
        config.data_dir = args.data_dir
    service = ContextService(config)
    try:
        service.initialize(mode=_context_mode(config), adapter='knowledge-cli')
        service._legacy_backend()
        body = json.loads(args.file.read_text(encoding='utf-8') if args.file else args.json)
        print(json.dumps(dispatch(service, args.method, args.route, body), ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
    finally:
        service.close()


if __name__ == '__main__':
    main()
