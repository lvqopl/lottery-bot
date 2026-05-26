from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import shlex
from typing import Any, Dict, List, Optional

from .utils import (
    esc,
    format_prize_items,
    jdump,
    jload,
    method_text,
    now_s,
    parse_int,
    parse_methods,
    parse_optional_int,
    parse_prize_items,
)


def patch_giveaway_bot(cls: Any) -> None:
    def parse_giveaway_id_arg(self, arg: str) -> int:
        import re
        match = re.match(r'^\s*#?(?P<gid>\d+)\s*$', arg.strip())
        if not match:
            raise ValueError('请输入抽奖编号，例如 1 或 #1。')
        return int(match.group('gid'))

    def build_giveaway_status_text(self, status: str) -> str:
        return {
            'scheduled': '未开始',
            'live': '进行中',
            'finalizing': '开奖中',
            'ended': '已结束',
            'cancelled': '已取消',
        }.get(status, '未知')

    def get_giveaways_by_filter(self, status_filter: str) -> List[Dict[str, Any]]:
        if status_filter == 'live':
            return self.db.all("SELECT * FROM giveaways WHERE status='live' ORDER BY id DESC")
        if status_filter == 'ended':
            return self.db.all("SELECT * FROM giveaways WHERE status IN ('ended', 'cancelled') ORDER BY id DESC")
        if status_filter == 'scheduled':
            return self.db.all("SELECT * FROM giveaways WHERE status='scheduled' ORDER BY id DESC")
        return self.db.list_giveaways(50)

    def normalize_list_filter(self, arg: str) -> str:
        token = (arg or '').strip().split(maxsplit=1)[0].lower() if (arg or '').strip() else ''
        if not token:
            return 'all'
        mapping = {'live': 'live', 'ongoing': 'live', 'active': 'live', 'ended': 'ended', 'finished': 'ended', 'done': 'ended', 'all': 'all', 'all_giveaways': 'all'}
        if token in mapping:
            return mapping[token]
        raise ValueError('请输入 live、ended 或 all。')

    def build_list_query_buttons(self) -> List[List[Dict[str, Any]]]:
        return [[{'text': '进行中', 'callback_data': 'list:live'}, {'text': '已结束', 'callback_data': 'list:ended'}, {'text': '全部', 'callback_data': 'list:all'}]]

    def build_giveaway_list_text(self, giveaways: List[Dict[str, Any]], status_filter: str) -> str:
        label = {'live': '进行中', 'ended': '已结束', 'all': '全部'}.get(status_filter, '全部')
        lines = [f'<b>抽奖列表 - {esc(label)}</b>', '']
        if not giveaways:
            lines.append('当前没有符合条件的抽奖。')
            return '\n'.join(lines)
        for giveaway in giveaways[:20]:
            prize = format_prize_items(jload(giveaway.get('prize_json'), [])) or giveaway.get('prize') or '奖品'
            status_text = self.build_giveaway_status_text(giveaway.get('status') or '')
            participant_count = self.db.count_participants(giveaway['id'])
            creator = self.db.get_user_by_id(int(giveaway['created_by']))
            creator_username = creator.get('username') if creator else giveaway.get('created_by_username')
            creator_display = self.display_user_html(creator_username, creator.get('first_name') if creator else None, creator.get('last_name') if creator else None, int(giveaway['created_by']))
            lines.extend([
                f'<b>#{giveaway["id"]} {esc(giveaway["title"])} </b>',
                f'状态：{esc(status_text)}',
                f'奖品：{esc(prize)}',
                f'已参与人数：{participant_count}',
                f'时间：{esc(giveaway["start_time"])} -> {esc(giveaway["end_time"] or "不限")}',
                f'创建者：{creator_display}',
                '',
            ])
        return '\n'.join(lines).rstrip()

    def send_giveaway_list(self, chat_id: Any, status_filter: str, message_id: Optional[int] = None) -> None:
        giveaways = self.get_giveaways_by_filter(status_filter)
        text = self.build_giveaway_list_text(giveaways, status_filter)
        buttons = {'inline_keyboard': self.build_list_query_buttons()}
        if message_id is not None:
            try:
                self.api.edit_message_text(chat_id, int(message_id), text, reply_markup=buttons)
                return
            except Exception as exc:
                logging.warning('Failed to edit giveaway list message: %s', exc)
        self.api.send_message(chat_id, text, reply_markup=buttons)

    def list_giveaways(self, message: Dict[str, Any], arg: str = '') -> None:
        chat = message['chat']
        try:
            status_filter = self.normalize_list_filter(arg) if arg.strip() else 'all'
            self.send_giveaway_list(chat['id'], status_filter)
        except Exception as exc:
            self.api.send_message(chat['id'], f'获取抽奖列表失败：{exc}')

    def handle_list_giveaways_callback(self, callback_query: Dict[str, Any], status_filter: str) -> None:
        message = callback_query.get('message') or {}
        chat = message.get('chat') or {}
        self.safe_answer_callback_query(callback_query['id'], '已刷新列表', show_alert=False)
        self.send_giveaway_list(chat['id'], status_filter, message_id=message.get('message_id'))

    def close_giveaway(self, message: Dict[str, Any], arg: str, cancel: bool = False) -> None:
        user = message['from']
        chat = message['chat']
        if not arg.strip():
            usage = '/cancel_giveaway <id>' if cancel else '/end_giveaway <id>'
            self.api.send_message(chat['id'], f'用法：{usage}，支持 1 或 #1 这种编号。')
            return
        try:
            giveaway_id = self.parse_giveaway_id_arg(arg)
        except Exception as exc:
            self.api.send_message(chat['id'], f'编号解析失败：{exc}')
            return
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            self.api.send_message(chat['id'], '找不到这个抽奖。')
            return
        if not self.is_admin_of_chat(giveaway['publish_chat_ref'], user['id']):
            self.api.send_message(chat['id'], '只有发布群/频道的管理员可以操作。')
            return
        if giveaway['status'] in {'ended', 'cancelled'}:
            self.api.send_message(chat['id'], '这个抽奖已经结束或取消，不能再次操作。')
            return
        if cancel:
            self.db.update_giveaway(giveaway_id, status='cancelled', cancelled_at=now_s(), cancellation_reason='manual_cancel')
            if giveaway.get('claim_topic_thread_id') and giveaway.get('claim_group_ref'):
                try:
                    self.api.delete_forum_topic(giveaway['claim_group_ref'], int(giveaway['claim_topic_thread_id']))
                except Exception as exc:
                    logging.warning('Failed to delete claim topic while cancelling giveaway %s: %s', giveaway_id, exc)
            self.db.log('giveaway_cancelled', user['id'], giveaway_id, {'manual': True})
            self.api.send_message(chat['id'], f'已取消抽奖 #{giveaway_id}。')
            return
        if giveaway['status'] != 'live':
            self.db.update_giveaway(giveaway_id, status='live')
        ok = self.finalize_giveaway(giveaway_id, manual=True)
        self.api.send_message(chat['id'], f'已立即开奖 #{giveaway_id}。' if ok else '立即开奖失败，请稍后重试。')

    def set_claim_topic_command(self, message: Dict[str, Any], arg: str) -> None:
        user = message['from']
        chat = message['chat']
        if not arg.strip():
            self.api.send_message(chat['id'], '用法：/set_claim_topic <id> -claim_group @group -claim_topic "领奖话题" -claim_hours 72')
            return
        try:
            tokens = shlex.split(arg)
            giveaway_id = self.parse_giveaway_id_arg(tokens[0])
            flags = self.parse_command_flags(' '.join(tokens[1:])) if len(tokens) > 1 else {}
        except Exception as exc:
            self.api.send_message(chat['id'], f'参数解析失败：{exc}')
            return
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            self.api.send_message(chat['id'], '找不到这个抽奖。')
            return
        if giveaway['status'] in {'ended', 'cancelled'}:
            self.api.send_message(chat['id'], '这个抽奖已结束或取消，不能修改领奖配置。')
            return
        if not flags:
            self.api.send_message(chat['id'], self.giveaway_summary(giveaway_id))
            return
        updates: Dict[str, Any] = {}
        claim_group_ref = self.flag_value(flags, 'claim_group', 'claim_group_ref')
        claim_topic_name = self.flag_value(flags, 'claim_topic', 'claim_topic_name')
        claim_hours_text = self.flag_value(flags, 'claim_hours', 'claim_topic_hours')
        if 'claim_group' in flags or 'claim_group_ref' in flags:
            updates['claim_group_ref'] = claim_group_ref or None
        if 'claim_topic' in flags or 'claim_topic_name' in flags:
            updates['claim_topic_name'] = claim_topic_name or None
        if 'claim_hours' in flags or 'claim_topic_hours' in flags:
            updates['claim_topic_hours'] = parse_optional_int(claim_hours_text, '领奖有效时长', default=72, minimum=1)
        effective_claim_group = updates.get('claim_group_ref', giveaway.get('claim_group_ref'))
        if (('claim_group' in flags or 'claim_group_ref' in flags or 'claim_topic' in flags or 'claim_topic_name' in flags or 'claim_hours' in flags or 'claim_topic_hours' in flags) and not effective_claim_group):
            self.api.send_message(chat['id'], '请先指定 -claim_group。')
            return
        if effective_claim_group and not self.is_admin_of_chat(effective_claim_group, user['id']):
            self.api.send_message(chat['id'], '你不是领奖群/频道管理员。')
            return
        updates['claim_topic_enabled'] = 1 if (updates.get('claim_group_ref') or giveaway.get('claim_group_ref') or updates.get('claim_topic_name') or giveaway.get('claim_topic_name')) else 0
        self.db.update_giveaway(giveaway_id, **updates)
        self.db.log('claim_topic_updated', user['id'], giveaway_id, updates)
        self.api.send_message(chat['id'], f'已更新抽奖 #{giveaway_id} 的领奖配置。')

    def edit_giveaway_command(self, message: Dict[str, Any], arg: str) -> None:
        user = message['from']
        chat = message['chat']
        if not arg.strip():
            self.api.send_message(chat['id'], '用法：/edit_giveaway <id> [flags]')
            return
        try:
            tokens = shlex.split(arg)
            giveaway_id = self.parse_giveaway_id_arg(tokens[0])
            flags = self.parse_command_flags(' '.join(tokens[1:])) if len(tokens) > 1 else {}
        except Exception as exc:
            self.api.send_message(chat['id'], f'参数解析失败：{exc}')
            return
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            self.api.send_message(chat['id'], '找不到这个抽奖。')
            return
        if giveaway['status'] in {'ended', 'cancelled'}:
            self.api.send_message(chat['id'], '这个抽奖已结束或取消，不能修改。')
            return
        if not self.is_admin_of_chat(giveaway['publish_chat_ref'], user['id']):
            self.api.send_message(chat['id'], '只有发布群/频道的管理员可以修改。')
            return
        updates: Dict[str, Any] = {}
        if self.flag_value(flags, 'title') is not None:
            updates['title'] = self.flag_value(flags, 'title')
        if self.flag_value(flags, 'prize') is not None:
            prize_text = self.flag_value(flags, 'prize') or ''
            updates['prize'] = prize_text
            updates['prize_json'] = jdump(parse_prize_items(prize_text))
        if self.flag_value(flags, 'num', 'winner_count', 'winners') is not None:
            updates['winner_count'] = parse_int(self.flag_value(flags, 'num', 'winner_count', 'winners') or '', '中奖人数', minimum=1)
        if self.flag_value(flags, 'condition') is not None:
            updates['participation_condition'] = self.flag_value(flags, 'condition')
        if self.flag_value(flags, 'methods') is not None:
            updates['entry_methods'] = jdump(parse_methods(self.flag_value(flags, 'methods') or ''))
        if self.flag_value(flags, 'keyword') is not None:
            updates['entry_keyword'] = self.flag_value(flags, 'keyword') or None
        if self.flag_value(flags, 'check_channel') is not None:
            required_channel = self.flag_value(flags, 'check_channel') or None
            updates['require_channel'] = 1 if required_channel else 0
            updates['required_channel'] = required_channel
        if self.flag_value(flags, 'invite_need') is not None:
            updates['invite_required_count'] = parse_int(self.flag_value(flags, 'invite_need') or '', '邀请人数', minimum=0)
        if self.flag_value(flags, 'invite_bonus') is not None:
            updates['invite_weight_bonus'] = parse_int(self.flag_value(flags, 'invite_bonus') or '', '邀请加权', minimum=0)
        if self.flag_value(flags, 'publish') is not None:
            updates['publish_chat_ref'] = self.flag_value(flags, 'publish')
        if self.flag_value(flags, 'start') is not None:
            from .utils import parse_dt
            updates['start_time'] = parse_dt(self.flag_value(flags, 'start') or 'now').isoformat(sep=' ')
        if self.flag_value(flags, 'end', 'end_time') is not None:
            from .utils import parse_dt
            end_time = parse_dt(self.flag_value(flags, 'end', 'end_time') or 'now')
            updates['end_time'] = end_time.isoformat(sep=' ')
        if self.flag_value(flags, 'draw_n') is not None:
            updates['draw_when_participants'] = parse_int(self.flag_value(flags, 'draw_n') or '', '自动开奖人数', minimum=1)
        if self.flag_value(flags, 'weight') is not None:
            weight_map = parse_weight_map(self.flag_value(flags, 'weight'), self.resolve_username_to_user_id)
            for uid, weight in weight_map.items():
                self.db.set_giveaway_weight(giveaway_id, int(uid), int(weight), reason='special_weight')
        if 'draw_when_participants' in updates or 'end_time' in updates or 'start_time' in updates:
            draw_when = updates.get('draw_when_participants', giveaway.get('draw_when_participants'))
            end_time = updates.get('end_time', giveaway.get('end_time'))
            if draw_when and end_time:
                updates['auto_draw_mode'] = 'both'
            elif draw_when:
                updates['auto_draw_mode'] = 'participants'
            elif end_time:
                updates['auto_draw_mode'] = 'time'
            else:
                updates['auto_draw_mode'] = 'manual'
        self.db.update_giveaway(giveaway_id, **updates)
        self.db.log('giveaway_updated', user['id'], giveaway_id, updates)
        if giveaway.get('announcement_sent') == 1 and giveaway.get('announcement_message_id') and (not updates.get('publish_chat_ref') or updates.get('publish_chat_ref') == giveaway.get('publish_chat_ref')):
            threading.Thread(target=self.refresh_announcement_count, args=(giveaway_id,), daemon=True).start()
        self.api.send_message(chat['id'], f'已更新抽奖 #{giveaway_id}。')

    def export_participants(self, message: Dict[str, Any], arg: str) -> None:
        user = message['from']
        chat = message['chat']
        if not arg.strip():
            self.api.send_message(chat['id'], '用法：/export <id>')
            return
        try:
            giveaway_id = self.parse_giveaway_id_arg(arg)
        except Exception as exc:
            self.api.send_message(chat['id'], f'编号解析失败：{exc}')
            return
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            self.api.send_message(chat['id'], '找不到这个抽奖。')
            return
        if not self.is_admin_of_chat(giveaway['publish_chat_ref'], user['id']):
            self.api.send_message(chat['id'], '只有发布群/频道管理员可以导出名单。')
            return
        participants = self.db.get_participants(giveaway_id)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(['giveaway_id', 'title', 'telegram_user_id', 'username', 'first_name', 'last_name', 'display_name', 'source', 'invited_by', 'joined_at', 'valid', 'invalid_reason'])
        for row in participants:
            display_name = self.display_user(row.get('username'), row.get('first_name'), row.get('last_name'), int(row['telegram_user_id']))
            writer.writerow([giveaway_id, giveaway.get('title', ''), row.get('telegram_user_id'), row.get('username') or '', row.get('first_name') or '', row.get('last_name') or '', display_name, row.get('source') or '', row.get('invited_by') or '', row.get('joined_at') or '', row.get('valid') if row.get('valid') is not None else 1, row.get('invalid_reason') or ''])
        content = ('\ufeff' + buffer.getvalue()).encode('utf-8')
        self.api.send_document(chat['id'], f'giveaway_{giveaway_id}_participants.csv', content, caption=f'抽奖 #{giveaway_id} 参与名单导出完成。')

    def giveaway_summary(self, giveaway_id: int) -> str:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return '找不到这个抽奖。'
        prize = format_prize_items(jload(giveaway.get('prize_json'), [])) or giveaway.get('prize') or '奖品'
        entry_methods = method_text(jload(giveaway['entry_methods'], []), giveaway.get('entry_keyword'), giveaway.get('required_channel'), int(giveaway.get('invite_required_count') or 0), int(giveaway.get('invite_weight_bonus') or 0))
        claim_group = giveaway.get('claim_group_ref') or '未设置'
        claim_topic = giveaway.get('claim_topic_name') or '未设置'
        claim_hours = giveaway.get('claim_topic_hours') or 72
        draw_mode = {'time': '按时间开奖', 'participants': '按人数开奖', 'both': '时间或人数满足即开奖', 'manual': '手动开奖'}.get(giveaway.get('auto_draw_mode') or 'time', '按时间开奖')
        return f'<b>抽奖 #{giveaway_id}</b>\n\n标题：{esc(giveaway["title"])}\n奖品：{esc(prize)}\n中奖人数：{esc(giveaway["winner_count"])}\n状态：{esc(self.build_giveaway_status_text(giveaway.get("status") or ""))}\n开奖模式：{esc(draw_mode)}\n开始时间：{esc(giveaway["start_time"])}\n结束时间：{esc(giveaway["end_time"] or "不限")}\n自动开奖人数：{esc(giveaway.get("draw_when_participants") or "未设置")}\n已参与人数：{self.db.count_participants(giveaway_id)}\n参与方式：{esc(entry_methods)}\n参与条件：{esc(giveaway["participation_condition"])}\n领奖群聊：{esc(claim_group)}\n领奖话题：{esc(claim_topic)}\n领奖有效时长：{esc(claim_hours)} 小时\n公布位置：{esc(giveaway["publish_chat_ref"])}'

    def build_result_verification_text(self, giveaway_id: int) -> str:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return '找不到这个抽奖。'
        result_payload = jload(giveaway.get('result_payload_json'), {})
        result_hash = giveaway.get('result_hash') or stable_result_hash(result_payload)
        seed_hash = giveaway.get('seed_hash') or ''
        steps = [
            '1. 取开奖消息里展示的“开奖结果哈希”。',
            '2. 将开奖结果载荷按规范化 JSON 进行序列化。',
            '3. 对序列化结果计算 SHA-256。',
            '4. 比对计算值是否与消息中的哈希一致。',
            '5. 如果一致，说明开奖结果未被改写。',
        ]
        return (
            f'<b>开奖结果验证</b>\n\n'
            f'抽奖编号：{giveaway_id}\n'
            f'结果哈希：<code>{esc(result_hash)}</code>\n'
            f'种子哈希：<code>{esc(seed_hash)}</code>\n'
            f'验证算法：SHA-256\n\n'
            '详细验证步骤：\n' + '\n'.join(steps) + '\n\n'
            '如果你需要自己复算，我可以把开奖载荷再展开给你。'
        )

    cls.parse_giveaway_id_arg = parse_giveaway_id_arg
    cls.build_giveaway_status_text = build_giveaway_status_text
    cls.get_giveaways_by_filter = get_giveaways_by_filter
    cls.normalize_list_filter = normalize_list_filter
    cls.build_list_query_buttons = build_list_query_buttons
    cls.build_giveaway_list_text = build_giveaway_list_text
    cls.send_giveaway_list = send_giveaway_list
    cls.list_giveaways = list_giveaways
    cls.handle_list_giveaways_callback = handle_list_giveaways_callback
    cls.close_giveaway = close_giveaway
    cls.set_claim_topic_command = set_claim_topic_command
    cls.edit_giveaway_command = edit_giveaway_command
    cls.export_participants = export_participants
    cls.giveaway_summary = giveaway_summary
    cls.build_result_verification_text = build_result_verification_text
