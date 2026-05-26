import datetime as dt
import hashlib
import hmac
import html
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple


def now() -> dt.datetime:
    return dt.datetime.now().replace(microsecond=0)


def now_s() -> str:
    return now().isoformat(sep=' ')


def esc(value: Any) -> str:
    return html.escape('' if value is None else str(value), quote=False)


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def jload(value: Optional[str], default: Any = None) -> Any:
    if not value:
        return {} if default is None else default
    try:
        return json.loads(value)
    except Exception:
        return {} if default is None else default


def parse_dt(text: str) -> dt.datetime:
    raw = (text or "").strip()
    if raw.lower() in {'now', '立即', '现在'}:
        return now()
    for fmt in ('%Y-%m-%d %H:%M', '%Y/%m/%d %H:%M', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S'):
        try:
            return dt.datetime.strptime(raw, fmt).replace(microsecond=0)
        except ValueError:
            pass
    raise ValueError('时间格式请使用 YYYY-MM-DD HH:MM 或 YYYY-MM-DD HH:MM:SS，例如 2026-05-25 20:00:30。')


def parse_duration(text: str) -> Optional[dt.timedelta]:
    raw = text.strip().lower()
    if raw in {'', 'none', 'unlimited', '不限', '无', 'no', 'off'}:
        return None
    raw = (
        raw.replace('天', 'd')
        .replace('小时', 'h')
        .replace('时', 'h')
        .replace('分钟', 'm')
        .replace('分', 'm')
        .replace('秒', 's')
    )
    if re.fullmatch(r'\d+', raw):
        return dt.timedelta(seconds=int(raw))
    match = re.fullmatch(r'(?:(?P<days>\d+)d)?(?:(?P<hours>\d+)h)?(?:(?P<minutes>\d+)m)?(?:(?P<seconds>\d+)s)?', raw)
    if not match or not any(match.groupdict().values()):
        raise ValueError('时长格式请使用 8h、30m、1d2h30m 或 unlimited。')
    parts = {key: int(value) if value else 0 for key, value in match.groupdict().items()}
    return dt.timedelta(days=parts['days'], hours=parts['hours'], minutes=parts['minutes'], seconds=parts['seconds'])


def parse_int(text: str, label: str, minimum: int = 1) -> int:
    try:
        value = int(text.strip())
    except Exception as exc:
        raise ValueError(f'{label} 必须是整数。') from exc
    if value < minimum:
        if minimum == 0:
            raise ValueError(f'{label} 不能小于 0。')
        raise ValueError(f'{label} 必须大于等于 {minimum}。')
    return value


def parse_optional_int(text: Optional[str], label: str, default: Optional[int] = None, minimum: int = 0) -> int:
    raw = (text or "").strip()
    if not raw:
        if default is None:
            raise ValueError(f'{label} 不能为空。')
        return default
    return parse_int(raw, label, minimum=minimum)


def parse_methods(text: str) -> List[str]:
    mapping = {
        'button': 'button', 'btn': 'button', '按钮': 'button', '点击按钮': 'button',
        'keyword': 'keyword', '关键词': 'keyword',
        'channel': 'channel', '群组': 'channel', '频道': 'channel',
        'invite': 'invite', '邀请': 'invite',
    }
    methods: List[str] = []
    for token in re.split(r'[\s,，、]+', text.strip().lower()):
        if not token:
            continue
        if token not in mapping:
            raise ValueError('参与方式仅支持 button, keyword, channel, invite（可用中文或英文填写，逗号分隔）。')
        method = mapping[token]
        if method not in methods:
            methods.append(method)
    if not methods:
        raise ValueError('至少选择一种参与方式。')
    return methods


def method_text(methods: List[str], keyword: Optional[str] = None, channel: Optional[str] = None, invite_count: int = 0, invite_bonus: int = 0) -> str:
    parts: List[str] = []
    for method in methods:
        if method == 'button':
            parts.append('点击按钮参与')
        elif method == 'keyword':
            parts.append('发送关键词 ' + (keyword or ''))
        elif method == 'channel':
            parts.append('加入频道/群组 ' + (channel or '') + ' 后参与')
        elif method == 'invite':
            extra = f'，每成功邀请 1 人增加 {invite_bonus} 权重' if invite_bonus > 0 else ''
            parts.append(f'邀请好友后参与（需成功邀请 {invite_count} 人{extra}）')
    return '、'.join(parts)


def parse_weight_map(text: Optional[str], username_resolver: Optional[Callable[[str], Optional[int]]] = None) -> Dict[int, int]:
    raw = text.strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    result: Dict[int, int] = {}
    if isinstance(parsed, dict):
        items = parsed.items()
    elif isinstance(parsed, list):
        items = []
        for item in parsed:
            if isinstance(item, dict):
                items.extend(item.items())
            else:
                raise ValueError('权重格式不正确。请使用 JSON 对象或 uid:weight 形式。')
    else:
        items = []
        for part in re.split(r'[\s,，、]+', raw):
            if not part:
                continue
            if ':' not in part:
                raise ValueError('权重格式不正确。请使用 uid:weight，例如 123456:100,234567:50。')
            uid_text, weight_text = part.split(':', 1)
            items.append((uid_text, weight_text))
    for uid_text, weight_text in items:
        token = str(uid_text).strip()
        try:
            if token.startswith('@'):
                if username_resolver is None:
                    raise ValueError('权重格式不正确，@username 需要先解析为已知用户 ID。')
                resolved = username_resolver(token.lstrip('@'))
                if resolved is None:
                    raise ValueError(f'无法解析用户名 {token}，请先让该用户与 bot 交互后再配置权重，或直接使用数字 ID。')
                uid = int(resolved)
            else:
                uid = int(token)
            weight = int(str(weight_text).strip())
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('权重格式不正确，用户ID和权重都必须是整数。') from exc
        if weight < 1:
            raise ValueError('权重必须大于等于 1。')
        result[uid] = weight
    return result


def parse_prize_items(text: str) -> List[Dict[str, Any]]:
    raw = text.strip()
    if not raw:
        return [{'name': '奖品1', 'count': 1}]
    items: List[Dict[str, Any]] = []
    for token in re.split(r'[，,、;；]+', raw):
        part = token.strip()
        if not part:
            continue
        match = re.match(r'^(?P<name>.+?)(?:\s*[*x×]\s*(?P<count>\d+))?$', part)
        if not match:
            raise ValueError('奖品格式不正确，请使用“奖品1*2、奖品2*3”这种格式。')
        name = match.group('name').strip()
        if not name:
            raise ValueError('奖品名称不能为空。')
        count = int(match.group('count') or 1)
        if count < 1:
            raise ValueError('奖品数量必须大于等于 1。')
        items.append({'name': name, 'count': count})
    return items or [{'name': '奖品1', 'count': 1}]


def format_prize_items(items: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for item in items:
        name = str(item.get('name', '')).strip() or '奖品'
        count = int(item.get('count') or 1)
        parts.append(f'{name}*{count}' if count != 1 else name)
    return '、'.join(parts) if parts else '奖品1'


def sign_invite_payload(secret: str, giveaway_id: int, referrer_user_id: int) -> str:
    payload = f'g{giveaway_id}_r{referrer_user_id}'
    sig = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()[:16]
    return f'{payload}_{sig}'


def verify_invite_payload(secret: str, payload: str) -> Optional[Tuple[int, int]]:
    match = re.match(r'^g(?P<gid>\d+)_r(?P<referrer>\d+)_(?P<sig>[0-9a-f]{16})$', payload)
    if not match:
        return None
    core = f"g{match.group('gid')}_r{match.group('referrer')}"
    expected = hmac.new(secret.encode('utf-8'), core.encode('utf-8'), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(expected, match.group('sig')):
        return None
    return int(match.group('gid')), int(match.group('referrer'))


def stable_result_hash(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def sign_join_payload(secret: str, giveaway_id: int) -> str:
    payload = f'j{giveaway_id}'
    sig = hmac.new(secret.encode('utf-8'), payload.encode('utf-8'), hashlib.sha256).hexdigest()[:16]
    return f'{payload}_{sig}'


def verify_join_payload(secret: str, payload: str) -> Optional[int]:
    match = re.match(r'^j(?P<gid>\d+)_(?P<sig>[0-9a-f]{16})$', payload)
    if not match:
        return None
    core = f"j{match.group('gid')}"
    expected = hmac.new(secret.encode('utf-8'), core.encode('utf-8'), hashlib.sha256).hexdigest()[:16]
    if not hmac.compare_digest(expected, match.group('sig')):
        return None
    return int(match.group('gid'))
