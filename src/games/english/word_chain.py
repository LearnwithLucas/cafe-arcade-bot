from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import discord

from src.db.repo.games_repo import GamesRepository
from src.db.repo.users_repo import UsersRepository
from src.services.cooldowns import Cooldowns
from src.services.economy_service import EconomyService
from src.services.leaderboard_publisher import LeaderboardPublisher
from src.services.rewards_service import RewardsService, RewardKey
from src.services.wordlist import WordList
from src.utils.text import is_valid_word_shape, normalize_word


class WordChainGame:
    key = "word_chain"

    def __init__(
        self,
        *,
        games_repo: GamesRepository,
        users_repo: UsersRepository,
        economy: EconomyService,
        rewards: RewardsService,
        cooldowns: Cooldowns,
        wordlist: WordList,
        allowed_channel_ids: set[int] | None = None,
        leaderboard_publisher: LeaderboardPublisher | None = None,
    ) -> None:
        self._games_repo = games_repo
        self._users_repo = users_repo
        self._economy = economy
        self._rewards = rewards
        self._cooldowns = cooldowns
        self._wordlist = wordlist
        self._allowed_channel_ids = allowed_channel_ids
        self._publisher = leaderboard_publisher

        # Throttle status embed edits (per channel)
        self._last_status_edit_at: dict[int, float] = {}
        self._edit_lock: dict[int, asyncio.Lock] = {}

        # IMPORTANT: serialize message handling per channel to prevent lost updates
        self._channel_lock: dict[int, asyncio.Lock] = {}

    def _new_state(self) -> dict[str, Any]:
        return {
            "last_word": None,
            "used_words": [],
            "last_player_id": None,
            "turn": 0,
            "streaks": {},  # discord_user_id(str) -> count
            "status_message_id": None,
            "last_status": "Round started. Waiting for the first word…",
            "last_milestone": 0,
        }

    def _get_channel_lock(self, channel_id: int) -> asyncio.Lock:
        if channel_id not in self._channel_lock:
            self._channel_lock[channel_id] = asyncio.Lock()
        return self._channel_lock[channel_id]

    async def start_in_channel(self, *, channel_id: int, status_message_id: int) -> str:
        # End any existing active word chain in this channel (hard reset)
        await self._games_repo.end_active_in_location(
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
            status="ended",
        )

        session_id = str(uuid.uuid4())
        state = self._new_state()
        state["status_message_id"] = int(status_message_id)

        await self._games_repo.upsert_active_session(
            session_id=session_id,
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
            state=state,
        )

        # reset throttles/locks
        self._last_status_edit_at[channel_id] = 0.0
        self._edit_lock.setdefault(channel_id, asyncio.Lock())
        self._channel_lock.setdefault(channel_id, asyncio.Lock())

        return session_id

    async def stop_in_channel(self, *, channel_id: int) -> int:
        return await self._games_repo.end_active_in_location(
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
            status="ended",
        )

    async def get_state(self, *, channel_id: int) -> dict[str, Any] | None:
        sess = await self._games_repo.get_active_session(
            platform="discord",
            location_id=str(channel_id),
            thread_id=None,
            game_key=self.key,
        )
        if not sess:
            return None
        return json.loads(sess.state_json)

    def _build_status_embed(self, *, state: dict[str, Any], channel_id: int) -> discord.Embed:
        last_word = state.get("last_word") or "—"
        last_status = state.get("last_status") or ""
        turn = int(state.get("turn", 0))

        next_letter = None
        if state.get("last_word"):
            next_letter = str(state["last_word"])[-1]
        next_letter_text = next_letter.upper() if next_letter else "ANY"

        embed = discord.Embed(
            title="🔤 Word Chain — Live Round",
            description=(
                "**How to play**\n"
                "• Send a single English word (letters only)\n"
                "• Next word must start with the **last letter** of the previous accepted word\n"
                "• No repeats\n"
                "• Ends immediately on a mistake\n\n"
                "**Write your first word:**"
            ),
        )
        embed.add_field(name="Last accepted word", value=f"**{last_word}**", inline=True)
        embed.add_field(name="Next letter", value=f"**{next_letter_text}**", inline=True)
        embed.add_field(name="Turns", value=str(turn), inline=True)
        embed.add_field(name="Status", value=last_status, inline=False)
        embed.set_footer(text=f"Channel: {channel_id}")
        return embed

    async def _edit_status_embed(
        self,
        *,
        channel: discord.abc.Messageable,
        channel_id: int,
        state: dict[str, Any],
        force: bool = False,
    ) -> None:
        msg_id = state.get("status_message_id")
        if not msg_id:
            return
        if not hasattr(channel, "fetch_message"):
            return

        now = time.time()
        last = self._last_status_edit_at.get(channel_id, 0.0)
        if not force and (now - last) < 1.2:
            return

        lock = self._edit_lock.setdefault(channel_id, asyncio.Lock())
        async with lock:
            now2 = time.time()
            last2 = self._last_status_edit_at.get(channel_id, 0.0)
            if not force and (now2 - last2) < 1.2:
                return

            try:
                status_msg = await channel.fetch_message(int(msg_id))  # type: ignore[attr-defined]
                embed = self._build_status_embed(state=state, channel_id=channel_id)
                await status_msg.edit(embed=embed)
                self._last_status_edit_at[channel_id] = time.time()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                return

    async def _maybe_announce_milestone(
        self,
        *,
        channel: discord.abc.Messageable,
        channel_id: int,
        state: dict[str, Any],
        actor_id: int,
    ) -> None:
        """
        Every 10 turns:
        - announce streak
        - force-update the status embed (bypasses throttle) so it stays accurate
        """
        turn = int(state.get("turn", 0))
        if turn <= 0 or turn % 10 != 0:
            return

        last_milestone = int(state.get("last_milestone", 0))
        if turn <= last_milestone:
            return

        state["last_milestone"] = turn

        # Force update embed at milestones (ignores the 1.2s edit throttle)
        await self._edit_status_embed(channel=channel, channel_id=channel_id, state=state, force=True)

        try:
            await channel.send(f"🎉 You’re on a **{turn}-word** streak! Great job <@{actor_id}>!")
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def handle_discord_message(self, message: discord.Message) -> bool:
        if not isinstance(message.channel, (discord.TextChannel, discord.Thread)):
            return False

        if self._allowed_channel_ids is not None and message.channel.id not in self._allowed_channel_ids:
            return False

        channel_id = message.channel.id

        word = normalize_word(message.content)
        if not is_valid_word_shape(word, min_len=2):
            return False

        # IMPORTANT: serialize updates in this channel
        async with self._get_channel_lock(channel_id):
            sess = await self._games_repo.get_active_session(
                platform="discord",
                location_id=str(channel_id),
                thread_id=None,
                game_key=self.key,
            )
            if not sess:
                return False

            state: dict[str, Any] = json.loads(sess.state_json)
            last_word: str | None = state.get("last_word")
            used_words: list[str] = state.get("used_words", [])
            last_player_id: str | None = state.get("last_player_id")
            streaks: dict[str, int] = state.get("streaks", {}) or {}

            author_id_str = str(message.author.id)
            unique_players = len(streaks.keys())
            required_letter: str | None = (last_word[-1] if last_word else None)

            # Failure checks (end round)
            if not self._wordlist.is_word(word):
                await self._finish_round(
                    sess_id=sess.id,
                    channel=message.channel,
                    channel_id=channel_id,
                    ended_by_discord_id=message.author.id,
                    reason="Not a valid dictionary word",
                    state=state,
                )
                return True

            if unique_players >= 2 and last_player_id == author_id_str:
                await self._finish_round(
                    sess_id=sess.id,
                    channel=message.channel,
                    channel_id=channel_id,
                    ended_by_discord_id=message.author.id,
                    reason="Played twice in a row",
                    state=state,
                )
                return True

            if required_letter and word[0] != required_letter:
                await self._finish_round(
                    sess_id=sess.id,
                    channel=message.channel,
                    channel_id=channel_id,
                    ended_by_discord_id=message.author.id,
                    reason=f"Wrong starting letter (needed **{required_letter}**)",
                    state=state,
                )
                return True

            if word in used_words:
                await self._finish_round(
                    sess_id=sess.id,
                    channel=message.channel,
                    channel_id=channel_id,
                    ended_by_discord_id=message.author.id,
                    reason="Repeated a used word",
                    state=state,
                )
                return True

            # Valid play
            used_words.append(word)
            state["last_word"] = word
            state["used_words"] = used_words[-1500:]
            state["last_player_id"] = author_id_str
            state["turn"] = int(state.get("turn", 0)) + 1

            streaks[author_id_str] = int(streaks.get(author_id_str, 0)) + 1
            state["streaks"] = streaks
            state["last_status"] = f"✅ Accepted: **{word}** (by <@{message.author.id}>)"

            await self._games_repo.upsert_active_session(
                session_id=sess.id,
                platform="discord",
                location_id=str(channel_id),
                thread_id=None,
                game_key=self.key,
                state=state,
            )

            # edits can be throttled; gameplay state is already saved
            await self._edit_status_embed(channel=message.channel, channel_id=channel_id, state=state, force=False)
            await self._maybe_announce_milestone(
                channel=message.channel,
                channel_id=channel_id,
                state=state,
                actor_id=message.author.id,
            )

            # persist milestone update if it changed
            await self._games_repo.upsert_active_session(
                session_id=sess.id,
                platform="discord",
                location_id=str(channel_id),
                thread_id=None,
                game_key=self.key,
                state=state,
            )

            return True

    async def _finish_round(
        self,
        *,
        sess_id: str,
        channel: discord.abc.Messageable,
        channel_id: int,
        ended_by_discord_id: int,
        reason: str,
        state: dict[str, Any],
    ) -> None:
        await self._games_repo.end_session(session_id=sess_id, status="ended")

        streaks: dict[str, int] = state.get("streaks", {}) or {}
        total_turns = int(state.get("turn", 0))
        last_word = state.get("last_word")

        # Always update status embed on end (force)
        state["last_status"] = f"🛑 Round ended — {reason}"
        await self._edit_status_embed(channel=channel, channel_id=channel_id, state=state, force=True)

        # Centralized payout multiplier: beans per streak unit (e.g. per accepted word)
        per_unit = int(self._rewards.amount(RewardKey.WORD_CHAIN_ROUND_PAYOUT))

        streak_lines: list[str] = []
        if streaks:
            sorted_rows = sorted(streaks.items(), key=lambda kv: kv[1], reverse=True)
            for discord_id_str, streak in sorted_rows:
                streak = int(streak)
                if streak <= 0:
                    continue

                discord_id = int(discord_id_str)
                beans = max(0, per_unit * streak)

                try:
                    user = await self._users_repo.get_or_create_discord_user(
                        discord_user_id=discord_id,
                        display_name=None,
                    )

                    if beans > 0:
                        await self._economy.award_beans_discord(
                            user_id=discord_id,
                            amount=beans,
                            reason="Word Chain round payout",
                            game_key=self.key,
                            display_name=None,
                            metadata=None,
                        )

                    context = {
                        "ended_by": ended_by_discord_id,
                        "reason": reason,
                        "turns": total_turns,
                        "last_word": last_word,
                        "streak": streak,
                        "per_unit": per_unit,
                    }
                    await self._games_repo.record_game_result(
                        user_id=user.id,
                        game_key=self.key,
                        score=beans,
                        beans_earned=beans,
                        context_json=json.dumps(context, ensure_ascii=False),
                    )

                    streak_lines.append(f"<@{discord_id}> — **{beans}** beans (streak {streak})")
                except Exception as e:
                    streak_lines.append(f"<@{discord_id}> — **{beans}** (payout failed)")
                    print(f"[WordChain payout error] discord_id={discord_id} err={e!r}")

        embed = discord.Embed(
            title="🛑 Word Chain Over!",
            description=f"Ended by <@{ended_by_discord_id}> — {reason}",
        )
        embed.add_field(name="Last word", value=f"**{last_word or '—'}**", inline=True)
        embed.add_field(name="Turns", value=str(total_turns), inline=True)
        embed.add_field(
            name="🔥 Streaks (beans awarded)",
            value="\n".join(streak_lines) if streak_lines else "(none)",
            inline=False,
        )
        embed.set_footer(text="Press Play again to instantly start a new round.")

        await channel.send(embed=embed, view=PlayAgainView(game=self, channel_id=channel_id))

        if self._publisher:
            self._publisher.schedule_refresh()


class PlayAgainView(discord.ui.View):
    def __init__(self, *, game: WordChainGame, channel_id: int, timeout: float = 900.0) -> None:
        super().__init__(timeout=timeout)
        self._game = game
        self._channel_id = channel_id

    @discord.ui.button(label="Play again", style=discord.ButtonStyle.success, emoji="🔁")
    async def play_again(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        embed = discord.Embed(
            title="🔤 Word Chain — Live Round",
            description="**Write your first word:**",
        )
        await interaction.response.send_message(embed=embed)
        status_msg = await interaction.original_response()

        await self._game.start_in_channel(channel_id=self._channel_id, status_message_id=status_msg.id)
