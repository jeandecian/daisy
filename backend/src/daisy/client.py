"""Minimal CLI client for the Daisy POST /chat/stream SSE endpoint."""

import argparse
import os
import sys
from collections.abc import Iterable, Iterator
from typing import Final

import httpx
from pydantic import ValidationError

from daisy.schema import DEFAULT_MODEL, ChatRequest, ErrorData, StatusData, TokenData

DEFAULT_URL: Final[str] = "http://127.0.0.1:8000"
STREAM_PATH: Final[str] = "/chat/stream"
TOKEN_ENV: Final[str] = "DAISY_AUTH_TOKEN"
EXIT_OK: Final[int] = 0
EXIT_FAILURE: Final[int] = 1
EXIT_USAGE: Final[int] = 2


def iter_sse(lines: Iterable[str]) -> Iterator[tuple[str, str]]:
    """Parse a text/event-stream line iterable into (event, data) pairs."""
    event = "message"
    data: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    if data:
        yield event, "\n".join(data)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments for the chat client."""
    parser = argparse.ArgumentParser(description="Stream a chat reply from Daisy.")
    parser.add_argument("message", help="User message to send.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Backend base URL.")
    parser.add_argument(
        "--token",
        default=os.environ.get(TOKEN_ENV, ""),
        help=f"Bearer token (defaults to ${TOKEN_ENV}).",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model name.")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    """Send the chat request and print the streamed reply; return an exit code."""
    if not args.token:
        print(f"error: no token given (use --token or ${TOKEN_ENV})", file=sys.stderr)
        return EXIT_USAGE
    url = args.url.rstrip("/") + STREAM_PATH
    headers = {"Authorization": f"Bearer {args.token}", "Accept": "text/event-stream"}
    printed_tokens = False
    try:
        request = ChatRequest(message=args.message, model=args.model)
        with httpx.Client(timeout=httpx.Timeout(10.0, read=300.0)) as client:
            with client.stream(
                "POST", url, json=request.model_dump(), headers=headers
            ) as response:
                if response.status_code != 200:
                    body = response.read().decode(errors="replace")
                    print(f"error: HTTP {response.status_code}: {body}", file=sys.stderr)
                    return EXIT_FAILURE
                for name, data in iter_sse(response.iter_lines()):
                    if name == "token":
                        sys.stdout.write(TokenData.model_validate_json(data).content)
                        sys.stdout.flush()
                        printed_tokens = True
                    elif name == "status":
                        status = StatusData.model_validate_json(data).message
                        print(f"[status] {status}", file=sys.stderr)
                    elif name == "error":
                        message = ErrorData.model_validate_json(data).message
                        print(f"error: {message}", file=sys.stderr)
                        return EXIT_FAILURE
                    else:
                        print(f"error: unexpected event '{name}'", file=sys.stderr)
                        return EXIT_FAILURE
    except ValidationError as exc:
        print(f"error: invalid request or event: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except httpx.HTTPError as exc:
        print(f"error: request failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    if printed_tokens:
        sys.stdout.write("\n")
    return EXIT_OK


def main(argv: list[str] | None = None) -> None:
    """Entry point for `python -m daisy.client`."""
    sys.exit(run(parse_args(argv)))


if __name__ == "__main__":
    main()
