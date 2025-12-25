from __future__ import annotations

import argparse

from .service import HeadlessService

# Small CLI for smoke tests or quick completions.

def main() -> int:
    parser = argparse.ArgumentParser(description="Persponify headless companion")
    parser.add_argument("--config", required=True, help="Path to config.json")
    parser.add_argument("--adapter", default=None, help="Adapter name override")
    parser.add_argument("--system", default=None, help="System prompt")
    parser.add_argument("--prompt", required=True, help="User prompt")
    parser.add_argument("--stream", action="store_true", help="Stream output")
    args = parser.parse_args()

    svc = HeadlessService.from_path(args.config)

    if args.stream:
        for chunk in svc.stream(args.prompt, system=args.system, adapter_name=args.adapter):
            print(chunk, end="", flush=True)
        print()
        return 0

    result = svc.complete(args.prompt, system=args.system, adapter_name=args.adapter)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
