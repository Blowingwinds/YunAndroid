import ipaddress
import os
import subprocess
import signal
import re
import socket
import sys
import time
from pathlib import Path
from threading import Lock, Thread
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# 配置
loop_count = 2  #示例数
start_local_port = 63000  #本机起始端口号
start_ip_first = 2  # first remote IP last octet
ssh_user_host = os.environ.get("YUNANDROID_SSH_USER_HOST", "")
pem_path = str(PROJECT_ROOT / "config" / "keys" / "atm-phone.pem")
LOG_FILE = PROJECT_ROOT / "data" / "logs" / "tunnel_adb_log.txt"
tunnel_wait = 3.0  # 隧道建立最大等待时间，网络差调大

_FORWARD_FAILURE_PATTERN = re.compile(
    r"channel_setup_fwd_listener_tcpip:\s*cannot listen to port:\s*(\d+)",
    re.IGNORECASE,
)
_FORWARD_BIND_FAILURE_PATTERN = re.compile(
    r"bind\s+\[?127\.0\.0\.1\]?:(\d+)\s*:",
    re.IGNORECASE,
)
_BACKGROUND_CREATION_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def replace_ssh_host(ssh_user_host: str, public_ip: str) -> str:
    """Replace only the host portion of an SSH target with a validated IPv4."""
    host = str(ipaddress.IPv4Address(str(public_ip).strip()))
    user, separator, _current_host = str(ssh_user_host).rpartition("@")
    return f"{user}@{host}" if separator else host


def parse_parameter_values(expression: str | int) -> list[int]:
    """Parse values such as ``12, 15-20`` into an ordered integer list."""
    text = str(expression).strip()
    if not text:
        raise ValueError("连接参数不能为空")

    values = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            raise ValueError(f"连接参数格式错误：{expression}")
        if not all(character.isdigit() or character.isspace() or character == "-" for character in token):
            raise ValueError(f"连接参数格式错误：{token}")
        parts = [part.strip() for part in token.split("-")]
        if len(parts) > 2 or any(not part.isdigit() for part in parts):
            raise ValueError(f"连接参数格式错误：{token}")
        start = int(parts[0])
        end = int(parts[-1])
        if len(parts) == 2 and end < start:
            raise ValueError(f"连接参数范围必须从小到大：{token}")
        values.extend(range(start, end + 1))
    return values


def build_connection_batches(
    start_local_port: str | int,
    loop_count: str | int,
    start_ip_first: str | int,
) -> list[dict[str, int]]:
    """Expand connection expressions into positionally paired batches."""
    local_ports = parse_parameter_values(start_local_port)
    loop_counts = parse_parameter_values(loop_count)
    ip_starts = parse_parameter_values(start_ip_first)
    max_length = max(len(local_ports), len(loop_counts), len(ip_starts))
    for name, values in (
        ("start_local_port", local_ports),
        ("loop_count", loop_counts),
        ("start_ip_first", ip_starts),
    ):
        if len(values) not in (1, max_length):
            raise ValueError(f"{name} 参数数量必须为 1 或 {max_length}")

    batches = []
    for index in range(max_length):
        local_port = local_ports[index] if len(local_ports) > 1 else local_ports[0]
        count = loop_counts[index] if len(loop_counts) > 1 else loop_counts[0]
        ip_start = ip_starts[index] if len(ip_starts) > 1 else ip_starts[0]
        if not 1 <= local_port <= 65535:
            raise ValueError(f"本地端口超出范围：{local_port}")
        if count < 1:
            raise ValueError(f"循环次数必须大于 0：{count}")
        if not 1 <= ip_start <= 254:
            raise ValueError(f"远端 IP 末段超出范围：{ip_start}")
        batches.append(
            {
                "start_local_port": local_port,
                "loop_count": count,
                "start_ip_first": ip_start,
            }
        )
    return batches


def configure_console_encoding():
    """Keep Unicode status messages printable on Windows consoles."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def run_adb_connect(adb_cmd, timeout=10.0):
    """Run adb and decode its UTF-8 output without failing on bad bytes."""
    ret = subprocess.run(
        adb_cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_BACKGROUND_CREATION_FLAGS,
    )
    stdout = ret.stdout.strip() if ret.stdout else ""
    stderr = ret.stderr.strip() if ret.stderr else ""
    return stdout, stderr


def probe_local_port(local_port, timeout=0.05):
    """Check whether the local SSH forwarding listener is accepting TCP."""
    try:
        with socket.create_connection(("127.0.0.1", int(local_port)), timeout=timeout):
            return True
    except (OSError, TypeError, ValueError):
        return False


def is_local_port_available(local_port):
    """Return whether the loopback port can be bound by a new SSH listener."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                probe.setsockopt(socket.SOL_SOCKET, exclusive, 1)
            probe.bind(("127.0.0.1", int(local_port)))
    except (OSError, TypeError, ValueError):
        return False
    return True


def allocate_connection_ports(mappings, port_available=is_local_port_available):
    """Assign each mapping the first available port at or after its queue port."""
    allocated = []
    reserved = set()
    for original in mappings:
        candidate = int(original["local_port"])
        if candidate < 1:
            raise ValueError(f"本地端口必须在 1-65535 范围内：{candidate}")
        while candidate <= 65535 and (
            candidate in reserved or not port_available(candidate)
        ):
            candidate += 1
        if candidate > 65535:
            raise RuntimeError("没有可用的本地端口")
        item = {**original, "local_port": candidate}
        allocated.append(item)
        reserved.add(candidate)
    return allocated


def build_mapping(idx, start_local_port, start_ip_first):
    """Build the ordered local-to-remote mapping for one tunnel."""
    return {
        "local_port": start_local_port + idx ,
        "remote_ip": f"10.237.0.{start_ip_first + idx }",
        "remote_port": 5555,
    }


def format_mapping_table(mappings):
    """Format all tunnel mappings and their ADB results for terminal output."""
    lines = ["\n端口映射表", "=" * 72]
    lines.append("映射关系                                      ADB状态")
    lines.append("-" * 72)
    for mapping in mappings:
        target = (
            f"127.0.0.1:{mapping['local_port']} -> "
            f"{mapping['remote_ip']}:{mapping['remote_port']}"
        )
        lines.append(f"{target:<48} {mapping.get('adb_status', '未执行')}")
    lines.append("=" * 72)
    return "\n".join(lines)


def parse_ssh_forward_failure_ports(output: str) -> set[int]:
    """Extract local ports whose SSH forwarding listeners failed to bind."""
    failed_ports = {
        int(match.group(1))
        for match in _FORWARD_FAILURE_PATTERN.finditer(output or "")
    }
    failed_ports.update(
        int(match.group(1))
        for match in _FORWARD_BIND_FAILURE_PATTERN.finditer(output or "")
    )
    return failed_ports


def ssh_forward_failure_ports(process: object) -> set[int]:
    """Read the asynchronously captured SSH diagnostics for failed forwards."""
    diagnostics = getattr(process, "_sfmanager_ssh_diagnostics", ())
    lock = getattr(process, "_sfmanager_ssh_diagnostics_lock", None)
    if lock is None:
        output = "".join(diagnostics)
    else:
        with lock:
            output = "".join(diagnostics)
    return parse_ssh_forward_failure_ports(output)


def build_ssh_command(mapping_or_mappings, ssh_user_host, pem_path):
    """Build one foreground SSH command containing one ``-L`` per mapping."""
    if isinstance(mapping_or_mappings, dict):
        mappings = [mapping_or_mappings]
    else:
        mappings = list(mapping_or_mappings)
    if not mappings:
        raise ValueError("至少需要一条端口转发规则")

    command = [
        "ssh",
        "-i",
        pem_path,
        "-o",
        "StrictHostKeyChecking=no",    # 自动接受主机指纹，不再弹出yes/no交互
        "-o",
        "UserKnownHostsFile=/dev/null",# 不写入known_hosts，避免缓存旧指纹
        "-o",
        "ExitOnForwardFailure=no",
        "-o",
        "ServerAliveInterval=30",
    ]
    for mapping in mappings:
        command.extend(
            [
                "-L",
                f"{mapping['local_port']}:{mapping['remote_ip']}:{mapping['remote_port']}",
            ]
        )
    command.extend(["-N", ssh_user_host])
    return command


def start_ssh_tunnel(ssh_cmd):
    """Start a tunnel and retain SSH diagnostics for per-forward failure checks."""
    process = subprocess.Popen(
        ssh_cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=_BACKGROUND_CREATION_FLAGS,
    )
    diagnostics = []
    diagnostics_lock = Lock()
    process._sfmanager_ssh_diagnostics = diagnostics
    process._sfmanager_ssh_diagnostics_lock = diagnostics_lock

    def drain_stderr():
        stream = process.stderr
        if stream is None:
            return
        try:
            for line in stream:
                with diagnostics_lock:
                    diagnostics.append(line)
        finally:
            stream.close()

    Thread(target=drain_stderr, name="ssh-stderr-reader", daemon=True).start()
    return process


def stop_ssh_process(process):
    """Stop one failed tunnel without affecting other active SSH processes."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def cleanup_ssh_tunnels(_ssh_processes=None):
    """Stop every local SSH tunnel, including tunnels left by an older run."""
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-Process ssh -ErrorAction SilentlyContinue | Stop-Process -Force",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_BACKGROUND_CREATION_FLAGS,
    )


def disconnect_adb_ports(mappings, adb_executable="adb"):
    """Disconnect each local ADB endpoint used by the tunnel mappings."""
    for mapping in mappings:
        subprocess.run(
            [adb_executable, "disconnect", f"127.0.0.1:{mapping['local_port']}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_BACKGROUND_CREATION_FLAGS,
        )


class TunnelManager:
    """Own and release only the SSH tunnels opened by one desktop session."""

    def __init__(
        self,
        ssh_user_host: str = ssh_user_host,
        pem_path: str = pem_path,
        tunnel_wait: float = tunnel_wait,
        adb_executable: str = "adb",
        start_tunnel: Callable = start_ssh_tunnel,
        stop_tunnel: Callable = stop_ssh_process,
        disconnect_ports: Callable | None = None,
        adb_connector: Callable | None = None,
        sleeper: Callable = time.sleep,
        adb_connect_timeout: float = 10.0,
        tunnel_probe: Callable | None = None,
        tunnel_poll_interval: float = 0.05,
        port_available: Callable | None = None,
        forward_failure_reader: Callable | None = None,
    ):
        self.ssh_user_host = ssh_user_host
        self.pem_path = pem_path
        self.tunnel_wait = tunnel_wait
        self.adb_executable = adb_executable
        self.start_tunnel = start_tunnel
        self.stop_tunnel = stop_tunnel
        self.disconnect_ports = disconnect_ports or (
            lambda mappings: disconnect_adb_ports(mappings, self.adb_executable)
        )
        try:
            self.adb_connect_timeout = float(adb_connect_timeout)
            self.tunnel_poll_interval = float(tunnel_poll_interval)
        except (TypeError, ValueError) as exc:
            raise ValueError("ADB timeout and tunnel poll interval must be numeric") from exc
        if self.adb_connect_timeout <= 0:
            raise ValueError("adb_connect_timeout must be positive")
        if self.tunnel_poll_interval <= 0:
            raise ValueError("tunnel_poll_interval must be positive")
        self.adb_connector = (
            adb_connector if adb_connector is not None else run_adb_connect
        )
        self.port_available = (
            port_available if port_available is not None else is_local_port_available
        )
        self.forward_failure_reader = (
            forward_failure_reader
            if forward_failure_reader is not None
            else ssh_forward_failure_ports
        )
        self.sleeper = sleeper
        self.tunnel_probe = (
            tunnel_probe if tunnel_probe is not None else (
                # Keep deterministic injected sleepers on the legacy wait path.
                probe_local_port if sleeper is time.sleep else None
            )
        )
        self.active_processes = []
        self.active_mappings = []
        self.pending_processes = []
        self._state_lock = Lock()
        self._connect_generation = 0

    def _generation_is_current(self, generation: int) -> bool:
        with self._state_lock:
            return generation == self._connect_generation

    def _register_pending_process(self, process: object, generation: int) -> bool:
        with self._state_lock:
            if generation != self._connect_generation:
                return False
            self.pending_processes.append(process)
            return True

    def _stop_pending_process(self, process: object) -> bool:
        with self._state_lock:
            if process not in self.pending_processes:
                return False
            self.pending_processes.remove(process)
        self.stop_tunnel(process)
        return True

    def _activate_process(self, process: object, mapping: dict, generation: int) -> bool:
        with self._state_lock:
            if generation != self._connect_generation:
                return False
            if process in self.pending_processes:
                self.pending_processes.remove(process)
            # Keep one positional entry per mapping for compatibility with
            # targeted disconnects; several entries may reference one tunnel.
            self.active_processes.append(process)
            self.active_mappings.append(mapping)
            return True

    def connect(
        self,
        start_local_port: str | int,
        loop_count: str | int,
        start_ip_first: str | int,
    ) -> list[dict]:
        """Open all requested mappings and return one result record per mapping."""
        batches = build_connection_batches(start_local_port, loop_count, start_ip_first)
        mappings = []
        for batch in batches:
            for index in range(batch["loop_count"]):
                mappings.append(build_mapping(index, batch["start_local_port"], batch["start_ip_first"]))
        return self.connect_mappings(mappings)

    @staticmethod
    def _process_is_alive(process: object) -> bool:
        poll = getattr(process, "poll", None)
        if not callable(poll):
            return True
        try:
            return poll() is None
        except Exception:
            return False

    def _read_forward_failures(self, processes) -> set[int]:
        """Collect diagnosed local ports without allowing diagnostics to break a batch."""
        failures = set()
        seen = set()
        for process in processes:
            marker = id(process)
            if marker in seen:
                continue
            seen.add(marker)
            try:
                failures.update(
                    int(port) for port in self.forward_failure_reader(process)
                )
            except (TypeError, ValueError, OSError):
                continue
            except Exception:
                continue
        return failures

    def _wait_for_tunnels(
        self,
        pending_connections: list[tuple[dict, dict, object]],
        generation: int,
    ) -> set[int]:
        """Wait until forwarding listeners are ready, bounded by tunnel_wait."""
        if self.tunnel_probe is None:
            self.sleeper(max(0.0, float(self.tunnel_wait)))
            return self._read_forward_failures(
                [process for _mapping, _record, process in pending_connections]
            )

        pending = list(pending_connections)
        failed_ports = set()
        deadline = time.monotonic() + max(0.0, float(self.tunnel_wait))
        while pending:
            if not self._generation_is_current(generation):
                return failed_ports
            diagnosed_failures = self._read_forward_failures(
                [process for _mapping, _record, process in pending]
            )
            unresolved = []
            for mapping, record, process in pending:
                local_port = int(mapping["local_port"])
                if not self._process_is_alive(process):
                    record["adb_status"] = "失败"
                    record["reason"] = "SSH 隧道进程已退出，端口可能被其他程序占用"
                    self._stop_pending_process(process)
                    continue
                if local_port in diagnosed_failures:
                    failed_ports.add(local_port)
                    continue
                try:
                    ready = bool(self.tunnel_probe(mapping["local_port"]))
                except Exception:
                    ready = False
                if not ready:
                    unresolved.append((mapping, record, process))
            if not unresolved:
                # Give the stderr reader one short scheduling window to receive
                # a bind diagnostic after the listener probes become ready.
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    self.sleeper(min(self.tunnel_poll_interval, remaining))
                    failed_ports.update(
                        self._read_forward_failures(
                            [process for _mapping, _record, process in pending]
                        )
                    )
                return failed_ports
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failed_ports.update(
                    self._read_forward_failures(
                        [process for _mapping, _record, process in pending]
                    )
                )
                return failed_ports
            self.sleeper(min(self.tunnel_poll_interval, remaining))
            pending = unresolved
        return failed_ports

    def _start_tunnel_batch(
        self,
        connections: list[tuple[dict, dict]],
        generation: int,
    ) -> tuple[object | None, list[tuple[dict, dict, object]]]:
        """Start one SSH process for a batch and register it for cleanup."""
        if not connections:
            return None, []

        process = None
        pending_registered = False
        try:
            public_ip = str(connections[0][0].get("public_ip", "")).strip()
            target = (
                replace_ssh_host(self.ssh_user_host, public_ip)
                if public_ip
                else self.ssh_user_host
            )
            command = build_ssh_command(
                [mapping for mapping, _record in connections],
                target,
                self.pem_path,
            )
            process = self.start_tunnel(command)
            pending_registered = self._register_pending_process(process, generation)
            if not pending_registered:
                for _mapping, record in connections:
                    record["adb_status"] = "已终止"
                self.stop_tunnel(process)
                return None, []
            return process, [
                (mapping, record, process) for mapping, record in connections
            ]
        except Exception as exc:
            status = "SSH 启动失败" if process is None else "失败"
            for _mapping, record in connections:
                record["adb_status"] = status
                record["reason"] = str(exc)
            if process is not None:
                if not self._stop_pending_process(process) and not pending_registered:
                    self.stop_tunnel(process)
            return None, []

    def _start_grouped_batches(
        self,
        connections: list[tuple[dict, dict]],
        generation: int,
    ) -> tuple[list[object], list[tuple[dict, dict, object]]]:
        """Start one shared SSH process for each public server in a batch."""
        groups: dict[str, list[tuple[dict, dict]]] = {}
        for mapping, record in connections:
            public_ip = str(mapping.get("public_ip", "")).strip()
            groups.setdefault(public_ip, []).append((mapping, record))

        processes = []
        pending = []
        for group in groups.values():
            process, group_pending = self._start_tunnel_batch(group, generation)
            if process is not None:
                processes.append(process)
            pending.extend(group_pending)
        return processes, pending

    def connect_mappings(
        self,
        mappings: list[dict],
        reserved_ports: set[int] | None = None,
    ) -> list[dict]:
        """Open mappings with one shared SSH process for all new forwards."""
        records = []
        reserved_ports = {
            int(port) for port in (reserved_ports or set())
        }
        with self._state_lock:
            generation = self._connect_generation
            reserved_ports.update(
                active.get("local_port") for active in self.active_mappings
            )
        pending_requests = []

        for mapping in mappings:
            record = {**mapping, "adb_status": "未执行"}
            records.append(record)

            if not self._generation_is_current(generation):
                record["adb_status"] = "已终止"
                continue

            with self._state_lock:
                existing_index = next(
                    (
                        index
                        for index, active in enumerate(self.active_mappings)
                        if active.get("local_port") == mapping.get("local_port")
                    ),
                    None,
                )
            if existing_index is not None:
                with self._state_lock:
                    existing = self.active_mappings[existing_index]
                if (
                    existing.get("public_ip") == mapping.get("public_ip")
                    and existing.get("remote_ip") == mapping.get("remote_ip")
                    and existing.get("remote_port") == mapping.get("remote_port")
                ):
                    record["adb_status"] = "已连接"
                else:
                    record["adb_status"] = "失败"
                    record["reason"] = (
                        f"本地端口 127.0.0.1:{mapping['local_port']} 已被其他连接占用"
                    )
                continue

            pending_requests.append((mapping, record))

        if pending_requests:
            def port_available(port):
                return port not in reserved_ports and self.port_available(port)

            try:
                allocated_mappings = allocate_connection_ports(
                    [mapping for mapping, _record in pending_requests],
                    port_available=port_available,
                )
            except Exception as exc:
                for _mapping, record in pending_requests:
                    record["adb_status"] = "失败"
                    record["reason"] = f"本地端口分配失败：{exc}"
                return records
        else:
            allocated_mappings = []

        allocated_connections = []
        for (_requested_mapping, record), mapping in zip(
            pending_requests,
            allocated_mappings,
        ):
            record.update(mapping)
            allocated_connections.append((mapping, record))

        primary_processes, primary_pending = self._start_grouped_batches(
            allocated_connections,
            generation,
        )
        started_processes = list(primary_processes)

        primary_failed_ports = set()
        if primary_pending:
            try:
                primary_failed_ports = self._wait_for_tunnels(
                    primary_pending,
                    generation,
                )
            except Exception as exc:
                for _mapping, record, _process in primary_pending:
                    record["adb_status"] = "失败"
                    record["reason"] = str(exc)
                for process in primary_processes:
                    self._stop_pending_process(process)
                return records

        if not self._generation_is_current(generation):
            for _mapping, record, _process in primary_pending:
                if record["adb_status"] == "未执行":
                    record["adb_status"] = "已终止"

        primary_connections = []
        retry_sources = []
        for mapping, record, process in primary_pending:
            if int(mapping["local_port"]) in primary_failed_ports:
                retry_sources.append((mapping, record))
            elif record["adb_status"] == "未执行":
                primary_connections.append((mapping, record, process))

        primary_connection_processes = {
            id(process) for _mapping, _record, process in primary_connections
        }
        for process in primary_processes:
            if id(process) not in primary_connection_processes:
                self._stop_pending_process(process)

        retry_connections = []
        if retry_sources and not self._generation_is_current(generation):
            for _mapping, record in retry_sources:
                if record["adb_status"] == "未执行":
                    record["adb_status"] = "已终止"
            retry_sources = []
        if retry_sources:
            retry_reserved = set(reserved_ports)
            retry_reserved.update(
                int(mapping["local_port"]) for mapping, _record in allocated_connections
            )
            with self._state_lock:
                retry_reserved.update(
                    int(active["local_port"])
                    for active in self.active_mappings
                    if active.get("local_port") is not None
                )
            retry_requests = [
                {**mapping, "local_port": int(mapping["local_port"]) + 1}
                for mapping, _record in retry_sources
            ]

            def retry_port_available(port):
                return port not in retry_reserved and self.port_available(port)

            try:
                retry_mappings = allocate_connection_ports(
                    retry_requests,
                    port_available=retry_port_available,
                )
            except Exception as exc:
                for _mapping, record in retry_sources:
                    record["adb_status"] = "失败"
                    record["reason"] = f"失败端口重分配失败：{exc}"
            else:
                retry_batch = []
                for (_old_mapping, record), retry_mapping in zip(
                    retry_sources,
                    retry_mappings,
                ):
                    record.update(retry_mapping)
                    record["adb_status"] = "未执行"
                    retry_batch.append((retry_mapping, record))

                retry_processes, retry_pending = self._start_grouped_batches(
                    retry_batch,
                    generation,
                )
                started_processes.extend(retry_processes)
                retry_failed_ports = set()
                if retry_pending:
                    try:
                        retry_failed_ports = self._wait_for_tunnels(
                            retry_pending,
                            generation,
                        )
                    except Exception as exc:
                        for _mapping, record, _process in retry_pending:
                            record["adb_status"] = "失败"
                            record["reason"] = f"补偿隧道等待失败：{exc}"
                        for process in retry_processes:
                            self._stop_pending_process(process)
                        retry_pending = []

                for mapping, record, process in retry_pending:
                    if int(mapping["local_port"]) in retry_failed_ports:
                        record["adb_status"] = "失败"
                        record["reason"] = (
                            f"SSH 转发失败：本地端口 127.0.0.1:{mapping['local_port']} 无法监听"
                        )
                    elif record["adb_status"] == "未执行":
                        retry_connections.append((mapping, record, process))

                retry_connection_processes = {
                    id(process) for _mapping, _record, process in retry_connections
                }
                for process in retry_processes:
                    if id(process) not in retry_connection_processes:
                        self._stop_pending_process(process)

        adb_connections = primary_connections + retry_connections
        activated_processes = set()
        for mapping, record, shared_process in adb_connections:
            if record["adb_status"] != "未执行":
                continue
            if not self._generation_is_current(generation):
                record["adb_status"] = "已终止"
                continue
            if not self._process_is_alive(shared_process):
                record["adb_status"] = "失败"
                record["reason"] = "SSH 隧道进程已退出，端口可能被其他程序占用"
                continue
            try:
                adb_command = [
                    self.adb_executable,
                    "connect",
                    f"127.0.0.1:{mapping['local_port']}",
                ]
                if self.adb_connector is run_adb_connect:
                    out, err = self.adb_connector(
                        adb_command,
                        timeout=self.adb_connect_timeout,
                    )
                else:
                    out, err = self.adb_connector(adb_command)
                connected = "connected to" in out.lower()
                if connected and not self._activate_process(shared_process, mapping, generation):
                    record["adb_status"] = "已终止"
                    continue
                if connected:
                    activated_processes.add(id(shared_process))
                    record["adb_status"] = "成功"
                else:
                    record["adb_status"] = "失败"
                    record["reason"] = f"adb 连接失败：{(out + ' ' + err).strip()}"
            except Exception as exc:
                record["adb_status"] = "失败"
                record["reason"] = str(exc)

        for process in started_processes:
            if id(process) not in activated_processes:
                self._stop_pending_process(process)
        return records

    def disconnect(self):
        """Stop owned tunnels and disconnect their local ADB endpoints."""
        with self._state_lock:
            self._connect_generation += 1
            owned_processes = list(self.active_processes) + list(self.pending_processes)
            active_mappings = list(self.active_mappings)
            self.active_processes.clear()
            self.active_mappings.clear()
            self.pending_processes.clear()

        seen_processes = set()
        for process in owned_processes:
            marker = id(process)
            if marker in seen_processes:
                continue
            seen_processes.add(marker)
            self.stop_tunnel(process)
        if active_mappings:
            self.disconnect_ports(active_mappings)

    def disconnect_mappings(self, mappings: list[dict]) -> list[dict]:
        """Stop only the owned tunnels matching the requested mappings."""
        requested = {
            (
                mapping.get("local_port"),
                mapping.get("public_ip"),
                mapping.get("remote_ip"),
                mapping.get("remote_port"),
            )
            for mapping in mappings
        }
        removed = []
        remaining_mappings = []
        remaining_processes = []
        removed_processes = {}
        with self._state_lock:
            active_mappings = list(self.active_mappings)
            active_processes = list(self.active_processes)
        for index, active in enumerate(active_mappings):
            process = active_processes[index] if index < len(active_processes) else None
            key = (
                active.get("local_port"),
                active.get("public_ip"),
                active.get("remote_ip"),
                active.get("remote_port"),
            )
            if key in requested:
                if process is not None:
                    removed_processes[id(process)] = process
                removed.append(active)
                continue
            remaining_mappings.append(active)
            remaining_processes.append(process)
        with self._state_lock:
            self.active_mappings = remaining_mappings
            self.active_processes = remaining_processes
        remaining_process_ids = {
            id(process) for process in remaining_processes if process is not None
        }
        for marker, process in removed_processes.items():
            if marker not in remaining_process_ids:
                self.stop_tunnel(process)
        if removed:
            self.disconnect_ports(removed)
        return removed


def register_interrupt_handler(ssh_processes, mappings):
    """Register Ctrl+C cleanup without affecting normal tunnel lifetime."""
    def handle_interrupt(_signum, _frame):
        print("\n收到 Ctrl+C，正在清理 SSH 隧道和 ADB 连接...")
        cleanup_ssh_tunnels(ssh_processes)
        disconnect_adb_ports(mappings)
        print(format_mapping_table(mappings))
        print("SSH 隧道和 ADB 连接已断开")
        raise SystemExit(130)

    signal.signal(signal.SIGINT, handle_interrupt)
    return handle_interrupt


def main():
    configure_console_encoding()

    # 日志记录
    success_list = []
    fail_list = []
    mapping_list = []
    ssh_processes = []
    register_interrupt_handler(ssh_processes, mapping_list)

    try:
        for idx in range(loop_count):
            mapping = build_mapping(idx, start_local_port, start_ip_first)
            mapping_record = {**mapping, "adb_status": "未执行"}
            mapping_list.append(mapping_record)
            local_port = mapping["local_port"]
            remote_ip = mapping["remote_ip"]

            ssh_cmd = build_ssh_command(mapping, ssh_user_host, pem_path)
            print(f"\n[{idx+1}/{loop_count}] SSH隧道: 127.0.0.1:{local_port} --> {remote_ip}:5555")
            try:
                ssh_process = start_ssh_tunnel(ssh_cmd)
                ssh_processes.append(ssh_process)
            except Exception as e:
                err_msg = f"SSH进程启动异常: {str(e)}"
                print(f"⚠️ 【告警】{err_msg}")
                mapping_record["adb_status"] = "SSH启动失败"
                fail_list.append({"port": local_port, "remote": remote_ip, "reason": err_msg})
                continue

            time.sleep(tunnel_wait)

            adb_cmd = ["adb", "connect", f"127.0.0.1:{local_port}"]
            print(f"执行 adb connect 127.0.0.1:{local_port}")
            out, err = run_adb_connect(adb_cmd)
            full_output = (out + " " + err).strip()
            print(f"adb输出: {full_output}")

            # 判断adb连接结果
            if "connected to" in out.lower():
                print(f"✅ 127.0.0.1:{local_port} adb连接成功")
                mapping_record["adb_status"] = "成功"
                success_list.append({"port": local_port, "remote": remote_ip})
            else:
                warn_msg = f"adb连接失败，输出：{full_output}"
                print(f"⚠️ 【告警】端口 {local_port} {warn_msg}")
                mapping_record["adb_status"] = "失败"
                fail_list.append({"port": local_port, "remote": remote_ip, "reason": warn_msg})
                # A rejected forwarding channel must not survive the failed ADB attempt.
                stop_ssh_process(ssh_process)

        # 汇总报告
        print(format_mapping_table(mapping_list))
        print("\n" + "="*60)
        print(f"执行完成｜成功：{len(success_list)} 个｜失败：{len(fail_list)} 个")
        if fail_list:
            print("\n❌ 失败列表：")
            for item in fail_list:
                print(f"  本地端口:{item['port']}, 远端:{item['remote']}, 原因:{item['reason']}")

        active_processes = [process for process in ssh_processes if process.poll() is None]
        if active_processes:
            print("\nSSH隧道已启动，按 Ctrl+C 断开并退出")
            while any(process.poll() is None for process in active_processes):
                time.sleep(1)
        else:
            print("\n没有可用的 SSH 隧道，程序退出")
    finally:
        # Also clean up on an unexpected exception so no SSH child is orphaned.
        cleanup_ssh_tunnels(ssh_processes)
        disconnect_adb_ports(mapping_list)

    # 可选：把结果写入日志文件
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"成功总数:{len(success_list)}\n")
        for s in success_list:
            f.write(f"SUCCESS port={s['port']} remote={s['remote']}\n")
        f.write(f"\n失败总数:{len(fail_list)}\n")
        for s in fail_list:
            f.write(f"FAIL port={s['port']} remote={s['remote']} reason={s['reason']}\n")
    print("\n日志已保存到 data/logs/tunnel_adb_log.txt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
