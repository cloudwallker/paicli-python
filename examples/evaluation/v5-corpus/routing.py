from collections.abc import Callable


class WeightedRouter:
    def __init__(self, weights: dict[str, int]):
        if not weights or any(type(weight) is not int or weight <= 0 for weight in weights.values()):
            raise ValueError("positive integer weights are required")
        self._schedule = tuple(
            endpoint for endpoint, weight in weights.items() for _ in range(weight)
        )
        self._position = 0

    def next_endpoint(self) -> str:
        endpoint = self._schedule[self._position]
        self._position = (self._position + 1) % len(self._schedule)
        return endpoint

    def dispatch(self, body: str, sender: Callable[[str, str], object]):
        return sender(self.next_endpoint(), body)
