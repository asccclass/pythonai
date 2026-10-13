import base64
import hashlib
import io
import json
import re

from communication_models import InboundAttachment

MAX_FILES = 4
MAX_FILE_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_MESSAGE_BYTES = 4 * 1024 * 1024


def conversation_id(value):
    if not isinstance(value, str) or (value and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value)):
        raise ValueError("Invalid conversation ID")
    return value


def scope_device(device, conversation):
    return device if not conversation else device + ":" + conversation


def decode_attachments(value):
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_FILES:
        raise ValueError("最多附加 4 個檔案")
    result, total = [], 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"filename", "content_type", "data_base64"}:
            raise ValueError("Invalid attachment fields")
        filename, mime, encoded = item["filename"], item["content_type"], item["data_base64"]
        if (not isinstance(filename, str) or not 1 <= len(filename.encode()) <= 255
                or any(char in filename for char in "/\\\x00") or filename in {".", ".."}):
            raise ValueError("Invalid attachment filename")
        if not isinstance(mime, str) or not re.fullmatch(r"[A-Za-z0-9.+_-]+/[A-Za-z0-9.+_-]+", mime):
            raise ValueError("Invalid attachment MIME")
        if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
            raise ValueError("單檔上限 1 MiB")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as error:
            raise ValueError("Invalid attachment Base64") from error
        total += len(data)
        if not data or len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
            raise ValueError("附件不可為空，單檔 1 MiB，合計 2 MiB")
        if mime.startswith("image/"):
            from PIL import Image
            formats = {"image/png": "PNG", "image/jpeg": "JPEG", "image/webp": "WEBP", "image/gif": "GIF"}
            if mime not in formats:
                raise ValueError("Unsupported image type")
            try:
                with Image.open(io.BytesIO(data)) as image:
                    if image.format != formats[mime] or image.width * image.height > 20_000_000:
                        raise ValueError("Invalid image format or dimensions")
                    image.verify()
            except Exception as error:
                raise ValueError("圖片損壞或尺寸超過上限") from error
        result.append(InboundAttachment(filename, mime, data))
    return tuple(result)


def dedup_input(text, attachments):
    if not attachments:
        return text
    return json.dumps({"text": text, "attachments": [{"filename": item.filename,
        "content_type": item.content_type, "sha256": hashlib.sha256(item.data).hexdigest()}
        for item in attachments]}, ensure_ascii=False, sort_keys=True)


def desktop_features():
    return {"conversation_sessions": True, "attachments": {"max_files": MAX_FILES,
            "max_file_bytes": MAX_FILE_BYTES, "max_total_bytes": MAX_TOTAL_BYTES}, "desktop_tools": True}
