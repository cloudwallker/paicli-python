import re
from dataclasses import dataclass


def valid_message_id(value: str) -> bool:
    return re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,31}", value) is not None


@dataclass(frozen=True)
class Message:
    message_id: str
    body: str


class MessageIndex:
    def __init__(self):
        self._messages: dict[str, Message] = {}

    def add(self, message_id: str, body: str) -> Message:
        if not valid_message_id(message_id):
            raise ValueError("invalid message identifier")
        if message_id in self._messages:
            raise ValueError("duplicate message identifier")
        message = Message(message_id, body)
        self._messages[message_id] = message
        return message

    def get_body(self, message_id: str) -> str:
        return self._messages[message_id].body

    def ids(self) -> tuple[str, ...]:
        return tuple(self._messages)
