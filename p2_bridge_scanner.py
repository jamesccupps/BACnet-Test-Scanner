#!/usr/bin/env python3
"""
P2 Bridge BACnet Scanner — read-only GUI tool for testing the P2_BACnet_Bridge.

Lightweight Tkinter front-end on top of bacpypes3 (https://github.com/JoelBender/BACpypes3).
Designed to verify a P2_BACnet_Bridge instance: discover the device, walk the
object list, bulk-read present values via RPM, and decode status-flags so
#COM / COMMUNICATION_FAILURE points light up clearly.

Run:  python p2_bridge_scanner.py
"""

from __future__ import annotations

import asyncio
import csv
import logging
import socket
import sys
import threading
import traceback
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import ttk, messagebox, filedialog

log = logging.getLogger("p2_bridge_scanner")

try:
    from bacpypes3.argparse import SimpleArgumentParser
    from bacpypes3.app import Application
    from bacpypes3.pdu import Address
    from bacpypes3.primitivedata import ObjectIdentifier
except ImportError:
    print(
        "\nERROR: bacpypes3 is not installed in this Python environment.\n"
        "Install it with:\n\n    pip install bacpypes3\n",
        file=sys.stderr,
    )
    raise


# ---------------------------------------------------------------------------
# Async event loop running in a background thread (so Tk stays responsive)
# ---------------------------------------------------------------------------

class AsyncRunner:
    """Run an asyncio event loop on a background thread and submit coroutines to it."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="bacnet-asyncio"
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro) -> Future:
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def shutdown(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except RuntimeError as e:
            # Loop already closed, which happens if shutdown runs twice.
            log.debug("event loop already stopped: %s", e)


# ---------------------------------------------------------------------------
# BACnet client wrapper
# ---------------------------------------------------------------------------

@dataclass
class DiscoveredDevice:
    instance: int
    address: str
    vendor_id: Optional[int] = None
    name: Optional[str] = None
    model: Optional[str] = None
    object_count: Optional[int] = None


@dataclass
class ObjectRow:
    obj_type: str          # short label e.g. "AI", "AV", "BI", "BV", "DEV"
    obj_type_full: str     # full BACnet type, e.g. "analog-input"
    instance: int
    name: str = ""
    description: str = ""
    units: str = ""
    present_value: Any = None
    status_flags: str = ""
    reliability: str = ""
    error: str = ""


_TYPE_SHORT = {
    "analog-input": "AI",
    "analog-output": "AO",
    "analog-value": "AV",
    "binary-input": "BI",
    "binary-output": "BO",
    "binary-value": "BV",
    "multi-state-input": "MSI",
    "multi-state-output": "MSO",
    "multi-state-value": "MSV",
    "device": "DEV",
}


def _short_type(full: str) -> str:
    return _TYPE_SHORT.get(full, full)


def _format_status_flags(sf: Any) -> str:
    """Render a BACnetStatusFlags bit string as '0100 FAULT' style text.

    Read the bits positionally. bacpypes3 exposes `StatusFlags.fault` and
    `.overridden` as class-level BIT POSITION constants (1 and 2), not as the
    value of that bit on this instance, so `getattr(sf, "fault")` returns a
    truthy 1 on every object regardless of its actual state. `in_alarm` and
    `out_of_service` are not exposed under those names at all.

    Reading them by attribute therefore reported FAULT and OVR on every single
    point, which turned every row pink and made the fault count equal the
    object count — the tool could not tell a healthy bridge from a broken one,
    which is the one thing it exists to do.

    Bit order per ASHRAE 135: in-alarm, fault, overridden, out-of-service.
    """
    _LABELS = ("ALARM", "FAULT", "OVR", "OOS")
    # A str is subscriptable, so an error value like 'communicationFailure'
    # would index character by character and come out as all four flags set.
    # Require something that yields ints, which a real bit string does.
    if isinstance(sf, (str, bytes, bytearray)) or sf is None:
        return ""
    try:
        raw = [sf[i] for i in range(4)]
        if not all(isinstance(b, (bool, int)) for b in raw):
            return ""
        bits = "".join("1" if b else "0" for b in raw)
    except (TypeError, IndexError, KeyError, AttributeError):
        # Unknown is NOT the same as "everything is in alarm". The previous
        # version used the string "?" as its unknown sentinel and then tested
        # it for truthiness, so a missing or errored status-flags rendered as
        # 1111 ALARM,FAULT,OVR,OOS.
        return ""
    labels = [lbl for bit, lbl in zip(bits, _LABELS) if bit == "1"]
    return f"{bits} {','.join(labels)}" if labels else bits


class BACnetClient:
    """Lightweight wrapper around bacpypes3.Application (read-only ops only)."""

    def __init__(self) -> None:
        self.app: Optional[Application] = None
        self.local_address: str = ""
        self.local_instance: int = 0

    async def start(self, local_address: str, local_instance: int,
                    name: str = "P2BridgeScanner") -> None:
        await self.stop()
        parser = SimpleArgumentParser()
        argv = [
            "--name", name,
            "--instance", str(local_instance),
            "--address", local_address,
        ]
        args = parser.parse_args(argv)
        self.app = Application.from_args(args)
        self.local_address = local_address
        self.local_instance = local_instance

    async def stop(self) -> None:
        if self.app is not None:
            try:
                self.app.close()
            except (OSError, RuntimeError, AttributeError) as e:
                # Nothing useful to do about a failed close, but silence here
                # hid genuine teardown problems during development.
                log.warning("error closing the BACnet application: %s", e)
            self.app = None

    def _require(self) -> Application:
        if self.app is None:
            raise RuntimeError("BACnet stack not initialized — click 'Start Stack' first.")
        return self.app

    async def who_is(
        self,
        target_address: Optional[str],
        low: int = 0,
        high: int = 4194303,
        timeout: float = 3.0,
    ) -> list[DiscoveredDevice]:
        app = self._require()
        addr = Address(target_address) if target_address else None
        i_ams = await app.who_is(low, high, address=addr, timeout=timeout)
        out: list[DiscoveredDevice] = []
        for iam in i_ams:
            try:
                did = iam.iAmDeviceIdentifier
                # ObjectIdentifier or tuple
                if hasattr(did, "instance"):
                    inst = int(did.instance)
                else:
                    inst = int(did[1]) if isinstance(did, tuple) else int(str(did).split(",")[-1])
                src = str(getattr(iam, "pduSource", ""))
                vid = getattr(iam, "vendorID", getattr(iam, "vendorIdentifier", None))
                out.append(DiscoveredDevice(instance=inst, address=src, vendor_id=int(vid) if vid is not None else None))
            except Exception as e:
                out.append(DiscoveredDevice(instance=-1, address=f"<parse error: {e}>"))
        # de-dup by instance keeping first
        seen: set[int] = set()
        deduped: list[DiscoveredDevice] = []
        for d in out:
            if d.instance in seen:
                continue
            seen.add(d.instance)
            deduped.append(d)
        return deduped

    async def read_property(
        self, address: str, oid: str, prop: str,
        array_index: Optional[int] = None,
    ) -> Any:
        app = self._require()
        kwargs = {}
        if array_index is not None:
            kwargs["array_index"] = array_index
        return await app.read_property(Address(address), ObjectIdentifier(oid), prop, **kwargs)

    async def read_device_info(self, address: str, instance: int) -> dict:
        """Read a small set of identifying device properties."""
        app = self._require()
        oid = ObjectIdentifier(f"device,{instance}")
        info: dict = {}
        for prop in (
            "object-name", "vendor-identifier", "vendor-name",
            "model-name", "firmware-revision", "application-software-version",
            "description", "system-status",
            "protocol-services-supported", "segmentation-supported",
        ):
            try:
                info[prop] = await app.read_property(Address(address), oid, prop)
            except Exception as e:
                info[prop] = f"<error: {e}>"
        return info

    async def read_object_list(
        self,
        address: str,
        instance: int,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> list:
        """Read object-list. Try whole-array first, fall back to indexed read.

        The whole-array fast path fails on devices with large object lists when
        the response exceeds the negotiated APDU size — Trane Tracer SC+ is the
        canonical case (4000+ objects, hits buffer-overflow even at 1024-byte
        APDUs). When that happens, fall back to reading the array length and
        then walking individual indices, which always fits.
        """
        app = self._require()
        oid = ObjectIdentifier(f"device,{instance}")
        # Fast path: whole-array read
        try:
            full = await app.read_property(Address(address), oid, "object-list")
            if progress_cb:
                progress_cb(len(full), len(full))
            return list(full)
        except Exception as e:
            # Common reasons: BufferOverflow, segmentation-not-supported, or
            # the property is too large for the negotiated APDU. Fall through
            # to indexed reads which always fit. Broad on purpose: bacpypes3
            # raises several unrelated types for these. Logged because "the
            # scan was slow" and "the fast path silently never works on this
            # device" look identical from the UI otherwise.
            log.info("whole-array object-list read failed (%s); "
                     "falling back to an indexed walk", e)

        # Slow path — read length first (array_index=0 returns the count),
        # then walk indices 1..length one at a time.
        try:
            length = await app.read_property(
                Address(address), oid, "object-list", array_index=0
            )
            length = int(length)
        except Exception as exc:
            raise RuntimeError(
                f"object-list read failed (whole-array AND indexed length): {exc}"
            ) from exc

        if progress_cb:
            progress_cb(0, length)
        results = []
        skipped = 0
        for i in range(1, length + 1):
            try:
                item = await app.read_property(
                    Address(address), oid, "object-list", array_index=i
                )
                results.append(item)
            except Exception as e:
                # Skip this index but keep walking — partial results are
                # better than nothing on a flaky device. Broad on purpose:
                # bacpypes3 surfaces device rejections as several unrelated
                # types. Logged so a device dropping half its object list is
                # visible rather than silently short.
                skipped += 1
                log.debug("object-list index %d failed: %s", i, e)
            if progress_cb and (i % 25 == 0 or i == length):
                progress_cb(i, length)
        if skipped:
            log.warning("object-list walk: %d of %d indices did not read back",
                        skipped, length)
        return results

    async def read_object_summary(
        self,
        address: str,
        oids: list,
        chunk_size: int = 15,
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> list[ObjectRow]:
        """Bulk-read presentValue/objectName/description/units/statusFlags via RPM.

        Strategy when a chunk fails:
          1. Halve the chunk size and retry the failed range (handles APDU
             buffer-overflow on devices that can't fit 15 objects per response).
          2. If even chunk_size=1 fails, fall back to per-property RP reads
             for that object (handles devices that don't support RPM at all).
        """
        app = self._require()
        rows: list[ObjectRow] = []
        addr = Address(address)
        total = len(oids)
        done = 0

        # engineering-units only valid on analog objects
        wanted_common = ["object-name", "description", "present-value",
                          "status-flags", "reliability"]
        wanted_analog = wanted_common + ["units"]

        async def _try_chunk(chunk: list, sz: int) -> list[ObjectRow]:
            """RPM with fallback: shrink, then per-property."""
            if not chunk:
                return []
            param_list = []
            for oid in chunk:
                full = _full_type_of(oid)
                props = wanted_analog if "analog" in full else wanted_common
                param_list.append((oid, props))
            try:
                results = await app.read_property_multiple(addr, param_list)
                return _rows_from_rpm(chunk, results)
            except Exception as e:
                emsg = str(e).lower()
                # Recurse with smaller chunks if we have room to shrink
                if sz > 1 and ("buffer" in emsg or "overflow" in emsg
                                or "segment" in emsg or "too large" in emsg
                                or "apdu" in emsg):
                    half = max(1, sz // 2)
                    out: list[ObjectRow] = []
                    for i in range(0, len(chunk), half):
                        out.extend(await _try_chunk(chunk[i:i + half], half))
                    return out
                # Per-property fallback for this chunk
                fallback_rows: list[ObjectRow] = []
                for oid in chunk:
                    fallback_rows.append(
                        await _read_one_object_fallback(app, addr, oid)
                    )
                return fallback_rows

        for start in range(0, total, chunk_size):
            chunk = oids[start:start + chunk_size]
            rows.extend(await _try_chunk(chunk, chunk_size))
            done += len(chunk)
            if progress_cb:
                progress_cb(done, total)
        return rows

    async def read_property_list(self, address: str, oid: Any) -> list[str]:
        """Read an object's property-list (the 'all properties this object
        actually has' enumeration). Returns kebab-case property names.

        Some older devices don't implement property-list; in that case we
        return an empty list and the caller falls back to a hardcoded set.
        """
        app = self._require()
        if not isinstance(oid, ObjectIdentifier):
            oid = ObjectIdentifier(str(oid))
        try:
            props = await app.read_property(Address(address), oid, "property-list")
            return [_prop_name(p) for p in props]
        except Exception as e:
            # Plenty of devices do not implement property-list. An empty list
            # is the right answer; the reason belongs in the log rather than
            # being discarded.
            log.debug("property-list not available for %s: %s", oid, e)
            return []

    async def read_all_object_properties(
        self,
        address: str,
        oid: Any,
        chunk_size: int = 8,
    ) -> list[tuple[str, Any, str]]:
        """Read every property the object exposes. Returns
        [(property_name, value_or_None, error_or_empty), ...].

        The standard approach: read property-list to learn what's defined,
        then RPM the lot in chunks. Falls back to a fixed set of universal
        properties if property-list is unavailable.
        """
        app = self._require()
        if not isinstance(oid, ObjectIdentifier):
            oid = ObjectIdentifier(str(oid))
        addr = Address(address)

        # Always include these — they're either required by ASHRAE or they
        # show up so often that omitting them would be surprising.
        always = ["object-identifier", "object-name", "object-type",
                  "description", "present-value", "status-flags",
                  "reliability", "out-of-service", "units"]

        # Discover the rest via property-list
        listed = await self.read_property_list(address, oid)
        # combine + dedupe while preserving order
        seen: set[str] = set()
        ordered: list[str] = []
        for p in always + listed:
            if p and p not in seen:
                seen.add(p)
                ordered.append(p)

        out: list[tuple[str, Any, str]] = []

        async def _read_chunk(props: list[str]) -> None:
            if not props:
                return
            try:
                results = await app.read_property_multiple(addr, [(oid, props)])
            except Exception as e:
                emsg = str(e).lower()
                if len(props) > 1 and ("buffer" in emsg or "overflow" in emsg
                                        or "apdu" in emsg or "segment" in emsg):
                    mid = len(props) // 2
                    await _read_chunk(props[:mid])
                    await _read_chunk(props[mid:])
                    return
                # Per-property fallback
                for p in props:
                    try:
                        v = await app.read_property(addr, oid, p)
                        out.append((p, v, ""))
                    except Exception as ex:
                        out.append((p, None, str(ex)))
                return
            for tup in results:
                try:
                    _oid, prop_id, _idx, value = tup
                except (TypeError, ValueError) as e:
                    log.debug("unexpected RPM result shape %r: %s", tup, e)
                    continue
                err = _is_error_value(value)
                if err:
                    out.append((_prop_name(prop_id), None, err))
                else:
                    out.append((_prop_name(prop_id), value, ""))

        for i in range(0, len(ordered), chunk_size):
            await _read_chunk(ordered[i:i + chunk_size])
        return out


def _full_type_of(oid) -> str:
    """Get the BACnet object-type string from an ObjectIdentifier-like."""
    try:
        ot = oid[0] if isinstance(oid, tuple) else oid.object_type
    except (AttributeError, IndexError, TypeError):
        ot = oid
    s = str(ot)
    # ObjectType enum prints as 'analog-input' or 'ObjectType.analogInput' — normalize
    if "." in s:
        s = s.split(".")[-1]
    # camelCase -> kebab
    out = []
    for i, c in enumerate(s):
        if c.isupper() and i > 0 and s[i - 1].islower():
            out.append("-")
        out.append(c.lower())
    return "".join(out)


def _instance_of(oid) -> int:
    try:
        return int(oid[1]) if isinstance(oid, tuple) else int(oid.instance)
    except (AttributeError, IndexError, TypeError, ValueError):
        try:
            return int(str(oid).rsplit(",", 1)[-1])
        except (IndexError, ValueError):
            log.debug("could not extract an instance number from %r", oid)
            return -1





def _rows_from_rpm(oids: list, rpm_results: Any) -> list[ObjectRow]:
    """Convert a bacpypes3 read_property_multiple result into ObjectRow list.

    Per bacpypes3.app.Application.read_property_multiple, the result is a flat
    list of (object_identifier, property_identifier, array_index, value) tuples.
    Errors for individual properties show up as ErrorType/ErrorRejectAbortNack
    instances in the value slot — we surface those in row.error.
    """
    # Build rows keyed by (full_type, instance) so we can fill them in as tuples arrive
    by_key: dict[tuple[str, int], ObjectRow] = {}
    for oid in oids:
        full = _full_type_of(oid)
        inst = _instance_of(oid)
        by_key[(full, inst)] = ObjectRow(
            obj_type=_short_type(full),
            obj_type_full=full,
            instance=inst,
        )

    if not isinstance(rpm_results, list):
        # unexpected shape — return what we have with an error note
        for r in by_key.values():
            r.error = "unrecognized RPM result shape"
        return list(by_key.values())

    for tup in rpm_results:
        try:
            oid, prop_id, array_idx, value = tup
        except (TypeError, ValueError) as e:
            log.debug("unexpected RPM result shape %r: %s", tup, e)
            continue
        full = _full_type_of(oid)
        inst = _instance_of(oid)
        row = by_key.get((full, inst))
        if row is None:
            row = ObjectRow(
                obj_type=_short_type(full), obj_type_full=full, instance=inst
            )
            by_key[(full, inst)] = row
        prop_name = _prop_name(prop_id)
        # detect per-property error (ErrorType etc. — these are not raised, they sit in value)
        err = _is_error_value(value)
        if err:
            existing = row.error
            row.error = f"{prop_name}:{err}" if not existing else f"{existing}; {prop_name}:{err}"
            continue
        if prop_name == "object-name":
            row.name = str(value)
        elif prop_name == "description":
            row.description = str(value)
        elif prop_name == "present-value":
            row.present_value = value
        elif prop_name == "status-flags":
            row.status_flags = _format_status_flags(value)
        elif prop_name == "reliability":
            row.reliability = str(value)
        elif prop_name == "units":
            row.units = str(value)

    # Preserve original input order
    out: list[ObjectRow] = []
    seen: set[tuple[str, int]] = set()
    for oid in oids:
        key = (_full_type_of(oid), _instance_of(oid))
        if key in seen:
            continue
        seen.add(key)
        if key in by_key:
            out.append(by_key[key])
    # Append any rows that came back without a matching input oid (shouldn't happen)
    for key, row in by_key.items():
        if key not in seen:
            out.append(row)
    return out


def _prop_name(prop_id: Any) -> str:
    """Normalize a bacpypes3 PropertyIdentifier (or string) to its kebab-case name."""
    s = str(prop_id)
    if "." in s:
        s = s.split(".")[-1]
    # camelCase -> kebab if needed
    if any(c.isupper() for c in s) and "-" not in s:
        out = []
        for i, c in enumerate(s):
            if c.isupper() and i > 0 and s[i - 1].islower():
                out.append("-")
            out.append(c.lower())
        s = "".join(out)
    return s.lower()


def _is_error_value(value: Any) -> str:
    """If RPM returned an error sentinel for this property, return a short string."""
    cls = type(value).__name__
    if cls in ("ErrorType", "ErrorRejectAbortNack", "Error"):
        # bacpypes3 ErrorType has errorClass + errorCode
        ec = getattr(value, "errorClass", None) or getattr(value, "error_class", None)
        cc = getattr(value, "errorCode", None) or getattr(value, "error_code", None)
        if ec or cc:
            return f"{ec}/{cc}"
        return cls
    return ""


async def _read_one_object_fallback(app: Application, addr: Address, oid: Any) -> ObjectRow:
    full = _full_type_of(oid)
    row = ObjectRow(
        obj_type=_short_type(full),
        obj_type_full=full,
        instance=_instance_of(oid),
    )
    props = ["object-name", "description", "present-value", "status-flags", "reliability"]
    if "analog" in full:
        props.append("units")
    for p in props:
        try:
            val = await app.read_property(addr, oid, p)
            if p == "object-name":
                row.name = str(val)
            elif p == "description":
                row.description = str(val)
            elif p == "present-value":
                row.present_value = val
            elif p == "status-flags":
                row.status_flags = _format_status_flags(val)
            elif p == "reliability":
                row.reliability = str(val)
            elif p == "units":
                row.units = str(val)
        except Exception as e:
            if not row.error:
                row.error = f"{p}: {e}"
    return row


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def _detect_local_ip() -> str:
    """Best-effort detection of the outbound IPv4 address."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.3)
        # No packet is sent; connect() on UDP just picks the outbound route.
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError as e:
        log.debug("could not detect the local IP: %s", e)
        return "0.0.0.0"
    finally:
        sock.close()


class ScannerGUI:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        root.title("P2 Bridge BACnet Scanner")
        root.geometry("1180x780")

        self.runner = AsyncRunner()
        self.client = BACnetClient()

        # State
        self.devices: list[DiscoveredDevice] = []
        self.current_device: Optional[DiscoveredDevice] = None
        self.object_oids: list = []
        self.rows: list[ObjectRow] = []
        self._selected_obj_oid: Optional[tuple[str, int]] = None
        self.filter_text = tk.StringVar()
        self.filter_text.trace_add("write", lambda *a: self._refresh_rows_view())

        self._build_ui()
        # auto-fill local address with /24:47809 (47808 + 1 to avoid clashing with bridge)
        self.local_addr_var.set(f"{_detect_local_ip()}/24:47809")
        # random-ish local instance in test range to avoid conflicts
        import random
        self.local_inst_var.set(str(800000 + random.randint(0, 99999)))

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # --- UI construction ---

    def _build_ui(self) -> None:
        pad = {"padx": 6, "pady": 4}

        # Top: local stack config
        top = ttk.LabelFrame(self.root, text="Local BACnet stack")
        top.pack(fill="x", **pad)
        ttk.Label(top, text="Bind address (ip/cidr:port):").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.local_addr_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.local_addr_var, width=28).grid(row=0, column=1, padx=4)
        ttk.Label(top, text="Local instance:").grid(row=0, column=2, sticky="w", padx=4)
        self.local_inst_var = tk.StringVar()
        ttk.Entry(top, textvariable=self.local_inst_var, width=10).grid(row=0, column=3, padx=4)
        self.start_btn = ttk.Button(top, text="Start stack", command=self._on_start_stack)
        self.start_btn.grid(row=0, column=4, padx=4)
        self.stop_btn = ttk.Button(top, text="Stop", command=self._on_stop_stack, state="disabled")
        self.stop_btn.grid(row=0, column=5, padx=4)
        self.stack_state_var = tk.StringVar(value="● not started")
        ttk.Label(top, textvariable=self.stack_state_var, foreground="#aa0000").grid(
            row=0, column=6, padx=10
        )

        # Discover row
        disc = ttk.LabelFrame(self.root, text="Discover devices")
        disc.pack(fill="x", **pad)
        ttk.Label(disc, text="Target (blank = local broadcast):").grid(row=0, column=0, sticky="w", padx=4, pady=4)
        self.target_var = tk.StringVar()
        ttk.Entry(disc, textvariable=self.target_var, width=28).grid(row=0, column=1, padx=4)
        ttk.Label(disc, text="Instance range:").grid(row=0, column=2, padx=4)
        self.low_var = tk.StringVar(value="0")
        self.high_var = tk.StringVar(value="4194303")
        ttk.Entry(disc, textvariable=self.low_var, width=8).grid(row=0, column=3)
        ttk.Label(disc, text="—").grid(row=0, column=4)
        ttk.Entry(disc, textvariable=self.high_var, width=8).grid(row=0, column=5)
        ttk.Label(disc, text="Timeout(s):").grid(row=0, column=6, padx=4)
        self.timeout_var = tk.StringVar(value="3.0")
        ttk.Entry(disc, textvariable=self.timeout_var, width=5).grid(row=0, column=7)
        ttk.Button(disc, text="Who-Is", command=self._on_who_is).grid(row=0, column=8, padx=8)

        # Middle split: devices left, summary right
        mid = ttk.Frame(self.root)
        mid.pack(fill="both", expand=False, **pad)

        dev_frame = ttk.LabelFrame(mid, text="Discovered devices")
        dev_frame.pack(side="left", fill="both", expand=True, padx=(0, 4))
        cols = ("instance", "address", "vendor", "name", "objects")
        self.dev_tree = ttk.Treeview(dev_frame, columns=cols, show="headings", height=6)
        for c, w in zip(cols, (90, 220, 70, 220, 80)):
            self.dev_tree.heading(c, text=c.title())
            self.dev_tree.column(c, width=w, anchor="w")
        self.dev_tree.pack(fill="both", expand=True, side="left")
        dev_sb = ttk.Scrollbar(dev_frame, orient="vertical", command=self.dev_tree.yview)
        dev_sb.pack(side="right", fill="y")
        self.dev_tree.configure(yscrollcommand=dev_sb.set)
        self.dev_tree.bind("<<TreeviewSelect>>", self._on_device_selected)

        info_frame = ttk.LabelFrame(mid, text="Selected device — properties")
        info_frame.pack(side="left", fill="both", expand=True, padx=(4, 0))
        self.info_text = tk.Text(info_frame, height=10, width=50, wrap="none", state="disabled")
        self.info_text.pack(fill="both", expand=True)

        # Object actions
        obj_actions = ttk.Frame(self.root)
        obj_actions.pack(fill="x", **pad)
        self.load_objs_btn = ttk.Button(obj_actions, text="Load object list", command=self._on_load_objects, state="disabled")
        self.load_objs_btn.pack(side="left", padx=4)
        self.read_pvs_btn = ttk.Button(obj_actions, text="Read all values (RPM)", command=self._on_read_all_values, state="disabled")
        self.read_pvs_btn.pack(side="left", padx=4)
        ttk.Label(obj_actions, text="Filter:").pack(side="left", padx=(16, 2))
        ttk.Entry(obj_actions, textvariable=self.filter_text, width=30).pack(side="left")
        self.export_btn = ttk.Button(obj_actions, text="Export CSV…", command=self._on_export_csv, state="disabled")
        self.export_btn.pack(side="right", padx=4)
        self.progress_var = tk.StringVar(value="")
        ttk.Label(obj_actions, textvariable=self.progress_var, foreground="#0050a0").pack(side="right", padx=8)

        # Object area: split horizontally — list on left, property tree on right.
        # Click an object on the left and the right pane fills with its full
        # property set (YABE-style).
        obj_outer = ttk.Frame(self.root)
        obj_outer.pack(fill="both", expand=True, **pad)
        obj_paned = ttk.PanedWindow(obj_outer, orient="horizontal")
        obj_paned.pack(fill="both", expand=True)

        # Left: object list
        obj_frame = ttk.LabelFrame(obj_paned, text="Objects")
        ocols = ("type", "instance", "name", "value", "units", "flags", "reliability", "description")
        widths = (50, 70, 220, 100, 70, 110, 110, 220)
        self.obj_tree = ttk.Treeview(obj_frame, columns=ocols, show="headings")
        for c, w in zip(ocols, widths):
            self.obj_tree.heading(c, text=c.title())
            self.obj_tree.column(c, width=w, anchor="w")
        self.obj_tree.pack(fill="both", expand=True, side="left")
        obj_sb = ttk.Scrollbar(obj_frame, orient="vertical", command=self.obj_tree.yview)
        obj_sb.pack(side="right", fill="y")
        self.obj_tree.configure(yscrollcommand=obj_sb.set)
        self.obj_tree.tag_configure("fault", background="#ffe6e6")
        self.obj_tree.tag_configure("error", background="#ffd0d0", foreground="#660000")
        self.obj_tree.bind("<<TreeviewSelect>>", self._on_object_selected)
        obj_paned.add(obj_frame, weight=3)

        # Right: property tree for the selected object
        prop_frame = ttk.LabelFrame(obj_paned, text="Object properties")
        prop_actions = ttk.Frame(prop_frame)
        prop_actions.pack(fill="x", padx=4, pady=2)
        self.prop_label_var = tk.StringVar(value="(select an object on the left)")
        ttk.Label(prop_actions, textvariable=self.prop_label_var,
                  foreground="#0050a0").pack(side="left")
        self.refresh_props_btn = ttk.Button(
            prop_actions, text="Refresh", width=8,
            command=self._on_refresh_object_props, state="disabled",
        )
        self.refresh_props_btn.pack(side="right", padx=2)
        pcols = ("property", "value")
        self.prop_tree = ttk.Treeview(prop_frame, columns=pcols, show="headings")
        self.prop_tree.heading("property", text="Property")
        self.prop_tree.heading("value", text="Value")
        self.prop_tree.column("property", width=180, anchor="w")
        self.prop_tree.column("value", width=320, anchor="w")
        self.prop_tree.pack(fill="both", expand=True, side="left")
        prop_sb = ttk.Scrollbar(prop_frame, orient="vertical", command=self.prop_tree.yview)
        prop_sb.pack(side="right", fill="y")
        self.prop_tree.configure(yscrollcommand=prop_sb.set)
        self.prop_tree.tag_configure("error", foreground="#aa0000")
        obj_paned.add(prop_frame, weight=2)

        # Status
        status_frame = ttk.LabelFrame(self.root, text="Log")
        status_frame.pack(fill="x", **pad)
        self.log_text = tk.Text(status_frame, height=8, wrap="word", state="disabled")
        self.log_text.pack(fill="both", expand=True, side="left")
        log_sb = ttk.Scrollbar(status_frame, orient="vertical", command=self.log_text.yview)
        log_sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_sb.set)

    # --- helpers ---

    def log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}\n"
        self.log_text.configure(state="normal")
        self.log_text.insert("end", line)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_stack_state(self, started: bool) -> None:
        if started:
            self.stack_state_var.set(f"● running  ({self.client.local_address}, inst {self.client.local_instance})")
            self.start_btn.configure(state="disabled")
            self.stop_btn.configure(state="normal")
        else:
            self.stack_state_var.set("● not started")
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.load_objs_btn.configure(state="disabled")
            self.read_pvs_btn.configure(state="disabled")
            self.export_btn.configure(state="disabled")

    def _submit(self, coro, on_done: Callable[[Any, Optional[BaseException]], None]) -> None:
        """Submit coro to async loop; deliver result on Tk thread."""
        fut = self.runner.submit(coro)

        def _cb(f: Future):
            try:
                result = f.result()
                err = None
            except BaseException as e:  # noqa: BLE001
                result = None
                err = e
            # hop back to Tk main thread
            self.root.after(0, lambda: on_done(result, err))

        fut.add_done_callback(_cb)

    # --- Stack start/stop ---

    def _on_start_stack(self) -> None:
        addr = self.local_addr_var.get().strip()
        try:
            inst = int(self.local_inst_var.get().strip())
        except ValueError:
            messagebox.showerror("Invalid input", "Local instance must be an integer.")
            return
        if not addr:
            messagebox.showerror("Invalid input", "Bind address required (e.g. 192.168.1.100/24:47809).")
            return
        self.start_btn.configure(state="disabled")
        self.log(f"Starting BACnet stack — bind {addr}, instance {inst}")
        self._submit(
            self.client.start(addr, inst),
            lambda r, e: self._after_start(e),
        )

    def _after_start(self, err: Optional[BaseException]) -> None:
        if err is not None:
            self.log(f"Start failed: {err}")
            messagebox.showerror("Start failed", str(err))
            self._set_stack_state(False)
            return
        self.log("Stack running.")
        self._set_stack_state(True)

    def _on_stop_stack(self) -> None:
        self.log("Stopping stack…")
        self._submit(self.client.stop(), lambda r, e: self._after_stop(e))

    def _after_stop(self, err: Optional[BaseException]) -> None:
        if err:
            self.log(f"Stop error: {err}")
        else:
            self.log("Stack stopped.")
        self._set_stack_state(False)

    # --- Discover ---

    def _on_who_is(self) -> None:
        if self.client.app is None:
            messagebox.showinfo("Stack not started", "Start the BACnet stack first.")
            return
        target = self.target_var.get().strip() or None
        try:
            low = int(self.low_var.get())
            high = int(self.high_var.get())
            timeout = float(self.timeout_var.get())
        except ValueError:
            messagebox.showerror("Invalid input", "Range and timeout must be numeric.")
            return
        if target:
            self.log(f"Sending Who-Is to {target} (range {low}-{high}, timeout {timeout}s)")
        else:
            self.log(f"Sending local-broadcast Who-Is (range {low}-{high}, timeout {timeout}s)")
        self._submit(
            self.client.who_is(target, low, high, timeout),
            lambda r, e: self._after_who_is(r, e),
        )

    def _after_who_is(self, devices: Optional[list[DiscoveredDevice]], err: Optional[BaseException]) -> None:
        if err:
            self.log(f"Who-Is failed: {err}")
            messagebox.showerror("Who-Is failed", str(err))
            return
        self.devices = devices or []
        self.dev_tree.delete(*self.dev_tree.get_children())
        for d in self.devices:
            self.dev_tree.insert(
                "", "end",
                values=(d.instance, d.address, d.vendor_id if d.vendor_id is not None else "",
                        d.name or "", d.object_count if d.object_count is not None else "")
            )
        self.log(f"Got {len(self.devices)} I-Am response(s).")
        # auto-select first
        children = self.dev_tree.get_children()
        if children:
            self.dev_tree.selection_set(children[0])
            self.dev_tree.focus(children[0])
            self._on_device_selected()

    # --- Device selection: read identifying props ---

    def _on_device_selected(self, event=None) -> None:
        sel = self.dev_tree.selection()
        if not sel:
            return
        idx = self.dev_tree.index(sel[0])
        if idx < 0 or idx >= len(self.devices):
            return
        self.current_device = self.devices[idx]
        d = self.current_device
        self._set_info(f"Reading properties for device {d.instance} @ {d.address}…\n")
        # Reset object pane state — old object list / props are for the
        # previous device.
        self.object_oids = []
        self.rows = []
        self._selected_obj_oid = None
        self._refresh_rows_view()
        self._set_prop_tree([("(select an object on the left)", "", "")])
        self.prop_label_var.set("(select an object on the left)")
        self.refresh_props_btn.configure(state="disabled")
        self.load_objs_btn.configure(state="normal")
        self.read_pvs_btn.configure(state="disabled")
        self._submit(
            self.client.read_device_info(d.address, d.instance),
            lambda r, e: self._after_device_info(r, e),
        )

    def _after_device_info(self, info: Optional[dict], err: Optional[BaseException]) -> None:
        if err:
            self.log(f"Device info read failed: {err}")
            self._set_info(f"Error reading device: {err}\n")
            return
        d = self.current_device
        if not d or not info:
            return
        # Update tree row name column for convenience
        name = info.get("object-name")
        if isinstance(name, str) and not name.startswith("<error"):
            d.name = name
            for iid in self.dev_tree.get_children():
                if int(self.dev_tree.item(iid, "values")[0]) == d.instance:
                    vals = list(self.dev_tree.item(iid, "values"))
                    vals[3] = name
                    self.dev_tree.item(iid, values=vals)
                    break

        lines = [f"Device {d.instance} @ {d.address}"]
        lines.append("-" * 50)
        for k in ("object-name", "vendor-identifier", "vendor-name", "model-name",
                  "firmware-revision", "application-software-version",
                  "description", "system-status", "segmentation-supported"):
            if k in info:
                lines.append(f"{k:34s} {info[k]}")
        self._set_info("\n".join(lines) + "\n")
        self.log(f"Device {d.instance}: {d.name or '?'}")

    def _set_info(self, text: str) -> None:
        self.info_text.configure(state="normal")
        self.info_text.delete("1.0", "end")
        self.info_text.insert("end", text)
        self.info_text.configure(state="disabled")

    # --- Object list ---

    def _on_load_objects(self) -> None:
        if not self.current_device:
            return
        d = self.current_device
        self.log(f"Reading object-list for device {d.instance}…")
        self.progress_var.set("loading object-list…")
        self.load_objs_btn.configure(state="disabled")

        def progress(done: int, total: int) -> None:
            # called from asyncio thread; marshal to Tk
            self.root.after(0, lambda: self.progress_var.set(f"object-list: {done}/{total}"))

        self._submit(
            self.client.read_object_list(d.address, d.instance, progress_cb=progress),
            lambda r, e: self._after_load_objects(r, e),
        )

    def _after_load_objects(self, oids: Optional[list], err: Optional[BaseException]) -> None:
        self.load_objs_btn.configure(state="normal")
        if err:
            self.log(f"object-list read failed: {err}")
            self.progress_var.set("object-list: failed")
            messagebox.showerror("object-list failed", str(err))
            return
        self.object_oids = list(oids or [])
        # filter out the device object itself for display purposes (we don't read PV on it)
        self.rows = [
            ObjectRow(
                obj_type=_short_type(_full_type_of(o)),
                obj_type_full=_full_type_of(o),
                instance=_instance_of(o),
            )
            for o in self.object_oids
        ]
        self._refresh_rows_view()
        # update device row's object count
        if self.current_device:
            self.current_device.object_count = len(self.object_oids)
            for iid in self.dev_tree.get_children():
                if int(self.dev_tree.item(iid, "values")[0]) == self.current_device.instance:
                    vals = list(self.dev_tree.item(iid, "values"))
                    vals[4] = len(self.object_oids)
                    self.dev_tree.item(iid, values=vals)
                    break
        type_counts = _count_by_type(self.rows)
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(type_counts.items()))
        self.log(f"object-list: {len(self.object_oids)} objects ({breakdown})")
        self.progress_var.set(f"loaded {len(self.object_oids)} objects")
        self.read_pvs_btn.configure(state="normal")
        self.export_btn.configure(state="normal")

        # Auto-RPM the values in the background. The user already clicked
        # "Load object list"; making them click again to fill in the value
        # column is a UX miss. The button stays available for manual refreshes.
        if self.object_oids:
            self._on_read_all_values()

    def _on_read_all_values(self) -> None:
        if not self.current_device or not self.object_oids:
            return
        # exclude the device object itself from value reads (avoid noise)
        oids_to_read = [
            o for o in self.object_oids if _full_type_of(o) != "device"
        ]
        d = self.current_device
        self.log(f"Reading values for {len(oids_to_read)} objects via RPM…")
        self.progress_var.set("reading values…")
        self.read_pvs_btn.configure(state="disabled")
        started = datetime.now()

        def progress(done: int, total: int) -> None:
            self.root.after(0, lambda: self.progress_var.set(f"values: {done}/{total}"))

        self._submit(
            self.client.read_object_summary(d.address, oids_to_read, progress_cb=progress),
            lambda r, e: self._after_read_values(r, e, started),
        )

    def _after_read_values(self, rows: Optional[list[ObjectRow]], err: Optional[BaseException], started: datetime) -> None:
        self.read_pvs_btn.configure(state="normal")
        if err:
            self.log(f"value read failed: {err}")
            self.progress_var.set("read values: failed")
            messagebox.showerror("Read values failed", str(err))
            return
        # merge new rows into self.rows by (type, instance)
        index = {(r.obj_type_full, r.instance): r for r in rows or []}
        for r in self.rows:
            key = (r.obj_type_full, r.instance)
            if key in index:
                new = index[key]
                r.name = new.name or r.name
                r.description = new.description or r.description
                r.units = new.units or r.units
                r.present_value = new.present_value
                r.status_flags = new.status_flags
                r.reliability = new.reliability
                r.error = new.error
        elapsed = (datetime.now() - started).total_seconds()
        fault_n = sum(1 for r in self.rows if "FAULT" in r.status_flags)
        err_n = sum(1 for r in self.rows if r.error)
        self.log(
            f"Read {len(rows or [])} values in {elapsed:.1f}s — "
            f"{fault_n} with FAULT flag, {err_n} with errors."
        )
        self.progress_var.set(f"done — {elapsed:.1f}s, {fault_n} fault, {err_n} errors")
        self._refresh_rows_view()

    # --- Object selection: full property tree on the right pane ---

    def _on_object_selected(self, event=None) -> None:
        sel = self.obj_tree.selection()
        if not sel:
            return
        item = self.obj_tree.item(sel[0], "values")
        if not item or len(item) < 3:
            return
        # the visible columns: type, instance, name, value, units, flags, reliab, desc
        try:
            inst = int(item[1])
        except (TypeError, ValueError):
            return
        # Find the matching ObjectRow to recover the full type
        match = None
        for r in self.rows:
            if r.instance == inst and r.obj_type == item[0]:
                match = r
                break
        if match is None:
            return
        self._selected_obj_oid = (match.obj_type_full, match.instance)
        self.prop_label_var.set(
            f"{match.obj_type} {match.instance}"
            + (f" — {match.name}" if match.name else "")
        )
        self.refresh_props_btn.configure(state="disabled")
        self._set_prop_tree([("(loading…)", "", "")])
        self._fetch_props_for(match)

    def _on_refresh_object_props(self) -> None:
        if not self._selected_obj_oid or not self.current_device:
            return
        full, inst = self._selected_obj_oid
        for r in self.rows:
            if r.obj_type_full == full and r.instance == inst:
                self.refresh_props_btn.configure(state="disabled")
                self._set_prop_tree([("(refreshing…)", "", "")])
                self._fetch_props_for(r)
                return

    def _fetch_props_for(self, row: ObjectRow) -> None:
        if not self.current_device:
            return
        d = self.current_device
        oid_str = f"{row.obj_type_full},{row.instance}"
        # Capture which oid this fetch is for; if the user clicks another
        # row before this returns, we only display the current one.
        target_key = (row.obj_type_full, row.instance)

        self._submit(
            self.client.read_all_object_properties(d.address, oid_str),
            lambda r, e: self._after_fetch_props(target_key, r, e),
        )

    def _after_fetch_props(
        self,
        target_key: tuple[str, int],
        results: Optional[list[tuple[str, Any, str]]],
        err: Optional[BaseException],
    ) -> None:
        # If the user has selected a different object since this fetch
        # started, don't clobber the current view with stale data.
        if self._selected_obj_oid != target_key:
            return
        self.refresh_props_btn.configure(state="normal")
        if err:
            self.log(f"property fetch failed: {err}")
            self._set_prop_tree([("(error)", str(err), "error")])
            return
        if not results:
            self._set_prop_tree([("(no properties returned)", "", "")])
            return
        rows: list[tuple[str, str, str]] = []
        for prop, value, e in results:
            if e:
                rows.append((prop, f"<error: {e}>", "error"))
            else:
                rows.append((prop, _format_prop_value(value), ""))
        self._set_prop_tree(rows)

    def _set_prop_tree(self, rows: list[tuple[str, str, str]]) -> None:
        self.prop_tree.delete(*self.prop_tree.get_children())
        for prop, val, tag in rows:
            self.prop_tree.insert(
                "", "end",
                values=(prop, val),
                tags=(tag,) if tag else (),
            )

    # --- View refresh / filter ---

    def _refresh_rows_view(self) -> None:
        self.obj_tree.delete(*self.obj_tree.get_children())
        needle = self.filter_text.get().strip().lower()
        for r in self.rows:
            if needle:
                hay = f"{r.obj_type} {r.instance} {r.name} {r.description} {r.units}".lower()
                if needle not in hay:
                    continue
            tags = []
            if r.error:
                tags.append("error")
            elif "FAULT" in r.status_flags:
                tags.append("fault")
            self.obj_tree.insert(
                "", "end",
                values=(
                    r.obj_type,
                    r.instance,
                    r.name,
                    _format_pv(r.present_value),
                    r.units,
                    r.status_flags,
                    r.reliability,
                    r.description,
                ),
                tags=tags,
            )

    def _on_export_csv(self) -> None:
        if not self.rows:
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("All files", "*.*")],
            initialfile="p2_bridge_scan.csv",
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["type", "instance", "name", "present_value", "units",
                            "status_flags", "reliability", "description", "error"])
                for r in self.rows:
                    w.writerow([
                        r.obj_type, r.instance, r.name, _format_pv(r.present_value),
                        r.units, r.status_flags, r.reliability, r.description, r.error,
                    ])
            self.log(f"Exported {len(self.rows)} rows to {path}")
        except (OSError, csv.Error, UnicodeEncodeError) as e:
            log.error("CSV export to %s failed: %s", path, e)
            messagebox.showerror("Export failed", str(e))

    # --- Shutdown ---

    def _on_close(self) -> None:
        try:
            if self.client.app is not None:
                fut = self.runner.submit(self.client.stop())
                try:
                    fut.result(timeout=2)
                except FutureTimeoutError:
                    log.warning("BACnet stack did not shut down within 2s; "
                                "closing anyway")
                except Exception:
                    # The coroutine itself raised. Closing regardless, but the
                    # traceback belongs in the log rather than nowhere.
                    log.exception("error while stopping the BACnet stack")
        finally:
            self.runner.shutdown()
            self.root.destroy()


def _format_pv(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        # Guarded by a try/except before; isinstance and %f formatting of a
        # float cannot raise, so the handler only obscured the intent.
        return f"{v:.3f}"
    return str(v)


def _format_prop_value(v: Any) -> str:
    """Render a BACnet property value for the property tree.

    Handles common types: floats get 3 decimals, lists print as comma-joined
    short forms, BACnet StatusFlags/PriorityArray get the same friendly
    formatting used in the object table, everything else falls back to str().
    Long values get truncated to keep the column readable.
    """
    if v is None:
        return ""
    cls = type(v).__name__
    # Status-flags-shaped objects
    if cls == "StatusFlags" or "statusflags" in cls.lower():
        return _format_status_flags(v)
    if isinstance(v, float):
        return f"{v:.3f}"
    if isinstance(v, (list, tuple)):
        # Lists of ObjectIdentifier or PropertyReference get long fast — show
        # count + first few entries.
        items = [_format_prop_value(x) for x in v[:8]]
        more = "" if len(v) <= 8 else f", … (+{len(v) - 8} more)"
        return f"[{len(v)}] " + ", ".join(items) + more
    s = str(v)
    if len(s) > 240:
        s = s[:237] + "…"
    return s


def _count_by_type(rows: list[ObjectRow]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        counts[r.obj_type] = counts.get(r.obj_type, 0) + 1
    return counts


# ---------------------------------------------------------------------------

def _configure_logging(argv: list[str]) -> None:
    """Send diagnostics to stderr.

    The GUI log pane shows what the operator needs; this is the other half —
    the reasons behind a fallback, a skipped object-list index, a device that
    does not implement property-list. Those used to be discarded, which made
    "this device is slow" and "the fast path never works here" look identical.

    -v / --verbose turns on DEBUG, which includes per-object detail.
    """
    verbose = any(a in ("-v", "--verbose") for a in argv)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    if not verbose:
        # bacpypes3 is chatty at DEBUG and it is not our output.
        logging.getLogger("bacpypes3").setLevel(logging.WARNING)


def main() -> int:
    _configure_logging(sys.argv[1:])
    log.info("P2 Bridge BACnet Scanner starting")
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", 1.2)
    except tk.TclError as e:
        log.debug("could not set Tk scaling: %s", e)
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        elif "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError as e:
        # Cosmetic only; the default theme is perfectly usable.
        log.debug("could not apply a ttk theme: %s", e)
    ScannerGUI(root)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
