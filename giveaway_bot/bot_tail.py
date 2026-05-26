from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import logging
import random
import re
import shlex
import threading
from typing import Any, Dict, List, Optional

from .utils import (
    esc,
    format_prize_items,
    jdump,
    jload,
    method_text,
    now,
    now_s,
    parse_dt,
    parse_duration,
    parse_int,
    parse_methods,
    parse_optional_int,
    parse_prize_items,
    parse_weight_map,
    stable_result_hash,
)


def patch_giveaway_bot(cls: Any) -> None:
    def build_result_announcement_text(self, giveaway: Dict[str, Any], winner_lines: List[str], claim_deadline: str, finished_at: str, result_hash: str, valid_count: int) -> str:
        prize_text = format_prize_items(jload(giveaway.get('prize_json'), [])) or giveaway.get('prize') or '奖品'
        lines = [
            '🎊 <b>抽奖结果公布！</b>',
            '',
            f'活动：{esc(giveaway["title"])}',
            f'奖品：{esc(prize_text)}',
            f'中奖人数：{esc(giveaway["winner_count"])}',
            f'有效参与人数：{valid_count}',
            f'开奖时间：{esc(finished_at)}',
            f'开奖结果哈希：<code>{esc(result_hash)}</code>',
            '',
            '中奖名单：',
        ]
        if winner_lines:
            lines.extend(f'• {line}' for line in winner_lines)
        else:
            lines.append('• 暂无符合条件的中奖者')
        lines.extend([
            '',
            f'领奖截止：{esc(claim_deadline)}',
            '点击下方按钮可查看开奖结果验证说明。',
        ])
        return '\n'.join(lines)

    def finalize_giveaway(self, giveaway_id: int, manual: bool = False) -> bool:
        with self.finalize_lock:
            if not self.db.start_finalization(giveaway_id):
                return False
            giveaway = self.db.get_giveaway(giveaway_id)
            if not giveaway or giveaway['status'] in {'ended', 'cancelled'}:
                return False

            participants = self.db.get_participants(giveaway_id)
            valid: List[Any] = []
            for participant in participants:
                if participant['valid'] == 0:
                    continue
                if self.db.is_blacklisted(participant['telegram_user_id']):
                    self.db.set_participant_validity(participant['id'], 0, '黑名单用户')
                    continue
                if giveaway.get('require_channel'):
                    ok, reason = self.check_required_channel(giveaway['required_channel'], participant['telegram_user_id'])
                    if not ok:
                        self.db.set_participant_validity(participant['id'], 0, reason)
                        continue
                valid.append(participant)

            weighted_pool = self.build_weighted_pool(giveaway, valid)
            total_weight = sum(item['weight'] for item in weighted_pool)
            winner_count = min(int(giveaway['winner_count']), len(valid))
            seed_material = jdump({
                'giveaway_id': giveaway_id,
                'seed_hash': giveaway.get('seed_hash'),
                'participants': sorted(int(p['telegram_user_id']) for p in valid),
                'weighted_pool': [(int(item['participant']['telegram_user_id']), int(item['weight'])) for item in weighted_pool],
                'winner_count': winner_count,
                'total_weight': total_weight,
            })
            seed_value = int(hashlib.sha256(seed_material.encode('utf-8')).hexdigest(), 16)
            rng = random.Random(seed_value)
            winners = self.weighted_sample_without_replacement(weighted_pool, winner_count, rng) if winner_count > 0 else []

            finished_at = now_s()
            claim_hours = int(giveaway.get('claim_topic_hours') or 72)
            claim_deadline = (dt.datetime.fromisoformat(finished_at) + dt.timedelta(hours=claim_hours)).isoformat(sep=' ')

            winner_lines: List[str] = []
            winner_payload: List[Dict[str, Any]] = []
            for winner in winners:
                participant = winner['participant']
                telegram_user_id = int(participant['telegram_user_id'])
                display_name = self.display_user_html(
                    participant.get('username'),
                    participant.get('first_name'),
                    participant.get('last_name'),
                    telegram_user_id,
                )
                winner_weight = int(winner['weight'])
                winner_payload.append({
                    'telegram_user_id': telegram_user_id,
                    'username': participant.get('username'),
                    'first_name': participant.get('first_name'),
                    'last_name': participant.get('last_name'),
                    'display_name': display_name,
                    'weight': winner_weight,
                    'winner_time': finished_at,
                })
                winner_lines.append(f'{display_name} | 权重 {winner_weight} | 开奖时间 {finished_at}')

            result_payload = {
                'giveaway_id': giveaway_id,
                'title': giveaway['title'],
                'finished_at': finished_at,
                'participants': len(valid),
                'winners': winner_payload,
                'seed_hash': giveaway.get('seed_hash'),
                'winner_count': winner_count,
                'total_weight': total_weight,
            }
            result_hash = stable_result_hash(result_payload)
            text = self.build_result_announcement_text(giveaway, winner_lines, claim_deadline, finished_at, result_hash, len(valid))

            try:
                result = self.api.send_message(
                    giveaway['publish_chat_ref'],
                    text,
                    reply_markup={'inline_keyboard': [[{'text': '验证开奖结果', 'callback_data': f'verify:{giveaway_id}'}]]},
                    message_thread_id=int(giveaway['publish_chat_thread_id']) if giveaway.get('publish_chat_thread_id') else None,
                )
                try:
                    self.api.pin_chat_message(giveaway['publish_chat_ref'], int(result.get('message_id')))
                except Exception as exc:
                    logging.warning('Failed to pin result for giveaway %s: %s', giveaway_id, exc)
            except Exception as exc:
                logging.exception('Failed to publish result for giveaway %s: %s', giveaway_id, exc)
                self.db.update_giveaway(giveaway_id, status='live', updated_at=now_s())
                return False

            self.db.finish_finalization(
                giveaway_id,
                status='ended',
                ended_at=finished_at,
                result_message_id=result.get('message_id'),
                winner_json=jdump(winner_payload),
                result_hash=result_hash,
                result_payload_json=jdump(result_payload),
                claim_deadline=claim_deadline,
            )
            self.db.log(
                'giveaway_ended',
                giveaway['created_by'],
                giveaway_id,
                {
                    'manual': manual,
                    'winner_count': len(winners),
                    'total_weight': total_weight,
                    'result_hash': result_hash,
                },
            )

            if giveaway.get('claim_topic_enabled') and giveaway.get('claim_group_ref'):
                self.create_claim_topic(giveaway_id)
            self.notify_winners(giveaway_id)
            return True

    def notify_winners(self, giveaway_id: int) -> None:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return
        winners = jload(giveaway.get('winner_json'), [])
        if not winners:
            return

        claim_deadline = giveaway.get('claim_topic_expire_at') or giveaway.get('claim_deadline') or ''
        claim_link = giveaway.get('claim_topic_invite_link')
        participants = self.db.get_participants(giveaway_id)
        participant_map = {int(row['telegram_user_id']): row for row in participants}

        for item in winners:
            telegram_user_id = int(item['telegram_user_id'])
            participant = participant_map.get(telegram_user_id)
            display_name = item.get('display_name') or self.display_user_html(
                item.get('username') or (participant.get('username') if participant else None),
                item.get('first_name') or (participant.get('first_name') if participant else None),
                item.get('last_name') or (participant.get('last_name') if participant else None),
                telegram_user_id,
            )
            lines = [
                '🎉 <b>恭喜中奖！</b>',
                '',
                f'活动：{esc(giveaway["title"])}',
                f'中奖身份：{display_name}',
                f'中奖权重：{esc(item.get("weight", 1))}',
            ]
            if claim_link:
                lines.append(f'领奖话题：{claim_link}')
                if claim_deadline:
                    lines.append(f'请在 {esc(claim_deadline)} 前进入领奖话题。')
            elif claim_deadline:
                lines.append(f'请在 {esc(claim_deadline)} 前联系管理员领奖。')
            try:
                self.api.send_message(telegram_user_id, '\n'.join(lines))
                self.db.set_participant_claim_notified(giveaway_id, telegram_user_id)
            except Exception as exc:
                logging.warning('Failed to notify winner %s: %s', telegram_user_id, exc)

    def create_claim_topic(self, giveaway_id: int) -> None:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway or not giveaway.get('claim_group_ref'):
            return
        if giveaway.get('claim_topic_thread_id') and not giveaway.get('claim_topic_deleted_at'):
            return
        topic_name = giveaway.get('claim_topic_name') or f'领奖话题 #{giveaway_id}'
        try:
            topic = self.api.create_forum_topic(giveaway['claim_group_ref'], topic_name)
            thread_id = int(topic.get('message_thread_id'))
            expire_at = giveaway.get('claim_deadline')
            invite_link = None
            if expire_at:
                try:
                    expire_dt = dt.datetime.fromisoformat(expire_at)
                    invite = self.api.create_chat_invite_link(
                        giveaway['claim_group_ref'],
                        name=f'claim-{giveaway_id}',
                        expire_date=int(expire_dt.timestamp()),
                    )
                    invite_link = invite.get('invite_link')
                except Exception as exc:
                    logging.warning('Failed to create claim invite link for giveaway %s: %s', giveaway_id, exc)
            self.db.update_giveaway(
                giveaway_id,
                claim_topic_thread_id=thread_id,
                claim_topic_invite_link=invite_link,
                claim_topic_expire_at=expire_at,
                claim_topic_deleted_at=None,
            )
        except Exception as exc:
            logging.warning('Failed to create claim topic for giveaway %s: %s', giveaway_id, exc)

    def process_due_claim_topics(self) -> None:
        current = now_s()
        for row in self.db.get_due_claim_topics(current):
            thread_id = row.get('claim_topic_thread_id')
            if not thread_id or row.get('claim_topic_deleted_at'):
                continue
            try:
                self.api.delete_forum_topic(row['claim_group_ref'], int(thread_id))
            except Exception as exc:
                logging.warning('Failed to delete claim topic for giveaway %s: %s', row['id'], exc)
            self.db.update_giveaway(row['id'], claim_topic_deleted_at=current)

    def parse_giveaway_id_arg(self, arg: str) -> int:
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
        }.get(status, '未知状态')

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
        mapping = {
            'live': 'live', 'ongoing': 'live', 'active': 'live',
            'ended': 'ended', 'finished': 'ended', 'done': 'ended',
            'all': 'all', 'all_giveaways': 'all',
        }
        if token in mapping:
            return mapping[token]
        raise ValueError('请输入 live、ended 或 all')

    def build_list_query_buttons(self) -> List[List[Dict[str, Any]]]:
        return [[
            {'text': '进行中', 'callback_data': 'list:live'},
            {'text': '已结束', 'callback_data': 'list:ended'},
            {'text': '全部', 'callback_data': 'list:all'},
        ]]

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
            creator_display = self.display_user_html(
                creator_username,
                creator.get('first_name') if creator else None,
                creator.get('last_name') if creator else None,
                int(giveaway['created_by'])
            )
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
        self.api.answer_callback_query(callback_query['id'], '已刷新列表', show_alert=False)
        try:
            self.send_giveaway_list(chat['id'], status_filter, message_id=message.get('message_id'))
        except Exception as exc:
            logging.warning('Failed to show giveaway list: %s', exc)
            try:
                self.api.send_message(chat['id'], f'获取抽奖列表失败：{exc}')
            except Exception:
                pass

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
            self.api.send_message(chat['id'], '只有发布群/频道的管理员可以操作这个抽奖。')
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
        if ok:
            self.api.send_message(chat['id'], f'已立即开奖 #{giveaway_id}。')
        else:
            self.api.send_message(chat['id'], '立即开奖失败，请稍后重试。')

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
            self.api.send_message(chat['id'], '这个抽奖已经结束或取消，不能修改领奖配置。')
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
            updates['claim_topic_hours'] = parse_optional_int(claim_hours_text, '领奖有效小时数', default=72, minimum=1)

        effective_claim_group = updates.get('claim_group_ref', giveaway.get('claim_group_ref'))
        if (('claim_group' in flags or 'claim_group_ref' in flags or 'claim_topic' in flags or 'claim_topic_name' in flags or 'claim_hours' in flags or 'claim_topic_hours' in flags) and not effective_claim_group):
            self.api.send_message(chat['id'], '请先指定 -claim_group，或保持已有领奖群设置不变。')
            return
        if effective_claim_group and not self.is_admin_of_chat(effective_claim_group, user['id']):
            self.api.send_message(chat['id'], '你不是领奖群/频道的管理员，无法修改这个配置。')
            return

        updates['claim_topic_enabled'] = 1 if (updates.get('claim_group_ref') or giveaway.get('claim_group_ref') or updates.get('claim_topic_name') or giveaway.get('claim_topic_name')) else 0
        self.db.update_giveaway(giveaway_id, **updates)
        self.db.log('claim_topic_updated', user['id'], giveaway_id, updates)

        if giveaway.get('claim_topic_thread_id') and giveaway.get('claim_group_ref') and updates.get('claim_topic_name'):
            try:
                self.api.edit_forum_topic(giveaway['claim_group_ref'], int(giveaway['claim_topic_thread_id']), name=updates['claim_topic_name'])
            except Exception as exc:
                logging.warning('Failed to rename claim topic for giveaway %s: %s', giveaway_id, exc)

        self.api.send_message(chat['id'], f'已更新抽奖 #{giveaway_id} 的领奖话题配置。')

    def edit_giveaway_command(self, message: Dict[str, Any], arg: str) -> None:
        user = message['from']
        chat = message['chat']
        if not arg.strip():
            self.api.send_message(chat['id'], self.new_giveaway_help_text())
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
            self.api.send_message(chat['id'], '这个抽奖已经结束或取消，不能修改。')
            return
        if not self.is_admin_of_chat(giveaway['publish_chat_ref'], user['id']):
            self.api.send_message(chat['id'], '只有发布群/频道的管理员可以修改这个抽奖。')
            return
        if not flags:
            self.api.send_message(chat['id'], self.giveaway_summary(giveaway_id))
            return

        updates: Dict[str, Any] = {}
        weight_map: Optional[Dict[int, int]] = None

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
            updates['publish_chat_ref'] = self.flag_value(flags, 'publish') or giveaway['publish_chat_ref']
        if self.flag_value(flags, 'draw_n') is not None:
            updates['draw_when_participants'] = parse_int(self.flag_value(flags, 'draw_n') or '', '自动开奖人数', minimum=1)

        start_time = giveaway['start_time']
        if self.flag_value(flags, 'start') is not None:
            start_token = self.flag_value(flags, 'start') or ''
            if start_token.lower() == 'now':
                start_time = now_s()
            else:
                start_time = parse_dt(start_token).isoformat(sep=' ')
            updates['start_time'] = start_time

        end_time = giveaway.get('end_time')
        if self.flag_value(flags, 'end') is not None:
            end_token = self.flag_value(flags, 'end') or ''
            if end_token.lower() in {'none', 'unlimited', '不限', '无限', 'no', 'off'}:
                end_time = None
            else:
                end_time = parse_dt(end_token).isoformat(sep=' ')
            updates['end_time'] = end_time
        elif self.flag_value(flags, 't') is not None:
            duration = parse_duration(self.flag_value(flags, 't') or '')
            if duration is None:
                end_time = None
            else:
                base_time = dt.datetime.fromisoformat(start_time)
                end_time = (base_time + duration).isoformat(sep=' ')
            updates['end_time'] = end_time

        if self.flag_value(flags, 'weight') is not None:
            weight_map = parse_weight_map(self.flag_value(flags, 'weight') or '', self.resolve_username_to_user_id)

        if self.flag_value(flags, 'claim_group') is not None or self.flag_value(flags, 'claim_group_ref') is not None:
            updates['claim_group_ref'] = self.flag_value(flags, 'claim_group', 'claim_group_ref') or None
        if self.flag_value(flags, 'claim_topic') is not None or self.flag_value(flags, 'claim_topic_name') is not None:
            updates['claim_topic_name'] = self.flag_value(flags, 'claim_topic', 'claim_topic_name') or None
        if self.flag_value(flags, 'claim_hours') is not None or self.flag_value(flags, 'claim_topic_hours') is not None:
            updates['claim_topic_hours'] = parse_optional_int(self.flag_value(flags, 'claim_hours', 'claim_topic_hours'), '领奖有效小时数', default=72, minimum=1)

        if updates.get('claim_group_ref') and not self.is_admin_of_chat(updates['claim_group_ref'], user['id']):
            self.api.send_message(chat['id'], '你不是领奖群/频道的管理员，无法把领奖话题放到这里。')
            return

        if 'draw_when_participants' in updates or 'end_time' in updates or 'start_time' in updates:
            draw_when = updates.get('draw_when_participants', giveaway.get('draw_when_participants'))
            current_end = updates.get('end_time', end_time)
            if draw_when and current_end:
                updates['auto_draw_mode'] = 'both'
            elif draw_when:
                updates['auto_draw_mode'] = 'participants'
            elif current_end:
                updates['auto_draw_mode'] = 'time'
            else:
                updates['auto_draw_mode'] = 'manual'

        self.db.update_giveaway(giveaway_id, **updates)

        if weight_map is not None:
            self.db.exec('DELETE FROM giveaway_weights WHERE giveaway_id=?', (giveaway_id,))
            for uid, weight in weight_map.items():
                self.db.set_giveaway_weight(giveaway_id, int(uid), int(weight), reason='edit_weight')

        if giveaway.get('announcement_sent') == 1 and giveaway.get('announcement_message_id') and (not updates.get('publish_chat_ref') or updates.get('publish_chat_ref') == giveaway.get('publish_chat_ref')):
            threading.Thread(target=self.refresh_announcement_count, args=(giveaway_id,), daemon=True).start()

        self.db.log('giveaway_updated', user['id'], giveaway_id, updates)
        self.api.send_message(chat['id'], f'已更新抽奖 #{giveaway_id}。')

    def export_participants(self, message: Dict[str, Any], arg: str) -> None:
        user = message['from']
        chat = message['chat']
        if not arg.strip():
            self.api.send_message(chat['id'], '用法：/export <id>，支持 1 或 #1 这种编号。')
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
            self.api.send_message(chat['id'], '只有发布群/频道的管理员可以导出名单。')
            return

        participants = self.db.get_participants(giveaway_id)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            'giveaway_id', 'title', 'telegram_user_id', 'username', 'first_name', 'last_name',
            'display_name', 'source', 'invited_by', 'joined_at', 'valid', 'invalid_reason'
        ])
        for row in participants:
            display_name = self.display_user(
                row.get('username'),
                row.get('first_name'),
                row.get('last_name'),
                int(row['telegram_user_id']),
            )
            writer.writerow([
                giveaway_id,
                giveaway.get('title', ''),
                row.get('telegram_user_id'),
                row.get('username') or '',
                row.get('first_name') or '',
                row.get('last_name') or '',
                display_name,
                row.get('source') or '',
                row.get('invited_by') or '',
                row.get('joined_at') or '',
                row.get('valid') if row.get('valid') is not None else 1,
                row.get('invalid_reason') or '',
            ])

        content = ('\ufeff' + buffer.getvalue()).encode('utf-8')
        filename = f'giveaway_{giveaway_id}_participants.csv'
        self.api.send_document(chat['id'], filename, content, caption=f'抽奖 #{giveaway_id} 参与名单导出完成。')

    def giveaway_summary(self, giveaway_id: int) -> str:
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            return '找不到这个抽奖。'
        prize = format_prize_items(jload(giveaway.get('prize_json'), [])) or giveaway.get('prize') or '奖品'
        entry_methods = method_text(
            jload(giveaway['entry_methods'], []),
            giveaway['entry_keyword'],
            giveaway['required_channel'],
            int(giveaway['invite_required_count'] or 0),
            int(giveaway['invite_weight_bonus'] or 0),
        )
        claim_group = giveaway.get('claim_group_ref') or '未设置'
        claim_topic = giveaway.get('claim_topic_name') or '未设置'
        claim_hours = giveaway.get('claim_topic_hours') or 72
        draw_mode = {
            'time': '按时间开奖',
            'participants': '按人数开奖',
            'both': '时间或人数满足即开奖',
            'manual': '手动开奖',
        }.get(giveaway.get('auto_draw_mode') or 'time', '按时间开奖')
        return (
            f'<b>抽奖 #{giveaway_id}</b>\n\n'
            f'标题：{esc(giveaway["title"])}\n'
            f'奖品：{esc(prize)}\n'
            f'中奖人数：{esc(giveaway["winner_count"])}\n'
            f'状态：{esc(self.build_giveaway_status_text(giveaway.get("status") or ""))}\n'
            f'开奖模式：{esc(draw_mode)}\n'
            f'开始时间：{esc(giveaway["start_time"])}\n'
            f'结束时间：{esc(giveaway["end_time"] or "不限")}\n'
            f'自动开奖人数：{esc(giveaway.get("draw_when_participants") or "未设置")}\n'
            f'已参与人数：{self.db.count_participants(giveaway_id)}\n'
            f'参与方式：{esc(entry_methods)}\n'
            f'参与条件：{esc(giveaway["participation_condition"])}\n'
            f'领奖群聊：{esc(claim_group)}\n'
            f'领奖话题：{esc(claim_topic)}\n'
            f'领奖有效时长：{esc(claim_hours)} 小时\n'
            f'公布位置：{esc(giveaway["publish_chat_ref"])}'
        )

    def build_result_announcement_text(self, giveaway: Dict[str, Any], winner_lines: List[str], claim_deadline: str, finished_at: str, result_hash: str, valid_count: int) -> str:
        prize_text = format_prize_items(jload(giveaway.get('prize_json'), [])) or giveaway.get('prize') or '奖品'
        lines = [
            '🎊 <b>抽奖结果公布！</b>',
            '',
            f'活动：{esc(giveaway["title"])}',
            f'奖品：{esc(prize_text)}',
            f'中奖人数：{esc(giveaway["winner_count"])}',
            f'有效参与人数：{valid_count}',
            f'开奖时间：{esc(finished_at)}',
            f'开奖结果哈希：<code>{esc(result_hash)}</code>',
            '',
            '中奖名单：',
        ]
        if winner_lines:
            lines.extend(f'• {line}' for line in winner_lines)
        else:
            lines.append('• 暂无符合条件的中奖者')
        lines.extend([
            '',
            f'领奖截止：{esc(claim_deadline)}',
            '点击下方按钮可查看开奖结果验证说明。',
        ])
        return '\n'.join(lines)

    def finalize_giveaway(self, giveaway_id: int, manual: bool = False) -> bool:
        with self.finalize_lock:
            if not self.db.start_finalization(giveaway_id):
                return False
            giveaway = self.db.get_giveaway(giveaway_id)
            if not giveaway or giveaway['status'] in {'ended', 'cancelled'}:
                return False
            participants = self.db.get_participants(giveaway_id)
            valid: List[Any] = []
            for participant in participants:
                if participant['valid'] == 0:
                    continue
                if self.db.is_blacklisted(participant['telegram_user_id']):
                    self.db.set_participant_validity(participant['id'], 0, '黑名单用户')
                    continue
                if giveaway.get('require_channel'):
                    ok, reason = self.check_required_channel(giveaway['required_channel'], participant['telegram_user_id'])
                    if not ok:
                        self.db.set_participant_validity(participant['id'], 0, reason)
                        continue
                valid.append(participant)
            weighted_pool = self.build_weighted_pool(giveaway, valid)
            total_weight = sum(item['weight'] for item in weighted_pool)
            winner_count = min(int(giveaway['winner_count']), len(valid))
            seed_material = jdump({
                'giveaway_id': giveaway_id,
                'seed_hash': giveaway.get('seed_hash'),
                'participants': sorted(int(p['telegram_user_id']) for p in valid),
                'weighted_pool': [(int(item['participant']['telegram_user_id']), int(item['weight'])) for item in weighted_pool],
                'winner_count': winner_count,
                'total_weight': total_weight,
            })
            seed_value = int(hashlib.sha256(seed_material.encode('utf-8')).hexdigest(), 16)
            rng = random.Random(seed_value)
            winners = self.weighted_sample_without_replacement(weighted_pool, winner_count, rng) if winner_count > 0 else []
            winner_lines: List[str] = []
            winner_payload: List[Dict[str, Any]] = []
            finished_at = now_s()
            for winner in winners:
                participant = winner['participant']
                telegram_user_id = int(participant['telegram_user_id'])
                display_name = self.display_user_html(participant.get('username'), participant.get('first_name'), participant.get('last_name'), telegram_user_id)
                winner_weight = int(winner['weight'])
                winner_payload.append({
                    'telegram_user_id': telegram_user_id,
                    'username': participant.get('username'),
                    'first_name': participant.get('first_name'),
                    'last_name': participant.get('last_name'),
                    'display_name': display_name,
                    'weight': winner_weight,
                    'winner_time': finished_at,
                })
                winner_lines.append(f'{display_name} | 权重 {winner_weight} | 开奖时间 {finished_at}')
            claim_hours = int(giveaway.get('claim_topic_hours') or 72)
            claim_deadline = (dt.datetime.fromisoformat(finished_at) + dt.timedelta(hours=claim_hours)).isoformat(sep=' ')
            result_payload = {
                'giveaway_id': giveaway_id,
                'title': giveaway['title'],
                'finished_at': finished_at,
                'participants': len(valid),
                'winners': winner_payload,
                'seed_hash': giveaway.get('seed_hash'),
                'winner_count': winner_count,
                'total_weight': total_weight,
            }
            result_hash = stable_result_hash(result_payload)
            text = self.build_result_announcement_text(giveaway, winner_lines, claim_deadline, finished_at, result_hash, len(valid))
            result = self.api.send_message(giveaway['publish_chat_ref'], text, reply_markup={'inline_keyboard': [[{'text': '验证开奖结果', 'callback_data': f'verify:{giveaway_id}'}]]}, message_thread_id=int(giveaway['publish_chat_thread_id']) if giveaway.get('publish_chat_thread_id') else None)
            try:
                self.api.pin_chat_message(giveaway['publish_chat_ref'], int(result.get('message_id')))
            except Exception as exc:
                logging.warning('Failed to pin result for giveaway %s: %s', giveaway_id, exc)
            self.db.finish_finalization(giveaway_id, status='ended', ended_at=finished_at, result_message_id=result.get('message_id'), winner_json=jdump(winner_payload), result_hash=result_hash, result_payload_json=jdump(result_payload), claim_deadline=claim_deadline)
            self.db.log('giveaway_ended', giveaway['created_by'], giveaway_id, {'manual': manual, 'winner_count': len(winners), 'total_weight': total_weight, 'result_hash': result_hash})
            if giveaway.get('claim_topic_enabled') and giveaway.get('claim_group_ref'):
                self.create_claim_topic(giveaway_id)
            self.notify_winners(giveaway_id)
            return True

    def build_giveaway_status_text(self, status: str) -> str:
        return {
            'scheduled': '未开始',
            'live': '进行中',
            'finalizing': '开奖中',
            'ended': '已结束',
            'cancelled': '已取消',
        }.get(status, '未知状态')

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
        raise ValueError('请输入 live、ended 或 all')

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
            lines.extend([f'<b>#{giveaway["id"]} {esc(giveaway["title"])} </b>', f'状态：{esc(status_text)}', f'奖品：{esc(prize)}', f'已参与人数：{participant_count}', f'时间：{esc(giveaway["start_time"])} -> {esc(giveaway["end_time"] or "不限")}', f'创建者：{creator_display}', ''])
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
        self.api.answer_callback_query(callback_query['id'], '已刷新列表', show_alert=False)
        self.send_giveaway_list(chat['id'], status_filter, message_id=message.get('message_id'))

    def close_giveaway(self, message: Dict[str, Any], arg: str, cancel: bool = False) -> None:
        user = message['from']
        chat = message['chat']
        if not arg.strip():
            usage = '/cancel_giveaway <id>' if cancel else '/end_giveaway <id>'
            self.api.send_message(chat['id'], f'用法：{usage}，支持 1 或 #1 这种编号。')
            return
        giveaway_id = self.parse_giveaway_id_arg(arg)
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            self.api.send_message(chat['id'], '找不到这个抽奖。')
            return
        if not self.is_admin_of_chat(giveaway['publish_chat_ref'], user['id']):
            self.api.send_message(chat['id'], '只有发布群/频道的管理员可以操作这个抽奖。')
            return
        if giveaway['status'] in {'ended', 'cancelled'}:
            self.api.send_message(chat['id'], '这个抽奖已经结束或取消，不能再次操作。')
            return
        if cancel:
            self.db.update_giveaway(giveaway_id, status='cancelled', cancelled_at=now_s(), cancellation_reason='manual_cancel')
            if giveaway.get('claim_topic_thread_id') and giveaway.get('claim_group_ref'):
                try:
                    self.api.delete_forum_topic(giveaway['claim_group_ref'], int(giveaway['claim_topic_thread_id']))
                except Exception:
                    pass
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
        tokens = shlex.split(arg)
        giveaway_id = self.parse_giveaway_id_arg(tokens[0])
        flags = self.parse_command_flags(' '.join(tokens[1:])) if len(tokens) > 1 else {}
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            self.api.send_message(chat['id'], '找不到这个抽奖。')
            return
        if giveaway['status'] in {'ended', 'cancelled'}:
            self.api.send_message(chat['id'], '这个抽奖已经结束或取消，不能修改领奖配置。')
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
            updates['claim_topic_hours'] = parse_optional_int(claim_hours_text, '领奖有效小时数', default=72, minimum=1)
        effective_claim_group = updates.get('claim_group_ref', giveaway.get('claim_group_ref'))
        if (('claim_group' in flags or 'claim_group_ref' in flags or 'claim_topic' in flags or 'claim_topic_name' in flags or 'claim_hours' in flags or 'claim_topic_hours' in flags) and not effective_claim_group):
            self.api.send_message(chat['id'], '请先指定 -claim_group，或保持已有领奖群设置不变。')
            return
        if effective_claim_group and not self.is_admin_of_chat(effective_claim_group, user['id']):
            self.api.send_message(chat['id'], '你不是领奖群/频道的管理员，无法修改这个配置。')
            return
        updates['claim_topic_enabled'] = 1 if (updates.get('claim_group_ref') or giveaway.get('claim_group_ref') or updates.get('claim_topic_name') or giveaway.get('claim_topic_name')) else 0
        self.db.update_giveaway(giveaway_id, **updates)
        self.db.log('claim_topic_updated', user['id'], giveaway_id, updates)
        if giveaway.get('claim_topic_thread_id') and giveaway.get('claim_group_ref') and updates.get('claim_topic_name'):
            try:
                self.api.edit_forum_topic(giveaway['claim_group_ref'], int(giveaway['claim_topic_thread_id']), name=updates['claim_topic_name'])
            except Exception:
                pass
        self.api.send_message(chat['id'], f'已更新抽奖 #{giveaway_id} 的领奖话题配置。')

    def edit_giveaway_command(self, message: Dict[str, Any], arg: str) -> None:
        user = message['from']
        chat = message['chat']
        if not arg.strip():
            self.api.send_message(chat['id'], self.new_giveaway_help_text())
            return
        tokens = shlex.split(arg)
        giveaway_id = self.parse_giveaway_id_arg(tokens[0])
        flags = self.parse_command_flags(' '.join(tokens[1:])) if len(tokens) > 1 else {}
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            self.api.send_message(chat['id'], '找不到这个抽奖。')
            return
        if giveaway['status'] in {'ended', 'cancelled'}:
            self.api.send_message(chat['id'], '这个抽奖已经结束或取消，不能修改。')
            return
        if not self.is_admin_of_chat(giveaway['publish_chat_ref'], user['id']):
            self.api.send_message(chat['id'], '只有发布群/频道的管理员可以修改这个抽奖。')
            return
        if not flags:
            self.api.send_message(chat['id'], self.giveaway_summary(giveaway_id))
            return
        updates: Dict[str, Any] = {}
        weight_map: Optional[Dict[int, int]] = None
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
            updates['publish_chat_ref'] = self.flag_value(flags, 'publish') or giveaway['publish_chat_ref']
        if self.flag_value(flags, 'draw_n') is not None:
            updates['draw_when_participants'] = parse_int(self.flag_value(flags, 'draw_n') or '', '自动开奖人数', minimum=1)
        start_time = giveaway['start_time']
        if self.flag_value(flags, 'start') is not None:
            start_token = self.flag_value(flags, 'start') or ''
            start_time = now_s() if start_token.lower() == 'now' else parse_dt(start_token).isoformat(sep=' ')
            updates['start_time'] = start_time
        end_time = giveaway.get('end_time')
        if self.flag_value(flags, 'end') is not None:
            end_token = self.flag_value(flags, 'end') or ''
            end_time = None if end_token.lower() in {'none', 'unlimited', '不限', '无限', 'no', 'off'} else parse_dt(end_token).isoformat(sep=' ')
            updates['end_time'] = end_time
        elif self.flag_value(flags, 't') is not None:
            duration = parse_duration(self.flag_value(flags, 't') or '')
            end_time = None if duration is None else (dt.datetime.fromisoformat(start_time) + duration).isoformat(sep=' ')
            updates['end_time'] = end_time
        if self.flag_value(flags, 'weight') is not None:
            weight_map = parse_weight_map(self.flag_value(flags, 'weight') or '', self.resolve_username_to_user_id)
        if self.flag_value(flags, 'claim_group') is not None or self.flag_value(flags, 'claim_group_ref') is not None:
            updates['claim_group_ref'] = self.flag_value(flags, 'claim_group', 'claim_group_ref') or None
        if self.flag_value(flags, 'claim_topic') is not None or self.flag_value(flags, 'claim_topic_name') is not None:
            updates['claim_topic_name'] = self.flag_value(flags, 'claim_topic', 'claim_topic_name') or None
        if self.flag_value(flags, 'claim_hours') is not None or self.flag_value(flags, 'claim_topic_hours') is not None:
            updates['claim_topic_hours'] = parse_optional_int(self.flag_value(flags, 'claim_hours', 'claim_topic_hours'), '领奖有效小时数', default=72, minimum=1)
        if updates.get('claim_group_ref') and not self.is_admin_of_chat(updates['claim_group_ref'], user['id']):
            self.api.send_message(chat['id'], '你不是领奖群/频道的管理员，无法把领奖话题放到这里。')
            return
        if 'draw_when_participants' in updates or 'end_time' in updates or 'start_time' in updates:
            draw_when = updates.get('draw_when_participants', giveaway.get('draw_when_participants'))
            current_end = updates.get('end_time', end_time)
            if draw_when and current_end:
                updates['auto_draw_mode'] = 'both'
            elif draw_when:
                updates['auto_draw_mode'] = 'participants'
            elif current_end:
                updates['auto_draw_mode'] = 'time'
            else:
                updates['auto_draw_mode'] = 'manual'
        self.db.update_giveaway(giveaway_id, **updates)
        if weight_map is not None:
            self.db.exec('DELETE FROM giveaway_weights WHERE giveaway_id=?', (giveaway_id,))
            for uid, weight in weight_map.items():
                self.db.set_giveaway_weight(giveaway_id, int(uid), int(weight), reason='edit_weight')
        if giveaway.get('announcement_sent') == 1 and giveaway.get('announcement_message_id') and (not updates.get('publish_chat_ref') or updates.get('publish_chat_ref') == giveaway.get('publish_chat_ref')):
            threading.Thread(target=self.refresh_announcement_count, args=(giveaway_id,), daemon=True).start()
        self.db.log('giveaway_updated', user['id'], giveaway_id, updates)
        self.api.send_message(chat['id'], f'已更新抽奖 #{giveaway_id}。')

    def export_participants(self, message: Dict[str, Any], arg: str) -> None:
        user = message['from']
        chat = message['chat']
        if not arg.strip():
            self.api.send_message(chat['id'], '用法：/export <id>，支持 1 或 #1 这种编号。')
            return
        giveaway_id = self.parse_giveaway_id_arg(arg)
        giveaway = self.db.get_giveaway(giveaway_id)
        if not giveaway:
            self.api.send_message(chat['id'], '找不到这个抽奖。')
            return
        if not self.is_admin_of_chat(giveaway['publish_chat_ref'], user['id']):
            self.api.send_message(chat['id'], '只有发布群/频道的管理员可以导出名单。')
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
        entry_methods = method_text(jload(giveaway['entry_methods'], []), giveaway['entry_keyword'], giveaway['required_channel'], int(giveaway['invite_required_count'] or 0), int(giveaway['invite_weight_bonus'] or 0))
        claim_group = giveaway.get('claim_group_ref') or '未设置'
        claim_topic = giveaway.get('claim_topic_name') or '未设置'
        claim_hours = giveaway.get('claim_topic_hours') or 72
        draw_mode = {'time': '按时间开奖', 'participants': '按人数开奖', 'both': '时间或人数满足即开奖', 'manual': '手动开奖'}.get(giveaway.get('auto_draw_mode') or 'time', '按时间开奖')
        return f'<b>抽奖 #{giveaway_id}</b>\n\n标题：{esc(giveaway["title"])}\n奖品：{esc(prize)}\n中奖人数：{esc(giveaway["winner_count"])}\n状态：{esc(self.build_giveaway_status_text(giveaway.get("status") or ""))}\n开奖模式：{esc(draw_mode)}\n开始时间：{esc(giveaway["start_time"])}\n结束时间：{esc(giveaway["end_time"] or "不限")}\n自动开奖人数：{esc(giveaway.get("draw_when_participants") or "未设置")}\n已参与人数：{self.db.count_participants(giveaway_id)}\n参与方式：{esc(entry_methods)}\n参与条件：{esc(giveaway["participation_condition"])}\n领奖群聊：{esc(claim_group)}\n领奖话题：{esc(claim_topic)}\n领奖有效时长：{esc(claim_hours)} 小时\n公布位置：{esc(giveaway["publish_chat_ref"])}'

    cls.build_result_announcement_text = build_result_announcement_text
    cls.finalize_giveaway = finalize_giveaway
    cls.notify_winners = notify_winners
    cls.create_claim_topic = create_claim_topic
    cls.process_due_claim_topics = process_due_claim_topics
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
