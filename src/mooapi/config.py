"""Configuration shared by the MooAPI hub and network transports."""

from __future__ import annotations

from dataclasses import dataclass, field

from .codec.primitives import CodecLimits


@dataclass(frozen=True, slots=True)
class ServerConfig:
    codec: CodecLimits = field(default_factory=CodecLimits)
    motd: bytes = b"MooAPI Version 1.22"
    require_hello: bool = True
    # Peer addresses are required for direct ``User: Blast`` UDP routing. Set
    # this to False for deployments that intentionally disable that feature.
    expose_client_ip: bool = True
    master_announce: str = "successor"  # "successor" | "original"
    duplicate_join: str = "ignore"  # "ignore" | "original"
    rename_broadcast: str = "deduplicated"  # "deduplicated" | "per-session"
    dialect: str = "auto"  # "A" | "B" | "auto"
    dialect_b_alias_policy: str = "padded-best-effort"
    # Native later Moo clients send User: Blast directly to the peer, and the
    # older MooGame 1.01 standalone behavior does not relay its private UDP form.
    # Keep server relay available only as an explicit compatibility experiment.
    relay_private_blast: bool = False

    max_connections: int = 512
    max_connections_per_ip: int = 8
    max_sessions: int = 1024
    max_sessions_per_connection: int = 32
    max_members_per_session: int = 128

    # Conservative transport defaults for public listeners.
    # Public Moo ports receive ordinary Internet noise (HTTP/TLS/SSH probes).  The
    # pre-Moo guard classifies those before they reach the Moo packet decoder while
    # preserving the original immediate MOTD/ID greeting sent on connect.
    non_moo_guard: bool = True
    identification_timeout: float = 10.0
    preidentify_buffer: int = 4096

    # Privacy defaults.  Native Moo exposes the TCP peer IP to channel peers for
    # direct User: Blast compatibility, but the replacement server does not need
    # to create a richer persistent tracking layer around that requirement.
    # Transport observations (such as the learned UDP source port) stay in memory
    # only and are discarded when the connection ends.
    persist_network_metadata: bool = False
    log_connection_classification: bool = False
    log_peer_addresses: bool = False
    log_foreign_payloads: bool = False
    hello_timeout: float = 30.0
    partial_packet_timeout: float = 30.0
    idle_timeout: float = 30.0 * 60.0  # 0 disables
    max_send_queue: int = 1024 * 1024
    tcp_read_size: int = 64 * 1024
    write_timeout: float = 10.0
    connect_rate_per_ip_per_minute: float = 10.0
    connect_burst_per_ip: float = 10.0
    packet_rate: float = 200.0
    packet_burst: float = 400.0
    byte_rate: float = 256 * 1024.0
    byte_burst: float = 512 * 1024.0
    join_rename_rate: float = 5.0
    join_rename_burst: float = 5.0
    max_udp_datagram: int = 4096
    udp_rate: float = 200.0
    udp_burst: float = 200.0
    udp_echo_to_sender: bool = False
    udp_endpoint_ttl: float = 10.0 * 60.0

    # Filesystem INI/IMI support is the default interoperability format.  Keeping
    # profile state in the historical file form makes it straightforward to move
    # saved data between this Python server, the Python MOO1/MOO2 server, and
    # original MOO/MOS deployments without requiring a database export/conversion.
    ini_enabled: bool = True
    ini_append_imi: bool = True
    ini_namespace: str = "default"
    # Filesystem-only profile layout used by the unified server.  By default each
    # TCP realm gets its own directory: C:\Moo\data\ini\<tcp-port>.
    # Set ini_shared=True to use ini_data_root directly, or ini_directory to name
    # an exact directory.
    ini_data_root: str = r"C:\Moo\data\ini"
    ini_shared: bool = False
    ini_directory: str | None = None
    ini_max_files: int = 64
    ini_max_keys_per_file: int = 4096
    ini_max_bytes: int = 4 * 1024 * 1024
    ini_write_rate: float = 20.0
    ini_write_burst: float = 20.0

    @classmethod
    def original_b_comparison(cls, **overrides):
        """Compatibility preset approximating MooGame 1.01 host behavior.

        The preset keeps the current parser, caps, and timeouts while selecting the
        older leave, rename, dialect-B alias, and UDP echo behavior.
        """
        values = dict(
            require_hello=False,
            expose_client_ip=True,
            master_announce="original",
            duplicate_join="original",
            rename_broadcast="per-session",
            dialect="B",
            dialect_b_alias_policy="original-A",
            relay_private_blast=False,
            udp_echo_to_sender=True,
        )
        values.update(overrides)
        return cls(**values)

    @classmethod
    def original_tcp_comparison(cls, **overrides):
        """Compatibility preset for older dialect-A TCP behavior.

        The preset changes normal-flow compatibility semantics while keeping the
        current parser, queue limits, and timeouts.
        """
        values = dict(
            require_hello=False,
            expose_client_ip=True,
            master_announce="original",
            duplicate_join="original",
            rename_broadcast="per-session",
            dialect="A",
            relay_private_blast=False,
        )
        values.update(overrides)
        return cls(**values)

    def __post_init__(self) -> None:
        if not self.motd:
            raise ValueError("MOTD must be non-empty")
        if len(self.motd) > self.codec.max_motd_bytes:
            raise ValueError("MOTD exceeds codec cap")
        if self.master_announce not in {"successor", "original"}:
            raise ValueError("master_announce must be 'successor' or 'original'")
        if self.duplicate_join not in {"ignore", "original"}:
            raise ValueError("duplicate_join must be 'ignore' or 'original'")
        if self.rename_broadcast not in {"deduplicated", "per-session"}:
            raise ValueError("rename_broadcast must be 'deduplicated' or 'per-session'")
        if self.dialect not in {"A", "B", "auto"}:
            raise ValueError("dialect must be 'A', 'B', or 'auto'")
        if self.dialect_b_alias_policy not in {"padded-best-effort", "original-A", "suppress"}:
            raise ValueError(
                "dialect_b_alias_policy must be 'padded-best-effort', 'original-A', or 'suppress'"
            )
        for field_name in (
            "max_connections",
            "max_connections_per_ip",
            "max_sessions",
            "max_sessions_per_connection",
            "max_members_per_session",
            "max_send_queue",
            "tcp_read_size",
            "preidentify_buffer",
            "max_udp_datagram",
            "ini_max_files",
            "ini_max_keys_per_file",
            "ini_max_bytes",
        ):
            if getattr(self, field_name) <= 0:
                raise ValueError(f"{field_name} must be positive")
        for field_name in (
            "identification_timeout",
            "hello_timeout",
            "write_timeout",
            "partial_packet_timeout",
            "idle_timeout",
            "connect_rate_per_ip_per_minute",
            "connect_burst_per_ip",
            "packet_rate",
            "packet_burst",
            "byte_rate",
            "byte_burst",
            "join_rename_rate",
            "join_rename_burst",
            "udp_rate",
            "udp_burst",
            "udp_endpoint_ttl",
            "ini_write_rate",
            "ini_write_burst",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must not be negative")
