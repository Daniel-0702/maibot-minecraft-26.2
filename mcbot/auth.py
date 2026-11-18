"""账号认证：离线模式 UUID、微软设备码登录、会话加入（session join）。

在线模式服务器（online-mode=true）的流程：

1. 服务器下发加密请求；
2. 客户端调用 ``/session/minecraft/join`` 把 serverId 绑定到正版账号；
3. 服务器再调用 ``hasJoined`` 校验，通过后才允许进入游戏。

离线模式（大多数自建服）只需要用户名 + 由名字推导的 UUID。
"""

from __future__ import annotations

import asyncio
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "AuthError",
    "AuthSession",
    "offline_session",
    "join_server",
    "microsoft_session",
    "load_cached_session",
    "save_cached_session",
    "DEFAULT_CLIENT_ID",
]

#: Minecraft 启动器使用的 Azure 应用 ID（微软设备码登录的公共客户端）。
DEFAULT_CLIENT_ID = "00000000402b5328"
SCOPE = "XboxLive.signin offline_access"
USER_AGENT = "MaiBot-Minecraft-Plugin/1.0"


class AuthError(RuntimeError):
    """认证流程失败（网络、账号或 Xbox Live 限制）。"""


@dataclass(slots=True)
class AuthSession:
    """一次登录会话。"""

    username: str
    uuid: uuid.UUID
    access_token: str | None = None
    source: str = "offline"
    expires_at: float = 0.0

    @property
    def online(self) -> bool:
        return bool(self.access_token)

    def is_expired(self) -> bool:
        return self.online and self.expires_at > 0 and time.time() >= self.expires_at - 120

    def to_dict(self) -> dict:
        return {
            "username": self.username,
            "uuid": str(self.uuid),
            "access_token": self.access_token,
            "source": self.source,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AuthSession":
        return cls(
            username=str(data["username"]),
            uuid=uuid.UUID(str(data["uuid"])),
            access_token=data.get("access_token"),
            source=str(data.get("source", "offline")),
            expires_at=float(data.get("expires_at", 0.0)),
        )


def offline_session(username: str) -> AuthSession:
    """离线模式：UUID = md5("OfflinePlayer:<name>") 的 v3 UUID。"""
    digest = uuid.uuid3(uuid.NAMESPACE_DNS, f"OfflinePlayer:{username}")
    return AuthSession(username=username, uuid=digest, access_token=None, source="offline")


# --------------------------------------------------------------------------- HTTP
def _request(method: str, url: str, payload: dict | None = None, headers: dict | None = None, form: bool = False) -> dict:
    data = None
    request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if payload is not None:
        if form:
            data = urllib.parse.urlencode(payload).encode()
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload).encode()
            request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            parsed = {"error": body.decode("utf-8", "replace")[:500]}
        raise AuthError(f"{url} 返回 HTTP {exc.code}: {parsed}") from exc
    except urllib.error.URLError as exc:
        raise AuthError(f"无法连接 {url}: {exc.reason}") from exc
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return {"raw": body.decode("utf-8", "replace")}


async def _arequest(method: str, url: str, payload: dict | None = None, headers: dict | None = None, form: bool = False) -> dict:
    return await asyncio.to_thread(_request, method, url, payload, headers, form)


# ------------------------------------------------------------------------ 微软登录
_XSTS_ERRORS = {
    "2148916233": "该微软账号还没有 Xbox 档案，请先在 Xbox 官网/主机上创建角色。",
    "2148916235": "该账号所在地区不支持 Xbox Live。",
    "2148916236": "该账号需要完成成年人验证。",
    "2148916237": "该账号需要完成成年人验证。",
    "2148916238": "未成年人账号需要先加入 Microsoft 家庭组。",
    "2148916227": "该账号已被 Xbox Live 封禁。",
}


def _xbox_error(data: dict) -> AuthError:
    xerr = str(data.get("XErr", ""))
    message = _XSTS_ERRORS.get(xerr, f"Xbox Live 授权失败（XErr={xerr}）")
    return AuthError(message)


async def _xbox_to_minecraft(ms_access_token: str) -> AuthSession:
    """微软 access token → Xbox Live → XSTS → Minecraft access token → 档案。"""
    xbl = await _arequest(
        "POST",
        "https://user.auth.xboxlive.com/user/authenticate",
        {
            "Properties": {
                "AuthMethod": "RPS",
                "SiteName": "user.auth.xboxlive.com",
                "RpsTicket": f"d={ms_access_token}",
            },
            "RelyingParty": "http://auth.xboxlive.com",
            "TokenType": "JWT",
        },
    )
    xbl_token = xbl.get("Token")
    if not xbl_token:
        raise AuthError(f"Xbox Live 认证失败: {xbl}")

    xsts = await _arequest(
        "POST",
        "https://xsts.auth.xboxlive.com/xsts/authorize",
        {
            "Properties": {"SandboxId": "RETAIL", "UserTokens": [xbl_token]},
            "RelyingParty": "rp://api.minecraftservices.com/",
            "TokenType": "JWT",
        },
    )
    if "XErr" in xsts:
        raise _xbox_error(xsts)
    xsts_token = xsts.get("Token")
    if not xsts_token:
        raise AuthError(f"XSTS 授权失败: {xsts}")
    uhs = xsts.get("DisplayClaims", {}).get("xui", [{}])[0].get("uhs")
    if not uhs:
        raise AuthError(f"XSTS 响应缺少 uhs: {xsts}")

    login = await _arequest(
        "POST",
        "https://api.minecraftservices.com/authentication/login_with_xbox",
        {"identityToken": f"XBL3.0 x={uhs};{xsts_token}"},
    )
    access_token = login.get("access_token")
    if not access_token:
        raise AuthError(f"Minecraft 登录失败: {login}")

    profile = await _arequest(
        "GET",
        "https://api.minecraftservices.com/minecraft/profile",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    if "id" not in profile:
        raise AuthError("该账号没有 Minecraft: Java Edition 的拥有记录。")
    return AuthSession(
        username=profile["name"],
        uuid=uuid.UUID(profile["id"]),
        access_token=access_token,
        source="microsoft",
        expires_at=time.time() + float(login.get("expires_in", 86400)),
    )


async def microsoft_session(
    device_code_callback=None,
    *,
    client_id: str = DEFAULT_CLIENT_ID,
    timeout: float = 600.0,
    cache_path: Path | str | None = None,
) -> AuthSession:
    """微软设备码登录。

    ``device_code_callback(user_code, verification_uri, expires_in)`` 会在需要用户
    在浏览器完成授权时被调用（同步或异步函数均可）。
    """
    cached = load_cached_session(cache_path)
    if cached and not cached.is_expired():
        return cached

    device = await _arequest(
        "POST",
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode",
        {"client_id": client_id, "scope": SCOPE},
        form=True,
    )
    user_code = device.get("user_code")
    if not user_code:
        raise AuthError(f"获取设备码失败: {device}")
    verification_uri = device.get("verification_uri", "https://microsoft.com/link")
    interval = float(device.get("interval", 5))
    expires_in = float(device.get("expires_in", 900))

    if device_code_callback is not None:
        result = device_code_callback(user_code, verification_uri, expires_in)
        if asyncio.iscoroutine(result):
            await result

    deadline = time.monotonic() + min(timeout, expires_in)
    token: dict = {}
    while time.monotonic() < deadline:
        await asyncio.sleep(interval)
        try:
            token = await _arequest(
                "POST",
                "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
                {
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "client_id": client_id,
                    "device_code": device["device_code"],
                },
                form=True,
            )
        except AuthError as exc:
            # authorization_pending 属于正常等待，其余错误直接抛出
            text = str(exc)
            if "authorization_pending" in text:
                continue
            if "slow_down" in text:
                interval += 5
                continue
            raise
        if token.get("access_token"):
            break
        error = token.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        if error:
            raise AuthError(f"设备码授权失败: {error} {token.get('error_description', '')}")
    else:
        raise AuthError("设备码授权超时，请重新开始登录。")

    session = await _xbox_to_minecraft(token["access_token"])
    save_cached_session(session, cache_path)
    return session


async def join_server(session: AuthSession, server_hash: str) -> None:
    """通知 Mojang 会话服务：本账号要加入 server_hash 对应的服务器。"""
    if not session.access_token:
        raise AuthError("离线会话无法加入正版验证服务器，请配置微软登录。")
    await _arequest(
        "POST",
        "https://sessionserver.mojang.com/session/minecraft/join",
        {
            "accessToken": session.access_token,
            "selectedProfile": session.uuid.hex,
            "serverId": server_hash,
        },
    )


# ------------------------------------------------------------------------ 缓存
def load_cached_session(cache_path: Path | str | None) -> AuthSession | None:
    if not cache_path:
        return None
    path = Path(cache_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AuthSession.from_dict(data)
    except Exception:
        return None


def save_cached_session(session: AuthSession, cache_path: Path | str | None) -> None:
    if not cache_path:
        return
    path = Path(cache_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass
