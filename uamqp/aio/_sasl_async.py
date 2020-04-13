#-------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
#--------------------------------------------------------------------------

import struct
from enum import Enum

from ._transport_async import AsyncTransport
from ..types import AMQPTypes, TYPE, VALUE
from ..constants import FIELD, SASLCode, SASL_HEADER_FRAME
from ..performatives import (
    SASLOutcome,
    SASLResponse,
    SASLChallenge,
    SASLInit
)


_SASL_FRAME_TYPE = b'\x01'


class SASLPlainCredential(object):
    """PLAIN SASL authentication mechanism.
    See https://tools.ietf.org/html/rfc4616 for details
    """

    mechanism = b'PLAIN'

    def __init__(self, authcid, passwd, authzid=None):
        self.authcid = authcid
        self.passwd = passwd
        self.authzid = authzid

    def start(self):
        if self.authzid:
            login_response = self.authzid.encode('utf-8')
        else:
            login_response = b''
        login_response += b'\0'
        login_response += self.authcid.encode('utf-8')
        login_response += b'\0'
        login_response += self.passwd.encode('utf-8')
        return login_response


class SASLAnonymousCredential(object):
    """ANONYMOUS SASL authentication mechanism.
    See https://tools.ietf.org/html/rfc4505 for details
    """

    mechanism = b'ANONYMOUS'

    def start(self):
        return b''


class SASLExternalCredential(object):
    """EXTERNAL SASL mechanism.
    Enables external authentication, i.e. not handled through this protocol.
    Only passes 'EXTERNAL' as authentication mechanism, but no further
    authentication data.
    """

    mechanism = b'EXTERNAL'

    def start(self):
        return b''


class SASLTransport(AsyncTransport):

    def __init__(self, host, credential, connect_timeout=None, ssl=None, **kwargs):
        self.credential = credential
        ssl = ssl or True
        super(SASLTransport, self).__init__(host, connect_timeout=connect_timeout, ssl=ssl, **kwargs)

    async def negotiate(self):
        await self.send_frame(0, SASL_HEADER_FRAME, frame_type=_SASL_FRAME_TYPE)
        _, returned_header = await self.receive_frame()
        if returned_header[1] != SASL_HEADER_FRAME:
            raise ValueError("Mismatching AMQP header protocol. Excpected: {}, received: {}".format(
                SASL_HEADER_FRAME, returned_header[1]))

        _, supported_mechansisms = await self.receive_frame(verify_frame_type=1)
        if self.credential.mechanism not in supported_mechansisms.sasl_server_mechanisms:
            raise ValueError("Unsupported SASL credential type: {}".format(self.credential.mechanism))
        sasl_init = SASLInit(
            mechanism=self.credential.mechanism,
            initial_response=self.credential.start(),
            hostname=self.host)
        await self.send_frame(0, sasl_init, frame_type=_SASL_FRAME_TYPE)

        _, next_frame = await self.receive_frame(verify_frame_type=1)
        if not isinstance(next_frame, SASLOutcome):
            raise NotImplementedError("Unsupported SASL challenge")
        if next_frame.code == SASLCode.Ok:
            return
        else:
            raise ValueError("SASL negotiation failed.\nOutcome: {}\nDetails: {}".format(
                next_frame.code, next_frame.additional_data))
