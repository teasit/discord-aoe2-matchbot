import asyncio
import base64
import functools
import logging
import struct
import time
import zlib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

import requests
import zstandard
from Cryptodome.Cipher import PKCS1_v1_5
from Cryptodome.PublicKey import RSA
from steam.client import EResult, SteamClient
from steam.client.cdn import CDNClient
from steam.core.connection import TCPConnection
from steam.core.crypto import symmetric_decrypt
from steam.core.msg import MsgProto
from steam.enums import EOSType
from steam.enums.emsg import EMsg
from steam.protobufs import steammessages_auth_pb2 as auth_pb2
from steam.steamid import SteamID
from steam.utils import ip4_to_int


class SteamAuthError(Exception):
    pass


class SteamAuthenticationService:
    """Steam's credential login (IAuthenticationService) that issues refresh tokens.

    Plain password logons to the CM servers (what `SteamClient.login` sends) are
    rejected with `InvalidPassword` even for correct credentials, so a refresh
    token has to be obtained here and used for the CM logon instead.
    """

    API_URL = "https://api.steampowered.com/IAuthenticationService/"
    PLATFORM_TYPE_STEAM_CLIENT = 1
    OS_TYPE_WINDOWS_10 = -500
    CONFIRMATION_NONE = 1
    POLL_ATTEMPTS = 10

    @classmethod
    def _call(cls, method: str, request, response_cls, http_get=False):
        data = {
            "input_protobuf_encoded": base64.b64encode(
                request.SerializeToString()
            ).decode()
        }
        url = f"{cls.API_URL}{method}/v1"
        if http_get:
            response = requests.get(url, params=data, timeout=20)
        else:
            response = requests.post(url, data=data, timeout=20)
        response.raise_for_status()
        eresult = int(response.headers.get("x-eresult", EResult.OK))
        if eresult != EResult.OK:
            raise SteamAuthError(f"{method} failed: {repr(EResult(eresult))}")
        result = response_cls()
        result.ParseFromString(response.content)
        return result

    @classmethod
    def get_refresh_token(cls, username: str, password: str) -> str:
        key = cls._call(
            "GetPasswordRSAPublicKey",
            auth_pb2.CAuthentication_GetPasswordRSAPublicKey_Request(
                account_name=username
            ),
            auth_pb2.CAuthentication_GetPasswordRSAPublicKey_Response,
            http_get=True,
        )
        rsa_key = RSA.construct(
            (int(key.publickey_mod, 16), int(key.publickey_exp, 16))
        )
        encrypted_password = base64.b64encode(
            PKCS1_v1_5.new(rsa_key).encrypt(password.encode())
        ).decode()

        request = auth_pb2.CAuthentication_BeginAuthSessionViaCredentials_Request(
            account_name=username,
            encrypted_password=encrypted_password,
            encryption_timestamp=key.timestamp,
            persistence=1,
            website_id="Client",
        )
        request.device_details.device_friendly_name = "discord-aoe2-matchbot"
        request.device_details.platform_type = cls.PLATFORM_TYPE_STEAM_CLIENT
        request.device_details.os_type = cls.OS_TYPE_WINDOWS_10
        session = cls._call(
            "BeginAuthSessionViaCredentials",
            request,
            auth_pb2.CAuthentication_BeginAuthSessionViaCredentials_Response,
        )
        confirmations = [c.confirmation_type for c in session.allowed_confirmations]
        if confirmations and cls.CONFIRMATION_NONE not in confirmations:
            raise SteamAuthError(
                "Two-factor authentication not supported. "
                "Please deactivate both email 2FA and steam guard!"
            )

        for _ in range(cls.POLL_ATTEMPTS):
            status = cls._call(
                "PollAuthSessionStatus",
                auth_pb2.CAuthentication_PollAuthSessionStatus_Request(
                    client_id=session.client_id,
                    request_id=session.request_id,
                ),
                auth_pb2.CAuthentication_PollAuthSessionStatus_Response,
            )
            if status.refresh_token:
                return status.refresh_token
            time.sleep(max(1, session.interval))
        raise SteamAuthError("No refresh token received.")


class DepotClient(CDNClient):
    """Downloads single files of an app's depots from the Steam CDN.

    Works around two incompatibilities of the `steam` library with the
    current Steam backend: depot manifests are listed as {"gid": ...} dicts
    and need a manifest request code (`get_manifests` breaks on both), and
    chunks are compressed with zstd ("VSZa"), which `get_chunk` cannot read.
    """

    def get_chunk(self, app_id, depot_id, chunk_id):
        if (depot_id, chunk_id) in self._chunk_cache:
            return self._chunk_cache[(depot_id, chunk_id)]
        resp = self.cdn_cmd("depot", f"{depot_id}/chunk/{chunk_id}")
        data = symmetric_decrypt(resp.content, self.get_depot_key(app_id, depot_id))
        if data[:4] != b"VSZa":
            return super().get_chunk(app_id, depot_id, chunk_id)
        # "VSZa", CRC32 of the decompressed data, zstd frame, footer.
        (checksum,) = struct.unpack_from("<I", data, 4)
        data = zstandard.ZstdDecompressor().decompressobj().decompress(data[8:])
        if zlib.crc32(data) != checksum:
            raise ValueError(f"VSZa: CRC32 mismatch in chunk {chunk_id}")
        self._chunk_cache[(depot_id, chunk_id)] = data
        return data

    def read_file(self, app_id: int, path: str, branch: str = "public") -> bytes:
        """Contents of the file at `path` (relative to the install dir) from
        the first depot of the app that contains it."""
        path = path.replace("\\", "/").lower()
        for depot_id, info in self.get_app_depot_info(app_id).items():
            if not str(depot_id).isdigit() or not isinstance(info, dict):
                continue
            manifest = info.get("manifests", {}).get(branch)
            if not manifest:
                continue
            gid = int(manifest["gid"] if isinstance(manifest, dict) else manifest)
            try:
                code = self.get_manifest_request_code(app_id, int(depot_id), gid)
                depot = self.get_manifest(
                    app_id, int(depot_id), gid, manifest_request_code=code
                )
            except Exception:
                # Depots of DLCs or platforms the account does not own.
                continue
            for file in depot.iter_files():
                if file.filename.replace("\\", "/").lower() == path:
                    return file.read()
        raise FileNotFoundError(f"{path} not found in the depots of app {app_id}.")


class TCPConnectionWithTimeout(TCPConnection):
    """`TCPConnection` whose connect gives up after CONNECT_TIMEOUT_SECONDS
    instead of the OS default (about 2 minutes on Linux)."""

    CONNECT_TIMEOUT_SECONDS = 10

    def _connect(self, server_addr):
        self.socket.settimeout(self.CONNECT_TIMEOUT_SECONDS)
        self.socket.connect(server_addr)
        self.socket.settimeout(None)


class SteamClientWithout2FA(SteamClient):
    # CM servers tried per login (5-10 s each) before giving up.
    CONNECT_RETRIES = 3

    logger: logging.Logger
    username: str = ""
    password: str = ""

    def __init__(
        self,
        username: str,
        password: str,
        logger: logging.Logger | None = None,
    ):
        super().__init__()
        self.connection = TCPConnectionWithTimeout()
        self.username = username
        self.password = password
        self._refresh_token = None
        self.logger = logger or logging.getLogger()
        self._register_event_handlers()

    def login(self):  # pyright: ignore[reportIncompatibleMethodOverride]
        self.logger.debug(f"Logging in as {self.username}...")
        result = EResult.Fail
        for _ in range(2):
            if not self._refresh_token:
                try:
                    self._refresh_token = SteamAuthenticationService.get_refresh_token(
                        self.username, self.password
                    )
                except Exception as e:
                    self.logger.error(f"Steam authentication failed: {e}")
                    return EResult.Fail
            result = self._login_with_refresh_token()
            if result == EResult.OK:
                return result
            # Token may have expired or been revoked, fetch a new one once.
            self._refresh_token = None
        self.logger.error(f"Login failed: {repr(result)}")
        return result

    def read_depot_file(self, app_id: int, path: str) -> bytes:
        """Downloads one file of an owned app from the Steam CDN, without
        installing the app. Logs in if needed and out again afterwards."""
        logged_in_here = not self.logged_on
        if logged_in_here and self.login() != EResult.OK:
            raise SteamAuthError("Steam login failed.")
        try:
            return DepotClient(self).read_file(app_id, path)
        finally:
            if logged_in_here:
                self.logout()

    def _pre_login(self) -> EResult:
        """`SteamClient._pre_login` without its unbounded waits: `connect()`
        retries forever by default and the wait for the disconnect after a
        missing hello has no timeout, so a dropped connection blocked forever."""
        if self.logged_on:
            raise RuntimeError("Already logged on")
        connecting = self.connected or self._connecting
        if not connecting and not self.connect(retry=self.CONNECT_RETRIES):
            return EResult.Fail
        # wait_event returns the event's arguments, None on timeout.
        hello = self.channel_secured or self.wait_event(
            self.EVENT_CHANNEL_SECURED, timeout=10
        )
        if hello is None:
            # Some CMs do not send hello.
            self.disconnect()
            return EResult.TryAnotherCM
        return EResult.OK

    def _login_with_refresh_token(self) -> EResult:
        """Same logon message as `SteamClient.login`, authenticated by the refresh token."""
        eresult = self._pre_login()
        if eresult != EResult.OK:
            return eresult

        message = MsgProto(EMsg.ClientLogon)
        message.header.steamid = SteamID(type="Individual", universe="Public")
        message.body.protocol_version = 65580
        message.body.client_package_version = 1561159470
        message.body.client_os_type = EOSType.Windows10
        message.body.client_language = "english"
        message.body.should_remember_password = True
        message.body.supports_rate_limit_response = True
        message.body.chat_mode = self.chat_mode
        message.body.obfuscated_private_ip.v4 = (
            ip4_to_int(self.connection.local_address) ^ 0xF00DBAAD
        )
        message.body.account_name = self.username
        message.body.access_token = self._refresh_token
        self.send(message)

        response = self.wait_msg(EMsg.ClientLogOnResponse, timeout=30)
        if response and response.body.eresult == EResult.OK:
            self.sleep(0.5)
        return EResult(response.body.eresult) if response else EResult.Fail

    def _register_event_handlers(self):
        def event_handler(event: str):
            def decorator(func):
                @functools.wraps(func)
                def wrapper(*args, **kwargs):
                    self.logger.debug(f"Event: {event}")
                    return func(*args, **kwargs)

                self.on(event, wrapper)
                return wrapper

            return decorator

        @event_handler(self.EVENT_ERROR)
        def _handle_error(result):
            self.logger.error(f"Error: {repr(result)}")

        @event_handler(self.EVENT_CHANNEL_SECURED)
        def _handle_channel_secured():
            if self.relogin_available:
                self.logger.info("Session expired. Re-login...")
                self.relogin()

        @event_handler(self.EVENT_AUTH_CODE_REQUIRED)
        def _handle_auth_code_required(is_2fa, code_mismatch):
            msg = (
                "Two-factor authentication not supported. "
                "Please deactivate both email 2FA and steam guard!"
            )
            self.logger.error(msg)

        @event_handler(self.EVENT_LOGGED_ON)
        def _handle_logged_on():
            msg = (
                "Logged in to Steam account successfully.\n"
                f"SteamID: {self.steam_id.as_64}\n"
                f"Community Profile: {self.steam_id.community_url}"
            )
            self.logger.debug(msg)

        @event_handler(self.EVENT_RECONNECT)
        def _handle_reconnect(delay):
            self.logger.debug(f"Reconnecting in {delay} seconds...")

        @event_handler(self.EVENT_CONNECTED)
        def _handle_connected():
            self.logger.debug(f"Connected to {self.current_server_addr}")

        @event_handler(self.EVENT_DISCONNECTED)
        def _handle_disconnected():
            self.logger.debug("Disconnected.")


class SteamThread:
    """A `SteamClientWithout2FA` that lives in a thread of its own.

    The steam library is built on gevent: its blocking calls run gevent's hub
    in the calling thread, so made from the asyncio event loop they stall it
    (and Discord's heartbeat) for as long as Steam takes. gevent objects are
    bound to the thread that created them, so the client is created in the
    worker thread too and must only be used through `run`.
    """

    # Upper bound for one call; the thread itself cannot be interrupted, but
    # the client's own timeouts end every call well before this.
    CALL_TIMEOUT_SECONDS = 5 * 60

    client: SteamClientWithout2FA

    def __init__(
        self,
        username: str,
        password: str,
        logger: logging.Logger | None = None,
    ):
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="steam")
        self.client = self._executor.submit(
            SteamClientWithout2FA, username, password, logger
        ).result()

    async def run[T](self, func: Callable[..., T], *args) -> T:
        """`func(client, *args)` in the Steam thread."""
        future = asyncio.get_running_loop().run_in_executor(
            self._executor, func, self.client, *args
        )
        return await asyncio.wait_for(future, self.CALL_TIMEOUT_SECONDS)
