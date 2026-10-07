from __future__ import annotations

from typing import Any, Dict, List

from core.user_modes.base_strategy import BaseUserModeStrategy


class MusicUserModeStrategy(BaseUserModeStrategy):
    mode_name = "music"
    api_method_name = "get_user_music"

    async def collect_items(self, sec_uid: str, user_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        return await self._collect_metadata_aweme(
            sec_uid,
            id_field="music_id",
            id_aliases=["musicId"],
            fetch_method_name="get_music_aweme",
        )
