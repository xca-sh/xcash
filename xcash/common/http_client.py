"""进程级复用的出站 HTTP 客户端。

httpx.get/post 和每次新建的 httpx.Client 都会现场构造 SSLContext、加载 certifi CA 包
（实测约 17ms CPU），连接用完即关，下一次请求还要重新 TCP + TLS 握手。Tron 扫描每块都要
发请求，这部分开销会随出块持续累积。

固定主机的出站调用（Tron 节点、SaaS、AML、行情源）统一走这里的共享 Client：连接按 origin
keep-alive 复用，SSLContext 只构造一次。超时、请求头等仍由调用方逐请求传入。

按进程惰性创建并记录创建者 pid：Celery prefork / gunicorn 的子进程各自持有一份，fork 后
首次使用会重建，绝不跨进程共享 socket。httpx.Client 本身线程安全，gthread 下可直接共用。

商户 Webhook 不能用共享 Client：投递时把连接固定到已校验的 IP、经 sni_hostname 指定证书
主机名，而连接池按 IP 复用连接，同 IP 的两个商户会复用对方已建立的 TLS 连接、绕过证书校验。
Webhook 只复用 ssl_context()。
"""

from __future__ import annotations

import os
import ssl
import threading

import httpx

# shared_client() 持锁时会调用 ssl_context()，必须用可重入锁。
_lock = threading.RLock()
_client: httpx.Client | None = None
_client_pid: int | None = None
_ssl_context: ssl.SSLContext | None = None
_ssl_context_pid: int | None = None


def shared_client() -> httpx.Client:
    """返回本进程的共享 Client；fork 后的子进程首次调用会重建。"""
    global _client, _client_pid  # noqa: PLW0603
    pid = os.getpid()
    if _client is None or _client_pid != pid:
        with _lock:
            if _client is None or _client_pid != pid:
                _client = httpx.Client(verify=ssl_context())
                _client_pid = pid
    return _client


def ssl_context() -> ssl.SSLContext:
    """返回本进程复用的默认证书校验上下文（与 httpx 默认校验行为一致）。"""
    global _ssl_context, _ssl_context_pid  # noqa: PLW0603
    pid = os.getpid()
    if _ssl_context is None or _ssl_context_pid != pid:
        with _lock:
            if _ssl_context is None or _ssl_context_pid != pid:
                _ssl_context = httpx.create_ssl_context()
                _ssl_context_pid = pid
    return _ssl_context


def request(method: str, url: str, **kwargs) -> httpx.Response:
    return shared_client().request(method, url, **kwargs)


def get(url: str, **kwargs) -> httpx.Response:
    return shared_client().get(url, **kwargs)


def post(url: str, **kwargs) -> httpx.Response:
    return shared_client().post(url, **kwargs)
