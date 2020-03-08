#-------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
#--------------------------------------------------------------------------

import uuid
import logging
from io import BytesIO

from ._decode import decode_payload
from .constants import DEFAULT_LINK_CREDIT
from .endpoints import Target
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


class ReceiverLink(Link):

    def __init__(self, session, handle, source, **kwargs):
        name = kwargs.pop('name', None) or str(uuid.uuid4())
        role = 'RECEIVER'
        target = kwargs.pop('target', None) or Target(address="receiver-link-{}".format(name))
        super(ReceiverLink, self).__init__(session, handle, name, role, source, target, **kwargs)


    def _process_incoming_message(self, frame, message):
        pass

    def _incoming_ATTACH(self, frame):
        super(ReceiverLink, self)._incoming_ATTACH(frame)
        if frame.initial_delivery_count is None:
            _LOGGER.info("Cannot get initial-delivery-count. Detaching link")
            self._remove_pending_deliveries()
            self._set_state(LinkState.DETACHED)  # TODO: Send detach now?
        self.delivery_count = frame.initial_delivery_count
        self.current_link_credit = self.link_credit
        self._outgoing_FLOW()

    def _incoming_TRANSFER(self, frame):
        self.current_link_credit -= 1
        self.delivery_count += 1
        self.received_delivery_id = frame.delivery_id
        if not self.received_delivery_id and not self._received_payload:
            pass  # TODO: delivery error
        if self._received_payload or frame.more:
            self._received_payload += frame._payload
        if not frame.more:
            payload_data = self._received_payload or frame._payload
            byte_buffer = BytesIO(payload_data)
            decoded_message = decode_payload(byte_buffer, length=len(payload_data))
            delivery_state = self._process_incoming_message(frame, decoded_message)
            if not frame.settled and delivery_state:
                self._outgoing_DISPOSITION(frame.delivery_id, delivery_state)

    def _outgoing_DISPOSITION(self, delivery_id, delivery_state):
        disposition_frame = DispositionFrame(
            role=self.role,
            first=delivery_id,
            last=delivery_id,
            settled=True,
            state=delivery_state,
            # batchable=
        )
        self._session._outgoing_DISPOSITION(disposition_frame)

    def send_disposition(self, delivery_id, delivery_state=None):
        if self._is_closed:
            raise ValueError("Link already closed.")
        self._outgoing_DISPOSITION(delivery_id, delivery_state)
