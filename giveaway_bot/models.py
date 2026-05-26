from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class GiveawayDraft:
    title: str = ''
    prize: str = ''
    winner_count: Optional[int] = None
    participation_condition: str = ''
    start_time: str = ''
    end_time: str = ''
    entry_methods: List[str] = field(default_factory=list)
    entry_keyword: Optional[str] = None
    require_channel: bool = False
    required_channel: Optional[str] = None
    invite_required_count: int = 0
    invite_weight_bonus: int = 0
    publish_chat_ref: str = ''
    publish_chat_thread_id: Optional[int] = None
    special_weights: Dict[int, int] = field(default_factory=dict)
    claim_topic_enabled: bool = False
    claim_group_ref: Optional[str] = None
    claim_topic_name: Optional[str] = None
    claim_topic_hours: int = 72
    auto_draw_mode: str = 'time'
    draw_when_participants: Optional[int] = None
    claim_deadline_hours: int = 72
