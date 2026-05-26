import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .utils import jdump


class TelegramClient:
    def __init__(self, token: str, timeout: int = 30) -> None:
        self.token = token
        self.timeout = timeout
        self.api_base = f'https://api.telegram.org/bot{token}'
        self._me: Optional[Dict[str, Any]] = None
        handlers = []
        proxy_url = os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') or os.environ.get('HTTP_PROXY') or os.environ.get('http_proxy')
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({'http': proxy_url, 'https': proxy_url}))
        self._opener = urllib.request.build_opener(*handlers) if handlers else urllib.request.build_opener()

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, files: Optional[Dict[str, Tuple[str, bytes, str]]] = None) -> Dict[str, Any]:
        url = f'{self.api_base}/{method}'
        params = params or {}
        last_exc: Optional[BaseException] = None
        for attempt in range(2):
            try:
                if files:
                    body, headers = self._encode_multipart(params, files)
                    req = urllib.request.Request(url, data=body, headers=headers)
                else:
                    encoded = urllib.parse.urlencode({k: self._stringify(v) for k, v in params.items()}).encode('utf-8')
                    req = urllib.request.Request(url, data=encoded)
                with self._opener.open(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode('utf-8'))
                if not payload.get('ok'):
                    raise RuntimeError(f'Telegram API {method} returned error: {payload}')
                return payload['result']
            except urllib.error.HTTPError as exc:
                body = exc.read().decode('utf-8', errors='replace')
                raise RuntimeError(f'Telegram API {method} failed: {exc.code} {body}') from exc
            except (urllib.error.URLError, ConnectionError, TimeoutError, ssl.SSLError, OSError) as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(0.4)
                    continue
                raise RuntimeError(f'Telegram API {method} failed: {exc}') from exc
            except Exception as exc:
                raise RuntimeError(f'Telegram API {method} failed: {exc}') from exc
        raise RuntimeError(f'Telegram API {method} failed: {last_exc}')
    def _stringify(self, value: Any) -> str:
        if value is None:
            return ''
        if isinstance(value, (dict, list, tuple)):
            return jdump(value)
        return str(value)

    def _encode_multipart(self, fields: Dict[str, Any], files: Dict[str, Tuple[str, bytes, str]]) -> Tuple[bytes, Dict[str, str]]:
        boundary = f'----LotteryBot{__import__("os").urandom(8).hex()}'
        body = bytearray()
        for key, value in fields.items():
            body.extend(f'--{boundary}\r\n'.encode('utf-8'))
            body.extend(f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode('utf-8'))
            body.extend(self._stringify(value).encode('utf-8'))
            body.extend(b'\r\n')
        for key, (filename, content, content_type) in files.items():
            body.extend(f'--{boundary}\r\n'.encode('utf-8'))
            body.extend(f'Content-Disposition: form-data; name="{key}"; filename="{filename}"\r\n'.encode('utf-8'))
            body.extend(f'Content-Type: {content_type}\r\n\r\n'.encode('utf-8'))
            body.extend(content)
            body.extend(b'\r\n')
        body.extend(f'--{boundary}--\r\n'.encode('utf-8'))
        return bytes(body), {'Content-Type': f'multipart/form-data; boundary={boundary}'}

    def get_me(self) -> Dict[str, Any]:
        if self._me is None:
            self._me = self.request('getMe')
        return self._me

    def get_updates(self, offset: Optional[int], timeout: int = 30):
        params: Dict[str, Any] = {'timeout': timeout, 'allowed_updates': jdump(['message', 'callback_query'])}
        if offset is not None:
            params['offset'] = offset
        return self.request('getUpdates', params)

    def send_message(self, chat_id: Any, text: str, reply_markup: Optional[Dict[str, Any]] = None, parse_mode: str = 'HTML', message_thread_id: Optional[int] = None):
        params: Dict[str, Any] = {'chat_id': chat_id, 'text': text, 'parse_mode': parse_mode, 'disable_web_page_preview': 1}
        if reply_markup:
            params['reply_markup'] = jdump(reply_markup)
        if message_thread_id is not None:
            params['message_thread_id'] = message_thread_id
        return self.request('sendMessage', params)

    def delete_message(self, chat_id: Any, message_id: int):
        return self.request('deleteMessage', {'chat_id': chat_id, 'message_id': message_id})

    def edit_message_text(self, chat_id: Any, message_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None, parse_mode: str = 'HTML', message_thread_id: Optional[int] = None):
        params: Dict[str, Any] = {'chat_id': chat_id, 'message_id': message_id, 'text': text, 'parse_mode': parse_mode}
        if reply_markup:
            params['reply_markup'] = jdump(reply_markup)
        if message_thread_id is not None:
            params['message_thread_id'] = message_thread_id
        return self.request('editMessageText', params)

    def answer_callback_query(self, callback_query_id: str, text: str = '', show_alert: bool = False):
        params = {'callback_query_id': callback_query_id, 'show_alert': 1 if show_alert else 0}
        if text:
            params['text'] = text
        return self.request('answerCallbackQuery', params)

    def get_chat_member(self, chat_id: Any, user_id: int):
        return self.request('getChatMember', {'chat_id': chat_id, 'user_id': user_id})

    def send_document(self, chat_id: Any, filename: str, content: bytes, caption: str = ''):
        return self.request('sendDocument', {'chat_id': chat_id, 'caption': caption}, {'document': (filename, content, 'text/csv; charset=utf-8')})

    def create_forum_topic(self, chat_id: Any, name: str, icon_color: Optional[int] = None, icon_custom_emoji_id: Optional[str] = None):
        params: Dict[str, Any] = {'chat_id': chat_id, 'name': name}
        if icon_color is not None:
            params['icon_color'] = icon_color
        if icon_custom_emoji_id:
            params['icon_custom_emoji_id'] = icon_custom_emoji_id
        return self.request('createForumTopic', params)

    def edit_forum_topic(self, chat_id: Any, message_thread_id: int, name: Optional[str] = None, icon_custom_emoji_id: Optional[str] = None):
        params: Dict[str, Any] = {'chat_id': chat_id, 'message_thread_id': message_thread_id}
        if name:
            params['name'] = name
        if icon_custom_emoji_id:
            params['icon_custom_emoji_id'] = icon_custom_emoji_id
        return self.request('editForumTopic', params)

    def close_forum_topic(self, chat_id: Any, message_thread_id: int):
        return self.request('closeForumTopic', {'chat_id': chat_id, 'message_thread_id': message_thread_id})

    def delete_forum_topic(self, chat_id: Any, message_thread_id: int):
        return self.request('deleteForumTopic', {'chat_id': chat_id, 'message_thread_id': message_thread_id})

    def create_chat_invite_link(self, chat_id: Any, name: Optional[str] = None, expire_date: Optional[int] = None, member_limit: Optional[int] = None, creates_join_request: Optional[bool] = None):
        params: Dict[str, Any] = {'chat_id': chat_id}
        if name:
            params['name'] = name
        if expire_date is not None:
            params['expire_date'] = expire_date
        if member_limit is not None:
            params['member_limit'] = member_limit
        if creates_join_request is not None:
            params['creates_join_request'] = 1 if creates_join_request else 0
        return self.request('createChatInviteLink', params)

    def pin_chat_message(self, chat_id: Any, message_id: int, disable_notification: bool = True):
        return self.request('pinChatMessage', {'chat_id': chat_id, 'message_id': message_id, 'disable_notification': 1 if disable_notification else 0})

    def unpin_chat_message(self, chat_id: Any, message_id: Optional[int] = None):
        params: Dict[str, Any] = {'chat_id': chat_id}
        if message_id is not None:
            params['message_id'] = message_id
        return self.request('unpinChatMessage', params)
