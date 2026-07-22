import json
from core.scenario import ScenarioEngine
from core.tts import TTSService


class MessageRouter:
    def __init__(self):
        self.scenario_engine = ScenarioEngine()
        self.tts = TTSService()
        self._ws_broadcaster = None

    def set_ws_broadcaster(self, broadcaster):
        self._ws_broadcaster = broadcaster

    async def handle_scenario_check(self, system_status: dict, window: dict | None = None):
        result = self.scenario_engine.detect(system_status, window)
        if result is None:
            return

        audio = await self.tts.synthesize(result["text"])

        payload = {
            "type": "speak",
            "text": result["text"],
            "expression": result["expression"],
            "motion": result["motion"],
            "audioUrl": audio["audio_url"] if audio else None,
        }
        await self._broadcast(payload)

    async def handle_chat(self, message: dict):
        payload = {
            "type": "speak",
            "text": "主人说得有道理~",
            "expression": "happy",
            "motion": "wave",
        }
        await self._broadcast(payload)
        return payload

    async def handle_interaction(self, action: dict):
        payload = {
            "type": "action",
            "expression": "surprised",
            "motion": "tap_body",
        }
        await self._broadcast(payload)
        return payload

    async def handle_client_message(self, message: dict):
        message_type = message.get("type")
        if message_type == "interaction":
            return await self.handle_interaction(message)
        if message_type == "chat":
            return await self.handle_chat(message)
        raise ValueError(f"unsupported message type: {message_type!r}")

    async def _broadcast(self, payload: dict):
        if self._ws_broadcaster:
            await self._ws_broadcaster(json.dumps(payload))
