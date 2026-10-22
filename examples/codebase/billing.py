class Billing:
    """Interview fixture: process-local idempotency, not production payment code."""

    def __init__(self):
        self.refunds = {}

    def refund_order(self, request_key, amount):
        if request_key not in self.refunds:
            self.refunds[request_key] = {"request_key": request_key, "amount": amount}
        return self.refunds[request_key]
