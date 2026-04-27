#!/usr/bin/env python3
import argparse
import os

import livekit.api as api


def build_token(
    api_key: str,
    api_secret: str,
    room: str,
    identity: str,
    can_publish: bool,
    can_subscribe: bool,
) -> str:
    grant = api.VideoGrants(
        room_join=True,
        room=room,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
    )
    return (
        api.AccessToken(api_key, api_secret)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(grant)
        .to_jwt()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate LiveKit JWT token")
    parser.add_argument("--room", required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--role", choices=["publisher", "viewer"], default="publisher")
    parser.add_argument("--api-key", default=os.getenv("LIVEKIT_API_KEY", ""))
    parser.add_argument("--api-secret", default=os.getenv("LIVEKIT_API_SECRET", ""))
    args = parser.parse_args()

    if not args.api_key or not args.api_secret:
        raise SystemExit("LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required")

    is_publisher = args.role == "publisher"
    token = build_token(
        api_key=args.api_key,
        api_secret=args.api_secret,
        room=args.room,
        identity=args.identity,
        can_publish=is_publisher,
        can_subscribe=True,
    )
    print(token)


if __name__ == "__main__":
    main()
