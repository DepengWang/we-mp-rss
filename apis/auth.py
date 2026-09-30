from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from datetime import timedelta
import time
from core.auth import (
    authenticate_user,
    create_access_token,
    get_current_user,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    create_ak,
    list_user_aks,
    deactivate_ak,
    delete_ak,
    update_ak,
    create_password_reset_code,
    verify_reset_code,
    reset_user_password,
    send_reset_code_notice,
    get_user
)
from .ver import API_VERSION
from .base import success_response, error_response
from driver.base import WX_API
from core.config import set_config, cfg
from core.print import print_warning
from core.switch_job import get_manager as get_switch_job_manager
from pydantic import BaseModel
from typing import Optional

router = APIRouter(prefix=f"/auth", tags=["认证"])
from driver.success import Success
from driver.wx_api import get_qr_code #通过API登录
from driver.wx import WX_API
def ApiSuccess(data):
    if data != None:
            print("\n登录结果:")
            print(f"Token: {data['token']}")
            set_config("token",data['token'])
            cfg.reload()
    else:
            print("\n登录失败，请检查上述错误信息")
@router.get("/qr/code", summary="获取登录二维码")
async def get_qrcode(current_user=Depends(get_current_user)):
    try:
        # Preserve an apparently valid active account before the new QR flow can
        # replace the single global token_data record.
        await WX_API.preserve_active_account_before_qr_login()
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"保全当前公众号授权失败，本次未启动扫码：{e}",
        )
    code_url=WX_API.GetCode(Success)
    return success_response(code_url)
@router.get("/qr/image", summary="获取登录二维码图片")
async def qr_image(current_user=Depends(get_current_user)):
    return success_response(WX_API.GetHasCode())

@router.get("/qr/status",summary="获取扫描状态")
async def qr_status(current_user=Depends(get_current_user)):
    #  from driver.success import  getStatus
     return success_response(WX_API.QrStatus())    
@router.get("/qr/over",summary="扫码完成")
async def qr_success(current_user=Depends(get_current_user)):
     return success_response(await WX_API.Close())    
@router.post("/login", summary="用户登录")
async def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=error_response(
                code=40101,
                message="用户名或密码错误"
            )
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return success_response({
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60
    })


@router.post("/token",summary="获取Token")
async def getToken(form_data: OAuth2PasswordRequestForm = Depends()):
    user = authenticate_user(form_data.username, form_data.password)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_202_ACCEPTED,
            detail=error_response(
                code=40101,
                message="用户名或密码错误"
            )
        )
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": user.username}, expires_delta=access_token_expires
    )
    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60
    }


@router.post("/logout", summary="用户注销")
async def logout(current_user: dict = Depends(get_current_user)):
    return {"code": 0, "message": "注销成功"}

@router.post("/refresh", summary="刷新Token")
async def refresh_token(current_user: dict = Depends(get_current_user)):
    access_token_expires = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    access_token = create_access_token(
        data={"sub": current_user["username"]}, expires_delta=access_token_expires
    )
    return success_response({
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60
    })

@router.get("/verify", summary="验证Token有效性")
async def verify_token(current_user: dict = Depends(get_current_user)):
    """验证当前token是否有效"""
    return success_response({
        "is_valid": True,
        "username": current_user["username"],
        "expires_at": current_user.get("exp")
    })


# ===== Access Key (AK) 管理接口 =====

class CreateAKRequest(BaseModel):
    """创建AK请求"""
    name: str  # AK名称
    description: Optional[str] = ""  # 描述
    permissions: Optional[list] = None  # 权限列表
    expires_in_days: Optional[int] = None  # 过期天数


class UpdateAKRequest(BaseModel):
    """更新AK请求"""
    name: Optional[str] = None
    description: Optional[str] = None
    permissions: Optional[list] = None
    is_active: Optional[bool] = None
    expires_at: Optional[str] = None


@router.post("/ak/create", summary="创建 Access Key")
async def create_access_key(
    req: CreateAKRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    为当前用户创建 Access Key
    
    用法示例：
    ```
    POST /api/v1/auth/ak/create
    Authorization: Bearer {token}
    Content-Type: application/json
    
    {
        "name": "我的API密钥",
        "description": "用于RSS同步的API密钥",
        "permissions": ["article:read", "article:sync"],
        "expires_in_days": 365
    }
    ```
    """
    try:
        user_id = current_user.get("original_user").id if hasattr(current_user.get("original_user"), "id") else current_user.get("user_id")
        ak_info = create_ak(
            user_id=user_id,
            name=req.name,
            permissions=req.permissions,
            description=req.description or "",
            expires_in_days=req.expires_in_days
        )
        return success_response(ak_info, "Access Key 创建成功")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(code=50001, message=f"创建失败: {str(e)}")
        )


@router.get("/ak/list", summary="获取 Access Keys 列表")
async def list_access_keys(current_user: dict = Depends(get_current_user)):
    """
    获取当前用户的所有 Access Keys
    
    用法示例：
    ```
    GET /api/v1/auth/ak/list
    Authorization: Bearer {token}
    ```
    """
    try:
        user_id = current_user.get("original_user").id if hasattr(current_user.get("original_user"), "id") else current_user.get("user_id")
        aks = list_user_aks(user_id)
        return success_response(aks, "获取成功")
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(code=50001, message=f"获取失败: {str(e)}")
        )


@router.put("/ak/{ak_id}", summary="更新 Access Key")
async def update_access_key(
    ak_id: str,
    req: UpdateAKRequest,
    current_user: dict = Depends(get_current_user)
):
    """
    更新 Access Key 信息
    
    用法示例：
    ```
    PUT /api/v1/auth/ak/{ak_id}
    Authorization: Bearer {token}
    Content-Type: application/json
    
    {
        "name": "新的AK名称",
        "description": "新的描述",
        "is_active": true
    }
    ```
    """
    try:
        update_data = {}
        if req.name is not None:
            update_data['name'] = req.name
        if req.description is not None:
            update_data['description'] = req.description
        if req.permissions is not None:
            update_data['permissions'] = req.permissions
        if req.is_active is not None:
            update_data['is_active'] = req.is_active
        
        if not update_data:
            return success_response(None, "没有更新内容")
        
        success = update_ak(ak_id, **update_data)
        if success:
            return success_response(None, "更新成功")
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_response(code=40401, message="Access Key 不存在")
            )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(code=50001, message=f"更新失败: {str(e)}")
        )


@router.post("/ak/{ak_id}/deactivate", summary="停用 Access Key")
async def deactivate_access_key(
    ak_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    停用 Access Key（保留记录但不能使用）
    
    用法示例：
    ```
    POST /api/v1/auth/ak/{ak_id}/deactivate
    Authorization: Bearer {token}
    ```
    """
    try:
        success = deactivate_ak(ak_id)
        if success:
            return success_response(None, "Access Key 已停用")
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_response(code=40401, message="Access Key 不存在")
            )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(code=50001, message=f"操作失败: {str(e)}")
        )


@router.delete("/ak/{ak_id}", summary="删除 Access Key")
async def delete_access_key(
    ak_id: str,
    current_user: dict = Depends(get_current_user)
):
    """
    删除 Access Key
    
    用法示例：
    ```
    DELETE /api/v1/auth/ak/{ak_id}
    Authorization: Bearer {token}
    ```
    """
    try:
        success = delete_ak(ak_id)
        if success:
            return success_response(None, "Access Key 已删除")
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=error_response(code=40401, message="Access Key 不存在")
            )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(code=50001, message=f"删除失败: {str(e)}")
        )


# ===== 密码找回接口 =====

class RequestResetCodeRequest(BaseModel):
    """请求重置验证码"""
    username: str


class ResetPasswordRequest(BaseModel):
    """重置密码请求"""
    username: str
    code: str
    new_password: str


@router.post("/password/reset-request", summary="请求密码重置验证码")
async def request_password_reset(req: RequestResetCodeRequest):
    """
    请求密码重置验证码
    
    验证码将通过系统通知（钉钉/飞书/微信等）发送给管理员
    
    用法示例：
    ```
    POST /api/v1/auth/password/reset-request
    Content-Type: application/json
    
    {
        "username": "your_username"
    }
    ```
    """
    try:
        # 检查用户是否存在
        user = get_user(req.username)
        if not user:
            # 不暴露用户是否存在的信息
            return success_response(None, "如果用户存在，验证码已发送到系统通知")
        
        # 生成验证码
        code = create_password_reset_code(req.username)
        
        # 发送通知
        send_success = send_reset_code_notice(req.username, code)
        
        if not send_success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=error_response(code=50002, message="验证码发送失败，请检查系统通知配置（需在 config.yaml 中配置 notice.webhook）")
            )
        
        return success_response(None, "验证码已发送，请联系管理员获取")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(code=50001, message=f"请求失败: {str(e)}")
        )


@router.post("/password/reset", summary="重置密码")
async def reset_password(req: ResetPasswordRequest):
    """
    使用验证码重置密码
    
    用法示例：
    ```
    POST /api/v1/auth/password/reset
    Content-Type: application/json
    
    {
        "username": "your_username",
        "code": "123456",
        "new_password": "new_password_123"
    }
    ```
    """
    try:
        # 验证验证码
        if not verify_reset_code(req.username, req.code):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_response(code=40001, message="验证码无效或已过期")
            )
        
        # 验证新密码长度
        if len(req.new_password) < 6:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_response(code=40002, message="密码长度不能少于6位")
            )
        
        # 重置密码
        success = reset_user_password(req.username, req.new_password)
        
        if not success:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=error_response(code=50003, message="密码重置失败")
            )
        
        return success_response(None, "密码重置成功，请使用新密码登录")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(code=50001, message=f"重置失败: {str(e)}")
        )


@router.get("/accounts/status", summary="获取公众号账号与Token有效期")
async def wx_account_status(current_user: dict = Depends(get_current_user)):
    """Return cached account summaries and the currently active account, without secrets."""
    from driver.token import get_account_sessions_data, get_account_status_data, _get_token_data

    cached = get_account_status_data()
    sessions = get_account_sessions_data()
    token_data = _get_token_data() or {}
    ext_data = token_data.get("ext_data") or {}
    current_name = str(ext_data.get("wx_app_name") or "").strip()
    current_expiry = token_data.get("expiry") or {}
    accounts = [dict(item) for item in cached.get("accounts", []) if isinstance(item, dict)]
    active_account_id = str(cached.get("active_account_id") or "")

    # Include any previously saved account session even if the lightweight
    # status summary was written by an older version or has not been refreshed.
    known_ids = {str(item.get("account_id") or "") for item in accounts}
    for account_id, session in sessions.items():
        account_id = str(account_id or "")
        session = session or {}
        if not account_id or account_id in known_ids:
            continue
        expiry = session.get("expiry") or {}
        try:
            token_status = "valid" if float(expiry.get("expiry_timestamp")) > time.time() else "expired"
        except (TypeError, ValueError):
            token_status = "unknown"
        accounts.append({
            "account_id": account_id,
            "name": str(session.get("name") or "未命名公众号"),
            "token_status": token_status,
            "expiry": {
                "expiry_timestamp": expiry.get("expiry_timestamp"),
                "expiry_time": str(expiry.get("expiry_time") or ""),
            },
            "ext_data": session.get("ext_data") or {},
            "checked_at": str(session.get("updated_at") or ""),
            "authorized_at": session.get("authorized_at"),
            "last_active_at": session.get("last_active_at"),
        })

    active_matches = [item for item in accounts if current_name and item.get("name") == current_name]
    cached_active_matches = [
        item for item in accounts
        if active_account_id and item.get("account_id") == active_account_id
    ]
    if (
        len(active_matches) > 1
        and len(cached_active_matches) == 1
        and cached_active_matches[0].get("name") == current_name
    ):
        active_matches = cached_active_matches
    if len(active_matches) == 1:
        active_account_id = str(active_matches[0].get("account_id") or "")
        active_matches[0]["expiry"] = {
            "expiry_timestamp": current_expiry.get("expiry_timestamp"),
            "expiry_time": str(current_expiry.get("expiry_time") or ""),
        }
        expiry_timestamp = current_expiry.get("expiry_timestamp")
        if expiry_timestamp is not None:
            try:
                is_valid = float(expiry_timestamp) > time.time()
            except (TypeError, ValueError):
                is_valid = False
            active_matches[0]["token_status"] = (
                "valid" if token_data.get("token") and is_valid else "expired"
            )
        else:
            active_matches[0]["token_status"] = "unknown"
    elif current_name:
        # A manually changed backend account can differ from the cached active ID.
        # Keep it visible as the active session instead of marking a stale cached row.
        active_account_id = ""
        accounts.append({
            "account_id": "",
            "name": current_name,
            "token_status": "unknown",
            "expiry": {
                "expiry_timestamp": current_expiry.get("expiry_timestamp"),
                "expiry_time": str(current_expiry.get("expiry_time") or ""),
            },
            "checked_at": "",
        })
        expiry_timestamp = current_expiry.get("expiry_timestamp")
        if expiry_timestamp is not None:
            try:
                is_valid = float(expiry_timestamp) > time.time()
            except (TypeError, ValueError):
                is_valid = False
            accounts[-1]["token_status"] = (
                "valid" if token_data.get("token") and is_valid else "expired"
            )
    else:
        active_account_id = ""

    now = time.time()
    for item in accounts:
        session = sessions.get(str(item.get("account_id") or ""), {}) or {}
        item["has_saved_session"] = bool(session.get("token") and session.get("cookies"))
        item["session_updated_at"] = str(session.get("updated_at") or "")
        item["authorized_at"] = session.get("authorized_at") or item.get("authorized_at")
        item["last_active_at"] = session.get("last_active_at") or item.get("last_active_at")
        expiry = item.get("expiry") or {}
        timestamp = expiry.get("expiry_timestamp")
        try:
            remaining = max(0, int(float(timestamp) - now)) if timestamp is not None else None
        except (TypeError, ValueError):
            remaining = None
        expiry["remaining_seconds"] = remaining
        item["expiry"] = expiry
        if remaining == 0 and item.get("token_status") == "valid":
            item["token_status"] = "expired"
        item["is_active"] = bool(
            (active_account_id and item.get("account_id") == active_account_id)
            or (
                not active_account_id
                and current_name
                and item.get("name") == current_name
                and not item.get("account_id")
            )
        )

    gather_model = str(cfg.get("gather.model") or "web").lower()
    uses_active_account = gather_model not in {"weread", "weread_mp"}
    for item in accounts:
        item["collector_associated"] = bool(item["is_active"] and uses_active_account)

    def account_recency(item):
        session = sessions.get(str(item.get("account_id") or ""), {}) or {}
        values = (
            session.get("last_active_at"), item.get("last_active_at"),
            session.get("authorized_at"), item.get("authorized_at"),
            session.get("updated_at"), item.get("checked_at"),
        )
        for value in values:
            try:
                return float(value)
            except (TypeError, ValueError):
                try:
                    return time.mktime(time.strptime(str(value), "%Y-%m-%d %H:%M:%S"))
                except (TypeError, ValueError, OverflowError):
                    continue
        return 0.0

    accounts.sort(key=lambda item: (bool(item.get("is_active")), account_recency(item)), reverse=True)
    accounts = accounts[:3]

    return success_response({
        "accounts": accounts,
        "active_account_id": active_account_id,
        "checked_at": str(cached.get("checked_at") or ""),
        "cached": bool(cached.get("checked_at")),
        "collector_mode": gather_model,
        "uses_active_account": uses_active_account,
    })


@router.get("/accounts/{account_id}/token", summary="按需查看指定公众号 Token")
async def wx_account_token(
    account_id: str,
    response: Response,
    current_user: dict = Depends(get_current_user),
):
    """Return one cached token on explicit request; never expose its cookies."""
    from driver.token import get_account_session_data

    response.headers["Cache-Control"] = "no-store"
    account = get_account_session_data(account_id)
    token = str(account.get("token") or "")
    if not token:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_response(code=40404, message="该账号没有已缓存的 Token；请先刷新账号状态"),
        )
    return success_response({
        "account_id": str(account.get("account_id") or account_id),
        "name": str(account.get("name") or ""),
        "token": token,
        "expiry": account.get("expiry") or {},
        "ext_data": account.get("ext_data") or {},
        "updated_at": str(account.get("updated_at") or ""),
    })


@router.post("/accounts/refresh", summary="手动刷新公众号账号状态（异步任务）")
async def refresh_wx_account_status(current_user: dict = Depends(get_current_user)):
    """Inspect account expiry metadata in the background, then restore the active account."""
    import asyncio
    import threading

    user_id = current_user.get("sub") or current_user.get("username") or current_user.get("id") or "unknown"
    manager = get_switch_job_manager()
    job = manager.create_job(user_id=str(user_id), target_username="__refresh_account_status__")
    manager.update(job.job_id, stage="queued", message="已入队，准备刷新公众号账号状态", progress=0)

    def _run_in_thread():
        def _target():
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                import sys
                if sys.platform == "win32":
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                try:
                    def progress_callback(stage: str, message: str, progress: int) -> None:
                        manager.update(job.job_id, stage=stage, message=message, progress=progress)
                    from driver.wx import WX_API as WX_BROWSER_API
                    result = loop.run_until_complete(
                        WX_BROWSER_API.refresh_account_status(progress_callback=progress_callback)
                    )
                    current_job = manager.get_job(job.job_id)
                    failure_message = (
                        current_job.message if current_job and current_job.message
                        else "刷新失败，请查看任务进度与日志"
                    )
                    manager.finish(
                        job.job_id,
                        ok=bool(result),
                        message=("公众号账号状态已刷新" if result else failure_message),
                    )
                finally:
                    loop.close()
            except Exception as e:
                manager.finish(job.job_id, ok=False, message=f"刷新任务异常: {e}")

        threading.Thread(target=_target, daemon=True).start()

    _run_in_thread()
    return success_response(job.to_dict(), "账号状态刷新任务已启动")


@router.post("/switch", summary="切换微信账号（异步任务）")
async def switch_wechat_account(
    current_user: dict = Depends(get_current_user),
    account_id: str = Query(..., min_length=1),
):
    """
    按后台账号列表中的 account_id 切换公众号，立即返回 job_id。

    用法示例：
    ```
    POST /api/v1/auth/switch?account_id=<account_id>
    Authorization: Bearer {token}

    # 轮询进度
    GET /api/v1/auth/switch/status?job_id=<job_id>

    # SSE 实时推送
    GET /api/v1/auth/switch/progress?job_id=<job_id>
    ```

    设计要点：
    * 实际切换流程（启动浏览器、点击切换按钮、轮询等待）可能耗时 30~120 秒，
      因此改在后台线程执行；HTTP 接口立即返回。
    * 切换期间已自动暂停抓取 / 内容任务队列，结束后自动恢复。
    * Token 失效时立即返回 ok=false 与阶段消息，前端可据此引导用户重新扫码。
    """
    import asyncio
    user_id = current_user.get("sub") or current_user.get("username") or current_user.get("id") or "unknown"
    manager = get_switch_job_manager()
    account_id = account_id.strip()
    if not account_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_response(code=40001, message="必须指定目标公众号 account_id"),
        )
    job = manager.create_job(user_id=str(user_id), target_username=account_id)
    manager.update(job.job_id, stage="queued", message=f"已入队，准备切换到账号 {account_id}", progress=0)

    def _run_in_thread() -> None:
        """在新线程中跑同步包装，确保 HTTP 立即返回。"""
        import threading
        def _target() -> None:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                # Windows 需要 ProactorEventLoop 给 Playwright 用
                import sys
                if sys.platform == "win32":
                    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
                try:
                    def progress_callback(stage: str, message: str, progress: int) -> None:
                        manager.update(job.job_id, stage=stage, message=message, progress=progress)
                    from driver.wx import WX_API as WX_BROWSER_API
                    result = loop.run_until_complete(
                        WX_BROWSER_API.switch_account(account_id=account_id, progress_callback=progress_callback)
                    )
                    current_job = manager.get_job(job.job_id)
                    result_message = (
                        current_job.message if current_job and current_job.message
                        else ("账号切换成功" if result else "切换失败，请查看服务日志")
                    )
                    manager.finish(job.job_id, ok=bool(result), message=result_message)
                finally:
                    try:
                        loop.close()
                    except Exception:
                        pass
            except Exception as e:
                manager.finish(job.job_id, ok=False, message=f"后台任务异常: {e}")
        threading.Thread(target=_target, daemon=True).start()

    _run_in_thread()
    return success_response(job.to_dict(), "切换任务已启动，可通过 job_id 查询进度")


@router.get("/switch/status", summary="查询切换账号任务状态")
async def switch_status(
    job_id: str,
    current_user: dict = Depends(get_current_user),
):
    """根据 job_id 返回当前阶段、消息、进度、是否完成。仅 job 所有者可访问。"""
    job = get_switch_job_manager().get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_response(code=40404, message=f"找不到切换任务 {job_id}"),
        )
    caller_id = str(
        current_user.get("sub")
        or current_user.get("username")
        or current_user.get("id")
        or ""
    )
    if job.user_id != caller_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_response(code=40303, message="无权访问该切换任务"),
        )
    return success_response(job.to_dict(), "ok")


@router.get("/switch/progress", summary="SSE 实时推送切换账号进度")
async def switch_progress(
    job_id: str,
    current_user: dict = Depends(get_current_user),
):
    """Server-Sent Events 流式推送，仅 job 所有者可订阅。

    事件格式（每行 JSON）：
    ```
    data: {"job_id":"abc...","stage":"clicking","message":"...","progress":65,"ok":null,"started_at":...,"finished_at":null}
    ```
    """
    import asyncio
    import json
    from fastapi.responses import StreamingResponse

    # 先做鉴权 + 所有权校验，避免给未授权 SSE 长连接
    job = get_switch_job_manager().get_job(job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_response(code=40404, message=f"找不到切换任务 {job_id}"),
        )
    caller_id = str(
        current_user.get("sub")
        or current_user.get("username")
        or current_user.get("id")
        or ""
    )
    if job.user_id != caller_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=error_response(code=40303, message="无权订阅该切换任务进度"),
        )

    async def event_stream():
        try:
            manager = get_switch_job_manager()
            q = await manager.subscribe(job_id)
        except KeyError:
            yield "event: error\ndata: {\"message\":\"job not found\"}\n\n"
            return
        try:
            while True:
                try:
                    snap = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # 心跳保活
                    yield "event: ping\ndata: {}\n\n"
                    continue
                yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n"
                if snap.get("finished_at") is not None:
                    break
        finally:
            try:
                get_switch_job_manager().unsubscribe(job_id, q)
            except Exception:
                pass

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/wechat/unbind", summary="解除微信授权")
async def unbind_wechat(current_user: dict = Depends(get_current_user)):
    """
    解除当前已绑定的微信公众号授权，删除本地 token 与 Redis 缓存。

    用法示例：
    ```
    POST /api/v1/auth/wechat/unbind
    Authorization: Bearer {token}
    ```

    解除后下次调用 `/api/v1/auth/qr/code` 时会自动引导重新扫码授权。
    """
    try:
        # 1. 清除本地 token 文件
        from driver.token import wx_cfg
        wx_cfg.set("token_data", {})
        wx_cfg.save_config()
        wx_cfg.reload()

        # 2. 清除 Redis 中的 token 与登录状态（仅作用于 werss 前缀键）
        from core.redis_client import redis_client
        cleared_keys: list = []
        if redis_client.is_connected:
            try:
                for key in redis_client._client.keys("werss:token:*"):
                    redis_client._client.delete(key)
                    cleared_keys.append(key.decode() if isinstance(key, bytes) else key)
                redis_client._client.set("werss:login:status", "0")
            except Exception as e:
                print_warning(f"清理 Redis token 时出错: {e}")

        # 3. 同步全局登录状态（内存回退）
        from driver.success import setStatus
        setStatus(False)

        return success_response(
            {
                "cleared_local_token": True,
                "cleared_redis_keys": cleared_keys,
                "next_step": "请重新扫码授权",
            },
            "微信授权已解除",
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=error_response(code=50002, message=f"解除微信授权失败: {str(e)}"),
        )
