"""附件服务（P2 多模态）：落盘 → 抽文本 → 注入 LLM 上下文。

## 整体链路

    前端选文件
      → POST /attachments（multipart）
         校验大小 / MIME → 落盘 ATTACHMENT_DIR → 抽文本 → 写 attachments 表
      → 返回 {attachment_id, name, mime, size, preview}
    用户发消息（带 attachment_ids）
      → ChatService 加载这些附件
         文本文件 → 拼成 [附件：name] 文本块追加到用户消息
         图片     → 转成 base64 data URL，作为多模态 content part 发给模型
      → 绑定 message_id（便于日后溯源/清理）

## 为什么抽文本要落库
抽文本（尤其 PDF）是有成本的，而同一附件可能被追问好几次。
落库后每次注入直接读 DB，不必重复解析磁盘文件。

## 安全边界
- 大小、MIME、数量三重校验（配置：ATTACHMENT_*）
- 落盘路径由 attachment_id 拼出，不直接用用户传的文件名
  （防路径穿越：用户文件名为 "../../etc/passwd" 时只会变成文件名的一部分）
- 下载接口做归属校验，不属于自己的附件一律 404（不泄露存在性）
"""

from __future__ import annotations

import base64
import logging
import mimetypes
import os
import re
import uuid
from typing import Any

from fastapi import HTTPException, status
from fastapi import UploadFile

from app.core.config import settings
from app.repo.attachment_repo import AttachmentRepository


logger = logging.getLogger(__name__)

# 文本类附件的字符集兜底：中文 Windows 产出的 txt 常是 GBK，
# utf-8 解码失败就退到 gbk / latin-1，绝不因编码问题丢掉整个附件。
_TEXT_ENCODINGS = ("utf-8", "gbk", "gb18030", "latin-1")

# 这些扩展名按纯文本处理（MIME 判定不可靠时以扩展名兜底）
_TEXT_EXT = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".log", ".py", ".js", ".ts", ".java",
    ".go", ".c", ".cpp", ".h", ".sql", ".ini", ".cfg", ".toml", ".env",
}

_IMAGE_PREFIX = "image/"


class AttachmentService:
    def __init__(self):
        self._repo = AttachmentRepository()
        self._started = False

    async def start(self):
        if not self._started:
            await self._repo.start()
            self._started = True

    async def stop(self):
        self._repo = None  # 共享池不在组件级关闭
        self._started = False

    @property
    def is_ready(self) -> bool:
        return self._repo is not None and self._repo.pool is not None

    # ----------------------------------------------------------------
    # 校验
    # ----------------------------------------------------------------

    @staticmethod
    def allowed_mime_prefixes() -> list[str]:
        raw = (settings.ATTACHMENT_ALLOWED_MIME_PREFIXES or "").strip()
        if not raw:
            return []
        return [p.strip().lower() for p in raw.split(",") if p.strip()]

    def _assert_mime_allowed(self, mime: str) -> None:
        prefixes = self.allowed_mime_prefixes()
        if not prefixes:
            return
        m = (mime or "").lower()
        if any(m.startswith(p) for p in prefixes):
            return
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"不支持的文件类型：{mime or '未知'}",
        )

    # ----------------------------------------------------------------
    # 上传
    # ----------------------------------------------------------------

    async def save_upload(
        self,
        *,
        user_id: int,
        upload: UploadFile,
        conversation_id: str | None = None,
    ) -> dict:
        """
        接收一个 multipart 上传，落盘 + 抽文本 + 写库。

        Raises:
            HTTPException 413: 超过 ATTACHMENT_MAX_BYTES
            HTTPException 415: MIME 不在白名单
        """
        if not self.is_ready:
            raise HTTPException(
                status_code=503, detail="Attachment service is not available",
            )

        raw_name = upload.filename or "unnamed"
        safe_name = self._safe_name(raw_name)
        ext = os.path.splitext(safe_name)[1].lower()

        # 客户端给的 Content-Type 不可信（可以随便伪造），以扩展名兜底纠正
        mime = (upload.content_type or "").strip().lower()
        guessed = mimetypes.guess_type(safe_name)[0] or ""
        if not mime or mime == "application/octet-stream":
            mime = guessed or "application/octet-stream"
        self._assert_mime_allowed(mime)

        data = await upload.read()
        size = len(data)
        max_bytes = int(settings.ATTACHMENT_MAX_BYTES or 0)
        if max_bytes > 0 and size > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=(
                    f"文件过大：{self._human_size(size)}，"
                    f"上限 {self._human_size(max_bytes)}"
                ),
            )
        if size == 0:
            raise HTTPException(status_code=400, detail="空文件")

        attachment_id = str(uuid.uuid4())

        # ---- 落盘 ----
        storage_path = self._write_file(user_id, attachment_id, ext, data)

        # ---- 抽文本（图片不抽，走 base64 多模态）----
        extracted = None
        if not mime.startswith(_IMAGE_PREFIX):
            extracted = self._extract_text(data, mime, ext)

        await self._repo.create(
            attachment_id=attachment_id,
            user_id=user_id,
            name=safe_name,
            mime=mime,
            size_bytes=size,
            storage_path=storage_path,
            extracted_text=extracted,
            conversation_id=conversation_id,
        )

        return {
            "attachment_id": attachment_id,
            "name": safe_name,
            "mime": mime,
            "size": size,
            "is_image": mime.startswith(_IMAGE_PREFIX),
            "extracted_chars": len(extracted) if extracted else 0,
            # 文本预览：给前端展示「已解析出 N 字」，也方便用户确认传对了文件
            "preview": (extracted or "")[:300],
        }

    # ----------------------------------------------------------------
    # 落盘 / 路径
    # ----------------------------------------------------------------

    @staticmethod
    def _safe_name(name: str) -> str:
        """
        把用户给的文件名压成安全的展示名。

        只保留 basename 并替换路径分隔符与控制字符 —— 落盘路径本身
        用 attachment_id 拼，不依赖这个名字，所以这里只防「展示/下载时
        出现 ../ 或换行」这类二次风险。
        """
        base = os.path.basename(name or "").strip()
        base = re.sub(r"[\r\n\t]", " ", base)
        base = re.sub(r'[\\/:*?"<>|]', "_", base)
        return (base or "unnamed")[:255]

    @staticmethod
    def _write_file(user_id: int, attachment_id: str, ext: str, data: bytes) -> str:
        """
        写文件到 ATTACHMENT_DIR/<user_id>/<attachment_id><ext>。

        返回相对路径（存进 DB）。目录不存在就创建。
        用 attachment_id 而不是用户文件名做文件名 —— 既避免路径穿越，
        也避免同名文件互相覆盖。
        """
        root = settings.ATTACHMENT_DIR or "./data/attachments"
        rel_dir = os.path.join(str(user_id))
        abs_dir = os.path.join(root, rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        filename = f"{attachment_id}{ext}"
        with open(os.path.join(abs_dir, filename), "wb") as f:
            f.write(data)
        return os.path.join(rel_dir, filename).replace("\\", "/")

    def _abs_path(self, storage_path: str) -> str:
        root = settings.ATTACHMENT_DIR or "./data/attachments"
        return os.path.join(root, (storage_path or "").replace("/", os.sep))

    # ----------------------------------------------------------------
    # 文本抽取
    # ----------------------------------------------------------------

    @staticmethod
    def _decode_text(data: bytes) -> str:
        for enc in _TEXT_ENCODINGS:
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return data.decode("utf-8", errors="replace")

    def _extract_text(self, data: bytes, mime: str, ext: str) -> str | None:
        """
        按类型抽纯文本。失败一律降级为 None（不让上传整体失败）。

        PDF 用 pypdf（可选依赖）：没装时明确告诉用户「无法解析 PDF 正文」，
        而不是静默塞一个空字符串 —— 后者会让模型以为附件是空的。
        """
        try:
            if mime == "application/pdf" or ext == ".pdf":
                return self._extract_pdf(data)
            if mime.startswith("text/") or ext in _TEXT_EXT:
                return self._decode_text(data)
        except Exception:
            logger.exception("attachment text extraction failed (mime=%s)", mime)
            return None
        return None

    @staticmethod
    def _extract_pdf(data: bytes) -> str | None:
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError:
            logger.warning("pypdf not installed, skip PDF text extraction")
            return None

        import io

        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        for page in reader.pages[:50]:  # 最多 50 页，防止超大 PDF 卡死
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(parts).strip()
        return text or None

    # ----------------------------------------------------------------
    # 加载与上下文构造
    # ----------------------------------------------------------------

    async def load_for_message(
        self,
        *,
        user_id: int,
        attachment_ids: list[str],
        message_id: str | None = None,
        conversation_id: str | None = None,
    ) -> list[dict]:
        """
        加载并校验一批附件，顺带绑定到消息上。

        非法 / 不属于自己的 id 直接跳过（不因为一个坏 id 让整条消息失败）。
        """
        if not attachment_ids:
            return []
        if not self.is_ready:
            logger.warning("AttachmentService not ready, skip attachments")
            return []

        limit = max(1, int(settings.ATTACHMENT_MAX_PER_MESSAGE or 5))
        rows: list[dict] = []
        for aid in attachment_ids[:limit]:
            try:
                row = await self._repo.get(aid)
            except Exception:
                logger.warning("invalid attachment id: %s", aid)
                continue
            if row is None or int(row["user_id"]) != int(user_id):
                logger.warning("attachment not owned / missing: %s", aid)
                continue
            if message_id:
                try:
                    await self._repo.bind_message(
                        aid, user_id, message_id, conversation_id,
                    )
                except Exception:
                    logger.exception("bind attachment failed: %s", aid)
            rows.append(row)
        return rows

    async def bind_all(
        self,
        *,
        user_id: int,
        attachment_ids: list[str],
        message_id: str,
        conversation_id: str | None = None,
    ) -> None:
        """
        把一批附件绑定到已落库的那条消息上。

        与 load_for_message 分开：消息要等 Agent 跑完才落库，
        而附件上下文在 Agent 跑之前就得准备好，两个时机不同。
        绑定失败只记日志（不影响已发出的回答）。
        """
        if not attachment_ids or not self.is_ready:
            return
        for aid in attachment_ids:
            try:
                await self._repo.bind_message(
                    aid, user_id, message_id, conversation_id,
                )
            except Exception:
                logger.exception("bind attachment failed: %s", aid)

    def build_context(self, rows: list[dict]) -> tuple[str, list[dict]]:
        """
        把附件转成「可注入 LLM 的两种内容」。

        Returns:
            (text_block, image_parts)
              - text_block: 追加到用户消息尾部的纯文本（Markdown 风格分节）
              - image_parts: OpenAI 多模态 content part 列表
                （[{"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]）

        图片是否生成 image_parts 由 ATTACHMENT_IMAGE_ENABLED 决定：
        模型不支持 vision 时关掉，图片退化成一占位文本，避免 400。
        """
        if not rows:
            return "", []

        text_parts: list[str] = []
        image_parts: list[dict] = []
        max_chars = max(500, int(settings.ATTACHMENT_MAX_TEXT_CHARS or 8000))
        image_enabled = bool(settings.ATTACHMENT_IMAGE_ENABLED)

        for row in rows:
            name = row.get("name") or "附件"
            mime = (row.get("mime") or "").lower()
            size = int(row.get("size_bytes") or 0)

            if mime.startswith(_IMAGE_PREFIX):
                if image_enabled:
                    url = self._image_data_url(row)
                    if url:
                        image_parts.append(
                            {"type": "image_url", "image_url": {"url": url}}
                        )
                        text_parts.append(f"【图片附件】{name}（{self._human_size(size)}）")
                        continue
                text_parts.append(
                    f"【图片附件】{name}（{self._human_size(size)}）\n"
                    "（当前模型未开启图片理解，无法读取图片内容，请用户用文字补充说明。）"
                )
                continue

            extracted = (row.get("extracted_text") or "").strip()
            if extracted:
                if len(extracted) > max_chars:
                    extracted = (
                        extracted[:max_chars]
                        + f"\n…（附件过长，已截断，共 {len(extracted)} 字）"
                    )
                text_parts.append(f"【附件】{name}\n```\n{extracted}\n```")
            else:
                text_parts.append(
                    f"【附件】{name}（{self._human_size(size)}，{mime}）\n"
                    "（未能从该文件抽取到文本内容，请用户直接说明文件内容或换用文本格式。）"
                )

        return "\n\n".join(text_parts), image_parts

    def _image_data_url(self, row: dict) -> str | None:
        """读磁盘上的图片并转成 base64 data URL。"""
        path = row.get("storage_path")
        if not path:
            return None
        try:
            with open(self._abs_path(path), "rb") as f:
                raw = f.read()
        except Exception:
            logger.exception("read attachment file failed: %s", path)
            return None
        mime = row.get("mime") or "image/png"
        b64 = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{b64}"

    # ----------------------------------------------------------------
    # 下载
    # ----------------------------------------------------------------

    async def read_for_download(
        self, attachment_id: str, user_id: int,
    ) -> tuple[bytes, str, str]:
        """返回 (bytes, mime, filename)。归属校验失败抛 404。"""
        if not self.is_ready:
            raise HTTPException(503, "Attachment service is not available")
        row = await self._repo.get_for_user(attachment_id, user_id)
        path = row.get("storage_path")
        if not path:
            raise HTTPException(404, "Attachment content not found")
        try:
            with open(self._abs_path(path), "rb") as f:
                data = f.read()
        except FileNotFoundError:
            logger.warning("attachment file missing on disk: %s", path)
            raise HTTPException(404, "Attachment content not found")
        return data, row.get("mime") or "application/octet-stream", row.get("name") or "file"

    # ----------------------------------------------------------------
    # 工具
    # ----------------------------------------------------------------

    @staticmethod
    def _human_size(n: int) -> str:
        if n < 1024:
            return f"{n}B"
        if n < 1024 * 1024:
            return f"{n / 1024:.1f}KB"
        return f"{n / 1024 / 1024:.1f}MB"

    def public_meta(self, row: dict) -> dict[str, Any]:
        """给前端的消息内回显用的精简元数据（不含 extracted_text 全文）。"""
        return {
            "attachment_id": row.get("attachment_id"),
            "name": row.get("name"),
            "mime": row.get("mime"),
            "size": int(row.get("size_bytes") or 0),
            "is_image": (row.get("mime") or "").lower().startswith(_IMAGE_PREFIX),
        }


attachment_service = AttachmentService()
