# -*- coding: utf-8 -*-
"""
企业微信通知服务
- 机器人 Webhook（单向推送）
- 自建应用（双向对话，需要 ngrok 回调）
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional

import requests

from ..config import JsonConfigFile, REQUEST_TIMEOUT, WXAPP_CONFIG_FILE, WXAPP_SERVER_PORT, LEGACY_PATHS


class WxRobotNotifier:
    """企业微信群机器人，单向推送"""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self._session = requests.Session()

    def send_text(self, content: str) -> bool:
        if not self.webhook_url:
            return False
        try:
            resp = self._session.post(
                self.webhook_url,
                json={"msgtype": "text", "text": {"content": content}},
                timeout=REQUEST_TIMEOUT,
            )
            return resp.status_code == 200 and resp.json().get("errcode") == 0
        except Exception:
            return False

    def send_markdown(self, content: str) -> bool:
        if not self.webhook_url:
            return False
        try:
            resp = self._session.post(
                self.webhook_url,
                json={"msgtype": "markdown", "markdown": {"content": content}},
                timeout=REQUEST_TIMEOUT,
            )
            return resp.status_code == 200 and resp.json().get("errcode") == 0
        except Exception:
            return False


class FeishuRobotNotifier:
    """飞书机器人，单向推送"""

    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        self._session = requests.Session()

    def send_text(self, content: str) -> tuple[bool, str]:
        if not self.webhook_url:
            return False, "feishu webhook empty"
        try:
            resp = self._session.post(
                self.webhook_url,
                json={"msg_type": "text", "content": {"text": content}},
                timeout=REQUEST_TIMEOUT,
            )
            data = resp.json() if resp.content else {}
            code = int(data.get("code", -1)) if isinstance(data, dict) else -1
            if resp.status_code == 200 and code == 0:
                return True, "ok"
            return False, str(data.get("msg") or data.get("message") or f"http {resp.status_code}")
        except Exception as exc:
            return False, str(exc)


class WxAppConfig:
    """企业微信自建应用配置读写"""

    def __init__(self, path: str = WXAPP_CONFIG_FILE):
        legacy_path = None if path != WXAPP_CONFIG_FILE else LEGACY_PATHS["wxapp_config"]
        self._file = JsonConfigFile(path, legacy_path)
        self.path = self._file.path
        self._data: dict = {}
        self._load()

    def _load(self):
        self._data = self._file.load(dict)
        self.path = self._file.path

    def _save(self):
        target = self._file.save(self._data)
        self.path = str(target)

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
        self._save()

    @property
    def corp_id(self) -> str:
        return self._data.get("corp_id", "")

    @property
    def corp_secret(self) -> str:
        value = str(self._data.get("corp_secret", "") or "").strip()
        if value:
            return value
        return str(self._data.get("secret", "") or "").strip()

    @property
    def agent_id(self) -> str:
        return self._data.get("agent_id", "")

    @property
    def token(self) -> str:
        return self._data.get("token", "")

    @property
    def encoding_aes_key(self) -> str:
        return self._data.get("encoding_aes_key", "")

    @property
    def access_token(self) -> str:
        return self._data.get("access_token", "")

    @property
    def token_expires_at(self) -> Optional[datetime.datetime]:
        ts = self._data.get("token_expires_at")
        return datetime.datetime.fromisoformat(ts) if ts else None

    def save_token(self, token: str, expires_in: int):
        self._data["access_token"] = token
        exp = datetime.datetime.now() + datetime.timedelta(seconds=expires_in - 60)
        self._data["token_expires_at"] = exp.isoformat()
        self._save()


class WxAppClient:
    """企业微信自建应用：Token 管理 + 消息发送"""

    TOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    SEND_URL = "https://qyapi.weixin.qq.com/cgi-bin/message/send"

    def __init__(self, config: WxAppConfig):
        self.config = config
        self._lock = threading.Lock()
        self._session = requests.Session()

    def ensure_token(self) -> bool:
        expires_at = self.config.token_expires_at
        if expires_at and datetime.datetime.now() < expires_at and self.config.access_token:
            return True
        return self._refresh_token()

    def _refresh_token(self) -> bool:
        try:
            resp = self._session.get(
                self.TOKEN_URL,
                params={"corpid": self.config.corp_id, "corpsecret": self.config.corp_secret},
                timeout=REQUEST_TIMEOUT,
            )
            data = resp.json()
            if data.get("errcode") == 0:
                self.config.save_token(data["access_token"], data.get("expires_in", 7200))
                return True
        except Exception:
            pass
        return False

    def send_text(self, to_user: str, content: str) -> bool:
        if not self.ensure_token():
            return False
        payload = {
            "touser": to_user,
            "msgtype": "text",
            "agentid": self.config.agent_id,
            "text": {"content": content},
        }
        try:
            with self._lock:
                resp = self._session.post(
                    f"{self.SEND_URL}?access_token={self.config.access_token}",
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )
            return resp.json().get("errcode") == 0
        except Exception:
            return False

    def send_text_with_reason(self, to_user: str, content: str) -> tuple[bool, str]:
        if not self.ensure_token():
            return False, "ensure_token failed"
        payload = {
            "touser": to_user,
            "msgtype": "text",
            "agentid": self.config.agent_id,
            "text": {"content": content},
        }
        try:
            with self._lock:
                resp = self._session.post(
                    f"{self.SEND_URL}?access_token={self.config.access_token}",
                    json=payload,
                    timeout=REQUEST_TIMEOUT,
                )
            data = resp.json()
            ok = data.get("errcode") == 0
            if ok:
                return True, "ok"
            return False, str(data.get("errmsg") or data.get("errcode") or "send failed")
        except Exception as exc:
            return False, str(exc)

    def broadcast_text(self, content: str) -> bool:
        return self.send_text("@all", content)

    def broadcast_text_with_reason(self, content: str) -> tuple[bool, str]:
        return self.send_text_with_reason("@all", content)


class NgrokManager:
    """管理 ngrok 隧道，自动启动/停止"""

    def __init__(self, local_port: int = WXAPP_SERVER_PORT):
        self.local_port = local_port
        self._process: Optional[subprocess.Popen] = None
        self.public_url: str = ""

    def start(self) -> str:
        self._process = subprocess.Popen(
            ["ngrok", "http", str(self.local_port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)
        try:
            resp = requests.get("http://localhost:4040/api/tunnels", timeout=5)
            tunnels = resp.json().get("tunnels", [])
            for tunnel in tunnels:
                if tunnel.get("proto") == "https":
                    self.public_url = tunnel["public_url"]
                    return self.public_url
        except Exception:
            pass
        return ""

    def stop(self):
        if self._process:
            self._process.terminate()
            self._process = None
        self.public_url = ""

    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None


class WxCallbackServer:
    """
    本地 HTTP 服务器，接收企业微信回调消息
    command_handler: Callable[[str, str], str]  接受 (from_user, content) 返回回复文本
    """

    def __init__(self, port: int = WXAPP_SERVER_PORT, command_handler: Optional[Callable[[str, str], str]] = None):
        self.port = port
        self.command_handler = command_handler
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        handler_cls = self._make_handler(self.command_handler)
        self._server = HTTPServer(("0.0.0.0", self.port), handler_cls)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None

    def is_running(self) -> bool:
        return self._server is not None

    @staticmethod
    def _make_handler(command_handler: Optional[Callable]):
        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                reply = ""
                if command_handler:
                    try:
                        data = json.loads(body)
                        from_user = data.get("FromUserName", "")
                        content = data.get("Content", "")
                        threading.Thread(target=command_handler, args=(from_user, content), daemon=True).start()
                        reply = "OK"
                    except Exception:
                        reply = "ERROR"
                self.send_response(200)
                self.end_headers()
                self.wfile.write(reply.encode())

            def log_message(self, *args):
                pass

        return _Handler
