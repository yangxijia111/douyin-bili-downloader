from __future__ import annotations

from typing import Any, Dict, List

from core.user_modes.base_strategy import BaseUserModeStrategy


class MixUserModeStrategy(BaseUserModeStrategy):
    mode_name = "mix"
    api_method_name = "get_user_mix"

    async def collect_items(self, sec_uid: str, user_info: Dict[str, Any]) -> List[Dict[str, Any]]:
        return await self._collect_metadata_aweme(
            sec_uid,
            id_field="mix_id",
            id_aliases=["mixId"],
            fetch_method_name="get_mix_aweme",
        )
