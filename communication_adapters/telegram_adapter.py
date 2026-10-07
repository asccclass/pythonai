from __future__ import annotations

import json
import mimetypes
import os
from pathlib import PurePosixPath
from typing import Any, Callable
from urllib import request

from communication_models import InboundAttachment, InboundMessage, OutboundMessage


TELEGRAM_MESSAGE_LIMIT = 4096


class TelegramAdapter:
    platform = "telegram"

    def __init__(
        self,
        bot_token: str | None = None,
        webhook_secret: str | None = None,
        http_post: Callable[[str, dict[str, Any]], Any] | None = None,
        http_get: Callable[[str, dict[str, Any]], Any] | None = None,
        http_download: Callable[[str], bytes] | None = None,
    ) -> None:
        self.bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "") if bot_token is None else bot_token
        self.webhook_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "") if webhook_secret is None else webhook_secret
        self.http_post = http_post or _json_post
        self.http_get = http_get or _json_get
        self.http_download = http_download or _download_bytes

    def verify_request(self, headers: dict[str, str], body: bytes, query: dict[str, str] | None = None) -> bool:
        if not self.webhook_secret:
            return True
        normalized = {key.lower(): value for key, value in headers.items()}
        return normalized.get("x-telegram-bot-api-secret-token") == self.webhook_secret

    def parse_events(
        self,
        headers: dict[str, str],
        body: bytes,
        query: dict[str, str] | None = None,
    ) -> list[InboundMessage]:
        payload = json.loads(body.decode("utf-8"))
        message = payload.get("message") or payload.get("edited_message")
        if not isinstance(message, dict):
            return []
        text = message.get("text") or message.get("caption") or ""
        attachments = self._extract_attachments(message)
        if not text and not attachments:
            return []
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        update_id = payload.get("update_id")
        message_id = message.get("message_id")
        platform_message_id = f"{update_id}:{message_id}"
        conversation_id = str(chat.get("id", ""))
        sender_id = str(sender.get("id", conversation_id))
        if not conversation_id or not sender_id:
            return []
        return [
            InboundMessage(
                platform=self.platform,
                platform_message_id=platform_message_id,
                conversation_id=conversation_id,
                sender_id=sender_id,
                text=str(text) or self._default_attachment_text(attachments),
                raw_payload=payload,
                attachments=tuple(attachments),
            )
        ]

    def _extract_attachments(self, message: dict[str, Any]) -> list[InboundAttachment]:
        attachments = []
        photo_sizes = message.get("photo")
        if isinstance(photo_sizes, list) and photo_sizes:
            best = max(
                (photo for photo in photo_sizes if isinstance(photo, dict)),
                key=lambda photo: int(photo.get("file_size") or photo.get("width") or 0),
                default=None,
            )
            if best and best.get("file_id"):
                attachments.append(
                    self._download_attachment(
                        str(best["file_id"]),
                        filename=f"telegram-photo-{best.get('file_unique_id') or best['file_id']}.jpg",
                        content_type="image/jpeg",
                    )
                )
        document = message.get("document")
        if isinstance(document, dict) and document.get("file_id"):
            filename = str(document.get("file_name") or f"telegram-file-{document['file_id']}")
            content_type = str(
                document.get("mime_type")
                or mimetypes.guess_type(filename)[0]
                or "application/octet-stream"
            )
            attachments.append(
                self._download_attachment(
                    str(document["file_id"]),
                    filename=filename,
                    content_type=content_type,
                )
            )
        return attachments

    def _download_attachment(self, file_id: str, filename: str, content_type: str) -> InboundAttachment:
        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required to download files")
        payload = self.http_get(f"https://api.telegram.org/bot{self.bot_token}/getFile", {"file_id": file_id})
        if isinstance(payload, bytes):
            payload = json.loads(payload.decode("utf-8"))
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(f"Telegram getFile failed: {payload}")
        result = payload.get("result")
        if not isinstance(result, dict) or not result.get("file_path"):
            raise RuntimeError(f"Telegram getFile returned invalid result: {payload}")
        file_path = str(result["file_path"])
        download_url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        downloaded = self.http_download(download_url)
        final_name = filename or PurePosixPath(file_path).name or file_id
        return InboundAttachment(
            filename=final_name,
            content_type=content_type,
            data=downloaded,
            platform_file_id=file_id,
        )

    def _default_attachment_text(self, attachments: list[InboundAttachment]) -> str:
        if not attachments:
            return ""
        names = ", ".join(attachment.filename for attachment in attachments)
        return f"Please process the uploaded file(s): {names}"

    def send_message(self, message: OutboundMessage) -> None:
        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required to send messages")
        for chunk in split_telegram_message(message.text):
            self.http_post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                {
                    "chat_id": message.conversation_id,
                    "text": chunk,
                },
            )

    def get_updates(
        self,
        offset: int | None = None,
        timeout: int = 30,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required to poll updates")
        params: dict[str, Any] = {
            "timeout": timeout,
            "limit": limit,
            "allowed_updates": json.dumps(["message", "edited_message"]),
        }
        if offset is not None:
            params["offset"] = offset
        payload = self.http_get(f"https://api.telegram.org/bot{self.bot_token}/getUpdates", params)
        if isinstance(payload, bytes):
            payload = json.loads(payload.decode("utf-8"))
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")
        result = payload.get("result", [])
        return result if isinstance(result, list) else []

    def get_me(self) -> dict[str, Any]:
        if not self.bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required to check bot connection")
        payload = self.http_get(f"https://api.telegram.org/bot{self.bot_token}/getMe", {})
        if isinstance(payload, bytes):
            payload = json.loads(payload.decode("utf-8"))
        if not isinstance(payload, dict) or not payload.get("ok"):
            raise RuntimeError(f"Telegram getMe failed: {payload}")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError(f"Telegram getMe returned invalid result: {payload}")
        return result


def split_telegram_message(text: str, limit: int = TELEGRAM_MESSAGE_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    remaining = text
    while remaining:
        chunks.append(remaining[:limit])
        remaining = remaining[limit:]
    return chunks


def _json_post(url: str, payload: dict[str, Any]) -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=20) as response:
        return response.read()


def _json_get(url: str, params: dict[str, Any]) -> Any:
    from urllib.parse import urlencode

    query = urlencode(params)
    target = f"{url}?{query}" if query else url
    req = request.Request(target, method="GET")
    with request.urlopen(req, timeout=int(params.get("timeout", 30)) + 10) as response:
        return json.loads(response.read().decode("utf-8"))


def _download_bytes(url: str) -> bytes:
    req = request.Request(url, method="GET")
    with request.urlopen(req, timeout=60) as response:
        return response.read()
