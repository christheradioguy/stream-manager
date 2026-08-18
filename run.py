#!/usr/bin/env python3
"""Entry point: python run.py [--host H] [--port P]"""

from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Streams Manager")
    parser.add_argument(
        "--host",
        default=os.environ.get("STREAMS_MANAGER_HOST", "127.0.0.1"),
        help="bind address (default 127.0.0.1; use 0.0.0.0 to expose on the LAN)",
    )
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("STREAMS_MANAGER_PORT", "8409"))
    )
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    parser.add_argument(
        "--proxy-headers",
        action="store_true",
        help="trust X-Forwarded-* headers (use behind a reverse proxy)",
    )
    args = parser.parse_args()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        proxy_headers=args.proxy_headers,
        forwarded_allow_ips="*" if args.proxy_headers else None,
        access_log=False,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
