"""Spawn-safe adapters for finite per-device operations."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from services.device_operations import DeviceOperations
from services.device_repository import Device
from workers.task_worker import ProcessTaskSpec


FINITE_DEVICE_OPERATIONS = frozenset(
    {
        "connect",
        "install_apk",
        "install_apk_from_url",
        "clear_third_party",
        "screenshot",
        "restart_app",
        "pull_artifact",
        "pull_json_files",
        "push_lua",
        "clear_sf_json",
        "push_file",
        "run_airtest",
        "run_login_autofill",
    }
)


def device_payload(device: Device) -> dict[str, Any]:
    """Return only dataclass fields so a Device can cross a spawn boundary."""
    return asdict(device)


def process_spec_for_device_operation(
    operation: str,
    device: Device,
    *args: Any,
    **kwargs: Any,
) -> ProcessTaskSpec:
    """Build a descriptor for one allowlisted `DeviceOperations` method."""
    normalized = str(operation).strip()
    if normalized not in FINITE_DEVICE_OPERATIONS:
        raise ValueError(f"unsupported process device operation: {operation}")
    return ProcessTaskSpec(
        target="workers.device_task_process:run_device_operation",
        args=(normalized, device_payload(device), *args),
        kwargs=kwargs,
    )


def process_spec_for_package_scan(
    mappings: list[dict[str, Any]],
    reuse_serials: set[str] | tuple[str, ...] | list[str],
) -> ProcessTaskSpec:
    """Build a child descriptor for the blocking package inventory scan."""
    return ProcessTaskSpec(
        target="workers.device_task_process:run_package_scan",
        args=(mappings, tuple(sorted(str(serial) for serial in reuse_serials))),
    )


def run_device_operation(operation: str, payload: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
    """Reconstruct one device and run a finite operation in the child."""
    normalized = str(operation).strip()
    if normalized not in FINITE_DEVICE_OPERATIONS:
        raise ValueError(f"unsupported process device operation: {operation}")
    device = Device(**payload)
    return getattr(DeviceOperations(), normalized)(device, *args, **kwargs)


def run_package_scan(
    mappings: list[dict[str, Any]],
    reuse_serials: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Run package inventory in a child and return records plus scan errors."""
    from tools.package_inventory import PackageInventory

    inventory = PackageInventory()
    records = inventory.collect_mappings(mappings, reuse_serials=reuse_serials)
    return {"records": records, "errors": list(inventory.errors)}
