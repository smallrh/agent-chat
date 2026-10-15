"""附件 API（P2 多模态）。

    POST /attachments          上传（multipart/form-data，字段名 file）
    GET  /attachments/{id}     下载 / 预览（?inline=1 走 inline disposition）

Go BFF 侧：上传走 /api/v1/attachments（统一信封），下载走 Raw 二进制透传。
"""

import logging

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import Response

from app.core.auth import get_current_user_id
from app.service.attachment_service import attachment_service


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/attachments", tags=["Attachments"])


def _svc():
    if not attachment_service.is_ready:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=503, detail="Attachment service is not available",
        )
    return attachment_service


@router.post("")
async def upload_attachment(
    file: UploadFile = File(..., description="要上传的文件"),
    conversation_id: str | None = Form(None, description="可选：所属会话"),
    user_id: int = Depends(get_current_user_id),
):
    """
    上传一个附件。返回 attachment_id，随后发消息时把它放进 attachment_ids。

    大小 / 类型限制见配置 ATTACHMENT_MAX_BYTES / ATTACHMENT_ALLOWED_MIME_PREFIXES。
    """
    return await _svc().save_upload(
        user_id=user_id,
        upload=file,
        conversation_id=conversation_id or None,
    )


@router.get("/{attachment_id}")
async def download_attachment(
    attachment_id: str,
    inline: bool = Query(False, description="true=浏览器内预览（图片），false=下载"),
    user_id: int = Depends(get_current_user_id),
):
    """下载 / 预览附件。只属于自己的附件可访问。"""
    data, mime, filename = await _svc().read_for_download(attachment_id, user_id)

    disposition = "inline" if inline else "attachment"
    # filename* 用 UTF-8 编码，避免中文文件名在部分浏览器里乱码
    headers = {
        "Content-Disposition": (
            f"{disposition}; filename*=UTF-8''"
            + _urlquote(filename)
        ),
        "Cache-Control": "private, max-age=300",
    }
    return Response(content=data, media_type=mime, headers=headers)


def _urlquote(s: str) -> str:
    from urllib.parse import quote

    return quote(s or "file", safe="")
