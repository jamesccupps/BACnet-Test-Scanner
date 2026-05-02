# P2 Bridge BACnet Scanner

A lightweight read-only BACnet/IP scanner with a Tkinter GUI, built on
[bacpypes3](https://github.com/JoelBender/BACpypes3) — sized for verifying a
[P2_BACnet_Bridge](https://github.com/jamesccupps/P2_BACnet_Bridge) instance.

Single file, no install step beyond `pip`, no config files.

## What it does

- Sends Who-Is and lists I-Am responses (instance, source, vendor)
- For a selected device, reads identifying properties (object-name,
  vendor, model, firmware, segmentation, system status)
- Reads `object-list` (whole-array fast path, falls back to indexed)
- Bulk-reads `object-name`, `description`, `present-value`, `status-flags`,
  `reliability`, and `units` for every object via ReadPropertyMultiple
- Decodes `status-flags` to readable labels (`ALARM`/`FAULT`/`OVR`/`OOS`)
- Highlights faulted points in red so #COM and `COMMUNICATION_FAILURE` are
  obvious
- Filter box for searching by name/description (e.g. `NODE1.AHU1`)
- CSV export of the full scan

It will **never** issue a write — there is no write path in the code.

## Quick start

```
pip install -r requirements.txt
python p2_bridge_scanner.py
```

1. **Start stack** — bind address auto-fills with your detected IP plus
   `/24:47809`. The non-default port (47809) avoids clashing with the bridge
   running on 47808 on the same host. Local instance defaults to a random
   value in the test range. Click *Start stack*.
2. **Who-Is** — leave Target blank for local broadcast, or enter the bridge
   host (e.g. `192.168.1.50`) to unicast directly. Click *Who-Is*.
3. Click the bridge in the discovered devices list — identifying properties
   load into the right pane.
4. Click **Load object list** — should match your manifest size.
5. Click **Read all values (RPM)** — chunks of 15 objects per request.
   Watch the log for elapsed time and fault count.

## What to verify against the P2 bridge

| Bridge claim | How to verify here |
|---|---|
| Device announces with configured instance | Who-Is → I-Am should return your `bacnet_device_instance` (default 599001) |
| Object count matches manifest | Load object list → count should match `manifest.json` size |
| AI/AV/BI/BV split is correct | Log line after object-list shows breakdown by type |
| Engineering units mapped from APOGEE | Units column populated for analog points |
| Description carries device + slot + units | Description column shows `slot N, app NNNN, UNIT` |
| #COM faults propagate (`reliability=NO_OUTPUT`) | Reliability column shows `noOutput`, row turns pink |
| Lost-PXC points show `COMMUNICATION_FAILURE` | Reliability column shows `communicationFailure` |
| Names follow `NODE.DEVICE.POINT` convention | Filter on `NODE1.` to confirm |
| RPM segmentation works at scale | Time the full read; ~786 points should complete in seconds |

## Same-host setup (bridge + scanner on one machine)

If you're running the scanner on the same machine as the bridge, you need a
different UDP port — the default `47809` in the bind address handles that.
Then either:

- Target the bridge's IP directly in Who-Is (`192.168.1.50`), or
- Send to local broadcast (leave Target blank). The bridge will respond from
  port 47808 to your port 47809.

## Cross-subnet

If the bridge is on a different subnet you can either bind directly to the
bridge's subnet (multi-homed host) or — if the bridge ever grows BBMD
support — register as a foreign device. The current bridge v0.1 doesn't
expose BBMD, so a routable host or a multi-homed scanner is the path.

## Layout

```
p2_bridge_scanner.py       — single-file GUI + bacpypes3 wrapper
requirements.txt           — bacpypes3
README.md                  — this file
```

## Notes

- The scanner runs the asyncio event loop in a background thread so the Tk
  UI stays responsive during long reads.
- Object-list fast path (`read_property` on the array as a whole) requires
  the bridge to support segmentation — bacpypes3 handles this automatically
  when it does. If segmentation isn't supported the scanner falls back to
  indexed reads automatically.
- RPM chunk size is 15 by default. Bridges with very long object names may
  need a smaller chunk; edit `chunk_size` in `read_object_summary` if you
  hit APDU size errors.
