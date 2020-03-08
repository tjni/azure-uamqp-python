#-------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
#--------------------------------------------------------------------------

import uuid
import logging
import time

from ._encode import encode_payload
from .endpoints import Source
from .link import Link
from .constants import (
    DEFAULT_LINK_CREDIT,
    SessionState,
    SessionTransferState,
    LinkDeliverySettleReason,
    LinkState
)
from .performatives import (
    AttachFrame,
    DetachFrame,
    TransferFrame,
    DispositionFrame,
    FlowFrame,
)

_LOGGER = logging.getLogger(__name__)


class PendingDelivery(object):

    def __init__(self, **kwargs):
        self.message = kwargs.get('message')
        self.sent = False
        self.frame = None
        self.on_delivery_settled = kwargs.get('on_delivery_settled')
        self.link = kwargs.get('link')
        self.start = time.time()
        self.transfer_state = None
        self.timeout = kwargs.get('timeout')
        self.settled = kwargs.get('settled', False)
    
    def on_settled(self, reason, state):
        if self.on_delivery_settled and not self.settled:
            try:
                self.on_delivery_settled(self.message, reason, state)
            except Exception as e:
                _LOGGER.warning("Message 'on_send_complete' callback failed: {}".format(e))


class SenderLink(Link):

    def __init__(self, session, handle, target, **kwargs):
        name = kwargs.pop('name', None) or str(uuid.uuid4())
        role = 'SENDER'
        source = kwargs.pop('source', None) or Source(address="sender-link-{}".format(name))
        super(SenderLink, self).__init__(session, handle, name, role, source, target, **kwargs)
        self._unsent_messages = []

    def _evaluate_timeout(self):
        self._update_pending_delivery_status()

    def _incoming_ATTACH(self, frame):
        super(SenderLink, self)._incoming_ATTACH(frame)
        self.current_link_credit = 0
        self._update_pending_delivery_status()

    def _incoming_FLOW(self, frame):
        rcv_link_credit = frame.link_credit
        rcv_delivery_count = frame.delivery_count
        if frame.handle:
            if rcv_link_credit is None or rcv_delivery_count is None:
                _LOGGER.info("Unable to get link-credit or delivery-count from incoming ATTACH. Detaching link.")
                self._remove_pending_deliveries()
                self._set_state(LinkState.DETACHED)  # TODO: Send detach now?
            else:
                self.current_link_credit = rcv_delivery_count + rcv_link_credit - self.delivery_count
        if self.current_link_credit > 0:
            self._send_unsent_messages()

    def _outgoing_TRANSFER(self, delivery):
        delivery_count = self.delivery_count + 1
        delivery.frame = TransferFrame(
            handle=self.handle,
            delivery_tag=bytes(delivery_count),
            message_format=delivery.message.FORMAT,
            settled=delivery.settled,
            more=False,
            _payload=encode_payload(b"", delivery.message)
        )
        self._session._outgoing_TRANSFER(delivery)
        if delivery.transfer_state == SessionTransferState.Okay:
            self.delivery_count = delivery_count
            self.current_link_credit -= 1
            delivery.sent = True
            if delivery.settled:
                delivery.on_settled(LinkDeliverySettleReason.Settled, None)
            else:
                self._pending_deliveries[delivery.frame.delivery_id] = delivery
        elif delivery.transfer_state == SessionTransferState.Error:
            raise ValueError("Message failed to send")

    def _incoming_DISPOSITION(self, frame):
        if not frame.settled:
            return
        range_end = (frame.last or frame.first) + 1
        settled_ids = [i for i in range(frame.first, range_end)]
        for settled_id in settled_ids:
            delivery = self._pending_deliveries.pop(settled_id, None)
            if delivery:
                delivery.on_settled(LinkDeliverySettleReason.DispositionReceived, frame.state)


    def _update_pending_delivery_status(self):
        now = time.time()
        if self.current_link_credit <= 0:
            self.current_link_credit = self.link_credit
            self._outgoing_FLOW()
        expired = []
        for delivery in self._pending_deliveries.values():
            if delivery.timeout and (now - delivery.start) >= delivery.timeout:
                expired.append(delivery.frame.delivery_id)
                delivery.on_settled(LinkDeliverySettleReason.Timeout, None)
        self._pending_deliveries = {i: d for i, d in self._pending_deliveries.items() if i not in expired}

    def _send_unsent_messages(self):
        unsent = []
        for delivery in self._unsent_messages:
            if not delivery.sent:
                self._outgoing_TRANSFER(delivery)
                if not delivery.sent:
                    unsent.append(delivery)
        self._unsent_messages = unsent

    def send_transfer(self, message, **kwargs):
        if self._is_closed:
            raise ValueError("Link already closed.")
        if self.state != LinkState.ATTACHED:
            raise ValueError("Link is not attached.")
        settled = self.send_settle_mode == 'SETTLED'
        if self.send_settle_mode == 'MIXED':
            settled = kwargs.pop('settled', True)
        delivery = PendingDelivery(
            on_delivery_settled=kwargs.get('on_send_complete'),
            timeout=kwargs.get('timeout'),
            link=self,
            message=message,
            settled=settled,
        )
        if self.current_link_credit == 0:
            self._unsent_messages.append(delivery)
        else:
            self._outgoing_TRANSFER(delivery)
            if not delivery.sent:
                self._unsent_messages.append(delivery)
        return delivery
    
    def cancel_transfer(self, delivery):
        try:
            delivery = self._pending_deliveries.pop(delivery.frame.delivery_id)
            delivery.on_settled(LinkDeliverySettleReason.Cancelled, None)
            return
        except KeyError:
            pass
        # todo remove from unset messages
        raise ValueError("No pending delivery with ID '{}' found.".format(delivery_id))
