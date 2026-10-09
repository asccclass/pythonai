from __future__ import annotations
import json
import os
import time
from urllib.parse import quote
from typing import Any, Callable
from urllib import request
from communication_models import InboundMessage, OutboundMessage
from .security import development_mode, fresh_timestamp, validate_api_result

class DiscordAdapter:
    platform = "discord"

    def __init__(
        self,
        bot_token: str | None = None,
        public_key: str | None = None,
        http_post: Callable[[str, dict[str, Any], dict[str, str]], Any] | None = None,
        http_patch: Callable[[str, dict[str, Any], dict[str, str]], Any] | None = None,
    ) -> None:
        self.bot_token = os.environ.get("DISCORD_BOT_TOKEN", "") if bot_token is None else bot_token
        self.public_key = os.environ.get("DISCORD_PUBLIC_KEY", "") if public_key is None else public_key
        self.http_post = http_post or _json_post_with_headers
        self.http_patch = http_patch or (lambda url, payload, headers: _json_post_with_headers(url, payload, headers, method="PATCH"))

    def verify_request(self, headers: dict[str, str], body: bytes, query: dict[str, str] | None = None) -> bool:
        if not self.public_key:
            return development_mode()
            
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        signature = normalized_headers.get("x-signature-ed25519")
        timestamp = normalized_headers.get("x-signature-timestamp")
        
        if not signature or not timestamp:
            return False
        if not fresh_timestamp(timestamp):
            return False
            
        try:
            from nacl.signing import VerifyKey
            from nacl.exceptions import BadSignatureError
            
            verify_key = VerifyKey(bytes.fromhex(self.public_key))
            verify_key.verify(timestamp.encode() + body, bytes.fromhex(signature))
            return True
        except Exception:
            return False

    def parse_events(
        self,
        headers: dict[str, str],
        body: bytes,
        query: dict[str, str] | None = None,
    ) -> list[InboundMessage]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            return []
            
        # PING handling (Discord requires returning type 1)
        if payload.get("type") == 1:
            # Note: The webhook handler needs to return {"type": 1} for pings.
            # We can handle this by adding a special message type or letting the server know.
            return [
                InboundMessage(
                    platform=self.platform,
                    platform_message_id="ping",
                    conversation_id="ping",
                    sender_id="ping",
                    text="/ping",
                    raw_payload=payload,
                )
            ]
            
        if payload.get("type") != 2: # APPLICATION_COMMAND
            return []
            
        data = payload.get("data", {})
        
        # We assume slash commands like /ask <text> or just text in options
        text = ""
        if data.get("name"):
            text = f"/{data.get('name')}"
            options = data.get("options", [])
            for opt in options:
                text += f" {opt.get('value', '')}"
                
        if not text:
            return []
            
        channel_id = payload.get("channel_id")
        member = payload.get("member", {})
        user = member.get("user", payload.get("user", {}))
        sender_id = user.get("id")
        interaction_token = payload.get("token")
        interaction_id = payload.get("id")
        
        if not channel_id or not sender_id or not interaction_id or not interaction_token or not payload.get("application_id"):
            return []
            
        return [
            InboundMessage(
                platform=self.platform,
                platform_message_id=interaction_id,
                conversation_id=channel_id,
                sender_id=sender_id,
                text=text,
                raw_payload={"token": interaction_token, "interaction_id": interaction_id,
                             "application_id": str(payload["application_id"]), "received_at": time.time()},
            )
        ]

    def send_message(self, message: OutboundMessage) -> None:
        context = message.reply_context
        token, app_id = context.get("token"), context.get("application_id")
        received = context.get("received_at", 0)
        if token and app_id and 0 <= time.time() - received < 14 * 60:
            for index in range(0, max(len(message.text), 1), 2000):
                target = f"https://discord.com/api/v10/webhooks/{quote(str(app_id), safe='')}/{quote(str(token), safe='')}"
                send = self.http_patch if index == 0 else self.http_post
                result = send(target + ("/messages/@original" if index == 0 else "?wait=true"),
                    {"content": message.text[index:index + 2000], "allowed_mentions": {"parse": []}}, {})
                validate_api_result(result, "Discord")
            return
        if not self.bot_token:
            raise ValueError("DISCORD_BOT_TOKEN is required to send messages")
            
        # Send a regular message to the channel (could also use interaction followup if token is fresh)
        for index in range(0, max(len(message.text), 1), 2000):
            result = self.http_post(f"https://discord.com/api/v10/channels/{quote(message.conversation_id, safe='')}/messages",
                           {"content": message.text[index:index + 2000], "allowed_mentions": {"parse": []}},
                           {"Authorization": f"Bot {self.bot_token}"})
            validate_api_result(result, "Discord")

def _json_post_with_headers(url: str, payload: dict[str, Any], headers: dict[str, str], method="POST") -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers)
    req = request.Request(
        url,
        data=data,
        headers=req_headers,
        method=method,
    )
    with request.urlopen(req, timeout=20) as response:
        return response.read()
