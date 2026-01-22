from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Dict, Optional, Set, Tuple

import discord

from src.services.geo_learning_bank import GeoLearningBank, Question, normalize_text


@dataclass
class ChannelSession:
    qtype: str  # "flag" | "script"
    question: Question
    asked_count: int = 0
    seen_answers: Set[str] = field(default_factory=set)
    scores: Dict[int, Tuple[int, int]] = field(default_factory=dict)


class GeoLearningGame:
    """
    Simple geo learning game:
    - /geo-learning start flags|scripts
    - Users answer via normal messages
    - Bot replies on correct and posts next prompt
    """

    key = "geo_learning"

    def __init__(
        self,
        bank: GeoLearningBank,
        allowed_channel_ids: Set[int],
    ) -> None:
        self._bank = bank
        self._allowed_channel_ids = allowed_channel_ids

        self._sessions: Dict[int, ChannelSession] = {}
        self._locks: Dict[int, asyncio.Lock] = {}

    def _lock_for(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()
        return self._locks[channel_id]

    def is_allowed_channel(self, channel_id: int) -> bool:
        return channel_id in self._allowed_channel_ids

    def has_session(self, channel_id: int) -> bool:
        return channel_id in self._sessions

    async def start(self, channel: discord.abc.Messageable, qtype: str) -> str:
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            raise ValueError("Channel has no id")

        if not self.is_allowed_channel(channel_id):
            raise ValueError("This channel is not allowed for geo-learning.")

        async with self._lock_for(channel_id):
            q = self._bank.get_random_question(qtype=qtype)
            sess = ChannelSession(qtype=qtype, question=q, asked_count=1)
            sess.seen_answers.add(self._seen_key(q))
            self._sessions[channel_id] = sess

        return q.prompt

    async def stop(self, channel_id: int) -> None:
        async with self._lock_for(channel_id):
            self._sessions.pop(channel_id, None)

    async def skip(self, channel_id: int) -> Optional[str]:
        async with self._lock_for(channel_id):
            sess = self._sessions.get(channel_id)
            if not sess:
                return None
            sess.question = self._bank.get_random_question(
                qtype=sess.qtype,
                exclude_answers=sess.seen_answers,
            )
            sess.asked_count += 1
            sess.seen_answers.add(self._seen_key(sess.question))
            return sess.question.prompt

    async def score_text(self, channel_id: int) -> Optional[str]:
        async with self._lock_for(channel_id):
            sess = self._sessions.get(channel_id)
            if not sess:
                return None
            if not sess.scores:
                return "No scores yet. Answer a question to get started."

            rows = []
            for uid, (correct, total) in sess.scores.items():
                acc = (correct / total) if total else 0.0
                rows.append((uid, correct, total, acc))
            rows.sort(key=lambda r: (r[1], r[3], r[2]), reverse=True)

            lines = ["🏁 **Geo-Learning Scoreboard**"]
            for i, (uid, correct, total, acc) in enumerate(rows[:10], start=1):
                lines.append(f"{i}. <@{uid}> — **{correct}/{total}** ({acc:.0%})")
            return "\n".join(lines)

    async def handle_discord_message(self, message: discord.Message) -> bool:
        """
        Let GameRegistry route messages here.
        Returns True if consumed.
        """
        if message.author.bot:
            return False

        channel_id = message.channel.id
        if not self.is_allowed_channel(channel_id):
            return False

        sess = self._sessions.get(channel_id)
        if not sess:
            return False

        content = normalize_text(message.content)
        if not content:
            return False

        async with self._lock_for(channel_id):
            sess = self._sessions.get(channel_id)
            if not sess:
                return False

            uid = message.author.id
            correct, total = sess.scores.get(uid, (0, 0))
            total += 1

            is_correct = content in sess.question.accepted

            if is_correct:
                correct += 1
                sess.scores[uid] = (correct, total)

                next_q = self._bank.get_random_question(
                    qtype=sess.qtype,
                    exclude_answers=sess.seen_answers,
                )
                sess.question = next_q
                sess.asked_count += 1
                sess.seen_answers.add(self._seen_key(next_q))

                await message.reply(
                    f"✅ Correct, **{message.author.display_name}**!\n\n{next_q.prompt}",
                    mention_author=False,
                )
                return True

            sess.scores[uid] = (correct, total)
            try:
                await message.add_reaction("❌")
            except Exception:
                pass
            return False

    @staticmethod
    def _seen_key(q: Question) -> str:
        if q.qtype == "flag":
            return normalize_text(q.answer)
        return normalize_text(q.prompt)
