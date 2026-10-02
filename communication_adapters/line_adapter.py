from __future__ import annotations
import json
import os
import hmac
import hashlib
import base64
from typing import Any, Callable
from urllib import request
from communication_models import InboundMessage, OutboundMessage

class LineAdapter:
    platform = "line"

    def __init__(
        self,
        channel_access_token: str | None = None,
        channel_secret: str | None = None,
        http_post: Callable[[str, dict[str, Any], dict[str, str]], Any] | None = None,
    ) -> None:
        self.channel_access_token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "") if channel_access_token is None else channel_access_token
        self.channel_secret = os.environ.get("LINE_CHANNEL_SECRET", "") if channel_secret is None else channel_secret
        self.http_post = http_post or _json_post_with_headers

    def verify_request(self, headers: dict[str, str], body: bytes, query: dict[str, str] | None = None) -> bool:
        if not self.channel_secret:
            return True
        normalized_headers = {key.lower(): value for key, value in headers.items()}
        signature = normalized_headers.get("x-line-signature")
        if not signature:
            return False
        
        hash_val = hmac.new(
            self.channel_secret.encode('utf-8'),
            body,
            hashlib.sha256
        ).digest()
        expected_signature = base64.b64encode(hash_val).decode('utf-8')
        
        return hmac.compare_digest(signature, expected_signature)

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
            
        events = payload.get("events", [])
        if not isinstance(events, list):
            return []
            
        inbound_messages = []
        for event in events:
            if event.get("type") != "message":
                continue
            message = event.get("message", {})
            if message.get("type") != "text":
                continue
                
            text = message.get("text")
            if not text:
                continue
                
            source = event.get("source", {})
            sender_id = source.get("userId", "")
            
            # Use groupId or roomId if available for conversation, else userId
            conversation_id = source.get("groupId") or source.get("roomId") or sender_id
            
            if not conversation_id or not sender_id:
                continue
                
            reply_token = event.get("replyToken")
            platform_message_id = message.get("id", "")
            
            # Storing replyToken in raw_payload to be able to use reply API later if needed
            inbound_messages.append(
                InboundMessage(
                    platform=self.platform,
                    platform_message_id=platform_message_id,
                    conversation_id=conversation_id,
                    sender_id=sender_id,
                    text=str(text),
                    raw_payload={"replyToken": reply_token, "event": event},
                )
            )
        return inbound_messages

    def send_message(self, message: OutboundMessage) -> None:
        if not self.channel_access_token:
            raise ValueError("LINE_CHANNEL_ACCESS_TOKEN is required to send messages")
        
        # In LINE, we use push message API because we might reply asynchronously long after the replyToken expires
        self.http_post(
            "https://api.line.me/v2/bot/message/push",
            {
                "to": message.conversation_id,
                "messages": [
                    {
                        "type": "text",
                        "text": message.text
                    }
                ]
            },
            {
                "Authorization": f"Bearer {self.channel_access_token}"
            }
        )

def _json_post_with_headers(url: str, payload: dict[str, Any], headers: dict[str, str]) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers)
    req = request.Request(
        url,
        data=data,
        headers=req_headers,
        method="POST",
    )
    with request.urlopen(req, timeout=20) as response:
        return response.read()